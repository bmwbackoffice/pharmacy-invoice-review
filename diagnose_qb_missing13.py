"""READ-ONLY. Pulls the exact records showing on /qb under the 'missing a store, date or
amount' gate, to see: what's missing on each, whether each has a real source email (so it
COULD be reprocessed) or is a legacy-migrated record with no email at all (can't be rerun --
must be fixed by hand), and how old each one is."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== invoices missing store/date/amount, not yet entered ===")
        cur.execute("""
            SELECT id, email_filename, gmail_link, legacy_migrated, date_added, vendor_matched,
                   store_matched, invoice_number, invoice_date, amount
            FROM invoices
            WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE
              AND duplicate_ignored IS NOT TRUE
              AND (coalesce(btrim(store_matched), '') = '' OR lower(btrim(store_matched)) = 'unknown'
                   OR coalesce(btrim(invoice_date), '') = '' OR amount IS NULL OR amount <= 0)
            ORDER BY id
        """)
        rows = cur.fetchall()
        for r in rows:
            missing = []
            if not (r["store_matched"] or "").strip() or (r["store_matched"] or "").strip().lower() == "unknown":
                missing.append("store")
            if not (r["invoice_date"] or "").strip():
                missing.append("date")
            if r["amount"] is None or r["amount"] <= 0:
                missing.append("amount")
            print(f"  id={r['id']} missing={missing} legacy_migrated={r['legacy_migrated']} "
                  f"date_added={r['date_added']} has_gmail_link={bool(r['gmail_link'])} "
                  f"vendor={r['vendor_matched']!r} file={r['email_filename']!r}")
        print(f"count: {len(rows)}\n")

        print("=== credit_memos missing store/date/amount, not yet entered ===")
        cur.execute("""
            SELECT id, email_filename, gmail_link, legacy_migrated, date_added, vendor_matched,
                   store_matched, credit_memo_number, credit_memo_date, amount
            FROM credit_memos
            WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE
              AND (coalesce(btrim(store_matched), '') = '' OR lower(btrim(store_matched)) = 'unknown'
                   OR coalesce(btrim(credit_memo_date), '') = '' OR amount IS NULL OR amount <= 0)
            ORDER BY id
        """)
        rows = cur.fetchall()
        for r in rows:
            missing = []
            if not (r["store_matched"] or "").strip() or (r["store_matched"] or "").strip().lower() == "unknown":
                missing.append("store")
            if not (r["credit_memo_date"] or "").strip():
                missing.append("date")
            if r["amount"] is None or r["amount"] <= 0:
                missing.append("amount")
            print(f"  id={r['id']} missing={missing} legacy_migrated={r['legacy_migrated']} "
                  f"date_added={r['date_added']} has_gmail_link={bool(r['gmail_link'])} "
                  f"vendor={r['vendor_matched']!r} file={r['email_filename']!r}")
        print(f"count: {len(rows)}")
finally:
    conn.close()
