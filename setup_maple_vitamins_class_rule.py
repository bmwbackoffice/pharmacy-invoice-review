"""Joel 2026-10-12: at Maple, anything classified to account Purchase:Vitamins gets class "Vitamins".
Adds the account-based class rule (Setup > Class rules: Store=Maple Pharmacy, Account=Purchase:Vitamins,
Class=Vitamins) and applies it to records NOT yet entered in QB (entered ones are never touched).

DRY RUN by default: shows exactly what would change, inside a transaction that is rolled back.
Pass --apply to save the rule and update the records."""
import sys
import database

STORE, ACCOUNT, CLASS = "Maple Pharmacy", "Purchase:Vitamins", "Vitamins"
APPLY = "--apply" in sys.argv

database.init_db()                      # adds class_rules.account if missing
conn = database.connect()
try:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM stores WHERE store_name = %s", (STORE,))
    if not cur.fetchone():
        sys.exit(f"Store {STORE!r} not found in stores -- stopping.")
    cur.execute("SELECT count(*) FROM invoices WHERE lower(standard_account) = lower(%s)", (ACCOUNT,))
    print(f"Invoices with standard account {ACCOUNT!r} (all stores, all time): {cur.fetchone()[0]}")
    cur.execute("SELECT id FROM class_rules WHERE lower(coalesce(store_matched,'')) = lower(%s) "
                "AND lower(coalesce(account,'')) = lower(%s)", (STORE, ACCOUNT))
    row = cur.fetchone()
    if row:
        print(f"Rule already exists (class_rules id={row[0]}).")
    else:
        cur.execute("INSERT INTO class_rules (store_matched, vendor_matched, account, class, note, created_by) "
                    "VALUES (%s, NULL, %s, %s, %s, %s) RETURNING id",
                    (STORE, ACCOUNT, CLASS, "Maple vitamins -> class Vitamins (Joel 2026-10-12)", "joel"))
        print(f"{'Added' if APPLY else 'Would add'} rule: {STORE} + {ACCOUNT} -> class {CLASS!r}")
    changes = database.apply_account_class_rules("joel", dry_run=not APPLY, cur=cur)
    print(f"\n{'Updated' if APPLY else 'Would update'} {len(changes)} not-yet-entered record(s):")
    from collections import Counter
    for (old, new), n in Counter(((o or '(blank)'), n_) for _, _, _, o, n_ in changes).most_common():
        print(f"   class {old!r} -> {new!r}: {n}")
    for t, rid, store, old, new in changes[:15]:
        print(f"   {t} id={rid} {store}: {old!r} -> {new!r}")
    if len(changes) > 15:
        print(f"   ... and {len(changes) - 15} more")
    cur.execute("SELECT count(*) FROM invoices WHERE entered_in_qb IS TRUE AND lower(store_matched)=lower(%s) "
                "AND lower(standard_account)=lower(%s) AND coalesce(class,'') <> %s", (STORE, ACCOUNT, CLASS))
    print(f"\n(Already entered in QB without class {CLASS!r}, left untouched: {cur.fetchone()[0]})")
    if APPLY:
        conn.commit(); print("\nSaved.")
    else:
        conn.rollback(); print("\nDRY RUN -- nothing saved. Re-run with --apply.")
finally:
    conn.close()
