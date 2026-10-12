"""READ-ONLY. Why does the export still show 'Vitality Plus' for Maple?
    python diagnose_vitality.py
"""
import psycopg2.extras
import database

PAT = "%vitalit%"


def show(cur, title, sql, params):
    cur.execute(sql, params)
    rows = cur.fetchall()
    print(f"\n=== {title}: {len(rows)} row(s) ===")
    for r in rows:
        print("  " + " | ".join(f"{k}={v!r}" for k, v in r.items()))
    return rows


conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        maps = show(cur, "vendor_name_map rows mentioning Vitality",
                    "SELECT id, standard_vendor_name, aliases, name_maple, name_madison, name_refuah, "
                    "name_westgate, name_3m, exclude_from_qb, edited_by, edited_at FROM vendor_name_map "
                    "WHERE row_to_json(vendor_name_map)::text ILIKE %s", (PAT,))
        ids = [m["id"] for m in maps]
        show(cur, "edit_log for those map rows (or mentioning Vitality)",
             "SELECT changed_at, table_name, record_id, field, old_value, new_value, changed_by "
             "FROM edit_log WHERE (table_name='vendor_name_map' AND record_id = ANY(%s)) "
             "   OR old_value ILIKE %s OR new_value ILIKE %s ORDER BY changed_at",
             (ids, PAT, PAT))
        for table, num in (("invoices", "invoice_number"), ("credit_memos", "credit_memo_number")):
            show(cur, f"{table} with Vitality anywhere in vendor fields",
                 f"SELECT id, store_matched, entered_in_qb, skip_qb_import, vendor_raw, vendor_matched, "
                 f"standard_vendor, qb_vendor, {num} AS num, amount, date_added FROM {table} "
                 f"WHERE vendor_raw ILIKE %s OR vendor_matched ILIKE %s OR standard_vendor ILIKE %s "
                 f"   OR qb_vendor ILIKE %s ORDER BY id", (PAT,) * 4)
        show(cur, "payments with Vitality",
             "SELECT id, store_matched, vendor_raw, vendor_matched, invoice_number, amount, date_added "
             "FROM payments WHERE vendor_raw ILIKE %s OR vendor_matched ILIKE %s ORDER BY id", (PAT, PAT))
        show(cur, "QB reconciliation rows with Vitality (qb_match_candidates)",
             "SELECT batch_id, record_type, record_id, qb_store, qb_name, qb_num, qb_amount, "
             "confidence_reason, status FROM qb_match_candidates WHERE qb_name ILIKE %s "
             "ORDER BY batch_id, record_id", (PAT,))
finally:
    conn.close()
