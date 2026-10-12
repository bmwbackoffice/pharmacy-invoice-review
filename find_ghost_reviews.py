"""
READ-ONLY diagnostic: find stale "ghost" review_queue rows — open review items whose
(vendor + number + amount) is ALREADY recorded as a NON-ignored invoice / credit_memo /
payment, i.e. the item was resolved and recorded but its old queue row was never removed
(the interrupted-run bug that delete_review_row now prevents going forward).

Prints each candidate with the recorded record it matches, so you can eyeball whether it's a
genuine ghost (safe to delete) or a real open duplicate (leave it). This script DELETES
NOTHING — it only reports. DB mode only (needs DATABASE_URL).

    python find_ghost_reviews.py
"""
import os
import sys

import psycopg2.extras

import database

# Match a review_queue row's content against each recorded table. Number columns differ per
# table; amount compared with a cent tolerance; auto/manually-ignored recorded rows excluded
# (an ignored row isn't the "resolved survivor" a ghost would duplicate).
_MATCH = [
    ("invoice", "invoices", "invoice_number", "duplicate_ignored IS NOT TRUE"),
    ("credit_memo", "credit_memos", "credit_memo_number", "TRUE"),
    ("payment", "payments", "reference_number", "duplicate_ignored IS NOT TRUE"),
]


def main():
    if not os.environ.get("DATABASE_URL"):
        print("find_ghost_reviews needs DATABASE_URL (DB mode). Nothing to do.")
        return
    conn = database.connect()
    try:
        candidates = []
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for rtype, table, numcol, live_clause in _MATCH:
                cur.execute(f"""
                    SELECT rq.id AS rq_id, rq.filename, rq.source_email, rq.vendor_matched,
                           rq.invoice_number, rq.amount,
                           rec.id AS rec_id, rec.email_filename AS rec_file,
                           rec.store_matched AS rec_store
                      FROM review_queue rq
                      JOIN {table} rec
                        ON lower(btrim(rec.vendor_matched)) = lower(btrim(rq.vendor_matched))
                       AND lower(btrim(rec.{numcol})) = lower(btrim(rq.invoice_number))
                       AND rec.vendor_matched IS NOT NULL AND rq.invoice_number IS NOT NULL
                       AND rq.invoice_number <> ''
                       AND (abs(COALESCE(rec.amount,0) - COALESCE(rq.amount,0)) <= 0.01)
                       AND {live_clause}
                      ORDER BY rq.id
                """)
                for r in cur.fetchall():
                    candidates.append((rtype, r))

        if not candidates:
            print("No ghost review rows found — every open review item's (vendor+number+amount) "
                  "is NOT already recorded. Nothing to clean up.")
            return

        # Group by review_queue id (a row could match more than one recorded table/row).
        by_rq = {}
        for rtype, r in candidates:
            by_rq.setdefault(r["rq_id"], {"row": r, "matches": []})
            by_rq[r["rq_id"]]["matches"].append((rtype, r))

        print(f"Found {len(by_rq)} open review item(s) whose content is ALREADY recorded "
              f"(candidate ghosts). Review each before deleting — this script deletes nothing.\n")
        for rq_id, g in by_rq.items():
            row = g["row"]
            amt = row["amount"]
            amt_s = f"${float(amt):,.2f}" if amt is not None else "(none)"
            print(f"review_queue id={rq_id}  {row['vendor_matched'] or '-'} #"
                  f"{row['invoice_number'] or '-'}  {amt_s}")
            print(f"    filename:     {row['filename']}")
            print(f"    source_email: {row['source_email']}")
            for rtype, m in g["matches"]:
                print(f"    already recorded as {rtype} id={m['rec_id']} "
                      f"(store {m['rec_store'] or '-'}, file {m['rec_file']})")
            print()

        ids = ", ".join(str(i) for i in by_rq)
        print("If you confirm these are all genuine ghosts, the deletion is a one-liner "
              "(review_queue ids above):")
        print(f"    DELETE FROM review_queue WHERE id IN ({ids});")
        print("Paste this list back and I can add a guarded --commit delete mode if you'd rather "
              "not run raw SQL.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
