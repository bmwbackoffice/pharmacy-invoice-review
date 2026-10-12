"""
Round 3, read-only. Round 2 found:
  - Rockland: id=2399 (amount 885.17, NOT suspect/ignored/entered) and id=2400 (amount 795.67,
    suspect=True) share duplicate_group_id 05F87873 and both trace back to matching
    invoice_history rows for #34968 -- so a live, unflagged sibling (2399) genuinely exists.
    Need to see why list_duplicate_candidates() isn't pairing them: check the EXACT
    invoice_number/vendor_matched strings currently stored on both live rows (not invoice_history's
    copy, which may have drifted from the live row after an edit).
  - Lantev 9IB1317/9IB1318: ZERO invoice_history rows at all for either number, which means
    check_duplicate() could not have set is_duplicate_suspect via the normal 'possible'/'duplicate'
    path (that requires a history match to even return non-'none'). So the flag likely came from
    a different code path -- check edit_log for exactly when/how it was set on these two records.

Run with the same Python env / DATABASE_URL as the web app:
    python diagnose_round3.py
"""
import database

conn = database.connect()
cur = conn.cursor()

print("=" * 70)
print("1) Rockland id=2399 vs id=2400 -- exact live field values")
print("=" * 70)
cur.execute("""
    SELECT id, vendor_matched, invoice_number, amount, duplicate_suspect,
           duplicate_ignored, entered_in_qb, duplicate_group_id
    FROM invoices WHERE id IN (2399, 2400) ORDER BY id
""")
for row in cur.fetchall():
    (rid, v, n, amt, susp, ign, ent, gid) = row
    print(f"id={rid}: vendor={v!r} number={n!r} amount={amt} "
          f"suspect={susp} ignored={ign} entered={ent} group={gid}")
print()
print("(If the two 'number' strings above are not byte-identical -- different case, "
      "whitespace, or one blank -- that alone explains why list_duplicate_candidates()'s "
      "exact-match grouping can't pair them, even with a live sibling in the same group.)")

print()
print("=" * 70)
print("2) edit_log history for Lantev ids 2473 / 2474 and Rockland 2399 / 2400")
print("=" * 70)
cur.execute("""
    SELECT record_id, field, old_value, new_value, changed_by, changed_at
    FROM edit_log
    WHERE table_name = 'invoices' AND record_id IN (2473, 2474, 2399, 2400)
    ORDER BY record_id, changed_at
""")
rows = cur.fetchall()
if not rows:
    print("No edit_log rows at all for these ids -- flags were never touched by any "
          "logged edit/backfill path; must have been set at original insert time.")
for (rid, field, old, new, who, when) in rows:
    print(f"id={rid}  {when}  {field}: {old!r} -> {new!r}   (by {who})")

print()
print("=" * 70)
print("3) Lantev 2473/2474 -- insert-time context (do they even LOOK like duplicates "
      "of each other, and what does duplicate_reason say)")
print("=" * 70)
cur.execute("""
    SELECT id, vendor_matched, invoice_number, amount, store_matched, invoice_date,
           duplicate_reason, duplicate_group_id, email_filename, date_added
    FROM invoices WHERE id IN (2473, 2474) ORDER BY id
""")
for row in cur.fetchall():
    (rid, v, n, amt, store, idate, reason, gid, fn, added) = row
    print(f"id={rid}: vendor={v!r} number={n!r} amount={amt} store={store!r} date={idate}")
    print(f"    group={gid} email_filename={fn!r} date_added={added}")
    print(f"    duplicate_reason={reason!r}")
    print()

conn.close()
