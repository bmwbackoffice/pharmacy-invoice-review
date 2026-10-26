"""READ-ONLY: state of the 14 records flagged by find_near_number_dups --flag."""
import psycopg2.extras, database
IDS = [257, 162, 243, 110, 1504, 3864, 1278, 1539, 231, 969, 2862, 2643, 2863, 2642]
conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, duplicate_suspect, duplicate_ignored, duplicate_group_id, "
                    "left(duplicate_reason, 90) AS reason FROM invoices WHERE id = ANY(%s)", (IDS,))
        rows = {r["id"]: r for r in cur.fetchall()}
        for i in IDS:
            r = rows.get(i)
            print(i, r and (r["duplicate_suspect"], r["duplicate_group_id"], r["reason"]))
finally:
    conn.close()
