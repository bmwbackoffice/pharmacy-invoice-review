"""Read-only follow-up to round12: breaks down the 161 'no trace anywhere'
stuck emails by sender (to separate real vendor invoices/statements from
scanner noise like 'Scan to E-mail Server Job'), and reports disk location
for ALL 237 stuck emails (eligible for auto-retry vs. archived out of reach)."""
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

        no_trace = []
        dir_location = {"test_emails": 0, "processed_emails": 0, "review_pending": 0, "missing": 0}
        for fn, frm, subj, recv in stuck:
            found = False
            cur.execute("SELECT 1 FROM invoices WHERE email_filename=%s "
                        "UNION ALL SELECT 1 FROM credit_memos WHERE email_filename=%s "
                        "UNION ALL SELECT 1 FROM payments WHERE email_filename=%s", (fn, fn, fn))
            if cur.fetchone():
                found = True
            cur.execute("SELECT 1 FROM review_queue WHERE filename=%s OR source_email=%s", (fn, fn))
            if cur.fetchone():
                found = True
            cur.execute("SELECT 1 FROM ignored_reviews WHERE filename=%s OR source_email=%s", (fn, fn))
            if cur.fetchone():
                found = True
            if not found:
                no_trace.append((fn, frm or "(none)", subj or "", recv))

            loc = "missing"
            for name, d in DIRS.items():
                if (d / fn).exists():
                    loc = name
                    break
            dir_location[loc] += 1

        print(f"No-trace total: {len(no_trace)}\n")
        print("No-trace by sender:")
        from collections import Counter
        c = Counter(frm for _, frm, _, _ in no_trace)
        for frm, n in c.most_common(25):
            print(f"  {frm:40s} {n}")

        print("\nNo-trace, excluding obvious scanner/noise senders (subject not a scan job):")
        real = [(fn, frm, subj, recv) for fn, frm, subj, recv in no_trace
                if "scan to e-mail" not in subj.lower() and "scan job" not in subj.lower()]
        print(f"  {len(real)} of {len(no_trace)}")
        for fn, frm, subj, recv in real[:40]:
            print(f"  {recv}  {frm:35s}  {subj}")

        print("\nDisk location, ALL 237 stuck:")
        for k, v in dir_location.items():
            print(f"  {k}: {v}")
finally:
    conn.close()
