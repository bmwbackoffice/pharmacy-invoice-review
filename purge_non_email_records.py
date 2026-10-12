"""
ONE-OFF MAINTENANCE — purge or archive old-system records that never came from a real
email (the migrate_from_excel.py Excel imports), so every remaining record traces to an
email. Real emails (Gmail-fetched, or dropped .eml processed by the fetcher) carry receipt
metadata; the migrated stubs do not.

The RECOMMENDED discriminator is a Gmail-flow go-live **date cutoff** (`--before DATE`), because
in practice "no Gmail metadata" also matches legitimate post-go-live manual `.msg`/`.eml` drops and
reprocessed review split-file records. --before targets ONLY records recorded before the cutoff that
ALSO lack any email/run metadata (belt-and-suspenders):
    date_added < DATE  AND  the email has NO gmail_message_id AND NO run_id.
(The legacy fingerprint — received_at & gmail_message_id both NULL — is kept for back-compat but is
NOT recommended; run the dry-run and eyeball the sample either way.)

    python purge_non_email_records.py --before 2026-08-06 --dry-run   # DRY-RUN (default): per-table counts + sample
    python purge_non_email_records.py --before 2026-08-06 --delete     # delete rows + ledger rows + orphaned stubs
    python purge_non_email_records.py --before 2026-08-06 --archive     # flag legacy_migrated + hide from email views
    python purge_non_email_records.py                                   # (no --before) legacy fingerprint dry-run

Require an explicit --delete or --archive; the dry-run alone changes nothing. Confirm the old Excel
(output/results.xlsx) is retained as the archive before --delete (which is NOT reversible; --archive is).
Standalone; uses DATABASE_URL; does NOT touch the classifier or normal runs.
"""

import sys

import psycopg2.extras

import database

REC_TABLES = ["invoices", "credit_memos", "payments"]
HIST_TABLES = ["invoice_history", "payment_history"]
_NUMCOL = {"invoices": "invoice_number", "credit_memos": "credit_memo_number",
           "payments": "reference_number"}

# Legacy fingerprint (no --before): email joins to a receipt-less stub, or has no email row.
_REC_FINGERPRINT = ("LEFT JOIN emails e ON e.filename = r.email_filename "
                    "WHERE e.filename IS NULL OR (e.received_at IS NULL AND e.gmail_message_id IS NULL)")
_HIST_FINGERPRINT = ("LEFT JOIN emails e ON e.filename = h.source_file "
                     "WHERE e.filename IS NULL OR (e.received_at IS NULL AND e.gmail_message_id IS NULL)")


def _rec_where(before):
    """(sql_after_table, params) for the record match, per mode. `r` is the record alias."""
    if before:
        return ("LEFT JOIN emails e ON e.filename = r.email_filename "
                "WHERE r.date_added < %s AND e.gmail_message_id IS NULL AND e.run_id IS NULL"), [before]
    return _REC_FINGERPRINT, []


def _hist_where(before):
    if before:
        return ("LEFT JOIN emails e ON e.filename = h.source_file "
                "WHERE h.date_added < %s AND e.gmail_message_id IS NULL AND e.run_id IS NULL"), [before]
    return _HIST_FINGERPRINT, []


def _orphan_emails_sql(before):
    """(DELETE sql, params) for stub emails with no remaining references. In --before mode
    the stub must ALSO be dated before the cutoff (so recent manual/unprocessed emails are
    never touched); legacy mode uses the received_at/gmail fingerprint."""
    if before:
        stub, params = "e.gmail_message_id IS NULL AND e.run_id IS NULL AND e.date_added < %s", [before]
    else:
        stub, params = "e.received_at IS NULL AND e.gmail_message_id IS NULL", []
    sql = (f"DELETE FROM emails e WHERE {stub} "
           "AND NOT EXISTS (SELECT 1 FROM invoices     x WHERE x.email_filename = e.filename) "
           "AND NOT EXISTS (SELECT 1 FROM credit_memos x WHERE x.email_filename = e.filename) "
           "AND NOT EXISTS (SELECT 1 FROM payments     x WHERE x.email_filename = e.filename) "
           "AND NOT EXISTS (SELECT 1 FROM review_queue x WHERE x.source_email  = e.filename)")
    return sql, params


def _rec_ids(cur, table, before):
    where, params = _rec_where(before)
    cur.execute(f"SELECT r.id FROM {table} r {where} ORDER BY r.id", params)
    return [row[0] for row in cur.fetchall()]


def _hist_ids(cur, table, before):
    where, params = _hist_where(before)
    cur.execute(f"SELECT h.id FROM {table} h {where} ORDER BY h.id", params)
    return [row[0] for row in cur.fetchall()]


def dry_run(before):
    rwhere, rparams = _rec_where(before)
    hwhere, hparams = _hist_where(before)
    conn = database.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            grand = 0
            for t in REC_TABLES:
                cur.execute(f"SELECT count(*) AS n FROM {t} r {rwhere}", rparams)
                n = cur.fetchone()["n"]; grand += n
                print(f"\n{t}: {n} old-system record(s)")
                if n:
                    cur.execute(f"SELECT r.vendor_matched, r.{_NUMCOL[t]} AS number, r.amount, "
                                f"r.date_added, r.email_filename FROM {t} r {rwhere} "
                                f"ORDER BY r.id LIMIT 5", rparams)
                    for s in cur.fetchall():
                        print(f"    {s['vendor_matched']!r}  #{s['number']}  {s['amount']}  "
                              f"added={s['date_added']}  {s['email_filename']!r}")
            for t in HIST_TABLES:
                cur.execute(f"SELECT count(*) AS n FROM {t} h {hwhere}", hparams)
                print(f"\n{t}: {cur.fetchone()['n']} old ledger row(s)")
            print(f"\nTOTAL old-system records: {grand}")
    finally:
        conn.close()
    print("\nDRY-RUN — no changes. Eyeball the sample; then re-run with --delete "
          "(output/results.xlsx kept as the archive) or --archive.")


def _purge(cur, before):
    """Delete old-system records + ledger rows + orphaned stub emails. Returns {table: n}."""
    removed = {}
    for t in REC_TABLES:
        ids = _rec_ids(cur, t, before)
        if ids:
            cur.execute(f"DELETE FROM {t} WHERE id = ANY(%s)", (ids,))
        removed[t] = len(ids)
    for t in HIST_TABLES:
        ids = _hist_ids(cur, t, before)
        if ids:
            cur.execute(f"DELETE FROM {t} WHERE id = ANY(%s)", (ids,))
        removed[t] = len(ids)
    osql, oparams = _orphan_emails_sql(before)
    cur.execute(osql, oparams)
    removed["emails (stubs)"] = cur.rowcount
    return removed


def _archive(cur, before):
    """Flag legacy_migrated=TRUE on old-system records + their stub emails. Reversible."""
    for t in REC_TABLES + ["emails"]:
        cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS legacy_migrated BOOLEAN DEFAULT FALSE")
    flagged = {}
    for t in REC_TABLES:
        ids = _rec_ids(cur, t, before)
        if ids:
            cur.execute(f"UPDATE {t} SET legacy_migrated = TRUE WHERE id = ANY(%s)", (ids,))
        flagged[t] = len(ids)
    # Flag the matching stub emails. In --before mode the stub must also be dated before the
    # cutoff, so recent manual/unprocessed emails are never flagged.
    if before:
        cur.execute("UPDATE emails SET legacy_migrated = TRUE WHERE gmail_message_id IS NULL "
                    "AND run_id IS NULL AND date_added < %s AND legacy_migrated IS NOT TRUE", (before,))
    else:
        cur.execute("UPDATE emails SET legacy_migrated = TRUE WHERE received_at IS NULL "
                    "AND gmail_message_id IS NULL AND legacy_migrated IS NOT TRUE")
    flagged["emails (stubs)"] = cur.rowcount
    return flagged


def _run(action_fn, label, before):
    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            counts = action_fn(cur, before)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"=== purge_non_email_records [{label}{'  before ' + before if before else ''}] ===")
    for k, v in counts.items():
        print(f"  {k:20} {v:6}")
    print(f"\n{label} complete.")


def _arg_value(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise SystemExit(f"{name} needs a value, e.g.  {name} 2026-08-06")
        return args[i + 1]
    return None


def main():
    args = sys.argv[1:]
    if not database.is_configured():
        raise SystemExit("DATABASE_URL is not set — cannot reach the database.")
    before = _arg_value(args, "--before")        # 'YYYY-MM-DD' or None (legacy fingerprint)
    if before and (len(before) != 10 or before[4] != "-" or before[7] != "-"):
        raise SystemExit("--before expects a date as YYYY-MM-DD, e.g. --before 2026-08-06")
    if "--delete" in args and "--archive" in args:
        raise SystemExit("Choose one: --delete OR --archive.")
    if "--delete" in args:
        _run(_purge, "DELETE", before)
        print("Deleted old-system records, ledger rows, and orphaned stub emails. "
              "output/results.xlsx remains your archive of the original import.")
    elif "--archive" in args:
        _run(_archive, "ARCHIVE", before)
        print("Flagged legacy_migrated=TRUE — kept but hidden from the Records list, the Emails "
              "ledger, and the ledger count. Reversible (set legacy_migrated=FALSE).")
    else:
        mode = f"before {before}" if before else "legacy fingerprint"
        print(f"=== purge_non_email_records [DRY-RUN · {mode}] ===")
        dry_run(before)


if __name__ == "__main__":
    main()
