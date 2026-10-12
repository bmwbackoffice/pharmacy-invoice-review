"""
ONE-TIME migration: backfill review_queue.review_key for every row that predates the column.

review_key is a STABLE identity (source email + normalized vendor + invoice/reference number)
that survives a reprocess renaming the display filename — so notes / manual attachments keyed to
it are never orphaned. New flagged rows get it automatically; this backfills TODAY's open items so
they're stable from now on.

It does NOT recover notes already orphaned under a now-abandoned filename from an earlier reprocess
of the same item — those are gone; re-add them by hand once. Going forward, no NEW note is lost.

Idempotent: only fills rows where review_key IS NULL. Prints what it changed. DB mode only.

    python migrate_review_keys.py            # show what would change (dry-run)
    python migrate_review_keys.py --commit   # backfill
"""
import os
import sys

import psycopg2.extras

import classify_emails as ce
import database


def main():
    commit = "--commit" in sys.argv[1:]
    if not os.environ.get("DATABASE_URL"):
        print("migrate_review_keys needs DATABASE_URL (DB mode). Nothing to do.")
        return
    database.init_db()   # ensure the review_key column exists before we touch it
    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, source_email, filename, vendor_matched, invoice_number "
                        "FROM review_queue WHERE review_key IS NULL ORDER BY id")
            rows = cur.fetchall()
            if not rows:
                print("Every review_queue row already has a review_key. Nothing to do.")
                return
            print(f"{'COMMIT' if commit else 'DRY-RUN'}: {len(rows)} row(s) missing review_key.\n")
            updates = []
            for r in rows:
                key = ce._stable_review_key(r["source_email"] or r["filename"],
                                            r["vendor_matched"], r["invoice_number"])
                updates.append((r["id"], key))
                print(f"  id={r['id']}  {r['filename']}\n       -> review_key = {key}")
            if not commit:
                print("\nDRY-RUN — no changes. Re-run with --commit to backfill.")
                return
            for rid, key in updates:
                cur.execute("UPDATE review_queue SET review_key = %s WHERE id = %s", (key, rid))
        conn.commit()
        print(f"\nBackfilled review_key on {len(updates)} row(s).")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back; no rows changed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
