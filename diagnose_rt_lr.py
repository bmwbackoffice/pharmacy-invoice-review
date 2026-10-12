"""Joel says R&T and L&R are genuinely two different, separate vendors in his
real world — so why is invoice #861 (vendor_raw/vendor_matched = 'R & T
Distributors') resolved to standard_vendor 'L&R Distributors, INC / LR-
Eshop' instead of 'R & T Dist.'? Check every other R&T-ish invoice's
resolution, and the edit_log history on invoice 861 specifically."""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== every invoice with vendor_matched/vendor_raw mentioning 'R & T' or 'R&T' ===")
        cur.execute("""
            SELECT id, vendor_raw, vendor_matched, standard_vendor, qb_vendor,
                   invoice_number, amount, store_matched, date_added
            FROM invoices
            WHERE vendor_raw ILIKE '%%R %% T%%' OR vendor_matched ILIKE '%%R %% T%%'
               OR vendor_raw ILIKE '%%R&T%%' OR vendor_matched ILIKE '%%R&T%%'
            ORDER BY id
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== edit_log entries for invoices id 861 ===")
        cur.execute("""
            SELECT field, old_value, new_value, changed_by, changed_at
            FROM edit_log WHERE table_name = 'invoices' AND record_id = '861'
            ORDER BY changed_at
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== vendor_name_map row creation/edit info for both entries ===")
        cur.execute("""
            SELECT id, standard_vendor_name, created_by, created_at, edited_by, edited_at
            FROM vendor_name_map WHERE id IN (305, 474)
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))
finally:
    conn.close()
