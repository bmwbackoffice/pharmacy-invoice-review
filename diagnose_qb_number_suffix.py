"""
READ-ONLY diagnostic. Run locally (has real DB access via database.connect()).
Paste the full output back.

Purpose: Joel pointed at a QB match-review candidate where the numbers are
  QB side:      1618282
  App record:   1618282-IN
...and asked whether this kind of cosmetic numbering difference (QB register
drops a trailing invoice-type suffix like "-IN" that the vendor's own invoice
carries) can be auto-resolved instead of flagged for manual review.

This script:
  1. Finds that specific pending candidate (if still pending) and prints its
     full row, to confirm which side has the suffix and the record it points to.
  2. Scans ALL qb_match_candidates (any status) whose confidence_reason is the
     "number missing/mismatched" case, and checks whether qb_num vs the
     linked record's own number differ ONLY by a trailing short suffix
     (e.g. -IN, -INV, -CM, -C, .0, etc.) once both are stripped of
     non-alphanumerics -- to size how common this exact pattern is, not just
     this one record.
"""
import re

import database

TARGET_QB_NUM = "1618282"
TARGET_REC_NUM = "1618282-IN"


def norm(v):
    return (v or "").strip().lower()


def strip_suffix(v):
    """Strip ONE trailing -<letters/digits, <=4 chars> suffix, e.g. '1618282-in' -> '1618282'."""
    v = norm(v)
    m = re.match(r"^(.*?)-([a-z0-9]{1,4})$", v)
    return m.group(1) if m else v


conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== 1) This specific candidate ===")
        cur.execute("""
            SELECT c.id, c.batch_id, c.record_type, c.record_id, c.qb_type, c.qb_num, c.qb_name,
                   c.qb_amount, c.qb_store, c.confidence_reason, c.status, c.decided_by, c.decided_at,
                   c.created_at
            FROM qb_match_candidates c
            WHERE c.qb_num = %s OR c.qb_num = %s
            ORDER BY c.created_at DESC
        """, (TARGET_QB_NUM, TARGET_REC_NUM))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        for r in rows:
            print(dict(zip(cols, r)))

        # Also look up the linked record directly by its own number, in case the
        # candidate above was already confirmed/rejected and no longer "pending".
        print("\n=== invoices with this number ===")
        cur.execute("""
            SELECT id, vendor_matched, standard_vendor, invoice_number, amount, invoice_date,
                   store_matched, entered_in_qb
            FROM invoices WHERE invoice_number IN (%s, %s)
        """, (TARGET_QB_NUM, TARGET_REC_NUM))
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== credit_memos with this number ===")
        cur.execute("""
            SELECT id, vendor_matched, standard_vendor, credit_memo_number, amount, credit_memo_date,
                   store_matched, entered_in_qb
            FROM credit_memos WHERE credit_memo_number IN (%s, %s)
        """, (TARGET_QB_NUM, TARGET_REC_NUM))
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== 2) All 'number missing/mismatched' candidates -- how many are suffix-only? ===")
        cur.execute("""
            SELECT c.id, c.record_type, c.record_id, c.qb_num, c.status,
                   COALESCE(i.invoice_number, cm.credit_memo_number) AS rec_number
            FROM qb_match_candidates c
            LEFT JOIN invoices i      ON c.record_type = 'invoice'     AND i.id  = c.record_id
            LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
            WHERE c.confidence_reason = 'vendor + amount match, date within 10 days; number missing/mismatched'
            ORDER BY c.id
        """)
        cols = [d[0] for d in cur.description]
        all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        print(f"Total candidates in this reason bucket: {len(all_rows)}")

        suffix_only = []
        other = []
        for r in all_rows:
            a, b = norm(r["qb_num"]), norm(r["rec_number"])
            if a == b:
                continue  # shouldn't happen given _num_eq already checked, but just in case
            if strip_suffix(a) == strip_suffix(b) or strip_suffix(b) == strip_suffix(a):
                suffix_only.append(r)
            else:
                other.append(r)

        print(f"\n--- Suffix-only differences ({len(suffix_only)}) ---")
        for r in suffix_only:
            print(r)

        print(f"\n--- Other number differences, NOT suffix-only ({len(other)}) ---")
        for r in other[:30]:
            print(r)
        if len(other) > 30:
            print(f"... and {len(other) - 30} more")

finally:
    conn.close()
