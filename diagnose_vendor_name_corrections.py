"""READ-ONLY. Investigates whether the uploaded QuickBooks reconciliation data (qb_match_candidates)
can be used to correct wrong/missing per-store vendor spellings in vendor_name_map (the
name_3m/name_madison/.../name_lakewood_rx columns), per Joel's rule: only propose a fix when the
evidence is unambiguous -- if anything conflicts, skip that vendor/store entirely rather than guess.

Evidence source: qb_match_candidates, two confidence_reason buckets:

  A) "number + amount match; vendor did not resolve to the same standard vendor"
     -- STRONGEST signal: our own record's number+amount matched the QB row exactly, but
     resolve_standard_vendor() couldn't map the QB row's raw name to that standard vendor at all.
     Direct evidence vendor_name_map is missing/wrong for that (standard_vendor, store).

  B) "vendor + amount match, date within 10 days; number missing/mismatched"
     -- vendor ALREADY resolved correctly, so this is only used to BACKFILL a currently-EMPTY
     name_<store> column, never to override an existing value (the mismatch here is about the
     NUMBER, not the vendor -- weaker evidence for an override).

Rules:
  - status <> 'rejected' only.
  - Grouped by (standard_vendor, qb_store). More than one DISTINCT qb_name text in a group ->
    SKIPPED entirely as ambiguous, reported separately so nothing is silently guessed.
  - Bucket A proposes a correction when the single consistent qb_name differs from the current
    stored value (including filling a NULL). Bucket B only proposes filling a NULL -- an existing
    value is left alone either way.

Writes nothing -- proposals only.
"""
import psycopg2.extras
import database

STORE_COL = {
    "3M Pharmacy": "name_3m", "Madison Pharmacy": "name_madison", "Maple Pharmacy": "name_maple",
    "Refuah Pharmacy": "name_refuah", "Westgate Pharmacy": "name_westgate",
    "BMW": "name_bmw", "Lakewood Rx": "name_lakewood_rx",
}

QUERY = """
    SELECT c.id AS cand_id, c.record_type, c.record_id, c.qb_name, c.qb_store, c.status,
           c.batch_id, c.created_at,
           COALESCE(i.standard_vendor, cm.standard_vendor) AS standard_vendor,
           COALESCE(i.store_matched, cm.store_matched) AS record_store
    FROM qb_match_candidates c
    LEFT JOIN invoices i ON c.record_type = 'invoice' AND i.id = c.record_id
    LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
    WHERE c.confidence_reason = %s AND c.status <> 'rejected'
    ORDER BY c.qb_store, standard_vendor, c.qb_name
"""

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(QUERY, ("number + amount match; vendor did not resolve to the same standard vendor",))
        rows_a = cur.fetchall()
        cur.execute(QUERY, ("vendor + amount match, date within 10 days; number missing/mismatched",))
        rows_b = cur.fetchall()

        cur.execute("""
            SELECT standard_vendor_name, name_3m, name_madison, name_maple, name_refuah,
                   name_westgate, name_bmw, name_lakewood_rx
            FROM vendor_name_map ORDER BY standard_vendor_name
        """)
        vmap = {r["standard_vendor_name"]: r for r in cur.fetchall()}

    print(f"Bucket A rows: {len(rows_a)}   Bucket B rows: {len(rows_b)}   "
          f"vendor_name_map rows: {len(vmap)}\n")

    def analyze(rows, label, backfill_only=False):
        groups = {}
        skipped_norecord = 0
        for r in rows:
            sv, store, name = r["standard_vendor"], r["qb_store"] or r["record_store"], (r["qb_name"] or "").strip()
            if not sv or not store or not name:
                skipped_norecord += 1
                continue
            groups.setdefault((sv, store), set()).add(name)

        proposals, ambiguous, no_change = [], [], []
        for (sv, store), names in sorted(groups.items()):
            col = STORE_COL.get(store)
            if not col:
                continue
            if len(names) > 1:
                ambiguous.append((sv, store, sorted(names)))
                continue
            (name,) = names
            current = ((vmap.get(sv) or {}).get(col) or "").strip() or None
            if backfill_only:
                if current is None:
                    proposals.append((sv, store, col, current, name))
                else:
                    no_change.append((sv, store, name))
            else:
                if current == name:
                    no_change.append((sv, store, name))
                else:
                    proposals.append((sv, store, col, current, name))

        print(f"\n=== {label} ===")
        print(f"(skipped {skipped_norecord} row(s) with no matching record / empty name)")
        print(f"\n--- proposed corrections ({len(proposals)}) ---")
        for sv, store, col, current, name in proposals:
            print(f"  {sv!r} @ {store}: {col} currently {current!r}  ->  {name!r}")

        print(f"\n--- already correct, no change ({len(no_change)}) ---")
        for sv, store, name in no_change[:10]:
            print(f"  {sv!r} @ {store}: already {name!r}")
        if len(no_change) > 10:
            print(f"  ... and {len(no_change) - 10} more")

        print(f"\n--- SKIPPED as ambiguous (conflicting qb_name spellings) ({len(ambiguous)}) ---")
        for sv, store, names in ambiguous:
            print(f"  {sv!r} @ {store}: saw {names}")

    analyze(rows_a, "A) vendor-did-not-resolve (propose correct OR fill)")
    analyze(rows_b, "B) number-mismatch (fill empty only, never override)")
finally:
    conn.close()
