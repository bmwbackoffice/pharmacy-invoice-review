"""
Re-read records still waiting for QuickBooks entry that are missing a store, date or amount, so the
classifier processes their emails again under TODAY's rules (QB-10). These are the records the
"missing a store, date or amount" banner counts on QB entry and Payments (web_db._missing_info_sql):
not entered in QB, not skip-QB, not an ignored duplicate. Most were saved before the store / date /
amount checks existed, or by an older reading. On the rerun each one either comes back complete, or is
flagged and goes to the Review queue with the reason, like a new email.

    python requeue_missing_info.py              # dry run: list what would be re-read
    python requeue_missing_info.py --commit     # back up, move each email back, status 'fetched'
    then: the next scheduled run (every 30 min) re-reads them, or click "Apply changes now"

Requeues like requeue_by_vendor.py: moves each source email back to test_emails/ and sets
emails.status='fetched'. The rerun deletes that email's old rows before saving (save_results), so
nothing is duplicated, and a record that now goes to review leaves QB entry / Payments.

A record saved from a review split file that's gone (its email_filename is the split PDF) is traced
to its original email by the Gmail message id in its link. --commit points the record at that email
(logged to edit_log), so the rerun replaces it instead of adding a second copy.

Left alone and listed:
  - a record whose source email isn't on disk;
  - every email with a record a person acted on (edited a field, ignored it by hand, or made a
    duplicate decision): a rerun re-reads every record of the email and would undo that. Records
    entered in QB are fine: a rerun keeps "entered" (LC-18).
The rerun itself also skips any email a person touched in the last 10 minutes.

--commit backs up every row of the affected emails to <table>_requeue_backup_<timestamp> first.
Nothing is deleted. DB mode only (needs DATABASE_URL).
"""
import datetime
import os
import re
import shutil
import sys

import psycopg2.extras

import database
import requeue_by_vendor as rbv
import web_db

ACTOR = "requeue_missing_info"
# table, number column, date column, has the duplicate columns
_TABLES = (("invoices", "invoice_number", "invoice_date", True),
           ("credit_memos", "credit_memo_number", "credit_memo_date", False),
           ("payments", "reference_number", "payment_date", True))
_GMAIL_ID = re.compile(r"#[^/]+/([0-9a-fA-F]+)")


def find_targets(cur):
    """Records waiting for QuickBooks entry that lack a store, date or amount: the same rows the QB
    entry / Payments banner counts (web_db.missing_info_counts)."""
    out = []
    for table, num, datecol, has_dup in _TABLES:
        dup = " AND r.duplicate_ignored IS NOT TRUE" if has_dup else ""
        miss = web_db._missing_info_sql(datecol)
        cur.execute(f"SELECT r.id, r.email_filename, r.gmail_link, r.vendor_matched, r.store_matched, "
                    f"r.{num} AS number, r.amount, r.{datecol} AS record_date, {miss} AS missing "
                    f"FROM {table} r WHERE r.entered_in_qb IS NOT TRUE AND r.skip_qb_import IS NOT TRUE"
                    f"{dup} AND {miss} <> '' ORDER BY r.id")
        out += [{"table": table, **r} for r in cur.fetchall()]
    return out


def source_email(cur, rec):
    """(filename, path) of the email to re-read for `rec`; path is None when it isn't on disk.
    Normally the record's own email_filename. A record saved from a review split file that's gone is
    traced to its original email by the Gmail message id in its link (exactly one email row)."""
    fn = rec["email_filename"]
    path = rbv._find_source_eml(fn) if fn else None
    if path:
        return fn, path
    m = _GMAIL_ID.search(rec.get("gmail_link") or "")
    if m:
        cur.execute("SELECT filename FROM emails WHERE gmail_message_id = %s AND filename <> %s",
                    (m.group(1), fn or ""))
        on_disk = [(r["filename"], rbv._find_source_eml(r["filename"])) for r in cur.fetchall()]
        on_disk = [(n, p) for n, p in on_disk if p]
        if len(on_disk) == 1:
            return on_disk[0]
    return fn, None


def _records_filed_under(cur, filenames):
    """Every record filed under these email filenames, all three tables."""
    out = {}
    for table, num, datecol, has_dup in _TABLES:
        dup = "duplicate_ignored_by" if has_dup else "NULL AS duplicate_ignored_by"
        cur.execute(f"SELECT id, email_filename, vendor_matched, store_matched, {num} AS number, amount, "
                    f"{datecol} AS record_date, entered_in_qb, edited_by, ignored_by, {dup} "
                    f"FROM {table} WHERE email_filename = ANY(%s) ORDER BY id", (list(filenames),))
        for r in cur.fetchall():
            out.setdefault(r["email_filename"], []).append({"table": table, **r})
    return out


def acted_on(rec):
    """What a person did to this record that a rerun would undo (empty list = nothing)."""
    why = []
    if (rec.get("edited_by") or "").strip():
        why.append(f"edited by {rec['edited_by']}")
    if (rec.get("ignored_by") or "").strip():
        why.append(f"ignored by {rec['ignored_by']}")
    if (rec.get("duplicate_ignored_by") or "").strip():
        why.append(f"duplicate decision by {rec['duplicate_ignored_by']}")
    return why


def plan(cur):
    """({email filename: {path, targets, repoint, records, held}}, [targets not on disk]).
    `records` is every record the rerun re-reads (the targets plus anything else from the email);
    `held` lists what a person did to any of them. An email with `held` entries isn't requeued."""
    emails, not_found = {}, []
    for rec in find_targets(cur):
        fn, path = source_email(cur, rec)
        if not path:
            not_found.append(rec)
            continue
        e = emails.setdefault(fn, {"path": path, "targets": [], "repoint": []})
        e["targets"].append(rec)
        if rec["email_filename"] != fn:
            e["repoint"].append(rec)
    filed = _records_filed_under(cur, set(emails) | {r["email_filename"] for e in emails.values()
                                                    for r in e["repoint"]})
    for fn, e in emails.items():
        records = list(filed.get(fn, []))
        for r in e["repoint"]:
            records += [x for x in filed.get(r["email_filename"], [])
                        if x["table"] == r["table"] and x["id"] == r["id"]]
        e["records"] = records
        e["held"] = [f"{x['table'][:-1].replace('_', ' ')} #{x['id']}: {why}"
                     for x in records for why in acted_on(x)]
    return emails, not_found


def commit(cur, emails, ts):
    """Back up, re-point, set status 'fetched' and move each email back to test_emails/.
    Returns (files moved, email rows set to 'fetched')."""
    keys = sorted(set(emails) | {r["email_filename"] for e in emails.values() for r in e["repoint"]})
    for table, *_ in _TABLES:
        backup = f"{table}_requeue_backup_{ts}"
        cur.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM {table} WHERE email_filename = ANY(%s)',
                    (keys,))
        if not cur.rowcount:
            cur.execute(f'DROP TABLE "{backup}"')       # nothing to keep; no empty table left behind
    for fn, e in emails.items():
        for r in e["repoint"]:
            cur.execute(f"UPDATE {r['table']} SET email_filename = %s WHERE id = %s AND email_filename = %s",
                        (fn, r["id"], r["email_filename"]))
            if cur.rowcount != 1:
                raise RuntimeError(f"{r['table']} #{r['id']} changed while this ran; nothing saved, run it again.")
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES (%s, %s, 'email_filename', %s, %s, %s)",
                        (r["table"], r["id"], r["email_filename"], fn, ACTOR))
    cur.execute("UPDATE emails SET status = 'fetched' WHERE filename = ANY(%s)", (list(emails),))
    n_status = cur.rowcount
    rbv.TEST_EMAILS.mkdir(exist_ok=True)
    moved = 0
    for fn, e in emails.items():
        if e["path"].parent != rbv.TEST_EMAILS:
            shutil.move(str(e["path"]), str(rbv.TEST_EMAILS / fn))
        moved += 1
    return moved, n_status


def _line(rec):
    day = rec.get("record_date") or "(no date)"
    store = rec.get("store_matched") or "(no store)"
    return (f"{rec['table'][:-1].replace('_', ' '):<11} #{rec['id']:<5} {rec.get('vendor_matched') or '-'} / "
            f"{store} / #{rec.get('number') or '-'} / {rbv._money(rec.get('amount'))} / {day}")


def report(emails, not_found, n_targets):
    ready = {fn: e for fn, e in emails.items() if not e["held"]}
    print(f"{n_targets} record(s) waiting for QuickBooks entry are missing a store, date or amount.\n")
    for fn, e in emails.items():
        tag = "HELD" if e["held"] else f"{e['path'].parent.name}/"
        print(f"[{tag}] {fn}")
        target_ids = {(t["table"], t["id"]) for t in e["targets"]}
        missing = {(t["table"], t["id"]): t["missing"] for t in e["targets"]}
        for r in e["records"]:
            key = (r["table"], r["id"])
            note = f"   <- missing {missing[key]}" if key in target_ids else "   (also re-read; complete)"
            if r["entered_in_qb"]:
                note += " [entered in QB: kept]"
            print(f"    {_line(r)}{note}")
        for r in e["repoint"]:
            print(f"    #{r['id']} was saved from a split file that's gone ({r['email_filename']}); "
                  f"--commit points it at this email so the rerun replaces it.")
        for h in e["held"]:
            print(f"    NOT requeued: {h}. A rerun would undo that; fix this one by hand.")
        print()
    if not_found:
        print(f"{len(not_found)} record(s) whose source email isn't on disk (left alone, fix by hand):")
        for r in not_found:
            print(f"    {_line(r)}   ({r['email_filename']})")
        print()
    return ready


def main():
    do_commit = "--commit" in sys.argv[1:]
    if not os.environ.get("DATABASE_URL"):
        print("requeue_missing_info needs DATABASE_URL (DB mode). Nothing to do.")
        return
    print(f"=== requeue_missing_info [{'COMMIT' if do_commit else 'DRY RUN'}] ===\n")
    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            emails, not_found = plan(cur)
            n_targets = sum(len(e["targets"]) for e in emails.values()) + len(not_found)
            if not n_targets:
                print("Nothing is missing a store, date or amount. Nothing to do.")
                conn.rollback()
                return
            ready = report(emails, not_found, n_targets)
            n_ready = sum(len(e["targets"]) for e in ready.values())
            if not ready:
                print("No email can be requeued. Nothing changed.")
                conn.rollback()
                return
            if not do_commit:
                print(f"DRY RUN: nothing changed. --commit requeues {len(ready)} email(s) for "
                      f"{n_ready} of the {n_targets} record(s): backs up their rows, points any split-file "
                      f"record at its email, moves each email to test_emails/ and sets its status to "
                      f"'fetched'.")
                conn.rollback()
                return
            moved, n_status = commit(cur, ready, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        conn.commit()
        print(f"Requeued {moved} email(s) for {n_ready} record(s); {n_status} email row(s) set to 'fetched'. "
              f"Backups: <table>_requeue_backup_<timestamp>.")
        print("Next: the scheduled run (every 30 min) re-reads them, or click \"Apply changes now\" on the "
              "dashboard or the Review queue. Each record then comes back complete or goes to the Review "
              "queue with the reason.")
    except Exception:
        conn.rollback()
        print("\nERROR: rolled back the database changes (any email already moved stays in test_emails/; "
              "running this again is safe).")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
