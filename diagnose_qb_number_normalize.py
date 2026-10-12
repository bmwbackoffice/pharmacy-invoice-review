"""
READ-ONLY diagnostic. Run locally. Paste the full output back.

Tests a proposed looser number-equality rule for the QB match module against
EVERY 'number missing/mismatched' candidate ever created (all 66, not just a
sample), to see exactly which ones it would newly treat as the same invoice
number, before any code change is made.

Rule: extract the longest run of digits from each string (ignoring any
letters/punctuation around it), strip leading zeros, and compare. This
catches cosmetic differences like a vendor prefix (IN-, INV, SINV, G...) or
padded zeros, while leaving genuinely different numbers alone.
"""
import re

import psycopg2.extras

import database


def digit_core(v):
    v = (v or "").strip()
    alnum = re.sub(r"[^0-9A-Za-z]", "", v)
    runs = re.findall(r"\d+", alnum)
    if not runs:
        return ""
    core = max(runs, key=len)
    return core.lstrip("0") or "0"


conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT c.id, c.record_type, c.record_id, c.qb_num, c.status, c.qb_name, c.qb_amount,
                   COALESCE(i.invoice_number, cm.credit_memo_number) AS rec_number
            FROM qb_match_candidates c
            LEFT JOIN invoices i      ON c.record_type = 'invoice'     AND i.id  = c.record_id
            LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
            WHERE c.confidence_reason = 'vendor + amount match, date within 10 days; number missing/mismatched'
            ORDER BY c.id
        """)
        rows = cur.fetchall()  # RealDictCursor -> already a list of dict-like rows, no zip needed

        would_match = []
        would_not = []
        for r in rows:
            a, b = digit_core(r["qb_num"]), digit_core(r["rec_number"])
            if a and a == b:
                would_match.append(r)
            else:
                would_not.append(r)

        print(f"Total candidates: {len(rows)}")
        print(f"\n=== WOULD NOW MATCH under the proposed rule ({len(would_match)}) ===")
        for r in would_match:
            print(f"  id={r['id']:>4}  status={r['status']:<9}  qb_num={r['qb_num']!r:<16} "
                  f"rec_number={r['rec_number']!r:<16} vendor={r['qb_name']!r}")

        print(f"\n=== Still WOULD NOT match ({len(would_not)}) ===")
        for r in would_not:
            print(f"  id={r['id']:>4}  status={r['status']:<9}  qb_num={r['qb_num']!r:<16} "
                  f"rec_number={r['rec_number']!r:<16} vendor={r['qb_name']!r}")

        bad = [r for r in would_match if r["status"] == "rejected"]
        print(f"\n=== Already-REJECTED candidates the rule would match (should be empty) ===")
        for r in bad:
            print(f"  id={r['id']}  qb_num={r['qb_num']!r}  rec_number={r['rec_number']!r}")
        if not bad:
            print("  (none)")
finally:
    conn.close()
