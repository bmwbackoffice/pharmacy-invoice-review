"""READ-ONLY. Scopes a proper merge of the 'Pharmacy Automation' and 'PAS PACKAGING'
vendor_name_map rows (Joel says they're the same company) -- checks how many invoices/credit
memos currently stand on EACH standard name, so merging doesn't orphan anything."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for name in ("Pharmacy Automation", "PAS PACKAGING"):
            print(f"=== invoices with standard_vendor = {name!r} ===")
            cur.execute("SELECT id, vendor_raw, vendor_matched, standard_vendor, store_matched, "
                        "invoice_number, amount, entered_in_qb FROM invoices "
                        "WHERE standard_vendor = %s ORDER BY id", (name,))
            rows = cur.fetchall()
            for r in rows:
                print(r)
            print(f"count: {len(rows)}\n")

            print(f"=== credit_memos with standard_vendor = {name!r} ===")
            cur.execute("SELECT id, vendor_raw, vendor_matched, standard_vendor, store_matched, "
                        "credit_memo_number, amount, entered_in_qb FROM credit_memos "
                        "WHERE standard_vendor = %s ORDER BY id", (name,))
            rows = cur.fetchall()
            for r in rows:
                print(r)
            print(f"count: {len(rows)}\n")
finally:
    conn.close()
