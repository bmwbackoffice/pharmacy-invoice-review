"""
Requeue EVERY item currently in the review queue so it reprocesses from scratch with
the CURRENT rules + learnings (e.g. after adding the Sageteq Bill-To rule and the
Lakewood Rx store). Each flagged item's SOURCE email is moved back to test_emails/,
emails.status='fetched', its review rows are deleted, and stale split files are
removed. Re-running classify_emails.py then re-classifies them: anything that now
resolves lands in results and leaves the queue; anything still ambiguous returns to
the queue.

Safe + additive — does NOT change the pipeline:
  * Backs up review_queue to a timestamped table first (the recovery path).
  * Only requeues an email whose source .eml is actually on disk; an item whose .eml
    can't be found is LEFT in the queue untouched (never silently dropped).
  * Re-reading an email whose clean records are already recorded does NOT duplicate
    them: save_results replaces by filename, and the duplicate check self-matches on
    source-email identity, so invoice/payment history is never double-counted.

Dry-run default; --commit to act. DB mode only (requires DATABASE_URL).

    python requeue_all_reviews.py            # show what would be requeued
    python requeue_all_reviews.py --commit   # back up, requeue, delete rows
    then:  python classify_emails.py         # re-classify; resolved items leave the queue
"""

import datetime
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


def _find_source_eml(filename):
    """Locate a review item's SOURCE email on disk. A per-record item's original
    email was archived to processed_emails/; a whole-email item sits in
    review_pending/. Returns the Path, or None if it isn't anywhere."""
    for d in (PROCESSED, REVIEW_PENDING, TEST_EMAILS):
        p = d / filename
        if p.exists():
            return p
    return None


def main():
    commit = "--commit" in sys.argv[1:]
    print(f"=== requeue_all_reviews [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM review_queue")
            total = cur.fetchone()["n"]
            if total == 0:
                print("review_queue is empty — nothing to requeue. Done.")
                return

            # Group EVERY review row by its source-email identity (the requeue unit).
            # A per-record split item has source_email = the original batch email;
            # a whole-email item has source_email NULL, so it keys on its own filename.
            cur.execute("SELECT id, filename, source_email, file_path FROM review_queue")
            rows = cur.fetchall()
            by_email = defaultdict(list)
            for r in rows:
                by_email[r["source_email"] or r["filename"]].append(r)

            found, missing = {}, []          # key -> Path ; [keys with no .eml on disk]
            for key in by_email:
                p = _find_source_eml(key)
                if p:
                    found[key] = p
                else:
                    missing.append(key)

            found_rows = sum(len(by_email[k]) for k in found)
            missing_rows = sum(len(by_email[k]) for k in missing)

            print(f"review_queue rows:            {total}")
            print(f"  distinct source emails:     {len(by_email)}")
            print(f"  requeue-able (eml on disk):  {len(found)} email(s), {found_rows} row(s)")
            print(f"  NOT on disk (left as-is):    {len(missing)} email(s), {missing_rows} row(s)\n")

            if found:
                print("Sample emails to requeue (up to 6):")
                for key in list(found)[:6]:
                    print(f"  {key}   [{len(by_email[key])} row(s), from {found[key].parent.name}/]")
                print()
            if missing:
                print("Left in the queue (source .eml not found — resolve manually):")
                for key in missing[:6]:
                    print(f"  {key}   [{len(by_email[key])} row(s)]")
                if len(missing) > 6:
                    print(f"  ... and {len(missing) - 6} more")
                print()

            if not found:
                print("No requeue-able items (no source emails on disk). Nothing to do.")
                return

            if not commit:
                print("DRY-RUN — no changes. Re-run with --commit to:")
                print("  1) back up review_queue")
                print(f"  2) move {len(found)} .eml back to test_emails/, set status='fetched',")
                print(f"     remove stale split files, delete {found_rows} review row(s)")
                print("  then:  python classify_emails.py")
                return

            # ---- COMMIT ----
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"review_queue_backup_{ts}"
            cur.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM review_queue')
            print(f"1) backed up review_queue -> {backup}")

            TEST_EMAILS.mkdir(exist_ok=True)
            keys = list(found)
            moved = 0
            for key, p in found.items():
                if p.parent != TEST_EMAILS:
                    shutil.move(str(p), str(TEST_EMAILS / key))
                moved += 1
            cur.execute("UPDATE emails SET status = 'fetched' WHERE filename = ANY(%s)", (keys,))

            # Remove stale split files for the requeued emails (regenerated on reprocess).
            # Guard: never delete the source email file itself (name == source_email).
            cur.execute("SELECT source_email, filename, file_path FROM review_queue "
                        "WHERE COALESCE(source_email, filename) = ANY(%s)", (keys,))
            removed = 0
            for r in cur.fetchall():
                fp = r["file_path"]
                if fp and Path(fp).name != (r["source_email"] or "") and REVIEW_PENDING in Path(fp).parents:
                    try:
                        if Path(fp).exists():
                            Path(fp).unlink()
                            removed += 1
                    except OSError:
                        pass

            cur.execute("DELETE FROM review_queue WHERE COALESCE(source_email, filename) = ANY(%s)", (keys,))
            deleted = cur.rowcount
            print(f"2) requeued {moved} email(s) -> test_emails/ (status='fetched'), "
                  f"removed {removed} split file(s), deleted {deleted} review row(s)")

        conn.commit()
        print("\n=== SUMMARY ===")
        print(f"  backup table:          {backup}")
        print(f"  emails requeued:       {len(found)} ({found_rows} review row(s) cleared)")
        print(f"  left in queue (no eml): {len(missing)} ({missing_rows} row(s))")
        print("\nNext:  python classify_emails.py   "
              "(resolved items leave the queue; still-ambiguous ones return)")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back DB changes (any files already moved stay moved; rerun is safe).")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
