"""
Round 2: invoices table has ZERO rows for EuroPharma/INV0800123, review_queue has
ZERO, yet invoice_files/ has 3 saved PDFs for this exact invoice (which only ever
get saved for an actual recorded invoice/payment). Searching by file_path instead
of vendor/number, across invoices/credit_memos/payments AND their history tables,
in case the vendor name or invoice number was captured differently each time.
Read-only.
"""
import database

conn = database.connect()
cur = conn.cursor()

for table in ("invoices", "credit_memos", "payments"):
    print(f"=== {table}: file_path ILIKE '%EuroPharma%' OR '%0800123%' ===")
    cur.execute(f"""
        SELECT id, vendor_matched, store_matched, file_path, email_filename, date_added
        FROM {table}
        WHERE file_path ILIKE %s OR file_path ILIKE %s OR email_filename ILIKE %s
    """, ('%EuroPharma%', '%0800123%', '%europharma%'))
    rows = cur.fetchall()
    print(f"{len(rows)} row(s)")
    for r in rows:
        print("   ", r)

for table, num_col, amt_col in [("invoice_history", "invoice_number", "invoice_amount"),
                                  ("payment_history", "reference_number", "amount")]:
    print(f"\n=== {table}: vendor/number/source_file ILIKE europharma ===")
    cur.execute(f"""
        SELECT id, vendor_matched, {num_col}, {amt_col}, source_file, date_added
        FROM {table}
        WHERE vendor_matched ILIKE %s OR {num_col} = %s OR source_file ILIKE %s
    """, ('%europharma%', 'INV0800123', '%europharma%'))
    rows = cur.fetchall()
    print(f"{len(rows)} row(s)")
    for r in rows:
        print("   ", r)

print("\n=== edit_log: anything mentioning europharma or 0800123 ===")
cur.execute("""
    SELECT id, table_name, record_id, field, old_value, new_value, changed_by, changed_at
    FROM edit_log
    WHERE old_value ILIKE %s OR new_value ILIKE %s OR old_value ILIKE %s OR new_value ILIKE %s
    ORDER BY changed_at DESC LIMIT 20
""", ('%europharma%', '%europharma%', '%0800123%', '%0800123%'))
rows = cur.fetchall()
print(f"{len(rows)} row(s)")
for r in rows:
    print("   ", r)

cur.close()
conn.close()
