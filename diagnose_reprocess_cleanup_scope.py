"""For the 13 confirmed same-email-reprocessed cases, check whether a SECOND
invoices row actually exists (email_filename = 'Maple Scanner - <id>.eml'),
and if so whether it's a live un-ignored duplicate_suspect, to scope the
cleanup needed."""
import database

msg_ids = [
    "19ff146f93e2fcc2", "19ff62aa77655265", "1a010edb7dbdbd16",
    "1a01f900cd357980", "1a010ef614486a62", "1a04558eab429b07",
    "1a06272d4728714f", "1a0627e6d5bd9eac", "1a07d4fbfb7f8100",
    "1a07d56d98a82502", "1a0a697fe9bbca9f", "1a0a6a8b3eead46b",
]

conn = database.connect()
try:
    with conn.cursor() as cur:
        for msg_id in msg_ids:
            fn = f"Maple Scanner - {msg_id}.eml"
            cur.execute("""
                SELECT id, vendor_matched, invoice_number, amount, invoice_date,
                       duplicate_suspect, duplicate_ignored, duplicate_group_id,
                       entered_in_qb
                FROM invoices WHERE email_filename = %s
            """, (fn,))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            print(f"\n{msg_id}: {len(rows)} invoices row(s) from the re-scan")
            for r in rows:
                print("  ", dict(zip(cols, r)))
finally:
    conn.close()
