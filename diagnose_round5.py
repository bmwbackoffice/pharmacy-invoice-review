"""
Diagnose: Group link ...group=D7F7C83F...&ignored=1&excluded=include shows NO rows.
Read-only. See prior version's docstring for background.

Round 3: live tables + history tables all came back empty for this id -- it exists
NOWHERE in current data. This round checks the edit_log for any trace of it (was a
record carrying this id ever deleted, or was the id itself cleared/overwritten?),
plus a wider sanity check: does the "Group" link even appear anywhere reachable
today (list_duplicate_candidates()' working queue), or was it from an old page
render that's no longer valid.
"""
import database

GID = "D7F7C83F"

conn = database.connect()
cur = conn.cursor()

print("=== edit_log mentioning this id anywhere (old_value or new_value) ===")
cur.execute("""
    SELECT id, table_name, record_id, field, old_value, new_value, changed_by, changed_at
    FROM edit_log
    WHERE old_value ILIKE %s OR new_value ILIKE %s
    ORDER BY changed_at DESC
    LIMIT 20
""", (f"%{GID}%", f"%{GID}%"))
rows = cur.fetchall()
print(f"{len(rows)} row(s)")
for r in rows:
    print("   ", r)

print("\n=== all_records view, by record_id (correct column name) ===")
cur.execute("""
    SELECT record_type, record_id, vendor_matched, number, duplicate_group_id,
           duplicate_suspect, duplicate_ignored, entered_in_qb
    FROM all_records
    WHERE UPPER(duplicate_group_id) = UPPER(%s)
""", (GID,))
rows = cur.fetchall()
print(f"{len(rows)} row(s)")
for r in rows:
    print("   ", r)

print("\n=== Any OTHER duplicate_group_id values currently live, for context "
      "(so we can tell a working Group link apart from a stale one) ===")
cur.execute("""
    SELECT 'invoices' AS t, vendor_matched, invoice_number, duplicate_group_id,
           duplicate_suspect, duplicate_ignored
    FROM invoices WHERE duplicate_group_id IS NOT NULL
    UNION ALL
    SELECT 'payments', vendor_matched, reference_number, duplicate_group_id,
           duplicate_suspect, duplicate_ignored
    FROM payments WHERE duplicate_group_id IS NOT NULL
    ORDER BY 1
    LIMIT 30
""")
rows = cur.fetchall()
print(f"{len(rows)} row(s) with a live duplicate_group_id right now")
for r in rows:
    print("   ", r)

cur.close()
conn.close()
