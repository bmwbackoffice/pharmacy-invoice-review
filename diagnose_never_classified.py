"""READ-ONLY. Investigates the 2 emails stuck at 'Never classified (fetched, no run)' on the
/emails ledger: 'Fwd: New scan' from vitamins@maplerx.com (9/28/26) and 'Maple Scanner'
(8/12/26, no sender). Pulls their full emails-table rows, and checks whether classify_emails.py
would currently pick them up (status, run_id, any error) and whether their source file is still
present locally."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== emails matching these two ===")
        cur.execute("""
            SELECT * FROM emails
            WHERE subject ILIKE %s OR subject ILIKE %s OR filename ILIKE %s
            ORDER BY received_at
        """, ("%Fwd: New scan%", "%Maple Scanner%", "%Maple Scanner%"))
        rows = cur.fetchall()
        for r in rows:
            print(r)
            print()
        print(f"count: {len(rows)}\n")

        print("=== overall count of status counts in emails table (context) ===")
        cur.execute("SELECT status, count(*) FROM emails GROUP BY status ORDER BY count(*) DESC")
        for r in cur.fetchall():
            print(r)

        print("\n=== how many emails total sit at status='fetched' with run_id IS NULL "
              "(i.e. same 'never classified' limbo) ===")
        cur.execute("SELECT count(*) FROM emails WHERE status = 'fetched' AND run_id IS NULL")
        print(cur.fetchone())
finally:
    conn.close()
