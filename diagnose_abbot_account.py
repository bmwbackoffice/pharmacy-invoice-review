"""READ-ONLY. After Joel's Abbott -> Abbot Nutrition merge, check whether the account mapping
for the two names differs (vendor_account_assignment), and live-test what resolve_standard_vendor
now returns for both spellings -- to see if backfilling invoices 1604/1605 to the new standard
name would also change their account."""
import psycopg2.extras
import database
import classify_emails as ce

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== vendor_account_assignment rows for Abbott / Abbot Nutrition ===")
        cur.execute("SELECT * FROM vendor_account_assignment WHERE standard_vendor_name ILIKE %s "
                    "OR standard_vendor_name ILIKE %s", ("%abbot%", "%abbott%"))
        for r in cur.fetchall():
            print(r)

        std = ce.load_vendor_standardization()
        print("\n=== live resolve_standard_vendor ===")
        print("Abbott ->", repr(ce.resolve_standard_vendor("Abbott", std_tables=std)))
        print("Abbot Nutrition ->", repr(ce.resolve_standard_vendor("Abbot Nutrition", std_tables=std)))

        print("\n=== current invoices 1604/1605 ===")
        cur.execute("SELECT id, vendor_raw, vendor_matched, standard_vendor, standard_account, "
                     "qb_vendor, qb_account FROM invoices WHERE id IN (1604, 1605)")
        for r in cur.fetchall():
            print(r)
finally:
    conn.close()
