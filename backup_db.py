"""
Nightly database backup (2026-10-26, Joel: yes). Everything the app knows that isn't in Gmail or on Drive --
records, review answers, "entered in QuickBooks" marks, Setup tables, learnings, the edit log -- lives only in
the local PostgreSQL database, so a disk failure would lose it. This:

  1. dumps the database with pg_dump (custom format, compressed) to backups/pharmacy_invoices_<date>_<time>.dump
     (the password goes to pg_dump through PGPASSWORD, never on its command line);
  2. checks the dump reads back (pg_restore --list);
  3. uploads it to Google Drive, Pharmacy Invoices/db_backups, PRIVATE: unlike the invoice documents it gets no
     "anyone with the link" sharing (it holds every record and the users' password hashes);
  4. keeps the last KEEP_DAYS days, on this PC and on Drive;
  5. appends a line to logs/backups.log (the dashboard's System health shows the last backup from it).

Run:   python backup_db.py            back up now
       python backup_db.py --list     what's kept, here and on Drive
       python backup_db.py --no-drive back up to this PC only
Nightly by the scheduled task setup_backup_task.ps1 sets up.

Restore (into an EMPTY or replaced database, after stopping the web app and the scheduled run):
       pg_restore --clean --if-exists --no-owner --dbname=<DATABASE_URL> backups\\<file>.dump
"""
import glob
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE = Path(__file__).parent
BACKUP_DIR = BASE / "backups"
LOG = BASE / "logs" / "backups.log"
DRIVE_FOLDER = "db_backups"           # inside Pharmacy Invoices/ (drive_uploader.ROOT_FOLDER)
DRIVE_PATH = f"Pharmacy Invoices/{DRIVE_FOLDER}"
KEEP_DAYS = 14
DUMP_TIMEOUT_SECONDS = 15 * 60
NAME_RE = re.compile(r"^pharmacy_invoices_(\d{8})_(\d{4})\.dump$")


def _log(line):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(line)
    try:
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{stamp}  {line}\n")
    except OSError:
        pass


def _pg_tool(name):
    """pg_dump / pg_restore: PG_BIN (a folder) if set, else the newest PostgreSQL install, else the PATH."""
    if os.environ.get("PG_BIN"):
        p = Path(os.environ["PG_BIN"]) / f"{name}.exe"
        if p.exists():
            return str(p)
    found = glob.glob(rf"C:\Program Files\PostgreSQL\*\bin\{name}.exe")
    if found:
        return max(found, key=lambda p: int(re.search(r"PostgreSQL\\(\d+)", p).group(1))
                   if re.search(r"PostgreSQL\\(\d+)", p) else 0)
    return shutil.which(name)


def _connection_args():
    """pg_dump arguments + environment from DATABASE_URL (the password only in the environment)."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL isn't set.")
    u = urlparse(url)
    args = ["--host", u.hostname or "localhost", "--port", str(u.port or 5432),
            "--username", unquote(u.username or ""), "--dbname", (u.path or "/").lstrip("/")]
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    return args, env


def dump():
    """Write a new dump and check it reads back. Returns its path."""
    pg_dump, pg_restore = _pg_tool("pg_dump"), _pg_tool("pg_restore")
    if not pg_dump or not pg_restore:
        raise RuntimeError("pg_dump / pg_restore not found (set PG_BIN to PostgreSQL's bin folder).")
    BACKUP_DIR.mkdir(exist_ok=True)
    out = BACKUP_DIR / f"pharmacy_invoices_{datetime.now():%Y%m%d_%H%M}.dump"
    args, env = _connection_args()
    r = subprocess.run([pg_dump, "--format=custom", "--compress=6", f"--file={out}", *args],
                       env=env, capture_output=True, text=True, timeout=DUMP_TIMEOUT_SECONDS)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed ({r.returncode}): {(r.stderr or '').strip()[:500]}")
    check = subprocess.run([pg_restore, "--list", str(out)], capture_output=True, text=True, timeout=300)
    if check.returncode != 0 or "TABLE DATA" not in check.stdout:
        raise RuntimeError(f"the dump doesn't read back: {(check.stderr or '').strip()[:300]}")
    tables = check.stdout.count("TABLE DATA")
    _log(f"dumped {out.name}: {out.stat().st_size / 1e6:.1f} MB, {tables} tables")
    return out


def _drive():
    from drive_uploader import ROOT_FOLDER, DriveUploader
    up = DriveUploader()
    root = up._find_or_create_folder(ROOT_FOLDER)
    return up.service, up._find_or_create_folder(DRIVE_FOLDER, root)


def upload(path):
    """Upload one dump to the private Drive folder (no sharing permission is created)."""
    from googleapiclient.http import MediaFileUpload
    service, folder = _drive()
    media = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=True)
    created = service.files().create(body={"name": path.name, "parents": [folder]}, media_body=media,
                                     fields="id, size").execute(num_retries=4)
    _log(f"uploaded {path.name} to Drive ({DRIVE_PATH}, private)")
    return created["id"]



def _drive_files(service, folder):
    files, token = [], None
    while True:
        resp = service.files().list(q=f"'{folder}' in parents and trashed=false", spaces="drive",
                                    fields="nextPageToken, files(id, name, createdTime, size)",
                                    pageToken=token).execute(num_retries=4)
        files += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            return files


def prune(use_drive=True):
    """Delete backups older than KEEP_DAYS, here and (only the ones this script made) on Drive."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    n_local = 0
    for p in BACKUP_DIR.glob("pharmacy_invoices_*.dump"):
        if NAME_RE.match(p.name) and datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cutoff:
            p.unlink()
            n_local += 1
    n_drive = 0
    if use_drive:
        service, folder = _drive()
        for f in _drive_files(service, folder):
            made = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
            if NAME_RE.match(f["name"]) and made < cutoff:
                service.files().delete(fileId=f["id"]).execute(num_retries=4)
                n_drive += 1
    if n_local or n_drive:
        _log(f"removed backups older than {KEEP_DAYS} days: {n_local} here, {n_drive} on Drive")


def list_backups():
    local = sorted(BACKUP_DIR.glob("pharmacy_invoices_*.dump"))
    print(f"On this PC ({BACKUP_DIR}): {len(local)}")
    for p in local:
        print(f"  {p.name}  {p.stat().st_size / 1e6:.1f} MB")
    try:
        service, folder = _drive()
        files = sorted(_drive_files(service, folder), key=lambda f: f["name"])
        print(f"On Drive ({DRIVE_PATH}): {len(files)}")
        for f in files:
            print(f"  {f['name']}  {int(f.get('size') or 0) / 1e6:.1f} MB")
    except Exception as e:
        print(f"(couldn't read the Drive folder: {e})")


def main():
    if "--list" in sys.argv:
        list_backups()
        return 0
    use_drive = "--no-drive" not in sys.argv
    try:
        path = dump()
    except Exception as e:
        _log(f"BACKUP FAILED: {e}")
        return 1
    status = 0
    if use_drive:
        try:
            upload(path)
        except Exception as e:
            _log(f"DRIVE UPLOAD FAILED (the backup is on this PC only): {e}")
            status = 2
    try:
        prune(use_drive=use_drive and status == 0)
    except Exception as e:
        _log(f"(couldn't remove old backups: {e})")
    _log("backup OK" + ("" if use_drive and status == 0 else " (this PC only)"))
    return status


if __name__ == "__main__":
    sys.exit(main())
