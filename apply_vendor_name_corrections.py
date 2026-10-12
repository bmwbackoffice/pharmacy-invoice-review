"""
ONE-OFF (idempotent, safe to re-run) -- applies apply_vendor_name_corrections_from_qb_history()
against the FULL existing qb_match_candidates history, for data uploaded before this feature
existed. Going forward this runs automatically at the end of every QB reconciliation upload
(see process_qb_export in qb_match.py) -- this script is only needed once, to catch up.

    python apply_vendor_name_corrections.py            # report only
    python apply_vendor_name_corrections.py --commit   # write
"""
import sys
import qb_match


def main():
    commit = "--commit" in sys.argv[1:]
    print(f"=== apply_vendor_name_corrections [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")
    result = qb_match.apply_vendor_name_corrections_from_qb_history(
        "one-time backfill", dry_run=not commit)
    print(f"Applied: {len(result['applied'])}")
    for sv, store, col, old, new, source, n in result["applied"]:
        print(f"  {sv!r} @ {store}: {col} {old!r} -> {new!r} ({source}, {n} batch(es))")
    print(f"\nSkipped (conflicting spellings): {len(result['skipped_ambiguous'])}")
    for sv, store, bucket, names in result["skipped_ambiguous"]:
        print(f"  {sv!r} @ {store} [{bucket}]: saw {names}")
    print(f"\nSkipped (thin evidence -- would override but <2 batches agree): "
          f"{len(result['skipped_thin'])}")
    for sv, store, current, proposed, source, n in result["skipped_thin"]:
        print(f"  {sv!r} @ {store}: keeping {current!r} (not overriding with {proposed!r}, "
              f"only {n} batch(es))")
    if not commit:
        print("\nDRY-RUN -- nothing written. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
