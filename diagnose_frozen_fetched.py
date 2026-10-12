"""READ-ONLY. Investigates the broader "fetched / run_id IS NULL" backlog (28 emails per the
last diagnostic) behind the 2 stuck rows on the /emails ledger ("Fwd: New scan" 9/28/26 and
"Maple Scanner" 8/12/26). Both of those 2 have their source .eml file ALREADY sitting in
processed_emails/ (confirmed by direct filesystem check) rather than test_emails/ -- meaning the
file DID leave the inbox at some point, but the emails-table status/run_id was never advanced off
its fetch-time value. This looks like the known "frozen 'fetched' row" bug class the code's own
comments describe (the 237-email and 64-email backlogs that stamp_handled_emails/
stamp_moved_from_queue were built to stop happening GOING FORWARD) -- just a batch of rows that
were already orphaned before those backstops existed, never swept up retroactively.

This script does NOT touch anything. It:
  1. Lists every emails row with status='fetched' AND run_id IS NULL (the full backlog, not just
     the 2 examples), with email_type/received_at/updated_at/from_address.
  2. For each, checks whether a record (invoice/credit_memo/payment) already exists with that
     email_filename -- i.e. whether the data is ALREADY correctly recorded and this is purely a
     stale status label, vs. a genuine orphan with nothing recorded.
  3. Checks edit_log for any rows referencing these filenames, to see if something touched them
     and when.
"""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== all emails stuck at status='fetched' AND run_id IS NULL ===")
        cur.execute("""
            SELECT filename, email_type, status, run_id, from_address, source,
                   received_at, updated_at
            FROM emails
            WHERE status = 'fetched' AND run_id IS NULL
            ORDER BY received_at
        """)
        stuck = cur.fetchall()
        for r in stuck:
            print(r)
        print(f"\ncount: {len(stuck)}\n")

        filenames = [r["filename"] for r in stuck]

        print("=== which of these already have a recorded invoice/credit_memo/payment row ===")
        cur.execute("""
            SELECT email_filename, 'invoice' AS record_type, id, invoice_number AS number, amount
            FROM invoices WHERE email_filename = ANY(%s)
            UNION ALL
            SELECT email_filename, 'credit_memo', id, credit_memo_number, amount
            FROM credit_memos WHERE email_filename = ANY(%s)
            UNION ALL
            SELECT email_filename, 'payment', id, reference_number, amount
            FROM payments WHERE email_filename = ANY(%s)
            ORDER BY email_filename
        """, (filenames, filenames, filenames))
        recs = cur.fetchall()
        for r in recs:
            print(r)
        recorded_fns = {r["email_filename"] for r in recs}
        print(f"\n{len(recorded_fns)} of {len(filenames)} stuck filenames already have at least one recorded row.")
        print(f"{len(filenames) - len(recorded_fns)} have NOTHING recorded under their filename.\n")

        print("=== edit_log entries mentioning these filenames (if the column exists) ===")
        try:
            cur.execute("""
                SELECT * FROM edit_log
                WHERE filename = ANY(%s) OR record_id::text = ANY(%s)
                ORDER BY changed_at
            """, (filenames, filenames))
            for r in cur.fetchall():
                print(r)
        except Exception as e:
            print(f"(skipped: {e})")
finally:
    conn.close()
