"""One-time retroactive cleanup: flip skip_qb_import=TRUE on every already-recorded
invoice/credit_memo whose vendor is on the excluded_vendors list but that slipped in
as QB-ready before the deterministic apply_vendor_exclusion enforcement existed.

Covers store-to-store inventory transfers now that the store names (Madison/Refuah/
Westgate/3M/Maple Pharmacy) are on Setup -> Excluded vendors: a transfer's vendor_matched
is the counterparty store, so the exclusion match catches both records.

Matching is IDENTICAL to classify_emails.is_excluded_vendor() (normalized substring,
either direction, via classify_emails._norm), so results match what a fresh pipeline run
would now produce. The one difference is performance: is_excluded_vendor() opens a fresh
DB connection on every call (database.load_excluded_vendors -> connect()), which over a
few-hundred-row backlog and a slow remote connect would take hours. So this loads the
excluded list ONCE, reuses ONE connection, and applies the same normalized match locally.

Idempotent: rows already skip_qb_import=TRUE are untouched; only ever flips
False/NULL -> TRUE. Run once by hand from the project root:

    python cleanup_transfer_backlog.py
"""
import classify_emails as ce
import database

# Load + normalize the exclusion list once (same source is_excluded_vendor reads).
_EXCLUDED = [n for n in (ce._norm(v) for v in ce.load_excluded_vendors()) if n]


def _is_excluded(vendor):
    """Exact reimplementation of classify_emails.is_excluded_vendor() using the
    pre-loaded, pre-normalized list — normalized substring match either direction."""
    name = ce._norm(vendor)
    if not name:
        return False
    return any(ex in name or name in ex for ex in _EXCLUDED)


def cleanup(table):
    conn = database.connect()
    n = 0
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, vendor_matched FROM {table} WHERE skip_qb_import IS NOT TRUE")
            ids = [rid for rid, vendor in cur.fetchall() if _is_excluded(vendor)]
            if ids:
                cur.execute(f"UPDATE {table} SET skip_qb_import = TRUE WHERE id = ANY(%s)", (ids,))
        conn.commit()
        n = len(ids)
    finally:
        conn.close()
    print(f"{table}: {n} record(s) updated to skip_qb_import = true")


if __name__ == "__main__":
    cleanup("invoices")
    cleanup("credit_memos")
