"""Resolve invoices recorded twice under different spellings of the same vendor
(found 2026-10-08: duplicate detection compared raw vendor_matched, not the standard vendor).

Groups = live (non-ignored) invoices with the same standard vendor + invoice # + amount + store.
  A  one copy already ENTERED in QB, another still on /qb  -> keep the entered one, ignore the rest
  B  2+ copies on /qb, none entered                        -> keep the vendor-email copy (not an
     'unknown - ' manual-import PDF), else the lowest id; ignore the rest
Groups with 2+ copies already entered in QB are only REPORTED (needs a human -- QB itself has a double).

Uses web_db.keep_and_ignore_others -- the same action as the /duplicates "keep this, ignore others"
button -- so every change is logged to edit_log and reversible from /qb (Show ignored -> un-ignore).

DRY RUN by default. Pass --apply to write.
"""
import re
import sys
import psycopg2.extras
import database
import web_db

APPLY = "--apply" in sys.argv
USER = "joel (cleanup: same-standard-vendor duplicates 2026-10-08)"

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

conn = database.connect()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT id, email_filename, vendor_matched, standard_vendor, store_matched,
                      invoice_number, amount, entered_in_qb, duplicate_suspect, skip_qb_import
               FROM invoices
               WHERE NOT COALESCE(duplicate_ignored, FALSE)
                 AND invoice_number IS NOT NULL AND invoice_number <> '' AND amount IS NOT NULL""")
groups = {}
for r in cur.fetchall():
    key = (norm(r["standard_vendor"] or r["vendor_matched"]), norm(r["invoice_number"]),
           round(float(r["amount"]), 2), norm(r["store_matched"]))
    groups.setdefault(key, []).append(r)
conn.close()

def on_qb(r):
    return not r["skip_qb_import"] and not r["duplicate_suspect"]

plan, double_in_qb = [], []
for g in groups.values():
    if len(g) < 2:
        continue
    entered = [r for r in g if r["entered_in_qb"]]
    pending = [r for r in g if on_qb(r) and not r["entered_in_qb"]]
    if len(entered) >= 2:
        double_in_qb.append(g); continue
    if entered and pending:
        keep = entered[0]
    elif len(pending) >= 2:
        keep = sorted(pending, key=lambda r: ((r["email_filename"] or "").startswith("unknown - "), r["id"]))[0]
    else:
        continue
    others = [r for r in pending if r["id"] != keep["id"]]
    if others:
        plan.append((keep, others, "A" if entered else "B"))

print(f"{'APPLYING' if APPLY else 'DRY RUN'} -- {len(plan)} group(s), "
      f"{sum(len(o) for _, o, _ in plan)} record(s) to ignore\n")
for keep, others, b in sorted(plan, key=lambda p: (p[2], p[0]["standard_vendor"] or "")):
    print(f"[{b}] {keep['standard_vendor'] or keep['vendor_matched']} #{keep['invoice_number']} "
          f"${keep['amount']} {keep['store_matched']}")
    print(f"     KEEP   id={keep['id']:<5} {'IN_QB' if keep['entered_in_qb'] else 'on/qb'}  {keep['email_filename']}")
    for o in others:
        print(f"     IGNORE id={o['id']:<5} on/qb  {o['email_filename']}")
    if APPLY:
        web_db.keep_and_ignore_others("invoice", keep["id"], [("invoice", o["id"]) for o in others], USER)

if double_in_qb:
    print(f"\n!! {len(double_in_qb)} group(s) already entered in QB MORE THAN ONCE -- not touched, check QB:")
    for g in double_in_qb:
        print(f"   {g[0]['standard_vendor']} #{g[0]['invoice_number']} ${g[0]['amount']}: ids "
              + ", ".join(str(r['id']) for r in g if r['entered_in_qb']))
print("\nDone." if APPLY else "\nNothing written. Re-run with --apply to make these changes.")
