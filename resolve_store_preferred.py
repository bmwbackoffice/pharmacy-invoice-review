"""
Post-save duplicate backstops (manual / catch-up). Both run automatically at the end of every
classify_emails run; this script is for a dry-run review or a one-time catch-up.

  1) Store-preference: on an EXACT duplicate (same vendor + number + amount + store) with one copy
     from a store scanner, keep the store-scanned copy and auto-IGNORE the vendor copy.
  2) Misread-number: same vendor + amount + date + store but a DIFFERENT invoice number (a misread
     digit) — FLAG both so neither silently reaches /qb; a human resolves on /duplicates.

    python resolve_store_preferred.py            # report only (dry run)
    python resolve_store_preferred.py --commit   # write
"""
import sys
import classify_emails as ce


def main():
    commit = "--commit" in sys.argv[1:]
    ce.USE_DB = True
    # The sweeps match vendors by standard name, which needs the vendor tables a classify run loads in
    # main(). Without this line they silently fell back to printed spellings here (fixed 2026-10-13).
    ce.VENDOR_STD = ce.load_vendor_standardization()
    tag = "manual catch-up" if commit else "dry-run"
    print(f"=== duplicate backstops [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")

    res = ce.resolve_store_preferred_duplicates(tag, dry_run=not commit)
    print(f"[store-preference] resolved (keep store scan, ignore vendor copy): {len(res['resolved'])}")
    for r in res["resolved"]:
        print(f"  {r['vendor']} #{r['number']} @ {r['store']}: keep id {r['keep_id']}, "
              f"ignore {r['ignored_ids']}")
    print(f"  skipped (two scans / no store-scanned copy): {len(res['skipped'])}")

    mm = ce.flag_number_misread_duplicates(tag, dry_run=not commit)
    print(f"\n[misread-number] flagged for review (same vendor/amount/date/store, differing number): "
          f"{len(mm['flagged'])}")
    for f in mm["flagged"]:
        print(f"  {f['vendor']} ${f['amount']} @ {f['store']}: numbers {f['numbers']} "
              f"(ids {f['ids']}, group {f['group']})")

    rq = ce.dedupe_review_queue(tag, dry_run=not commit)
    print(f"\n[review-queue dedupe] collapsed duplicate review items (kept store scan): "
          f"{len(rq['dismissed'])}")
    for d in rq["dismissed"]:
        print(f"  {d['vendor']} ${d['amount']}: keep review #{d['keep_id']}, dismiss {d['dismissed_ids']}")

    if not commit:
        print("\nDRY-RUN -- nothing written. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
