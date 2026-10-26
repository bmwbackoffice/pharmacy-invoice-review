"""READ-ONLY. Why Lantev $718.08 Refuah (#9304241 / #93G4241) isn't on "Is this in QuickBooks twice?".
Shows: the bills in the saved QuickBooks register for that amount, the export coverage for Refuah, the
app's three records with how each got its "in QB" mark, and what the doubles check makes of them."""
import psycopg2.extras
import database
import web_db

conn = database.connect()
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
print("== QuickBooks register: bills of $718.08 ==")
cur.execute("SELECT id, batch_id, qb_store, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, qb_amount "
            "FROM qb_register WHERE abs(qb_amount - 718.08) < 0.005 ORDER BY qb_date, id")
for r in cur.fetchall():
    print("  ", dict(r))
print("== Export coverage (Refuah) ==")
cur.execute("SELECT batch_id, qb_store, date_from, date_to, uploaded_at FROM qb_register_coverage "
            "WHERE qb_store ILIKE '%refu%' ORDER BY uploaded_at DESC LIMIT 6")
for r in cur.fetchall():
    print("  ", dict(r))
print("== App records ==")
cur.execute("SELECT id, invoice_number, invoice_date, amount, store_matched, entered_in_qb, entered_qb_by, "
            "entered_qb_at, duplicate_ignored, duplicate_suspect, duplicate_group_id, qb_twice "
            "FROM invoices WHERE abs(amount - 718.08) < 0.005 AND vendor_matched ILIKE '%lantev%' ORDER BY id")
recs = cur.fetchall()
for r in recs:
    print("  ", dict(r))
    cur.execute("SELECT field, old_value, new_value, changed_by, changed_at FROM edit_log "
                "WHERE table_name='invoices' AND record_id=%s AND field IN ('entered_in_qb','duplicate_ignored') "
                "ORDER BY id", (r["id"],))
    for e in cur.fetchall():
        print("       log:", dict(e))
print("== Doubles check (all, incl. dismissed) for $718.08 ==")
for p in web_db.qb_double_entries("all"):
    if abs(float(p["a"]["qb_amount"]) - 718.08) < 0.005:
        print("  ", p["why"], "| dismissed:", bool(p["dismissed"]), "|", p["a"]["qb_num"], p["b"]["qb_num"])
conn.close()
