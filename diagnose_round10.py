"""
Characterize the 117 rows on the new /reconcile page (invoice_history rows with
no matching live invoices row) before deciding how to recover them in bulk.
Checking: how many distinct source emails (one root cause repeated vs many
separate incidents), email_status breakdown, and whether any of these also
show up in statement_lines under the same source_file (which would mean they
came from statement-recon metadata rather than a genuinely lost separate
invoice -- a different, probably-not-a-bug explanation). Read-only.
"""
import database

conn = database.connect()
cur = conn.cursor()

print("=== Total orphaned invoice_history rows + distinct source emails ===")
cur.execute("""
    SELECT count(*) AS n_rows, count(DISTINCT h.source_file) AS n_source_emails
    FROM invoice_history h
    WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS (
        SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number)
          AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))
""")
print("   ", cur.fetchone())

print("\n=== Breakdown by source email's status (emails.status) ===")
cur.execute("""
    SELECT COALESCE(e.status, '(no emails row)') AS status, count(*) AS n
    FROM invoice_history h LEFT JOIN emails e ON e.filename = h.source_file
    WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS (
        SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number)
          AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))
    GROUP BY 1 ORDER BY n DESC
""")
for r in cur.fetchall():
    print("   ", r)

print("\n=== Source emails with the MOST orphaned invoice_history rows (top 15) ===")
cur.execute("""
    SELECT h.source_file, count(*) AS n_orphaned_rows,
           array_agg(DISTINCT h.vendor_matched) AS vendors
    FROM invoice_history h
    WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS (
        SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number)
          AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))
    GROUP BY h.source_file ORDER BY n_orphaned_rows DESC LIMIT 15
""")
for r in cur.fetchall():
    print("   ", r)

print("\n=== Of those orphaned rows, how many ALSO appear in statement_lines "
      "under the same source_file + invoice_number (statement-recon metadata, "
      "not a separately-processed invoice email)? ===")
cur.execute("""
    SELECT count(*) FROM invoice_history h
    WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS (
        SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number)
          AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))
      AND EXISTS (
        SELECT 1 FROM statement_lines sl
        WHERE sl.statement_file = h.source_file
          AND lower(sl.invoice_number) = lower(h.invoice_number))
""")
print("   ", cur.fetchone())

print("\n=== Dollar total of all 117 orphaned invoice amounts (rough exposure) ===")
cur.execute("""
    SELECT sum(h.invoice_amount) FROM invoice_history h
    WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS (
        SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number)
          AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))
""")
print("   ", cur.fetchone())

cur.close()
conn.close()
