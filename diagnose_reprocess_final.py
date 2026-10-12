"""Pull the ORIGINAL manual-import invoice for each of the 4 undetected
duplicates, side by side with the re-scanned copy, to confirm they're the
same invoice. Also check the emails-table status/needs_review for the 3
zero-row cases to see what happened to them."""
import database

undetected = {
    "1a010edb7dbdbd16": ("Maple - Rockland Cosmetics - 34598 - $554.00 - 8.5.2026.pdf", 2990),  # placeholder, fixed below
}

# (original_filename, rescanned_invoice_id)
pairs = [
    ("3M - Emerson Ecologics - E294025997 - $456.79 - 8.12.2026.pdf", 2990),
    ("Maple - M.R. Health Products - 7224.pdf", 2991),
    ("Maple - Hold End Dist - HE-013809 - $5,542.67 - 9.2.2026.pdf", 2996),
    ("Maple - Cong Chasudim Lovim - 888522 - $792.00 - 9.7.2026.pdf", 2998),
]

zero_row_filenames = [
    "Maple - Rockland Cosmetics - 34598 - $554.00 - 8.5.2026.pdf",       # msg 19ff146f93e2fcc2
    "Maple - 325709 - $5,715.35 - 3.17.2026.pdf",                        # msg 19ff62aa77655265
    "Maple - Zahler - INV202647908 - $403.44 - 8.27.2026.pdf",           # msg 1a06272d4728714f
]
zero_row_scanner = ["19ff146f93e2fcc2", "19ff62aa77655265", "1a06272d4728714f"]

conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== Original vs re-scanned copy ===")
        for orig_fn, dup_id in pairs:
            cur.execute("""
                SELECT id, vendor_matched, invoice_number, amount, invoice_date, entered_in_qb
                FROM invoices WHERE email_filename = %s
            """, (orig_fn,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            print(f"\nORIGINAL ({orig_fn}):")
            print("  ", dict(zip(cols, row)) if row else "NOT FOUND")
            print(f"RE-SCAN (id={dup_id}) already shown above")

        print("\n\n=== The 3 zero-row cases: what status did their re-scan land at? ===")
        for fn, msg_id in zip(zero_row_filenames, zero_row_scanner):
            scanner_fn = f"Maple Scanner - {msg_id}.eml"
            cur.execute("""
                SELECT filename, status, email_type, run_id, date_added
                FROM emails WHERE filename = %s
            """, (scanner_fn,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            print(f"\n{scanner_fn}:")
            print("  ", dict(zip(cols, row)) if row else "NOT FOUND")
            # also check credit_memos/payments in case it landed there instead
            for table in ("credit_memos", "payments"):
                cur.execute(f"SELECT id FROM {table} WHERE email_filename = %s", (scanner_fn,))
                r2 = cur.fetchall()
                if r2:
                    print(f"   -> found in {table}: ids {[x[0] for x in r2]}")
finally:
    conn.close()
