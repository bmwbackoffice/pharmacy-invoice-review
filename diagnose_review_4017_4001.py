"""Shows review items 4017 and 4001 (plus the source email's current records), and the 8 That Sushi
Spot bills that the misread-duplicate check wrongly flagged as 4 duplicate pairs.
  python diagnose_review_4017_4001.py         -> read-only report
  python diagnose_review_4017_4001.py --fix   -> also clears the false duplicate flag on the sushi bills
                                                 (they are 8 separate receipts, one per order)"""
import sys
import psycopg2.extras
import database

IDS = (4017, 4001)
FIX = "--fix" in sys.argv

conn = database.connect()
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

for rid in IDS:
    print("=" * 70)
    cur.execute("SELECT * FROM review_queue WHERE id=%s", (rid,))
    r = cur.fetchone()
    if not r:
        print(f"review #{rid}: NOT in review_queue (already answered/dismissed?)")
        continue
    for k in ("id", "filename", "source_email", "flagged_at", "run_id", "email_type", "vendor_matched",
              "store_matched", "invoice_number", "amount", "answer", "duplicate_group_id"):
        print(f"  {k:18}: {r.get(k)}")
    print("  question:\n   ", (r.get("question") or "").replace("\n", "\n    ")[:1500])
    src = r.get("source_email")
    if src:
        cur.execute("SELECT filename, status, email_type, subject FROM emails WHERE filename=%s", (src,))
        print("  email row:", cur.fetchone())
        for t in ("invoices", "payments", "credit_memos"):
            cur.execute(f"SELECT * FROM {t} WHERE email_filename=%s ORDER BY id", (src,))
            for x in cur.fetchall():
                x = {k: x.get(k) for k in ("id", "vendor_matched", "store_matched", "invoice_number",
                                           "reference_number", "amount", "needs_review") if k in x}
                print(f"   {t}: {dict(x)}")
        cur.execute("SELECT id, vendor_matched, invoice_number, amount FROM review_queue "
                    "WHERE source_email=%s AND id<>%s", (src, rid))
        for x in cur.fetchall():
            print("   other open review on same email:", dict(x))

print("=" * 70)
print("That Sushi Spot bills:")
cur.execute("SELECT id, store_matched, invoice_number, amount, invoice_date, duplicate_suspect, "
            "duplicate_group_id, entered_in_qb, email_filename FROM invoices "
            "WHERE vendor_matched ILIKE '%%sushi%%' ORDER BY store_matched, invoice_number")
rows = cur.fetchall()
for x in rows:
    print("  ", dict(x))
if FIX:
    ids = [x["id"] for x in rows if x["duplicate_suspect"]
           and (x["email_filename"] or "").startswith("Sushi charges")]
    for i in ids:
        cur.execute("UPDATE invoices SET duplicate_suspect=FALSE, duplicate_reason=NULL, "
                    "duplicate_group_id=NULL, edited_at=now() WHERE id=%s", (i,))
        cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('invoices',%s,'duplicate_suspect','true','false','joel (sushi fix)')", (i,))
    print(f"FIX: cleared the duplicate flag on {len(ids)} sushi bill(s).")
conn.close()
