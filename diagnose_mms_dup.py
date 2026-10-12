"""Read-only: investigate the Micro Merchant Systems duplicate-suspect cluster keyed
on 'ARC_52AFLAM16TUJ' (Maple/NY, $238.42 and $5,483.06) — is this a payment record
(mislabeled 'Invoice #' due to the known bug) or an actual invoice? And does the
corresponding invoice-side set for the same batch/email NOT show as duplicate-flagged,
as Joel expects, or is something else going on?"""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== Exact match on this number/reference, both tables ===")
        for table, numcol, amtcol in [("invoices", "invoice_number", "amount"),
                                       ("payments", "reference_number", "amount")]:
            cur.execute(f"""
                SELECT id, email_filename, vendor_matched, store_matched,
                       {numcol} AS num, {amtcol} AS amt, duplicate_suspect, duplicate_ignored,
                       duplicate_reason, date_added
                FROM {table}
                WHERE {numcol} = 'ARC_52AFLAM16TUJ'
                ORDER BY date_added
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            print(f"\n-- {table} --")
            for r in rows:
                print(dict(zip(cols, r)))

        print("\n=== All emails from the two senders around that window ===")
        cur.execute("""
            SELECT filename, subject, from_address, received_at
            FROM emails WHERE from_address IN ('cspitzer@maplerx.com','admin@maplerx.com')
              AND received_at BETWEEN '2026-08-19' AND '2026-08-21'
            ORDER BY received_at
        """)
        for r in cur.fetchall():
            print(r)

        print("\n=== Every invoice/payment for Micro Merchant Systems, Maple, around that window ===")
        for table, numcol, amtcol, datecol in [("invoices", "invoice_number", "amount", "invoice_date"),
                                                 ("payments", "reference_number", "amount", "payment_date")]:
            cur.execute(f"""
                SELECT id, email_filename, {numcol} AS num, {amtcol} AS amt, {datecol} AS doc_date,
                       duplicate_suspect, duplicate_ignored, date_added
                FROM {table}
                WHERE vendor_matched ILIKE '%%Micro Merchant%%' AND store_matched = 'maple'
                  AND date_added BETWEEN '2026-08-17' AND '2026-08-22'
                ORDER BY date_added
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            print(f"\n-- {table} --")
            for r in rows:
                print(dict(zip(cols, r)))
finally:
    conn.close()
