"""
Upload invoice/payment source documents to Google Drive and return a shareable
link, so the DB file_path column points to a viewable copy instead of a local
path.

Drive layout (created on first use):
    Pharmacy Invoices/
        invoice_files/
        payment_files/

Each uploaded file is set to 'anyone with the link can view' and its webViewLink
is returned. Auth reuses google_auth.py (drive.file scope; Techloq-aware TLS).

As a module:
    from drive_uploader import DriveUploader
    up = DriveUploader()
    link = up.upload("invoice_files", Path("output/.../inv.pdf"))

Standalone (smoke test):
    python drive_uploader.py path/to/file.pdf [invoice_files|payment_files]
"""

import sys
import time
from pathlib import Path

from google_auth import build_service, get_credentials

ROOT_FOLDER = "Pharmacy Invoices"
SUBFOLDERS = ("invoice_files", "payment_files")

# Retries (2026-10-08). Uploads used to fail outright on any momentary network hiccup — likely through
# the Techloq TLS proxy — about 1-2 files per run, and those records silently kept a local-only file.
# Every Drive call now uses googleapiclient's own retry (connection resets, timeouts, SSL errors, 5xx,
# 429 — exponential backoff), and a whole upload is tried up to UPLOAD_ATTEMPTS times. An expired or
# revoked Google login isn't retried (it can't succeed without a person signing in again).
NUM_RETRIES = 4
UPLOAD_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 3


class DriveUploader:
    def __init__(self, creds=None):
        self.service = build_service("drive", "v3", creds or get_credentials())
        self._folder_ids = {}

    def _find_or_create_folder(self, name, parent_id=None):
        query = (
            "mimeType='application/vnd.google-apps.folder' and trashed=false "
            f"and name='{name}'"
        )
        if parent_id:
            query += f" and '{parent_id}' in parents"
        found = self.service.files().list(
            q=query, spaces="drive", fields="files(id,name)").execute(
                num_retries=NUM_RETRIES).get("files", [])
        if found:
            return found[0]["id"]
        metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            metadata["parents"] = [parent_id]
        return self.service.files().create(body=metadata, fields="id").execute(
            num_retries=NUM_RETRIES)["id"]

    def _subfolder_id(self, subfolder):
        if subfolder not in SUBFOLDERS:
            raise ValueError(f"subfolder must be one of {SUBFOLDERS}")
        if subfolder not in self._folder_ids:
            root_id = self._find_or_create_folder(ROOT_FOLDER)
            self._folder_ids[subfolder] = self._find_or_create_folder(subfolder, root_id)
        return self._folder_ids[subfolder]

    def upload(self, subfolder, file_path):
        """Upload file_path into Pharmacy Invoices/<subfolder>, make it
        link-viewable, and return its shareable webViewLink. Retried (see NUM_RETRIES /
        UPLOAD_ATTEMPTS); a copy left half-done by a failed attempt (uploaded but not yet shared)
        is deleted so it can't linger privately. Raises the last error if every attempt fails."""
        file_path = Path(file_path)
        parent_id = self._subfolder_id(subfolder)
        last_error = None
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                return self._upload_once(parent_id, file_path)
            except Exception as e:
                if _is_login_error(e):
                    raise
                last_error = e
                if attempt < UPLOAD_ATTEMPTS:
                    time.sleep(RETRY_PAUSE_SECONDS * attempt)
        raise last_error

    def _upload_once(self, parent_id, file_path):
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(file_path), resumable=False)
        created = self.service.files().create(
            body={"name": file_path.name, "parents": [parent_id]},
            media_body=media, fields="id, webViewLink").execute(num_retries=NUM_RETRIES)
        file_id = created["id"]
        try:
            # anyone with the link can view
            self.service.permissions().create(
                fileId=file_id, body={"type": "anyone", "role": "reader"}).execute(num_retries=NUM_RETRIES)
            link = created.get("webViewLink")
            if not link:
                link = self.service.files().get(
                    fileId=file_id, fields="webViewLink").execute(num_retries=NUM_RETRIES).get("webViewLink")
            if not link:
                raise RuntimeError("Drive returned no share link")
            return link
        except Exception:
            try:
                self.service.files().delete(fileId=file_id).execute(num_retries=NUM_RETRIES)
            except Exception:
                pass                              # best effort — the next attempt uploads a fresh copy
            raise


def _is_login_error(e):
    """An expired/revoked Google login (google-auth RefreshError) — retrying can't fix it."""
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:
        return False
    return isinstance(e, RefreshError)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python drive_uploader.py <file> [invoice_files|payment_files]")
        raise SystemExit(1)
    sub = sys.argv[2] if len(sys.argv) > 2 else "invoice_files"
    print(DriveUploader().upload(sub, sys.argv[1]))
