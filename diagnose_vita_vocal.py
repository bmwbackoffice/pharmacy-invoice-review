"""Read-only: investigate the Vita Vocal Health / H002863 duplicate-set that Joel says
both columns link to the same record. Pull every invoice row matching that vendor+number
to see if there are genuinely two distinct DB rows, and what their real field values are
(especially received_at/sender_email, which render blank on the 'Likely Correct' column)."""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, email_filename, vendor_matched, store_matched, invoice_number,
                   amount, invoice_date, duplicate_suspect, duplicate_ignored,
                   duplicate_reason, date_added, file_path
            FROM invoices
            WHERE invoice_number = 'H002863'
            ORDER BY id
        """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        print(f"Found {len(rows)} invoice row(s) with invoice_number = 'H002863':\n")
        for r in rows:
            print(dict(zip(cols, r)))
            print()

        # Now join against emails to see each row's real received_at/sender (the duplicates
        # page pulls these from the all_records view, via emails join presumably)
        print("--- emails table join (by email_filename) ---")
        for r in rows:
            d = dict(zip(cols, r))
            cur.execute("SELECT filename, from_address, received_at, status FROM emails WHERE filename = %s",
                        (d["email_filename"],))
            e = cur.fetchone()
            print(f"invoice id={d['id']} email_filename={d['email_filename']!r} -> {e}")

        # Check the all_records view directly too, since that's what list_duplicate_candidates() queries
        print("\n--- all_records view, same invoice_number ---")
        cur.execute("""
            SELECT record_id, record_type, email_filename, vendor_matched, number, amount,
                   received_at, sender, duplicate_suspect, duplicate_ignored
            FROM all_records
            WHERE record_type = 'invoice' AND number = 'H002863'
            ORDER BY record_id
        """)
        rows2 = cur.fetchall()
        cols2 = [d[0] for d in cur.description]
        for r in rows2:
            print(dict(zip(cols2, r)))
finally:
    conn.close()
