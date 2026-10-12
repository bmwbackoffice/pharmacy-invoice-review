"""
ONE-OFF — clean up review items caused by the email-date false positive (scanner
clocks put the 'Date:' header years off, so an invoice date looked "later than the
email"). The classifier now uses the authoritative received time, so:

  * PURE date items  — a review row whose ONLY reason was the date discrepancy, on
    an email where EVERY flagged record is like that — are REQUEUED: the source .eml
    goes back to test_emails/, emails.status='fetched', its rows are deleted, stale
    split files removed. Re-running classify_emails.py resolves them and they vanish.

  * MIXED items — a review row that ALSO carries a genuine reason (e.g. the Maxi
    Health "page 1 of 3, pages missing" scan) — are KEPT. We only STRIP the date
    sentence out of the row's `question` in place, so the remaining review line shows
    just the real issue to focus on. These are never requeued or deleted.

So the date discrepancy disappears everywhere, pure false positives clear themselves
on the next run, and genuine review lines stay exactly where they are (minus the
date note). Additive; does NOT change the pipeline. Dry-run default; --commit to act.
Every in-place question edit is logged to edit_log.

    python requeue_email_date_flags.py            # show requeues + before/after strips
    python requeue_email_date_flags.py --commit   # back up, requeue pure, strip mixed
    then:  python classify_emails.py              # pure items resolve; mixed stay clean
"""

import datetime
import re
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

# Phrases that mark a sentence as the email-date-vs-invoice-date discrepancy. Used
# both to FIND candidate rows (as %..% ILIKE patterns) and to STRIP the offending
# sentence out of a mixed row's question. Dry-run shows every match so the operator
# can eyeball them before committing.
DATE_PHRASES = [
    "later than the email", "after the email", "email transmission", "transmission date",
    "email date", "email was sent", "email's date", "predates the email",
    "before the email", "later than the received", "received date", "email received",
]
TARGET_PATTERNS = [f"%{p}%" for p in DATE_PHRASES]


def _split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _strip_date_sentences(question):
    """The question with any date-discrepancy sentence removed (may be empty)."""
    kept = [s for s in _split_sentences(question)
            if not any(p in s.lower() for p in DATE_PHRASES)]
    return " ".join(kept).strip()


_CLEARED_NOTE = ("Date-discrepancy flag cleared (false positive from scanner clock); "
                 "verify this record.")


def _find_source_eml(filename):
    for d in (PROCESSED, REVIEW_PENDING, TEST_EMAILS):
        p = d / filename
        if p.exists():
            return p
    return None


def main():
    commit = "--commit" in sys.argv[1:]
    print(f"=== requeue_email_date_flags [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM review_queue")
            total = cur.fetchone()["n"]
            cur.execute("SELECT id, filename, source_email, file_path, question "
                        "FROM review_queue WHERE question ILIKE ANY(%s) ORDER BY id",
                        (TARGET_PATTERNS,))
            targets = cur.fetchall()
            for r in targets:
                r["_stripped"] = _strip_date_sentences(r["question"])
                r["_pure"] = (r["_stripped"] == "")

            # Group candidates by their source email (the requeue unit), and pull
            # EVERY row of those emails so we can tell a pure-date email (all its rows
            # are date-only) from a mixed one (has any other reason to keep).
            by_email = defaultdict(list)
            for r in targets:
                by_email[r["source_email"] or r["filename"]].append(r)
            keys = list(by_email)
            cur.execute("SELECT id, COALESCE(source_email, filename) AS key FROM review_queue "
                        "WHERE COALESCE(source_email, filename) = ANY(%s)", (keys,))
            rows_per_email = defaultdict(set)
            for r in cur.fetchall():
                rows_per_email[r["key"]].add(r["id"])

            requeue_keys = []           # pure-date emails -> whole-email requeue
            strip_rows = []             # (id, old_q, new_q) -> edit question in place
            for key, matched in by_email.items():
                matched_ids = {r["id"] for r in matched}
                has_other = bool(rows_per_email[key] - matched_ids)  # unmatched rows exist
                if all(r["_pure"] for r in matched) and not has_other:
                    requeue_keys.append(key)
                else:
                    for r in matched:
                        strip_rows.append((r["id"], r["question"], r["_stripped"] or _CLEARED_NOTE))

            requeue_row_count = sum(len(rows_per_email[k]) for k in requeue_keys)
            print(f"review_queue rows:                 {total}")
            print(f"  date-discrepancy matches:        {len(targets)}")
            print(f"  pure-date emails to requeue:     {len(requeue_keys)} "
                  f"(clears {requeue_row_count} row(s), which then resolve on rerun)")
            print(f"  mixed rows to strip in place:    {len(strip_rows)} (kept, date note removed)\n")

            if strip_rows:
                print("Sample in-place strips (up to 4):")
                for _id, old_q, new_q in strip_rows[:4]:
                    print(f"  #{_id}")
                    print(f"     before: {(old_q or '')[:110]!r}")
                    print(f"     after:  {new_q[:110]!r}")
                print()

            if not targets:
                print("Nothing matched — no date-discrepancy rows. Done.")
                return

            if not commit:
                print("DRY-RUN — no changes. Re-run with --commit to:")
                print(f"  1) back up review_queue")
                print(f"  2) requeue {len(requeue_keys)} pure-date email(s) (.eml -> test_emails/, "
                      f"status='fetched', delete {requeue_row_count} row(s), remove split files)")
                print(f"  3) strip the date sentence from {len(strip_rows)} mixed row(s), logged to edit_log")
                return

            # ---- COMMIT ----
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"review_queue_backup_{ts}"
            cur.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM review_queue')
            print(f"1) backed up review_queue -> {backup}")

            # (2) requeue the pure-date emails
            TEST_EMAILS.mkdir(exist_ok=True)
            moved, missing = [], []
            for key in requeue_keys:
                p = _find_source_eml(key)
                if p and p.parent != TEST_EMAILS:
                    shutil.move(str(p), str(TEST_EMAILS / key)); moved.append(key)
                elif p:
                    moved.append(key)
                else:
                    missing.append(key)
            if requeue_keys:
                cur.execute("UPDATE emails SET status = 'fetched' WHERE filename = ANY(%s)", (requeue_keys,))
                # remove stale split files for those emails (regenerated on reprocess)
                cur.execute("SELECT source_email, filename, file_path FROM review_queue "
                            "WHERE COALESCE(source_email, filename) = ANY(%s)", (requeue_keys,))
                removed = 0
                for r in cur.fetchall():
                    fp = r["file_path"]
                    if fp and Path(fp).name != (r["source_email"] or "") and REVIEW_PENDING in Path(fp).parents:
                        try:
                            if Path(fp).exists():
                                Path(fp).unlink(); removed += 1
                        except OSError:
                            pass
                cur.execute("DELETE FROM review_queue WHERE COALESCE(source_email, filename) = ANY(%s)",
                            (requeue_keys,))
                print(f"2) requeued {len(moved)} email(s) "
                      f"({len(missing)} not on disk), removed {removed} split file(s), "
                      f"deleted {cur.rowcount} row(s)")
            else:
                print("2) no pure-date emails to requeue")

            # (3) strip the date sentence out of the mixed rows, keep them
            for _id, old_q, new_q in strip_rows:
                cur.execute("UPDATE review_queue SET question = %s WHERE id = %s", (new_q, _id))
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, "
                            "new_value, changed_by) VALUES ('review_queue',%s,'question',%s,%s,'requeue_script')",
                            (_id, old_q, new_q))
            print(f"3) stripped the date note from {len(strip_rows)} mixed row(s) (kept, logged)")

        conn.commit()
        print("\n=== SUMMARY ===")
        print(f"  backup table:            {backup}")
        print(f"  pure emails requeued:    {len(requeue_keys)} ({len(missing)} not on disk)")
        print(f"  mixed rows cleaned:      {len(strip_rows)}")
        print("\nNext:  python classify_emails.py   (pure items resolve; mixed lines stay, date note gone)")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back DB changes (any files already moved stay moved; rerun is safe).")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
