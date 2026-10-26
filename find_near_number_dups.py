"""READ-ONLY. Lists live invoice pairs that are probably the same bill but whose invoice # was read
one character differently (same store, same amount, related vendor, invoice dates within 7 days) --
do-not-enter (excluded) vendors like McKesson are skipped entirely --
the kind the duplicate check missed before 2026-10-19 (Zahler INV/2026/511123 vs 51123, Maple).
Pairs where BOTH are entered in QuickBooks are listed first: those need a fix in QB.

    python find_near_number_dups.py          # report only, changes nothing
    python find_near_number_dups.py --flag   # also mark each pair as a possible duplicate (same group)
                                             # so it shows on the Duplicates page for a person to pick one.
                                             # Never ignores anything; logged to edit_log; safe to re-run.
"""
import secrets
import sys
import psycopg2.extras
import database
import classify_emails as ce

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, vendor_matched, store_matched, invoice_number, invoice_date, amount, "
                    "entered_in_qb, email_filename, duplicate_group_id, duplicate_suspect FROM invoices "
                    "WHERE duplicate_ignored IS NOT TRUE AND ignored_at IS NULL "
                    "  AND skip_qb_import IS NOT TRUE "
                    "  AND amount IS NOT NULL AND invoice_number IS NOT NULL")
        rows = cur.fetchall()
finally:
    conn.close()

FLAG = "--flag" in sys.argv[1:]
ce.VENDOR_STD = ce.load_vendor_standardization()
excluded = ce._excluded_vendor_keys()        # do-not-enter vendors (Setup > Vendor names, Exclude from QB)
rows = [r for r in rows if ce._norm_vendor_std_key(r["vendor_matched"]) not in excluded]
groups = {}
for r in rows:
    groups.setdefault((ce._norm(r["store_matched"]), round(float(r["amount"]), 2)), []).append(r)

pairs = []
for grp in groups.values():
    for i in range(len(grp)):
        for j in range(i + 1, len(grp)):
            a, b = grp[i], grp[j]
            na, nb = ce._num_key(a["invoice_number"]), ce._num_key(b["invoice_number"])
            if len(na) < 6 or len(nb) < 6 or not ce._one_edit_apart(na, nb):
                continue
            da, db = ce._loose_date(a["invoice_date"]), ce._loose_date(b["invoice_date"])
            if da and db and abs((da - db).days) > 7:
                continue
            if not ce._vendors_related(a["vendor_matched"], b["vendor_matched"]):
                continue
            pairs.append((a, b))

pairs.sort(key=lambda p: -(bool(p[0]["entered_in_qb"]) + bool(p[1]["entered_in_qb"])))
print(f"=== {len(pairs)} likely same-bill pair(s) with invoice #s one character apart ===\n")
for a, b in pairs:
    both = a["entered_in_qb"] and b["entered_in_qb"]
    print(("!! BOTH IN QUICKBOOKS" if both else "   ") + f"  {a['store_matched']}  ${float(a['amount']):,.2f}")
    for r in (a, b):
        print(f"     id={r['id']:<6} {r['vendor_matched']!r:<40} #{r['invoice_number']!r:<22} "
              f"date={r['invoice_date']}  in QB={bool(r['entered_in_qb'])}  ({r['email_filename']})")
    print()

if FLAG and pairs:
    conn = database.connect()
    try:
        with conn.cursor() as cur:
            n = 0
            for a, b in pairs:
                gid = a["duplicate_group_id"] or b["duplicate_group_id"] or secrets.token_hex(4).upper()
                for me, other in ((a, b), (b, a)):
                    if me["duplicate_suspect"] and me["duplicate_group_id"] == gid:
                        continue                      # already flagged in this group (re-run)
                    qb = (" BOTH copies are already entered in QuickBooks -- delete one bill in QuickBooks, "
                          "then ignore that copy here." if a["entered_in_qb"] and b["entered_in_qb"] else "")
                    reason = (f"Possible duplicate: #{me['invoice_number']} ({me['vendor_matched']}) looks like "
                              f"#{other['invoice_number']} ({other['vendor_matched']}, id {other['id']}) -- same store, "
                              f"same amount ${float(me['amount']):,.2f}, invoice numbers one character apart "
                              f"(likely a misread). Keep one.{qb}")
                    cur.execute("UPDATE invoices SET duplicate_suspect = TRUE, duplicate_reason = %s, "
                                "duplicate_group_id = %s WHERE id = %s", (reason, gid, me["id"]))
                    cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                                "VALUES ('invoices', %s, 'duplicate_suspect', %s, 'True', 'find_near_number_dups --flag')",
                                (me["id"], str(me["duplicate_suspect"])))
                    me["duplicate_suspect"], me["duplicate_group_id"] = True, gid
                    n += 1
        conn.commit()
        print(f"Flagged {n} record(s) as possible duplicates -- see the Duplicates page.")
    finally:
        conn.close()
elif pairs:
    print("Report only. Re-run with --flag to mark these as possible duplicates.")
