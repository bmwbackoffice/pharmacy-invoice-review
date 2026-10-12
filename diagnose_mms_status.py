"""Current state of the Micro Merchant Systems invoices/payment case: are the
two invoices (TRINV-685027, CINV-662750) and the shared-payment-ID duplicates
now resolved as expected?"""
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
        print("=== invoices matching TRINV-685027 or CINV-662750 ===")
        cur.execute("""
            SELECT id, vendor_matched, invoice_number, amount, invoice_date,
                   duplicate_suspect, duplicate_ignored, duplicate_group_id, email_filename
            FROM invoices
            WHERE invoice_number IN ('TRINV-685027', 'CINV-662750')
            ORDER BY id
        """)
        for r in cur.fetchall():
            print(dict(r))

        print("\n=== payments matching reference ARC_52AFLAM16TUJ ===")
        cur.execute("""
            SELECT id, vendor_matched, reference_number, invoice_number, amount,
                   payment_date, duplicate_suspect, duplicate_ignored, duplicate_group_id,
                   email_filename
            FROM payments
            WHERE reference_number ILIKE '%%52AFLAM16TUJ%%'
            ORDER BY id
        """)
        for r in cur.fetchall():
            print(dict(r))

        print("\n=== any Micro Merchant Systems / PrimeRx / Versapay records flagged duplicate_suspect still open ===")
        cur.execute("""
            SELECT 'invoices' AS tbl, id, vendor_matched, invoice_number, amount, duplicate_suspect, duplicate_ignored
            FROM invoices WHERE vendor_matched ILIKE '%%micro merchant%%' OR vendor_matched ILIKE '%%primerx%%'
            UNION ALL
            SELECT 'payments' AS tbl, id, vendor_matched, invoice_number, amount, duplicate_suspect, duplicate_ignored
            FROM payments WHERE vendor_matched ILIKE '%%micro merchant%%' OR vendor_matched ILIKE '%%primerx%%'
            ORDER BY 1,2
        """)
        for r in cur.fetchall():
            print(dict(r))
finally:
    conn.close()
