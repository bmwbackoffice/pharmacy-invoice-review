"""READ-ONLY. Joel says 'Abbot Nutrition' is missing from his vendor list. Checks:
1) vendor_name_map for any row matching abbot/abbott (either spelling).
2) Any invoices/credit_memos referencing this vendor (vendor_raw/vendor_matched), to see what
   name(s) are actually in use and whether they're standardized.
3) Whether there's any audit trail (edit_log doesn't cover vendor_name_map deletes directly,
   but check anyway) of a vendor_name_map row ever existing for this name and being removed.
"""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== vendor_name_map rows matching abbot/abbott ===")
        cur.execute("""
            SELECT * FROM vendor_name_map
            WHERE standard_vendor_name ILIKE %s OR standard_vendor_name ILIKE %s
               OR aliases ILIKE %s OR aliases ILIKE %s
               OR name_3m ILIKE %s OR name_madison ILIKE %s OR name_maple ILIKE %s
               OR name_refuah ILIKE %s OR name_westgate ILIKE %s OR name_bmw ILIKE %s
               OR name_lakewood_rx ILIKE %s
        """, tuple(["%abbot%", "%abbott%"] + ["%abbot%"] * 9))
        rows = cur.fetchall()
        for r in rows:
            print(r)
        print(f"count: {len(rows)}\n")

        print("=== invoices with vendor_raw/vendor_matched/standard_vendor matching abbot/abbott ===")
        cur.execute("""
            SELECT id, vendor_raw, vendor_matched, standard_vendor, standard_account, qb_vendor,
                   store_matched, invoice_number, amount, invoice_date, needs_review, review_reason
            FROM invoices
            WHERE vendor_raw ILIKE %s OR vendor_matched ILIKE %s OR standard_vendor ILIKE %s
               OR vendor_raw ILIKE %s OR vendor_matched ILIKE %s OR standard_vendor ILIKE %s
            ORDER BY id DESC LIMIT 30
        """, ("%abbot%", "%abbot%", "%abbot%", "%abbott%", "%abbott%", "%abbott%"))
        rows = cur.fetchall()
        for r in rows:
            print(r)
        print(f"count (showing up to 30): {len(rows)}\n")

        print("=== credit_memos with vendor_raw/vendor_matched/standard_vendor matching abbot/abbott ===")
        cur.execute("""
            SELECT id, vendor_raw, vendor_matched, standard_vendor, store_matched,
                   credit_memo_number, amount, credit_memo_date, needs_review
            FROM credit_memos
            WHERE vendor_raw ILIKE %s OR vendor_matched ILIKE %s OR standard_vendor ILIKE %s
               OR vendor_raw ILIKE %s OR vendor_matched ILIKE %s OR standard_vendor ILIKE %s
            ORDER BY id DESC LIMIT 30
        """, ("%abbot%", "%abbot%", "%abbot%", "%abbott%", "%abbott%", "%abbott%"))
        rows = cur.fetchall()
        for r in rows:
            print(r)
        print(f"count: {len(rows)}\n")

        print("=== excluded_vendors (skip_qb list) matching abbot/abbott, in case it's that list ===")
        cur.execute("SELECT * FROM excluded_vendors WHERE vendor_name ILIKE %s OR vendor_name ILIKE %s",
                    ("%abbot%", "%abbott%"))
        for r in cur.fetchall():
            print(r)
finally:
    conn.close()
