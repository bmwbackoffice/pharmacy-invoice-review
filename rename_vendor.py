"""
ONE-OFF MAINTENANCE — collapse a vendor recorded under many spellings to ONE canonical
name across already-recorded rows.

Renames vendor_matched from one or more EXACT (trimmed, case-sensitive) source names to a
single CANONICAL name across the recorded tables (invoices, credit_memos, payments,
review_queue) AND the dedup ledgers (invoice_history, payment_history — so future duplicate
checks match the canonical name too), writing an edit_log audit row per updated record.
Standalone; uses DATABASE_URL; does NOT touch the classifier or normal runs. Idempotent
(re-running finds 0). It only normalizes the NAME on existing rows — it does not merge or
re-dedupe records. Stopgap until the vendor-match DB owns alias->canonical normalization.

    # Discovery — find the exact spellings (repeatable):
    python rename_vendor.py --like "prestige"
    python rename_vendor.py --like "a&s" --like "a & s"

    # Map MANY variants -> ONE canonical (preview, then apply):
    python rename_vendor.py --to "A&S Sales" \\
        --from "Prestige Sales Inc." --from "A & S Sales" \\
        --from "A&S Sales (Prestige Sales Inc.)" --from "Prestige Sales Inc. (A&S Sales)" --dry-run
    python rename_vendor.py --to "A&S Sales" --from "Prestige Sales Inc." --from "A & S Sales" ...

    # Single-pair shorthand still works:
    python rename_vendor.py "Prestige Sales Inc." "A&S Sales" [--dry-run]
"""

import sys

import database

TABLES = ["invoices", "credit_memos", "payments", "review_queue",
          "invoice_history", "payment_history"]


def discover(patterns):
    """Print distinct trimmed vendor_matched values (+ per-table row counts) matching
    ILIKE %pattern% per table, so the operator can see the exact spellings to pass."""
    conn = database.connect()
    try:
        with conn.cursor() as cur:
            for term in patterns:
                print(f"\n========== vendor_matched ILIKE %{term}% ==========")
                for t in TABLES:
                    cur.execute(
                        f"SELECT btrim(vendor_matched) AS v, count(*) AS n FROM {t} "
                        f"WHERE vendor_matched ILIKE %s GROUP BY btrim(vendor_matched) "
                        f"ORDER BY n DESC, v", (f"%{term}%",))
                    rows = cur.fetchall()
                    print(f"\n  {t}: {sum(n for _, n in rows)} matching row(s)")
                    for v, n in rows:
                        print(f"      {n:6}  {v!r}")
    finally:
        conn.close()


def rename_many(froms, canonical, commit):
    """Map each source name in `froms` (trimmed) to `canonical` across all tables.
    Returns {source_name: {table: count}} of rows changed. A source that already equals
    canonical is skipped (its dict is all zeros)."""
    results = {frm: {t: 0 for t in TABLES} for frm in froms}
    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for frm in froms:
                if frm == canonical:
                    continue                       # already canonical — no-op
                for t in TABLES:
                    cur.execute(
                        f"SELECT id FROM {t} WHERE btrim(vendor_matched) = %s "
                        f"AND vendor_matched IS DISTINCT FROM %s ORDER BY id", (frm, canonical))
                    ids = [r[0] for r in cur.fetchall()]
                    results[frm][t] = len(ids)
                    if commit and ids:
                        cur.execute(f"UPDATE {t} SET vendor_matched = %s WHERE id = ANY(%s)",
                                    (canonical, ids))
                        for rid in ids:
                            cur.execute(
                                "INSERT INTO edit_log (table_name, record_id, field, old_value, "
                                "new_value, changed_by) "
                                "VALUES (%s, %s, 'vendor_matched', %s, %s, 'rename_vendor')",
                                (t, rid, frm, canonical))
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return results


def _print_summary(froms, canonical, results, commit):
    verb = "updated" if commit else "would change"
    total = 0
    for frm in froms:
        if frm == canonical:
            print(f"  {frm!r} -> (already canonical, skipped)")
            continue
        counts = results[frm]
        sub = sum(counts.values())
        total += sub
        print(f"  {frm!r} -> {canonical!r}: {sub} row(s) {verb}")
        if sub:
            for t in TABLES:
                if counts[t]:
                    print(f"      {t:16} {counts[t]}")
        else:
            print("      (no matching rows)")
    print(f"\n  TOTAL: {total} row(s) {verb}")
    return total


def main():
    args = sys.argv[1:]
    if not database.is_configured():
        raise SystemExit("DATABASE_URL is not set — cannot reach the database.")

    likes, froms, pos = [], [], []
    canonical = None
    commit = True
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--like", "--to", "--from"):
            if i + 1 >= len(args):
                raise SystemExit(f"{a} needs a value")
            val = args[i + 1]
            if a == "--like":
                likes.append(val)
            elif a == "--to":
                canonical = val
            else:
                froms.append(val)
            i += 2
        elif a == "--dry-run":
            commit = False
            i += 1
        elif a.startswith("--"):
            raise SystemExit(f"unknown option {a}")
        else:
            pos.append(a)
            i += 1

    if likes:
        print(f"=== rename_vendor discovery ({len(likes)} pattern(s)) ===")
        discover(likes)
        return

    # Resolve the mapping: --to/--from (many->one) OR positional OLD NEW (single).
    if canonical is not None:
        if not froms:
            raise SystemExit("--to needs at least one --from VARIANT")
    elif len(pos) == 2:
        froms, canonical = [pos[0]], pos[1]
    else:
        raise SystemExit(__doc__)

    # Trim inputs (match key is btrim(vendor_matched)); drop duplicate froms.
    canonical = canonical.strip()
    froms = list(dict.fromkeys(f.strip() for f in froms))

    print(f"=== rename_vendor [{'APPLY' if commit else 'DRY-RUN'}] ===")
    print(f"  CANONICAL: {canonical!r}")
    print(f"  {len(froms)} source variant(s)\n")

    results = rename_many(froms, canonical, commit)
    total = _print_summary(froms, canonical, results, commit)

    if not commit:
        print("\nDRY-RUN — no changes. Re-run without --dry-run to apply.")
    elif total:
        print(f"\nApplied. Wrote {total} edit_log audit row(s) (changed_by='rename_vendor'). "
              "Idempotent — re-running finds 0.")
    else:
        print("\nNothing to change (already canonical, or no EXACT match — use --like to find "
              "the exact spellings).")


if __name__ == "__main__":
    main()
