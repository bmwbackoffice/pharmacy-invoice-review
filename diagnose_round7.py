"""
Diagnose: EuroPharma INV0800123 reprocessed 3x (3 suffixed PDFs in invoice_files/),
but the emails ledger still shows the original 9/22 error row with no records
linked. Checking whether duplicate detection caught the repeats. Read-only.
"""
import database

conn = database.connect()
cur = conn.cursor()

print("=== invoices rows for EuroPharma / INV0800123 ===")
cur.execute("""
    SELECT id, vendor_matched, invoice_number, amount, store_matched, file_path,
           email_filename, duplicate_suspect, duplicate_ignored, duplicate_group_id,
           entered_in_qb, date_added
    FROM invoices
    WHERE vendor_matched ILIKE %s OR invoice_number = %s
    ORDER BY date_added
""", ('%europharma%', 'INV0800123'))
for r in cur.fetchall():
    print("   ", r)

print("\n=== emails rows mentioning europharma / this filename ===")
cur.execute("""
    SELECT filename, gmail_message_id, status, run_id, from_address, received_at
    FROM emails
    WHERE filename ILIKE %s OR from_address ILIKE %s
""", ('%europharma%', '%europharma%'))
for r in cur.fetchall():
    print("   ", r)

print("\n=== review_queue rows mentioning europharma ===")
cur.execute("""
    SELECT id, source_email, vendor_matched, invoice_number, date_flagged, question
    FROM review_queue
    WHERE source_email ILIKE %s OR vendor_matched ILIKE %s
""", ('%europharma%', '%europharma%'))
for r in cur.fetchall():
    print("   ", r)

print("\n=== invoice_history rows for EuroPharma / INV0800123 (dedup ledger) ===")
cur.execute("""
    SELECT id, vendor_matched, invoice_number, amount, date_added
    FROM invoice_history
    WHERE vendor_matched ILIKE %s OR invoice_number = %s
""", ('%europharma%', 'INV0800123'))
for r in cur.fetchall():
    print("   ", r)

cur.close()
conn.close()
