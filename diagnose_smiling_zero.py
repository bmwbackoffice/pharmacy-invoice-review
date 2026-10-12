"""READ-ONLY. Finds the Smiling Herbs $0.00 record (was referred to as id=454) wherever it lives,
plus any row with id=454 in every table that has an id column. Writes nothing."""
import psycopg2.extras
import database

conn = database.connect()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("""SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema='public'""")
cols = {}
for r in cur.fetchall():
    cols.setdefault(r["table_name"], set()).add(r["column_name"])

def dump(t, r):
    print(f"-- {t}: " + " | ".join(f"{k}={v}" for k, v in r.items()
                                     if v not in (None, "", False) and k not in ("raw_json", "extracted_json")))

print("=== rows with id=454 in any table")
for t, c in sorted(cols.items()):
    if "id" in c:
        cur.execute(f"SELECT * FROM {t} WHERE id=454")
        for r in cur.fetchall():
            dump(t, r)

print("\n=== Smiling Herbs rows with zero/NULL amount")
for t, c in sorted(cols.items()):
    amt = next((a for a in ("amount", "invoice_amount", "total") if a in c), None)
    vcols = [v for v in ("vendor_raw", "vendor_matched", "standard_vendor", "vendor", "qb_vendor", "qb_name") if v in c]
    if not amt or not vcols:
        continue
    where = " OR ".join(f"{v} ILIKE '%%smiling%%'" for v in vcols)
    cur.execute(f"SELECT * FROM {t} WHERE ({where}) AND COALESCE({amt},0)=0")
    for r in cur.fetchall():
        dump(t, r)
conn.close()
