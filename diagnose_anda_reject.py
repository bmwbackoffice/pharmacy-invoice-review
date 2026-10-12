"""READ-ONLY. Pulls full context for the two 'anda' candidates (641, 724) that the
proposed number-normalization rule would match, but Joel already rejected -- to see
WHY, before trusting the rule on anything."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for cid in (641, 724):
            cur.execute("SELECT * FROM qb_match_candidates WHERE id = %s", (cid,))
            c = cur.fetchone()
            print(f"=== candidate {cid} ===")
            print(c)
            if not c:
                continue
            table = "invoices" if c["record_type"] == "invoice" else "credit_memos"
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (c["record_id"],))
            print(f"--- linked {table} record {c['record_id']} ---")
            print(cur.fetchone())
            print()
finally:
    conn.close()
