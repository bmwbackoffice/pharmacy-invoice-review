"""
ONE-OFF BACKFILL — rewrite already-stored Gmail deep-links to the corrected,
mailbox-pinned form so existing records/review items open the right mailbox.

Old (broken):  https://mail.google.com/mail/u/0/#all/<id>
New (correct):  https://mail.google.com/mail/u/0/?authuser=<mailbox>#all/<id>

The mailbox is read from the Gmail API (getProfile), not hardcoded. Rewrites the
gmail_link column in emails, review_queue, invoices, payments, credit_memos, and
the output/gmail_links.json sidecar. Idempotent: links that already carry
?authuser= are left alone. Defaults to --dry-run; act with --commit.

    python backfill_gmail_links.py            # report only
    python backfill_gmail_links.py --commit   # rewrite
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
SIDECAR = BASE_DIR / "output" / "gmail_links.json"

# Insert ?authuser=<mailbox> right before the '#', but only for /u/N/# links that
# don't already have a query string (so it stays idempotent).
_PATTERN = re.compile(r"(https://mail\.google\.com/mail/u/\d+/)#")


def _rewrite(link, mailbox):
    if not link or "authuser=" in link:
        return link  # already fixed / unknown form
    return _PATTERN.sub(lambda m: f"{m.group(1)}?authuser={mailbox}#", link, count=1)


def _get_mailbox():
    from gmail_fetcher import get_mailbox_email
    from google_auth import build_service
    return get_mailbox_email(build_service("gmail", "v1"))


def main():
    commit = "--commit" in sys.argv[1:]
    mailbox = _get_mailbox()
    print(f"=== backfill_gmail_links [{'COMMIT' if commit else 'DRY-RUN'}] ===")
    print(f"mailbox: {mailbox}\n")

    total_changed = 0

    # ---- sidecar JSON ----
    if SIDECAR.exists():
        data = json.loads(SIDECAR.read_text(encoding="utf-8"))
        n = 0
        for fn, meta in data.items():
            new = _rewrite(meta.get("gmail_link"), mailbox)
            if new != meta.get("gmail_link"):
                meta["gmail_link"] = new
                meta.setdefault("mailbox", mailbox)
                n += 1
        print(f"gmail_links.json: {n} link(s) to rewrite")
        total_changed += n
        if commit and n:
            SIDECAR.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ---- database tables ----
    try:
        import database
        if not database.is_configured():
            print("DATABASE_URL not set — skipping DB backfill.")
        else:
            conn = database.connect()
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    for table in ("emails", "review_queue", "invoices", "payments", "credit_memos"):
                        cur.execute(
                            f"SELECT count(*) FROM {table} "
                            f"WHERE gmail_link LIKE 'https://mail.google.com/mail/u/%/#%' "
                            f"AND gmail_link NOT LIKE '%authuser=%'")
                        n = cur.fetchone()[0]
                        print(f"{table}: {n} link(s) to rewrite")
                        total_changed += n
                        if commit and n:
                            # regexp_replace inserts ?authuser=<mailbox> before the '#'
                            cur.execute(
                                f"UPDATE {table} SET gmail_link = regexp_replace("
                                f"gmail_link, '(/mail/u/[0-9]+/)#', "
                                f"'\\1?authuser={mailbox}#') "
                                f"WHERE gmail_link LIKE 'https://mail.google.com/mail/u/%/#%' "
                                f"AND gmail_link NOT LIKE '%authuser=%'")
                if commit:
                    conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"DB backfill skipped/failed: {e}")

    print(f"\nTotal links {'rewritten' if commit else 'to rewrite'}: {total_changed}")
    if not commit:
        print("DRY-RUN — nothing changed. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
