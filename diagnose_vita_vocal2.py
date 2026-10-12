"""Follow-up: check QB-entry status on both Vita Vocal Health H002863 rows, since that's
objective evidence of whether id 1276 ($1,365/8.24) was ever treated as a real, separately
entered bill historically, vs just bad migrated data."""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, amount, invoice_date, entered_in_qb, entered_qb_by, entered_qb_at,
                   skip_qb_import, class, notes
            FROM invoices WHERE id IN (1276, 2993) ORDER BY id
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))
finally:
    conn.close()
