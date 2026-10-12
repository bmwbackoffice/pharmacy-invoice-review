"""
Requeue already-CLEAN (never-flagged) RECORDED records for reprocessing, filtered by
vendor — the counterpart to requeue_all_reviews.py (which handles review-queue items).

Use when a NEW prompt rule should retroactively re-sort records that already classified
cleanly and were recorded. Current motivating case: the multi-store cost-split rule
(Sageteq's monthly IT bill — one invoice # split across every store) should make already-
recorded Sageteq records like #29365 / #29389 come back skip_qb_import=true on reprocess,
so they leave qb_ready and their per-store files are no longer needed.

A cleanly-recorded record's source .eml was archived to processed_emails/ with
emails.status='processed'; that status is what load_processed_filenames() checks to SKIP a
file even if it's back in test_emails/. So this tool resets BOTH pieces of state that make a
file eligible again: (1) it moves the source .eml back to test_emails/, and (2) it sets
emails.status='fetched'. Re-running classify_emails.py then re-classifies it under the
CURRENT rules; save_results replaces the DB rows by filename (ON CONFLICT DO UPDATE) and the
duplicate check self-matches on source-email identity, so history is never double-counted.

Matches vendor_matched by the SAME normalized-substring rule as is_excluded_vendor() — so
"Sageteq" matches every Sageteq record. NOTE: it matches by VENDOR, not by invoice SHAPE, so
it will also list Sageteq's genuine single-store invoices (#29347-29353). That's expected —
the new prompt rule is what re-sorts split-vs-single on reprocess, not this tool.

Safe + additive — does NOT change the pipeline:
  * On --commit, backs up every affected result row to timestamped table(s) first (recovery).
  * Only requeues a source email whose .eml is actually on disk; one that can't be found is
    reported and LEFT untouched (never silently dropped).
  * Deletes NOTHING itself. Reprocessing replaces the DB rows; any invoice_files/ PDF the
    reprocessed record no longer needs (e.g. it's now skip_qb_import) is ORPHANED on disk —
    those exact paths are listed so you can remove them by hand. This tool requeues; it
    does not clean up files.

Dry-run default; --commit to act. DB mode only (requires DATABASE_URL).

    python requeue_by_vendor.py "Sageteq"            # show what would be requeued
    python requeue_by_vendor.py "Sageteq" --commit   # back up, move .eml, status='fetched'
    then:  python run_all.py --drive                 # re-classify with the current rules
"""

import datetime
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2.extras

import database

BASE_DIR = Path(__file__).parent
TEST_EMAILS = BASE_DIR / "test_emails"
PROCESSED = BASE_DIR / "processed_emails"
REVIEW_PENDING = BASE_DIR / "review_pending"

# The three recorded result tables and their per-record number column (all key back to the
# source email via email_filename, and carry file_path to the saved document).
_RESULT_TABLES = [("invoices", "invoice_number"),
                  ("credit_memos", "credit_memo_number"),
                  ("payments", "reference_number")]


def _find_source_eml(filename):
    """Locate a recorded record's SOURCE email on disk — archived to processed_emails/ once
    classified (a per-record split's original batch email), possibly sitting in
    review_pending/ or already back in test_emails/. Returns the Path, or None if nowhere."""
    for d in (PROCESSED, REVIEW_PENDING, TEST_EMAILS):
        p = d / filename
        if p.exists():
            return p
    return None


def _vendor_matches(vendor, query_norm):
    """Normalized-substring match, SAME semantics as is_excluded_vendor()/_norm(): matches
    either direction so the query is treated like every other vendor name in the app."""
    nm = database._norm(vendor)
    return bool(nm and (query_norm in nm or nm in query_norm))


def _money(v):
    if v is None:
        return "(none)"
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def main():
    rest = sys.argv[1:]
    commit = "--commit" in rest
    args = [a for a in rest if a != "--commit"]
    if not args:
        print('Usage: python requeue_by_vendor.py "<vendor>" [--commit]')
        return
    vendor_query = args[0]
    query_norm = database._norm(vendor_query)
    if not query_norm:
        print("Empty vendor query — nothing to do.")
        return
    if not os.environ.get("DATABASE_URL"):
        print("requeue_by_vendor needs DATABASE_URL (DB mode). Nothing to do.")
        return

    print(f'=== requeue_by_vendor "{vendor_query}" [{"COMMIT" if commit else "DRY-RUN"}] ===\n')

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Gather every recorded record whose vendor matches, across the three tables.
            records = []
            for table, numcol in _RESULT_TABLES:
                cur.execute(
                    f"SELECT id, email_filename, vendor_matched, store_matched, "
                    f"{numcol} AS number, amount, file_path, skip_qb_import FROM {table}")
                for r in cur.fetchall():
                    if _vendor_matches(r["vendor_matched"], query_norm):
                        records.append({"table": table, **dict(r)})

            if not records:
                print(f'No recorded records match vendor "{vendor_query}". Nothing to do.')
                return

            # Group by source-email filename (the requeue unit — a split invoice's 7 records
            # all share ONE source .eml, so moving it back requeues all of them together).
            by_email = defaultdict(list)
            no_source = []
            for rec in records:
                fn = rec.get("email_filename")
                (by_email[fn].append(rec) if fn else no_source.append(rec))

            found, missing = {}, []
            for fn in by_email:
                p = _find_source_eml(fn)
                if p:
                    found[fn] = p
                else:
                    missing.append(fn)

            # Per-matching-email detail so the operator can eyeball the batch before committing.
            print(f'Matched {len(records)} recorded record(s) for "{vendor_query}" across '
                  f'{len(by_email)} source email(s):\n')
            for fn in by_email:
                loc = f"{found[fn].parent.name}/" if fn in found else "NOT FOUND on disk"
                print(f"[{loc}] {fn}   ({len(by_email[fn])} record(s))")
                for rec in by_email[fn]:
                    tag = " [skip_qb]" if rec.get("skip_qb_import") else ""
                    print(f"    - {rec['table'][:-1]:11} "
                          f"{rec.get('vendor_matched') or '—'} / {rec.get('store_matched') or '—'} / "
                          f"#{rec.get('number') or '—'} / {_money(rec.get('amount'))}{tag}")
            print()

            if missing:
                print(f"{len(missing)} source email(s) NOT found on disk (left untouched):")
                for fn in missing:
                    print(f"  {fn}   [{len(by_email[fn])} record(s)]")
                print()
            if no_source:
                print(f"{len(no_source)} matching record(s) have no source-email filename — "
                      f"cannot requeue (listed above under no filename); left untouched.\n")

            # invoice_files/ PDFs of the requeue-able records — potentially orphaned after
            # reprocess if the record no longer needs a file. Listed, never deleted.
            stale_files = []
            for fn in found:
                for rec in by_email[fn]:
                    fp = rec.get("file_path")
                    if fp and Path(fp).exists():
                        stale_files.append(fp)

            if not found:
                print("No requeue-able source emails on disk. Nothing to do.")
                return

            found_records = sum(len(by_email[f]) for f in found)

            if not commit:
                print("DRY-RUN — no changes. Re-run with --commit to:")
                print(f"  1) back up the {found_records} affected result row(s) to timestamped table(s)")
                print(f"  2) move {len(found)} source .eml -> test_emails/, set emails.status='fetched'")
                print("  (nothing is deleted; reprocessing replaces the DB rows via ON CONFLICT.)")
                if stale_files:
                    print(f"\n  {len(stale_files)} saved file(s) may be ORPHANED after reprocess "
                          f"(NOT deleted — remove by hand if the reprocessed record drops its file):")
                    for fp in stale_files:
                        print(f"    {fp}")
                print('\n  then:  python run_all.py --drive   (re-classify with current rules)')
                return

            # ---- COMMIT ----
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            keys = list(found)
            print("1) backing up affected result rows (recovery path):")
            for table, _ in _RESULT_TABLES:
                backup = f"{table}_requeue_backup_{ts}"
                cur.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM {table} '
                            f'WHERE email_filename = ANY(%s)', (keys,))
                if cur.rowcount:
                    print(f"   {table}: {cur.rowcount} row(s) -> {backup}")
                else:
                    cur.execute(f'DROP TABLE "{backup}"')   # nothing matched; no empty table left behind

            TEST_EMAILS.mkdir(exist_ok=True)
            moved = 0
            for fn, p in found.items():
                if p.parent != TEST_EMAILS:
                    shutil.move(str(p), str(TEST_EMAILS / fn))
                moved += 1
            cur.execute("UPDATE emails SET status='fetched' WHERE filename = ANY(%s)", (keys,))
            print(f"2) moved {moved} .eml -> test_emails/, set emails.status='fetched' "
                  f"for {cur.rowcount} email row(s)")

        conn.commit()
        print("\n=== SUMMARY ===")
        print(f'  vendor query:            "{vendor_query}"')
        print(f"  source emails requeued:  {len(found)} ({found_records} recorded record(s))")
        print(f"  not on disk (skipped):   {len(missing)}")
        if stale_files:
            print(f"\n  WARNING: {len(stale_files)} existing saved file(s) will be ORPHANED on disk if")
            print("    the reprocessed record no longer needs a file (e.g. now skip_qb_import). They are")
            print("    NOT auto-deleted — review and remove by hand AFTER reprocessing confirms they're stale:")
            for fp in stale_files:
                print(f"      {fp}")
        print("\nNext:  python run_all.py --drive        (or: python classify_emails.py --drive)")
        print("       -> re-classifies the requeued emails under the current prompt rules.")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back DB changes (any files already moved stay moved; rerun is safe).")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
