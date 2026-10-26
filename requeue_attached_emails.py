"""One-time (2026-10-19): re-run the emails that contain ATTACHED EMAILS (message/rfc822), now that the
classifier reads them (classify_emails._attached_emails). Before, their content was skipped -- e.g. the
8 That Sushi Spot receipts inside Sam's "Sushi charges" email, and Schwartz invoice #232266's PDF.

Same mechanism as requeue_by_vendor.py: back up the email's current record rows, move the .eml back to
test_emails/ and set emails.status='fetched'; the next fetch/classify run re-reads it under the current
code and REPLACES its rows (filename-scoped), and duplicate detection checks the new records as usual.

  python requeue_attached_emails.py            -> DRY RUN: lists the emails + their current records
  python requeue_attached_emails.py --commit   -> back up + requeue
Then run "Fetch New Invoices.bat" (or wait for the scheduled run).
"""
import datetime
import email
import email.policy
import shutil
import sys
from pathlib import Path

import psycopg2.extras

import database

BASE = Path(__file__).parent
DIRS = [BASE / "processed_emails", BASE / "review_pending", BASE / "test_emails"]
INBOX = BASE / "test_emails"
COMMIT = "--commit" in sys.argv
TABLES = [("invoices", "invoice_number"), ("credit_memos", "credit_memo_number"), ("payments", "reference_number")]


def has_attached_email(path):
    try:
        raw = path.read_bytes()
        if b"message/rfc822" not in raw.lower():      # quick skip: no attached email in this file
            return 0
        m = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return 0
    return sum(1 for p in m.walk() if p.get_content_type() == "message/rfc822")


found = {}
for d in DIRS:
    if d.exists():
        files = list(d.glob("*.eml"))
        print(f"Scanning {len(files)} email(s) in {d.name}/ ...", flush=True)
        for i, p in enumerate(files, 1):
            n = has_attached_email(p)
            if n:
                found[p.name] = (p, n)
            if i % 500 == 0:
                print(f"  ...{i}", flush=True)
print(f"{'COMMIT' if COMMIT else 'DRY RUN'} -- {len(found)} email(s) contain attached emails\n")

conn = database.connect()
conn.autocommit = False
try:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    names = list(found)
    any_entered = False
    for fn, (p, n) in sorted(found.items()):
        cur.execute("SELECT status, email_type FROM emails WHERE filename = %s", (fn,))
        er = cur.fetchone() or {}
        print(f"[{p.parent.name}] {fn}\n    {n} attached email(s) · now: {er.get('email_type') or '?'} / {er.get('status') or '?'}")
        for t, numcol in TABLES:
            cur.execute(f"SELECT id, vendor_matched, store_matched, {numcol} AS num, amount, entered_in_qb "
                        f"FROM {t} WHERE email_filename = %s ORDER BY id", (fn,))
            for r in cur.fetchall():
                any_entered |= bool(r["entered_in_qb"])
                print(f"      {t[:-1]:11} id={r['id']:<5} {r['vendor_matched']} / {r['store_matched']} / "
                      f"#{r['num']} / {r['amount']}{'  ** ENTERED IN QB **' if r['entered_in_qb'] else ''}")
        cur.execute("SELECT count(*) AS n FROM review_queue WHERE source_email = %s", (fn,))
        nrev = cur.fetchone()["n"]
        if nrev:
            print(f"      + {nrev} open review item(s) for this email")
    if any_entered:
        print("\nNOTE: some current records are already entered in QB. Reprocessing replaces the rows; the app's"
              "\nentered-in-QB marks are carried over to the re-recorded rows, but check them on /qb afterwards.")
    if not COMMIT:
        print("\nNothing changed. Re-run with --commit to requeue.")
        sys.exit(0)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for t, _ in TABLES:
        backup = f"_backup_{t}_attached_{stamp}"
        cur.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM {t} WHERE email_filename = ANY(%s)', (names,))
        if cur.rowcount:
            print(f"backed up {cur.rowcount} {t} row(s) -> {backup}")
        else:
            cur.execute(f'DROP TABLE "{backup}"')
    INBOX.mkdir(exist_ok=True)
    for fn, (p, _) in found.items():
        if p.parent != INBOX:
            shutil.move(str(p), str(INBOX / fn))
    cur.execute("UPDATE emails SET status = 'fetched' WHERE filename = ANY(%s)", (names,))
    conn.commit()
    print(f"\nRequeued {len(found)} email(s). Next: run Fetch New Invoices.bat (or wait for the scheduled run).")
except Exception:
    conn.rollback()
    print("ERROR -- rolled back database changes (any file already moved stays moved; re-running is safe).")
    raise
finally:
    conn.close()
