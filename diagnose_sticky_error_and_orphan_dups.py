"""
Read-only diagnostic for two reported issues:

1) EuroPharma / INV0800123 (and any other email) stuck showing "Error / unreadable"
   even though invoice records now exist for it -- tests the theory that emails.status
   is never re-set to 'processed' after a successful reprocess of a previously-failed
   email, and that the ledger's CASE ordering checks status='error' before checking
   whether records exist.

2) Lantev (and other) orphaned "possible duplicate" singles on /duplicates -- tests
   the theory that a same-document sibling IS ignored/entered in the DB but is never
   found by the 9/23 fix because it compares invoice_number as a RAW string while
   check_duplicate() matches on a normalized (.strip().lower()) string.

Nothing is written. Run with the same Python env / DATABASE_URL as the web app:
    python diagnose_sticky_error_and_orphan_dups.py
"""
import database

conn = database.connect()
cur = conn.cursor()

print("=" * 70)
print("1) STICKY 'error' STATUS CHECK")
print("=" * 70)
cur.execute("""
    SELECT e.filename, e.status, e.error, e.run_id,
           (SELECT count(*) FROM invoices i WHERE i.email_filename = e.filename) AS n_inv,
           (SELECT count(*) FROM credit_memos c WHERE c.email_filename = e.filename) AS n_cm,
           (SELECT count(*) FROM payments p WHERE p.email_filename = e.filename) AS n_pay
    FROM emails e
    WHERE e.status = 'error'
    ORDER BY e.filename
""")
rows = cur.fetchall()
stuck = [r for r in rows if (r[4] or 0) + (r[5] or 0) + (r[6] or 0) > 0]
print(f"Emails with status='error': {len(rows)}")
print(f"  ...of those, {len(stuck)} actually HAVE records recorded (stale status -- the bug):\n")
for filename, status, error, run_id, n_inv, n_cm, n_pay in stuck:
    print(f"- {filename}")
    print(f"    records: {n_inv} invoice(s), {n_cm} credit memo(s), {n_pay} payment(s)")
    print(f"    stored error text: {error!r}")
    print(f"    run_id: {run_id}")
    print()
if not stuck:
    print("  None found -- theory not confirmed by current data.\n")

print("=" * 70)
print("2) ORPHANED DUPLICATE SUSPECTS -- case/whitespace-drifted number check")
print("=" * 70)
cur.execute("""
    SELECT id, email_filename, vendor_matched, invoice_number, amount,
           duplicate_suspect, duplicate_ignored, duplicate_group_id, entered_in_qb
    FROM invoices
    WHERE duplicate_suspect IS TRUE AND duplicate_ignored IS NOT TRUE
    ORDER BY lower(vendor_matched), invoice_number
""")
live_suspects = cur.fetchall()
print(f"Live (non-ignored) duplicate_suspect invoices: {len(live_suspects)}\n")

for (sid, sfn, svendor, snum, samt, ssusp, signored, sgid, sentered) in live_suspects:
    # Any OTHER invoice, same vendor (normalized) + same number (case/whitespace-normalized
    # the way check_duplicate() compares), that's either ignored or entered -- but does NOT
    # match by exact raw string (which is what the current display code requires).
    cur.execute("""
        SELECT id, invoice_number, amount, duplicate_ignored, entered_in_qb, duplicate_group_id
        FROM invoices
        WHERE id <> %s
          AND lower(btrim(vendor_matched)) = lower(btrim(%s))
          AND lower(btrim(invoice_number)) = lower(btrim(%s))
          AND (duplicate_ignored IS TRUE OR entered_in_qb IS TRUE)
    """, (sid, svendor, snum))
    normalized_siblings = cur.fetchall()
    exact_raw_siblings = [s for s in normalized_siblings if s[1] == snum]
    drifted_siblings = [s for s in normalized_siblings if s[1] != snum]
    if drifted_siblings:
        print(f"- ORPHAN CONFIRMED: id={sid} {svendor!r} #{snum!r} ${samt} (group={sgid})")
        for (oid, onum, oamt, oign, oent, ogid) in drifted_siblings:
            print(f"    sibling id={oid} #{onum!r} (differs only by case/whitespace) "
                  f"${oamt} ignored={oign} entered={oent} group={ogid}")
        print()
    elif not exact_raw_siblings:
        print(f"- id={sid} {svendor!r} #{snum!r} ${samt}: no ignored/entered sibling found at all "
              f"(normalized or raw) -- may be a genuinely standalone flag, not an orphan pairing issue.")

conn.close()
