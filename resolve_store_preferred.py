"""
Store-preference duplicate resolver (manual / catch-up). On an EXACT invoice duplicate (same
normalized vendor + number + amount + store) that is still a live 'possible duplicate', keeps the
store-SCANNED copy (sender in sender_store_map) and auto-ignores the vendor copy. This runs
automatically at the end of every classify_emails run (see resolve_store_preferred_duplicates's hook
in main); this script is for a dry-run review or a one-time catch-up.

    python resolve_store_preferred.py            # report only (dry run)
    python resolve_store_preferred.py --commit   # write
"""
import sys
import classify_emails as ce


def main():
    commit = "--commit" in sys.argv[1:]
    ce.USE_DB = True
    print(f"=== resolve_store_preferred [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")
    res = ce.resolve_store_preferred_duplicates(
        "store-preferred backstop (manual)" if commit else "dry-run", dry_run=not commit)
    print(f"Resolved: {len(res['resolved'])}")
    for r in res["resolved"]:
        print(f"  {r['vendor']} #{r['number']} @ {r['store']}: keep id {r['keep_id']}, "
              f"ignore {r['ignored_ids']}")
    print(f"\nSkipped (ambiguous — two scans, or no store-scanned copy): {len(res['skipped'])}")
    for s in res["skipped"]:
        k = s["key"]
        print(f"  {k[0]!r} #{k[1]} ${k[2]} @ {k[3]}: scans={s['n_scans']} other-copies={s['n_other']}")
    if not commit:
        print("\nDRY-RUN -- nothing written. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
