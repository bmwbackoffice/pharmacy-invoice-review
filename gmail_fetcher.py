"""
Fetch invoice emails from Gmail into test_emails/ so the classifier can process
them exactly like manually-dropped files.

    python gmail_fetcher.py

Auth uses the shared google_auth.py (one OAuth flow covering gmail.modify +
drive.file, Techloq-aware TLS). For each matching message we:
  - save the full raw message as a .eml file in test_emails/,
  - build a Gmail deep-link that opens the ORIGINAL email in the CORRECT mailbox:
    https://mail.google.com/mail/u/0/?authuser=<mailbox>#all/<messageID>
    (authuser pins the account, read from the API via getProfile — not hardcoded
    — so the link doesn't open whichever account is first in the viewer's browser),
  - record filename -> {gmail_link, message_id, subject} in
    output/gmail_links.json (the sidecar the classifier reads), and in the
    Postgres `emails` table when DATABASE_URL is set,
  - add a Gmail label ("InvoicesFetched") so the message isn't fetched again.

Configure the search with the GMAIL_QUERY env var (Gmail search syntax).
Default: EVERY inbox message from the last 30 days (not just ones with
attachments — many invoices/payment requests arrive in the body), excluding
already-fetched ones. Gmail search excludes Spam/Trash automatically; "in:inbox"
also excludes Sent. Narrow it with GMAIL_QUERY (e.g. a label or from: filter)
or widen the window; GMAIL_MAX_RESULTS caps how many are pulled per run.
"""

import base64
import json
import os
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote

from google_auth import build_service, get_credentials

BASE_DIR = Path(__file__).parent
EMAILS_DIR = BASE_DIR / "test_emails"
OUTPUT_DIR = BASE_DIR / "output"
LINKS_SIDECAR = OUTPUT_DIR / "gmail_links.json"

FETCHED_LABEL = "InvoicesFetched"
# Every inbox email (last 30 days), not only ones with attachments — invoices
# and payment requests often arrive in the body. Promotions/Social are excluded
# as obvious non-invoice noise. The date window is a SAFETY RAIL, not a dedup:
# it keeps the pipeline from dredging up old already-handled invoices that would
# look "new" and risk a double QuickBooks entry. Override with GMAIL_QUERY.
DEFAULT_QUERY = "in:inbox newer_than:30d -category:promotions -category:social"
# ...but a Promotions-tab email that is plainly a bill IS fetched (2026-10-29: Mars Med Supply's Shopify
# "Invoice #D2872" landed in Promotions and was never read). Searched separately and merged in; promotional
# blasts that still slip through are typed "none" by the classifier.
PROMO_BILLS_QUERY = ('in:inbox newer_than:30d category:promotions '
                     'subject:(invoice OR receipt OR statement OR "credit memo" OR "payment received" OR bill)')
# Per-run cap only (each fetched email = one downstream classification API call).
# Fetched emails are labeled, so runs self-pace — rerun to grab the next batch.
# This is the TOTAL across pages; fetch() paginates (Gmail returns <=500 per page).
MAX_RESULTS = int(os.environ.get("GMAIL_MAX_RESULTS", "200"))


def _load_sidecar():
    if LINKS_SIDECAR.exists():
        return json.loads(LINKS_SIDECAR.read_text(encoding="utf-8"))
    return {}


def _save_sidecar(data):
    OUTPUT_DIR.mkdir(exist_ok=True)
    LINKS_SIDECAR.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_label(service):
    """Return the id of the FETCHED_LABEL, creating it if needed."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == FETCHED_LABEL:
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": FETCHED_LABEL, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def _header(payload, name):
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _internal_date_iso(internal_date_ms):
    """Gmail internalDate (string, ms since epoch) -> UTC ISO string, or None."""
    if not internal_date_ms:
        return None
    try:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def get_mailbox_email(service):
    """The authenticated mailbox address (e.g. maplegroupap@lakewoodrx.com),
    read from Gmail rather than hardcoded."""
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def build_gmail_link(mailbox, message_id):
    """A deep-link that opens the RIGHT mailbox regardless of which Google
    account is first in the viewer's browser. `authuser=<email>` pins the
    account; `#all/<id>` opens the specific message.

        https://mail.google.com/mail/u/0/?authuser=<mailbox>#all/<messageID>

    If `#all/<hex id>` ever proves unreliable, switch to the message-id search
    form (build_gmail_link_rfc822) — the RFC822 Message-ID is captured at fetch."""
    return f"https://mail.google.com/mail/u/0/?authuser={quote(mailbox, safe='@')}#all/{message_id}"


def build_gmail_link_rfc822(mailbox, rfc822_message_id):
    """Fallback deep-link that finds the message by its RFC822 Message-ID header
    (robust across Gmail's internal id quirks)."""
    mid = (rfc822_message_id or "").strip().lstrip("<").rstrip(">")
    return (f"https://mail.google.com/mail/u/0/?authuser={quote(mailbox, safe='@')}"
            f"#search/{quote('rfc822msgid:' + mid, safe='@:')}")


def _safe_stem(subject, message_id):
    stem = (subject or "email").strip() or "email"
    stem = "".join(c for c in stem if c not in '\\/:*?"<>|').strip()[:80]
    return f"{stem} - {message_id}"  # message_id keeps it unique


def fetch(query=None):
    creds = get_credentials()
    service = build_service("gmail", "v1", creds)
    EMAILS_DIR.mkdir(exist_ok=True)

    label_id = _ensure_label(service)
    mailbox = get_mailbox_email(service)
    print(f"Mailbox: {mailbox}")
    q = f"({query or os.environ.get('GMAIL_QUERY', DEFAULT_QUERY)}) -label:{FETCHED_LABEL}"
    print(f"Gmail search: {q}")

    # Paginate: Gmail returns at most 500 ids per page, so collect across pages up to MAX_RESULTS
    # (the TOTAL cap). Without this, a single list call silently capped a run at 500.
    messages = []
    page_token = None
    while len(messages) < MAX_RESULTS:
        page_size = min(500, MAX_RESULTS - len(messages))
        resp = service.users().messages().list(
            userId="me", q=q, maxResults=page_size, pageToken=page_token).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    # Promotions-tab bills (only with the default search; a custom GMAIL_QUERY is used as given).
    if not query and not os.environ.get("GMAIL_QUERY") and len(messages) < MAX_RESULTS:
        seen = {m["id"] for m in messages}
        resp = service.users().messages().list(
            userId="me", q=f"({PROMO_BILLS_QUERY}) -label:{FETCHED_LABEL}",
            maxResults=min(100, MAX_RESULTS - len(messages))).execute()
        extra = [m for m in resp.get("messages", []) if m["id"] not in seen]
        if extra:
            print(f"  + {len(extra)} bill-like email(s) from the Promotions tab")
            messages.extend(extra)
    if not messages:
        print("No new messages matched.")
        return []

    # DB is optional; store metadata in the emails table when configured.
    db = None
    try:
        import database
        if database.is_configured():
            database.init_db()
            db = database
    except Exception as e:  # pragma: no cover - DB is optional
        print(f"(DB not used for email metadata: {e})")

    sidecar = _load_sidecar()
    saved = []
    for m in messages:
        mid = m["id"]
        full = service.users().messages().get(userId="me", id=mid, format="raw").execute()
        raw = base64.urlsafe_b64decode(full["raw"].encode("ascii"))

        meta = service.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["Subject", "Message-ID", "From"]).execute()
        payload = meta.get("payload", {})
        subject = _header(payload, "Subject")
        rfc822_msgid = _header(payload, "Message-ID")  # captured for the fallback link form
        # Sender email only (drop the display name) — used as a self-scan store
        # signal during classification (sender_store_map).
        from_address = (parseaddr(_header(payload, "From") or "")[1] or "").strip().lower() or None
        # internalDate = Gmail's server receive time (ms since epoch), AUTHORITATIVE.
        # The 'Date:' header is unreliable — store scanners have mis-set clocks that
        # put it years off, which used to falsely trip the invoice-date sanity check.
        received_at = _internal_date_iso(meta.get("internalDate"))

        filename = _safe_stem(subject, mid) + ".eml"
        (EMAILS_DIR / filename).write_bytes(raw)

        gmail_link = build_gmail_link(mailbox, mid)
        sidecar[filename] = {"gmail_link": gmail_link, "gmail_message_id": mid,
                             "rfc822_msgid": rfc822_msgid, "mailbox": mailbox,
                             "subject": subject, "from_address": from_address,
                             "received_at": received_at}
        if db:
            db.upsert_email_source(filename, gmail_message_id=mid, gmail_link=gmail_link,
                                   subject=subject, source="gmail", from_address=from_address,
                                   received_at=received_at)

        # Label it so we don't re-fetch on the next run.
        service.users().messages().modify(
            userId="me", id=mid, body={"addLabelIds": [label_id]}).execute()

        saved.append(filename)
        print(f"  saved {filename}")

    _save_sidecar(sidecar)
    print(f"\nFetched {len(saved)} message(s) into {EMAILS_DIR}.")
    return saved


if __name__ == "__main__":
    fetch()
