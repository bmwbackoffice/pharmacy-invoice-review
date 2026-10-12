"""Read-only: for every email stuck at status='fetched'/run_id IS NULL, check
whether it actually WAS handled somewhere (review_queue, ignored_reviews,
live invoices/credit_memos/payments under this exact filename, investigations)
-- i.e. is this just a stale ledger row for work that's actually done, or is
it a genuine untracked gap with no record anywhere? Also checks whether the
source file still physically exists in test_emails/ (eligible for automatic
retry) vs. was archived to processed_emails/ or review_pending/ (invisible to
future runs regardless of DB status)."""
import database
from pathlib import Path

BASE = Path(__file__).parent
DIRS = {
    "test_emails": BASE / "test_emails",
    "processed_emails": BASE / "processed_emails",
    "review_pending": BASE / "review_pending",
}

conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT filename, from_address, subject, received_at
            FROM emails
            WHERE status = 'fetched' AND run_id IS NULL AND legacy_migrated IS NOT TRUE
            ORDER BY received_at
        """)
        stuck = cur.fetchall()

        buckets = {"live_record": 0, "review_queue": 0, "ignored_reviews": 0,
                   "investigations": 0, "none_found": 0}
        none_found_samples = []
        dir_location = {"test_emails": 0, "processed_emails": 0, "review_pending": 0, "missing": 0}

        for fn, frm, subj, recv in stuck:
            found = False
            cur.execute("SELECT 1 FROM invoices WHERE email_filename=%s "
                        "UNION ALL SELECT 1 FROM credit_memos WHERE email_filename=%s "
                        "UNION ALL SELECT 1 FROM payments WHERE email_filename=%s", (fn, fn, fn))
            if cur.fetchone():
                buckets["live_record"] += 1
                found = True
            cur.execute("SELECT 1 FROM review_queue WHERE filename=%s OR source_email=%s", (fn, fn))
            if cur.fetchone():
                buckets["review_queue"] += 1
                found = True
            cur.execute("SELECT 1 FROM ignored_reviews WHERE filename=%s OR source_email=%s", (fn, fn))
            if cur.fetchone():
                buckets["ignored_reviews"] += 1
                found = True
            if not found:
                buckets["none_found"] += 1
                if len(none_found_samples) < 15:
                    none_found_samples.append((fn, frm, subj, recv))

            loc = "missing"
            for name, d in DIRS.items():
                if (d / fn).exists():
                    loc = name
                    break
            dir_location[loc] += 1

        print(f"Total stuck: {len(stuck)}\n")
        print("Found elsewhere (so the ledger row is just stale, work was actually done):")
        for k in ("live_record", "review_queue", "ignored_reviews"):
            print(f"  {k}: {buckets[k]}")
        print(f"\nNO trace anywhere (invoices/credit_memos/payments/review_queue/ignored_reviews): {buckets['none_found']}")
        print("\nSample of 'no trace' ones:")
        for fn, frm, subj, recv in none_found_samples:
            print(f"  {recv}  {frm:35s}  {subj}")

        print("\nCurrent file location on disk:")
        for k, v in dir_location.items():
            print(f"  {k}: {v}")
finally:
    conn.close()
