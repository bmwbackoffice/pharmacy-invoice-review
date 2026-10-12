"""
ONE-OFF BACKFILL — fill emails.received_at (authoritative server receive time) for
rows that predate the internalDate capture, so the invoice-date sanity check no
longer trips on scanner-clock 'Date:' headers for old emails too.

Source of truth, in order:
  1. the gmail_links.json sidecar's received_at (if gmail_fetcher already captured it),
  2. the stored .eml's TOP 'Received:' header (added last by the delivering server, so
     closest to true arrival), found in processed_emails/, test_emails/, or review_pending/.

Additive; does NOT touch the pipeline. Defaults to --dry-run (report only); acts only
with --commit. Only rows where received_at IS NULL are considered.

    python backfill_received_at.py            # report what would be filled
    python backfill_received_at.py --commit   # write received_at
"""

import json
import sys
from pathlib import Path

import database
from classify_emails import _received_from_eml

BASE_DIR = Path(__file__).parent
SIDECAR = BASE_DIR / "output" / "gmail_links.json"
SEARCH_DIRS = [BASE_DIR / "processed_emails", BASE_DIR / "test_emails", BASE_DIR / "review_pending"]


def _sidecar_received():
    if not SIDECAR.exists():
        return {}
    try:
        data = json.loads(SIDECAR.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {fn: meta["received_at"] for fn, meta in data.items() if meta.get("received_at")}


def _find_eml(filename):
    for d in SEARCH_DIRS:
        p = d / filename
        if p.exists():
            return p
    return None


def _resolve(filename, sidecar):
    """(iso_string, source) for a filename, or (None, None) if nothing found."""
    if filename in sidecar:
        return sidecar[filename], "sidecar"
    p = _find_eml(filename)
    if p:
        dt = _received_from_eml(p)
        if dt:
            return dt.isoformat(), f"eml:{p.parent.name}"
    return None, None


def main():
    commit = "--commit" in sys.argv[1:]
    print(f"=== backfill_received_at [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")
    sidecar = _sidecar_received()

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM emails WHERE received_at IS NULL ORDER BY filename")
            missing = [r[0] for r in cur.fetchall()]
            print(f"emails missing received_at: {len(missing)}")

            resolved, by_source = [], {}
            for fn in missing:
                iso, src = _resolve(fn, sidecar)
                if iso:
                    resolved.append((fn, iso))
                    by_source[src.split(":")[0]] = by_source.get(src.split(":")[0], 0) + 1

            print(f"resolvable now:            {len(resolved)}  {dict(by_source)}")
            print(f"still unknown:             {len(missing) - len(resolved)}\n")
            for fn, iso in resolved[:5]:
                print(f"  {iso}  {fn}")
            if len(resolved) > 5:
                print(f"  ... and {len(resolved) - 5} more")

            if not commit:
                print("\nDRY-RUN — nothing written. Re-run with --commit to fill received_at.")
                return

            for fn, iso in resolved:
                cur.execute("UPDATE emails SET received_at = %s WHERE filename = %s AND received_at IS NULL",
                            (iso, fn))
        conn.commit()
        print(f"\nDone — set received_at on {len(resolved)} row(s).")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
