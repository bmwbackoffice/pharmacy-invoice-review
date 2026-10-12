"""READ-ONLY. Duplicate detection compares vendor_matched (e.g. 'Costco (Same-Day/Instacart)' vs
'Costco'), so a pair that only agrees on standard_vendor slips through and BOTH stay live on /qb.
Lists every group of LIVE invoices (not duplicate_ignored) sharing standard_vendor + invoice # +
amount, with how many are already entered in QB. Writes nothing."""
import re
import psycopg2.extras
import database

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

conn = database.connect()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT id, email_filename, vendor_matched, standard_vendor, store_matched,
                      invoice_number, invoice_date, amount, entered_in_qb, duplicate_group_id
               FROM invoices
               WHERE NOT COALESCE(duplicate_ignored, FALSE)
                 AND COALESCE(route_to,'') <> 'none'
                 AND invoice_number IS NOT NULL AND invoice_number <> ''""")
groups = {}
for r in cur.fetchall():
    key = (norm(r["standard_vendor"] or r["vendor_matched"]), norm(r["invoice_number"]),
           round(float(r["amount"] or 0), 2))
    groups.setdefault(key, []).append(r)
hits = [g for g in groups.values() if len(g) > 1]
missed = [g for g in hits if len({norm(r["vendor_matched"]) for r in g}) > 1]
print(f"Live groups with same standard vendor + # + amount: {len(hits)}")
print(f"  ...of which vendor_matched DIFFERS (the gap this checks): {len(missed)}\n")
for g in sorted(hits, key=lambda g: g[0]["standard_vendor"] or ""):
    tag = "VENDOR-NAME GAP" if g in missed else "same vendor_matched"
    print(f"== {g[0]['standard_vendor']} #{g[0]['invoice_number']} ${g[0]['amount']}  [{tag}]")
    for r in g:
        print(f"   id={r['id']:<5} in_qb={bool(r['entered_in_qb'])!s:<5} grp={r['duplicate_group_id'] or '-':<9}"
              f" date={r['invoice_date']}  vendor_matched={r['vendor_matched']!r}  src={r['email_filename']}")
conn.close()
