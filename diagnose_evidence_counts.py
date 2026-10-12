"""READ-ONLY. Fixes the aliasing bug in the previous script's last query (two unaliased count(*)
columns collided in RealDictCursor). Just the occurrence-count check for all 3 proposals."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for sv, store, reason in [
            ("PAS PACKAGING", "Madison Pharmacy", "number + amount match; vendor did not resolve to the same standard vendor"),
            ("R & T Dist.", "Madison Pharmacy", "number + amount match; vendor did not resolve to the same standard vendor"),
            ("Bordova Innovations", "Lakewood Rx", "vendor + amount match, date within 10 days; number missing/mismatched"),
        ]:
            cur.execute("""
                SELECT count(*) AS n, min(c.created_at) AS first_seen, max(c.created_at) AS last_seen,
                       count(DISTINCT c.batch_id) AS n_batches
                FROM qb_match_candidates c
                LEFT JOIN invoices i ON c.record_type = 'invoice' AND i.id = c.record_id
                LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
                WHERE c.confidence_reason = %s AND c.qb_store = %s
                  AND COALESCE(i.standard_vendor, cm.standard_vendor) = %s AND c.status <> 'rejected'
            """, (reason, store, sv))
            r = cur.fetchone()
            print(f"{sv!r} @ {store}: {r['n']} row(s) across {r['n_batches']} batch(es), "
                  f"first {r['first_seen']}, last {r['last_seen']}")
finally:
    conn.close()
