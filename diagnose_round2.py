"""
Round 2, read-only. Round 1 ruled out both original theories:
  - the 1 errored email has 0 records either way (not a stale-status/priority-ordering bug)
  - Lantev/Rockland's duplicate_suspect flags have NO sibling in the live `invoices` table,
    normalized or raw -- so whatever check_duplicate() matched against isn't a live invoice row.

This checks the two real remaining leads:
  1) Full detail (error text, run_id, email_type) for the one email currently status='error'.
  2) invoice_history (the separate dedup ledger check_duplicate() actually compares against,
     per classify_emails.load_history/save_history) for the specific orphaned records reported:
     Lantev #9IB1317 / #9IB1318, and Rockland Cosmetics #34968 -- to see what they matched,
     and whether that match has a corresponding live invoices row at all.

Run with the same Python env / DATABASE_URL as the web app:
    python diagnose_round2.py
"""
import database

conn = database.connect()
cur = conn.cursor()

print("=" * 70)
print("1) THE ONE status='error' EMAIL -- full detail")
print("=" * 70)
cur.execute("SELECT filename, status, error, email_type, run_id, gmail_message_id, "
            "source, date_added, updated_at FROM emails WHERE status = 'error'")
for row in cur.fetchall():
    (fn, status, error, etype, run_id, gmid, source, added, updated) = row
    print(f"filename:   {fn}")
    print(f"status:     {status}")
    print(f"error:      {error!r}")
    print(f"email_type: {etype}")
    print(f"run_id:     {run_id}")
    print(f"source:     {source}")
    print(f"date_added: {added}   updated_at: {updated}")
    print()

print("=" * 70)
print("2) invoice_history entries for the orphaned Lantev / Rockland records")
print("=" * 70)
targets = [
    ("Lantev Distributing Company", "9IB1317"),
    ("Lantev Distributing Company", "9IB1318"),
    ("Rockland Cosmetics", "34968"),
]
for vendor, num in targets:
    print(f"--- {vendor!r} #{num!r} ---")
    cur.execute("""
        SELECT vendor_matched, invoice_number, invoice_amount, store_matched,
               source_file, source_email_id, duplicate_group_id
        FROM invoice_history
        WHERE lower(btrim(vendor_matched)) = lower(btrim(%s))
          AND lower(btrim(invoice_number)) = lower(btrim(%s))
    """, (vendor, num))
    hist_rows = cur.fetchall()
    if not hist_rows:
        print("  No invoice_history rows at all for this vendor+number.")
    for (hv, hn, hamt, hstore, hsrc_file, hsrc_email, hgid) in hist_rows:
        print(f"  history: amount={hamt} store={hstore} source_file={hsrc_file!r} "
              f"source_email_id={hsrc_email!r} group={hgid}")
        # Does a live invoices row still exist for that exact source file?
        cur.execute("""
            SELECT id, amount, duplicate_suspect, duplicate_ignored, entered_in_qb, email_filename
            FROM invoices WHERE email_filename = %s
        """, (hsrc_file,))
        live = cur.fetchall()
        if live:
            for l in live:
                print(f"      -> live invoices row: id={l[0]} amount={l[1]} "
                      f"suspect={l[2]} ignored={l[3]} entered={l[4]} email_filename={l[5]!r}")
        else:
            print(f"      -> NO live invoices row has email_filename={hsrc_file!r} "
                  f"(the history entry is orphaned -- its original invoice row is gone/renamed)")
    print()

conn.close()
