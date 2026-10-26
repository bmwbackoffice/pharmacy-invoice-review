"""READ-ONLY: who/what marked these invoices as entered in QuickBooks, and any QB-upload match.
    python check_entered_marks.py 1504 3864
"""
import sys
import psycopg2.extras
import database

ids = [int(x) for x in sys.argv[1:]]
conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, invoice_number, amount, entered_in_qb, entered_qb_by, entered_qb_at "
                    "FROM invoices WHERE id = ANY(%s) ORDER BY id", (ids,))
        for r in cur.fetchall():
            print(dict(r))
        cur.execute("SELECT record_id, batch_id, qb_num, qb_name, qb_amount, qb_date, confidence_reason, status "
                    "FROM qb_match_candidates WHERE record_type = 'invoice' AND record_id = ANY(%s) "
                    "ORDER BY record_id, batch_id", (ids,))
        rows = cur.fetchall()
        print(f"\nQuickBooks-upload matches: {len(rows)}")
        for r in rows:
            print(" ", dict(r))
finally:
    conn.close()
