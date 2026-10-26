"""READ-ONLY. What the saved QuickBooks exports hold for one amount, and what the "twice" page makes of it.
  python diagnose_qb_bill.py 206.45"""
import sys
import psycopg2.extras
import database
import web_db

amt = float(sys.argv[1]) if len(sys.argv) > 1 else 206.45
conn = database.connect()
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
print(f"== QuickBooks register rows within $1 of {amt} ==")
cur.execute("SELECT batch_id, qb_store, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, qb_amount FROM qb_register "
            "WHERE abs(qb_amount - %s) <= 1 ORDER BY qb_store, qb_date", (amt,))
for r in cur.fetchall():
    print("  ", dict(r))
print("== Latest export coverage per store ==")
cur.execute("SELECT DISTINCT ON (qb_store) qb_store, date_from, date_to, uploaded_at FROM qb_register_coverage "
            "ORDER BY qb_store, uploaded_at DESC")
for r in cur.fetchall():
    print("  ", dict(r))
print("== App invoices of that amount marked entered ==")
cur.execute("SELECT id, store_matched, vendor_matched, invoice_number, invoice_date, entered_qb_by, entered_qb_at, "
            "duplicate_group_id, duplicate_ignored FROM invoices WHERE abs(amount - %s) < 0.005 AND entered_in_qb IS TRUE", (amt,))
for r in cur.fetchall():
    print("  ", dict(r))
print("== On the twice page (all) ==")
for p in web_db.qb_double_entries("all"):
    try:
        if abs(float(p["a"]["qb_amount"]) - amt) <= 1:
            print("  ", p["kind"], "|", p["why"], "|", p["a"].get("qb_num"), p["b"].get("qb_num"), "| dismissed:", bool(p["dismissed"]))
    except (TypeError, ValueError):
        pass
conn.close()
