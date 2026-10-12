"""READ-ONLY follow-up. Checks the one Bucket-A proposal that looked suspicious:
'R & T Dist.' @ Madison Pharmacy: name_madison 'R & T Dist.' -> 'R & T DIst.'
(only a capitalization difference in "Dist." -- looks like it could be a one-off QB data-entry
typo rather than a real correction). This pulls:
  1. The exact qb_match_candidates row(s) behind it -- how many, which batches, when.
  2. Every vendor_name_map row whose name could plausibly collide with "R & T" / "R & T Dist."
     (the same kind of multi-row-vendor mess found earlier this session for PAS Packaging and
     Abbott), to see if that's WHY this one row's name failed to resolve while all the other
     stores' "R & T Dist." rows (already-correct, per the last run) resolved fine.
"""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== qb_match_candidates rows behind the R & T Dist. / Madison proposal ===")
        cur.execute("""
            SELECT c.*, i.standard_vendor, i.vendor_matched, i.store_matched
            FROM qb_match_candidates c
            LEFT JOIN invoices i ON c.record_type = 'invoice' AND i.id = c.record_id
            WHERE c.qb_store = 'Madison Pharmacy' AND c.qb_name ILIKE 'R & T%'
            ORDER BY c.created_at
        """)
        for r in cur.fetchall():
            print(r)

        print("\n=== every vendor_name_map row whose name/alias could collide with 'R & T' ===")
        cur.execute("""
            SELECT id, standard_vendor_name, aliases, name_3m, name_madison, name_maple,
                   name_refuah, name_westgate, name_bmw, name_lakewood_rx
            FROM vendor_name_map
            WHERE standard_vendor_name ILIKE '%%R & T%%' OR standard_vendor_name ILIKE '%%R&T%%'
               OR aliases ILIKE '%%R & T%%' OR aliases ILIKE '%%R&T%%'
               OR name_3m ILIKE '%%R & T%%' OR name_madison ILIKE '%%R & T%%'
               OR name_maple ILIKE '%%R & T%%' OR name_refuah ILIKE '%%R & T%%'
               OR name_westgate ILIKE '%%R & T%%' OR name_bmw ILIKE '%%R & T%%'
               OR name_lakewood_rx ILIKE '%%R & T%%'
        """)
        for r in cur.fetchall():
            print(r)

        print("\n=== how many TOTAL qb_match_candidates rows feed EACH of the 3 proposals "
              "(occurrence count, not just distinct spellings) ===")
        for sv, store, reason in [
            ("PAS PACKAGING", "Madison Pharmacy", "number + amount match; vendor did not resolve to the same standard vendor"),
            ("R & T Dist.", "Madison Pharmacy", "number + amount match; vendor did not resolve to the same standard vendor"),
            ("Bordova Innovations", "Lakewood Rx", "vendor + amount match, date within 10 days; number missing/mismatched"),
        ]:
            cur.execute("""
                SELECT count(*), min(c.created_at), max(c.created_at), count(DISTINCT c.batch_id)
                FROM qb_match_candidates c
                LEFT JOIN invoices i ON c.record_type = 'invoice' AND i.id = c.record_id
                LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
                WHERE c.confidence_reason = %s AND c.qb_store = %s
                  AND COALESCE(i.standard_vendor, cm.standard_vendor) = %s AND c.status <> 'rejected'
            """, (reason, store, sv))
            n, first, last, nbatches = cur.fetchone()
            print(f"  {sv!r} @ {store}: {n} row(s) across {nbatches} batch(es), first {first}, last {last}")
finally:
    conn.close()
