"""READ-ONLY. Refines diagnose_live_dups_by_standard_vendor.py to what actually matters for QB:
a row is ON /qb when skip_qb_import, duplicate_suspect and duplicate_ignored are all not TRUE
(same filter as web_db.list_qb_ready). Groups = same standard vendor (fallback vendor_matched)
+ invoice # + amount, among non-ignored invoices. Buckets:
  A  one copy ALREADY entered in QB and another still ON /qb, not entered -> double-entry risk
  B  2+ copies ON /qb, none entered yet -> would be entered twice
  C  extra copies are hidden (possible-duplicate on /duplicates, or skip_qb) -> no QB risk now
Writes nothing."""
import re
import psycopg2.extras
import database

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

conn = database.connect()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT id, email_filename, vendor_matched, standard_vendor, store_matched,
                      invoice_number, invoice_date, amount, entered_in_qb, duplicate_group_id,
                      duplicate_suspect, skip_qb_import, needs_review
               FROM invoices
               WHERE NOT COALESCE(duplicate_ignored, FALSE)
                 AND invoice_number IS NOT NULL AND invoice_number <> ''""")
groups = {}
for r in cur.fetchall():
    if r["amount"] is None:
        continue                                   # no amount -> can't be QB-entered anyway
    key = (norm(r["standard_vendor"] or r["vendor_matched"]), norm(r["invoice_number"]),
           round(float(r["amount"]), 2))
    groups.setdefault(key, []).append(r)

def on_qb(r):
    return not r["skip_qb_import"] and not r["duplicate_suspect"]

buckets = {"A": [], "B": [], "C": []}
for g in groups.values():
    if len(g) < 2:
        continue
    entered = [r for r in g if r["entered_in_qb"]]
    pending = [r for r in g if on_qb(r) and not r["entered_in_qb"]]
    if entered and pending:
        buckets["A"].append(g)
    elif len(pending) >= 2:
        buckets["B"].append(g)
    else:
        buckets["C"].append(g)

labels = {"A": "ALREADY IN QB + another copy still on /qb (double-entry risk)",
          "B": "2+ copies on /qb, none entered yet",
          "C": "extra copies already hidden from /qb (no QB risk)"}
for b in "ABC":
    print(f"\n######## {b}: {labels[b]} -- {len(buckets[b])} group(s)")
    if b == "C":
        continue
    for g in sorted(buckets[b], key=lambda g: (g[0]["standard_vendor"] or "", g[0]["invoice_number"])):
        print(f"== {g[0]['standard_vendor'] or g[0]['vendor_matched']} #{g[0]['invoice_number']} ${g[0]['amount']}"
              f"  store={g[0]['store_matched']}")
        for r in sorted(g, key=lambda r: r["id"]):
            st = "IN_QB" if r["entered_in_qb"] else ("on/qb" if on_qb(r) else
                 ("suspect" if r["duplicate_suspect"] else "skipqb"))
            print(f"   id={r['id']:<5} {st:<7} store={r['store_matched']!s:<16} vendor={r['vendor_matched']!r}"
                  f"  src={r['email_filename']}")
conn.close()
