"""
Upload the documents of records whose File link still points at a file on THIS PC (invoice_files/ or
payment_files/) to Google Drive, and switch each record's link to the Drive copy — what a classify run
does for every new record. Background: PROJECT_STATUS "INVESTIGATION: records whose document never
reached Drive" (336 records on 2026-10-08: runs started without --drive, plus uploads that failed).

DRY RUN BY DEFAULT — lists what it would upload and changes nothing.

    python upload_local_files_to_drive.py            # dry run
    python upload_local_files_to_drive.py --commit   # upload + switch the links

Needs your Google login (the same token.json gmail_fetcher and classify use). The local files are kept,
so older QuickBooks memo links to them keep working. Each record is switched only if its link is still
that same local path, one record per commit, and logged to edit_log (changed_by 'drive-backfill') — so
it can be stopped and re-run safely; records already on Drive are skipped. A file shared by several
records is uploaded once. A file that still fails after the uploader's retries is reported and its
records are left as they were (re-run later). Covers invoices, credit memos and payments, ignored
duplicates included. DB mode only (needs DATABASE_URL).
"""
import os
import sys

import database

ACTOR = "drive-backfill"
TABLES = (("invoices", "invoice_files"), ("credit_memos", "invoice_files"), ("payments", "payment_files"))


def local_records(cur):
    """[(table, id, file_path, subfolder)] for every record whose file_path is a local path."""
    out = []
    for table, sub in TABLES:
        cur.execute(f"SELECT id, file_path FROM {table} "
                    f" WHERE coalesce(file_path, '') <> '' AND file_path NOT ILIKE 'http%%' ORDER BY id")
        out += [(table, rid, path, sub) for rid, path in cur.fetchall()]
    return out


def main():
    if not os.environ.get("DATABASE_URL"):
        print("upload_local_files_to_drive needs DATABASE_URL (DB mode). Nothing to do.")
        return
    commit = "--commit" in sys.argv
    conn = database.connect()
    try:
        with conn.cursor() as cur:
            todo = local_records(cur)
        present = [t for t in todo if os.path.exists(t[2])]
        missing = [t for t in todo if not os.path.exists(t[2])]
        files = {os.path.normcase(os.path.abspath(t[2])) for t in present}
        size = sum(os.path.getsize(p) for p in files)
        by_table = {}
        for t in present:
            by_table[t[0]] = by_table.get(t[0], 0) + 1
        print(f"{len(todo)} record(s) link to a file on this PC instead of Drive: "
              + ", ".join(f"{n} {tbl.replace('_', ' ')}" for tbl, n in by_table.items())
              + f" — {len(files)} distinct file(s), {size / 1e6:.0f} MB.")
        if missing:
            print(f"{len(missing)} of them point at a file that no longer exists (left as they are):")
            for table, rid, path, _ in missing[:10]:
                print(f"    {table} #{rid}: {path}")
        if not present:
            print("Nothing to upload.")
            return
        if not commit:
            for table, rid, path, sub in present[:5]:
                print(f"    would upload {table} #{rid} -> Drive {sub}/: {os.path.basename(path)}")
            print("\nDry run — nothing changed. Re-run with --commit to upload them and switch the links.")
            return

        from drive_uploader import DriveUploader
        uploader = DriveUploader()
        links, failed, switched, skipped = {}, [], 0, 0
        for i, (table, rid, path, sub) in enumerate(present, 1):
            key = os.path.normcase(os.path.abspath(path))
            if key not in links:
                try:
                    links[key] = uploader.upload(sub, path)
                except Exception as e:
                    links[key] = None
                    failed.append((table, rid, path, e))
                    print(f"  FAILED {table} #{rid} ({os.path.basename(path)}): {e}")
            link = links[key]
            if not link:
                continue
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {table} SET file_path = %s WHERE id = %s AND file_path = %s",
                            (link, rid, path))
                if cur.rowcount == 1:
                    cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                                "changed_by) VALUES (%s, %s, 'file_path', %s, %s, %s)",
                                (table, rid, path, link, ACTOR))
                    switched += 1
                else:
                    skipped += 1                  # the record changed meanwhile — left alone
            conn.commit()
            if i % 25 == 0:
                print(f"  ... {i}/{len(present)} done")
        print(f"\n--commit: switched {switched} record(s) to their Drive copy "
              f"({sum(1 for v in links.values() if v)} file(s) uploaded)"
              + (f"; {skipped} changed meanwhile and were left alone" if skipped else "")
              + (f"; {len(failed)} failed — re-run to retry them" if failed else "") + ".")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
