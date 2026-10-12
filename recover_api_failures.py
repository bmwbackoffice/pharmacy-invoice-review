"""
ONE-OFF RECOVERY — requeue emails that were falsely routed to the review queue
because of an invalid ANTHROPIC_API_KEY (every Claude call returned 401, so ~190
emails exhausted retries and landed as "unreadable"). The key is now fixed;
these are NOT genuine review items and should be reprocessed automatically.

Additive: does not change the CLI pipeline. Defaults to --dry-run (report only);
acts only with --commit. Always backs up review_queue first.

Targets ONLY the API-failure rows:
    review_queue WHERE email_type = 'unreadable'
                   AND question LIKE 'Automatic reading failed%'
Genuine flags (email_type invoice/payment/credit_memo, real vendor/store/amount)
are left untouched.

    python recover_api_failures.py            # dry-run: show what would happen
    python recover_api_failures.py --commit   # do it (backup, move, requeue, delete)
    then:  python classify_emails.py          # re-classify (~190 real API calls)
"""

import datetime
import shutil
import sys
from pathlib import Path

import psycopg2.extras

import database

BASE_DIR = Path(__file__).parent
REVIEW_PENDING = BASE_DIR / "review_pending"
TEST_EMAILS = BASE_DIR / "test_emails"

TARGET_WHERE = "email_type = 'unreadable' AND question LIKE 'Automatic reading failed%'"


def main():
    commit = "--commit" in sys.argv[1:]
    mode = "COMMIT" if commit else "DRY-RUN"
    print(f"=== recover_api_failures.py [{mode}] ===\n")

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM review_queue")
            total = cur.fetchone()["n"]
            cur.execute(f"SELECT count(*) AS n FROM review_queue WHERE {TARGET_WHERE}")
            target_count = cur.fetchone()["n"]
            genuine = total - target_count

            print(f"review_queue rows:            {total}")
            print(f"  API-failure targets:        {target_count}")
            print(f"  genuine review items kept:  {genuine}\n")

            cur.execute(f"SELECT id, filename FROM review_queue WHERE {TARGET_WHERE} ORDER BY id")
            targets = cur.fetchall()

            if target_count == 0:
                print("Nothing to recover — no 'Automatic reading failed' rows. Done.")
                return

            # show a few so the operator can eyeball the selection
            print("Sample targets (up to 5):")
            for r in targets[:5]:
                print(f"  #{r['id']}  {r['filename']}")
            print()

            if not commit:
                print("DRY-RUN — no changes made. Re-run with --commit to:")
                print("  1) back up review_queue to a timestamped table")
                print(f"  2) move {target_count} source file(s) review_pending/ -> test_emails/")
                print("  3) set emails.status='fetched' for them (so Phase-2 reprocesses)")
                print("  4) delete those rows from review_queue")
                return

            # ---- COMMIT ----
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"review_queue_backup_{ts}"
            cur.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM review_queue')
            print(f"1) backed up review_queue -> {backup}")

            # 3) move files (filesystem, non-transactional) — report & skip missing
            TEST_EMAILS.mkdir(exist_ok=True)
            moved, missing = [], []
            for r in targets:
                fn = r["filename"]
                src = REVIEW_PENDING / (fn or "")
                if fn and src.exists():
                    shutil.move(str(src), str(TEST_EMAILS / fn))
                    moved.append(fn)
                else:
                    missing.append(fn)
            print(f"2) moved {len(moved)} file(s) back to test_emails/"
                  f"{f'; {len(missing)} missing (skipped)' if missing else ''}")

            filenames = [r["filename"] for r in targets if r["filename"]]
            # 4) status back to 'fetched' so skip-logic (status='processed') doesn't skip them
            cur.execute("UPDATE emails SET status = 'fetched' WHERE filename = ANY(%s)", (filenames,))
            print(f"3) set emails.status='fetched' for {cur.rowcount} email row(s)")

            # 5) delete the target review rows
            ids = [r["id"] for r in targets]
            cur.execute("DELETE FROM review_queue WHERE id = ANY(%s)", (ids,))
            deleted = cur.rowcount
            print(f"4) deleted {deleted} row(s) from review_queue")

        conn.commit()
        print("\n=== SUMMARY ===")
        print(f"  backed up:          {backup}")
        print(f"  files moved:        {len(moved)}")
        print(f"  files missing:      {len(missing)}")
        if missing:
            for m in missing:
                print(f"      missing: {m}")
        print(f"  review rows deleted:{deleted}")
        print(f"  genuine items kept: {genuine}")
        print("\nNext:  python classify_emails.py   "
              "(re-classifies the requeued emails; expect ~real API calls + cost)")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back DB changes (any files already moved stay moved; "
              "rerun is safe).")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
