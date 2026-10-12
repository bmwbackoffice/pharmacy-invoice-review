"""Why didn't 'R & T Dist.' resolve to the same standard vendor as invoice #861
after a variant/alias was added to the vendor list?"""
import classify_emails as ce
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== invoices id 861 ===")
        cur.execute("SELECT id, vendor_raw, vendor_matched, standard_vendor, qb_vendor, "
                     "invoice_number, amount, store_matched, entered_in_qb, skip_qb_import "
                     "FROM invoices WHERE id = 861")
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        print(dict(zip(cols, row)) if row else "NOT FOUND")

        print("\n=== qb_match_candidates row(s) for record_id 861 ===")
        cur.execute("SELECT id, record_type, record_id, qb_type, qb_num, qb_name, qb_amount, "
                     "qb_store, confidence_reason, status FROM qb_match_candidates "
                     "WHERE record_id = 861 AND record_type = 'invoice'")
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== vendor_name_map rows mentioning 'R & T' or 'R&T' anywhere ===")
        cur.execute("""
            SELECT id, standard_vendor_name, aliases, name_3m, name_madison, name_maple,
                   name_refuah, name_westgate, name_bmw, name_lakewood_rx
            FROM vendor_name_map
            WHERE standard_vendor_name ILIKE '%%R%%T%%Dist%%'
               OR aliases ILIKE '%%R%%T%%'
               OR name_3m ILIKE '%%R%%T%%'
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))
finally:
    conn.close()

print("\n=== live resolve_standard_vendor('R & T Dist.') ===")
std = ce.load_vendor_standardization()
print("resolved ->", ce.resolve_standard_vendor("R & T Dist.", std_tables=std))
print("\n=== live resolve_standard_vendor('R & T Distributors') for comparison ===")
print("resolved ->", ce.resolve_standard_vendor("R & T Distributors", std_tables=std))
