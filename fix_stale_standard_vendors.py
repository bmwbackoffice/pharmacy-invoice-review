"""Records whose saved standard vendor name is no longer in Setup > Vendor names (e.g. 'Parmed', merged into
'Parmed Pharmaceuticals') get the name their spelling resolves to now. Only the standard_vendor column
changes (logged); QuickBooks names, amounts and entered marks are untouched.
  python fix_stale_standard_vendors.py           -> list
  python fix_stale_standard_vendors.py --commit  -> fix"""
import sys
import psycopg2.extras
import classify_emails as ce
import database

commit = "--commit" in sys.argv
ce.VENDOR_STD = ce.load_vendor_standardization()
lk = ce.VENDOR_STD.get("lookup") or {}
conn = database.connect()
conn.autocommit = False
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT standard_vendor_name FROM vendor_name_map")
names = {r["standard_vendor_name"] for r in cur.fetchall()}
n = 0
for table in ("invoices", "credit_memos"):
    cur.execute(f"SELECT id, vendor_matched, standard_vendor FROM {table} "
                f"WHERE standard_vendor IS NOT NULL AND standard_vendor <> ''")
    for r in cur.fetchall():
        if r["standard_vendor"] in names:
            continue
        k = ce._norm_vendor_std_key(r["vendor_matched"] or "")
        new = lk.get(k) or (ce._best_fuzzy_vendor_match(k, lk)[0] if k else None)
        print(f"  {table} {r['id']}: '{r['vendor_matched']}'  standard '{r['standard_vendor']}' -> '{new or '(no match - left)'}'")
        if new and commit:
            cur.execute(f"UPDATE {table} SET standard_vendor = %s WHERE id = %s", (new, r["id"]))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES (%s,%s,'standard_vendor',%s,%s,'joel (fix_stale_standard_vendors.py)')",
                        (table, r["id"], r["standard_vendor"], new))
            n += 1
conn.commit() if commit else conn.rollback()
conn.close()
print(f"\n{'Fixed' if commit else 'Would fix'} {n if commit else 'the lines above'}." + ("" if commit else " Run with --commit."))
