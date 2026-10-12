"""READ-ONLY. Two checks:

1) Tests adding an OCR I<->1 / O<->0 character fold on top of the existing digit-core number
   rule (Joel wants '86I0721' to match '8610721' -- a classic OCR confusion, 6 of 7 chars
   identical). Re-validates against ALL 66 past 'number missing/mismatched' candidates to make
   sure this doesn't newly match anything already rejected.

2) Pulls the vendor_name_map rows for 'Pharmacy Automation' and 'PAS Packaging' plus invoice
   #1724's own stored vendor fields, and live-tests resolve_standard_vendor('Pharmacy
   Automation') against the CURRENT table, to see why Joel's alias edit didn't make it resolve.
"""
import re

import psycopg2.extras

import database
import classify_emails as ce


def digit_core_ocr(v):
    v = (v or "").strip()
    alnum = re.sub(r"[^0-9A-Za-z]", "", v)
    folded = alnum.translate(str.maketrans("IiOo", "1100"))
    runs = re.findall(r"\d+", folded)
    if not runs:
        return ""
    return max(runs, key=len).lstrip("0") or "0"


conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("########## 1) OCR fold re-validation against all 66 past candidates ##########")
        cur.execute("""
            SELECT c.id, c.status, c.qb_num, c.qb_name,
                   COALESCE(i.invoice_number, cm.credit_memo_number) AS rec_number
            FROM qb_match_candidates c
            LEFT JOIN invoices i      ON c.record_type = 'invoice'     AND i.id  = c.record_id
            LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
            WHERE c.confidence_reason = 'vendor + amount match, date within 10 days; number missing/mismatched'
            ORDER BY c.id
        """)
        rows = cur.fetchall()
        would_match, would_not, bad = [], [], []
        for r in rows:
            a, b = digit_core_ocr(r["qb_num"]), digit_core_ocr(r["rec_number"])
            hit = bool(a and a == b)
            (would_match if hit else would_not).append(r)
            if hit and r["status"] == "rejected":
                bad.append(r)
        print(f"Total: {len(rows)}  would-match: {len(would_match)}  would-not: {len(would_not)}")
        print("\n-- WOULD MATCH --")
        for r in would_match:
            print(f"  id={r['id']:>4} status={r['status']:<9} qb_num={r['qb_num']!r:<16} "
                  f"rec_number={r['rec_number']!r:<16} vendor={r['qb_name']!r}")
        print(f"\n-- SAFETY CHECK: already-rejected candidates this would now match "
              f"(should be empty) --")
        for r in bad:
            print(f"  id={r['id']} qb_num={r['qb_num']!r} rec_number={r['rec_number']!r}")
        if not bad:
            print("  (none)")

        print("\n\n########## 2) PAS Packaging / Pharmacy Automation vendor mapping ##########")
        cur.execute("SELECT * FROM vendor_name_map WHERE standard_vendor_name ILIKE %s "
                    "OR standard_vendor_name ILIKE %s OR aliases ILIKE %s OR aliases ILIKE %s",
                    ("%pharmacy automation%", "%pas packaging%", "%pharmacy automation%", "%pas packaging%"))
        for r in cur.fetchall():
            print(r)
            print()

        print("--- invoice 1724 ---")
        cur.execute("SELECT id, vendor_raw, vendor_matched, standard_vendor, standard_account, "
                     "qb_vendor, qb_account, edited_by, edited_at FROM invoices WHERE id = 1724")
        print(cur.fetchone())

        print("\n--- live resolve_standard_vendor('Pharmacy Automation') against CURRENT table ---")
        std = ce.load_vendor_standardization()
        print(repr(ce.resolve_standard_vendor("Pharmacy Automation", std_tables=std)))
        print("--- live resolve_standard_vendor('PAS Packaging') against CURRENT table ---")
        print(repr(ce.resolve_standard_vendor("PAS Packaging", std_tables=std)))
finally:
    conn.close()
