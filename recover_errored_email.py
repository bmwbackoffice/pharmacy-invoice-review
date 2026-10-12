"""
Proper recovery path for an email stuck at status='error'.

Replaces the ad-hoc `process_source_email` calls that recovered EuroPharma INV0800123 three times
WITHOUT ever completing the save pipeline (no invoices row, no emails-status update, orphaned
invoice_history). This tool instead REQUEUES the email so the NEXT `classify_emails.py` run
reprocesses it through the exact same pipeline a normal success uses (classify -> save_results ->
_upsert_email), which cannot silently produce a history-only/PDF-only orphan.

For each target email it:
  1) moves its source file back to test_emails/ (from processed_emails/ or review_pending/),
  2) sets emails.status='fetched' so the run picks it up (and the ledger isn't stuck on 'error'),
  3) DELETES any ORPHANED invoice_history/payment_history row for it (one with no live record) so the
     reprocess isn't shadowed by the dedup check and can record cleanly. (A ledger row WITH a live
     record is never touched.)

Additive: changes nothing in the CLI pipeline. Defaults to --dry-run; acts only with --commit.

    python recover_errored_email.py                      # dry-run: every status='error' email
    python recover_errored_email.py "<email filename>"   # dry-run: just that one
    python recover_errored_email.py --commit             # requeue all error emails
    python recover_errored_email.py "<file>" --commit    # requeue that one
    python recover_errored_email.py --orphaned --commit  # requeue every email with an orphaned ledger row
    python recover_errored_email.py --from-file=list.txt --commit   # requeue a newline-delimited list
    then:  python classify_emails.py                     # reprocess through the FULL pipeline
"""

import shutil
import sys
from pathlib import Path

import psycopg2.extras

import database

BASE_DIR = Path(__file__).parent
TEST_EMAILS = BASE_DIR / "test_emails"
SEARCH_DIRS = ["test_emails", "processed_emails", "review_pending", "review_pending/manual"]


def _locate(fn):
    for d in SEARCH_DIRS:
        p = BASE_DIR / d / fn
        if p.exists():
            return p
    return None


def main():
    flags = {"--commit", "--orphaned"}
    excludes = {a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--exclude=")}
    # --from-file=PATH: read newline-delimited filenames (blank lines / leading-# comments ignored)
    # and treat them exactly like explicit name arguments. For large recovery batches that would
    # otherwise overflow the command line (e.g. the scanner-backlog list).
    from_files = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--from-file=")]
    file_names = []
    for fp in from_files:
        for line in Path(fp).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                file_names.append(line)
    names = [a for a in sys.argv[1:]
             if a not in flags and not a.startswith("--exclude=") and not a.startswith("--from-file=")]
    names = names + file_names
    commit = "--commit" in sys.argv[1:]
    orphaned = "--orphaned" in sys.argv[1:]
    mode = "COMMIT" if commit else "DRY-RUN"
    # Target: explicit filename(s) (any status) | --orphaned (every email with an orphaned ledger
    # row, status error/fetched — the /reconcile backlog) | default (status='error').
    label = (f"{len(names)} named" if names else "--orphaned" if orphaned else "status=error")
    print(f"=== recover_errored_email.py [{mode}] — {label} ===\n")

    # Ensure the schema helpers (norm_vendor_dedupe, the view) exist — on its own connection, BEFORE
    # we open the transaction below (init_db takes ACCESS-EXCLUSIVE locks; running it inside an open
    # txn would deadlock). Idempotent / fast on an existing DB.
    database.init_db()

    # SQL fragment: emails whose ledger rows (invoice_history / payment_history) have no live record.
    ORPHAN_EMAILS = (
        "SELECT DISTINCT h.source_file AS filename FROM invoice_history h "
        " WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS ("
        "   SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number) "
        "     AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched)) "
        "UNION "
        "SELECT DISTINCT h.source_file FROM payment_history h "
        " WHERE coalesce(h.reference_number,'') <> '' AND NOT EXISTS ("
        "   SELECT 1 FROM payments p WHERE lower(p.reference_number)=lower(h.reference_number) "
        "     AND norm_vendor_dedupe(p.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))")

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if names:
                cur.execute("SELECT filename FROM emails WHERE filename = ANY(%s)", (names,))
                found = {r["filename"] for r in cur.fetchall()}
                for n in names:
                    if n not in found:
                        print(f"  (skipping {n!r} — not in the emails table)")
                targets = sorted(found)
            elif orphaned:
                cur.execute(f"SELECT e.filename FROM emails e WHERE e.filename IN ({ORPHAN_EMAILS}) "
                            f"ORDER BY e.filename")
                targets = [r["filename"] for r in cur.fetchall()]
            else:
                cur.execute("SELECT filename FROM emails WHERE status = 'error' ORDER BY filename")
                targets = [r["filename"] for r in cur.fetchall()]
            if excludes:
                before = len(targets)
                targets = [t for t in targets if t not in excludes]
                print(f"(excluded {before - len(targets)} email(s) via --exclude)\n")
            if not targets:
                print("No matching email(s) to recover. Done.")
                return
            print(f"{len(targets)} email(s) to requeue:\n")

            TEST_EMAILS.mkdir(exist_ok=True)
            moved, missing, hist_cleared = [], [], 0
            for fn in targets:
                src = _locate(fn)
                where = str(src.parent.name) if src else "NOT FOUND"
                # Orphaned ledger rows for this email (no live record, matched like /reconcile).
                _ORPH_INV = (
                    "FROM invoice_history h WHERE h.source_file = %s AND coalesce(h.invoice_number,'')<>'' "
                    "AND NOT EXISTS (SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number) "
                    "  AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))")
                _ORPH_PAY = (
                    "FROM payment_history h WHERE h.source_file = %s AND coalesce(h.reference_number,'')<>'' "
                    "AND NOT EXISTS (SELECT 1 FROM payments p WHERE lower(p.reference_number)=lower(h.reference_number) "
                    "  AND norm_vendor_dedupe(p.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))")
                cur.execute(f"SELECT count(*) c {_ORPH_INV}", (fn,))
                oi = cur.fetchone()["c"]
                cur.execute(f"SELECT count(*) c {_ORPH_PAY}", (fn,))
                op = cur.fetchone()["c"]
                print(f"  {fn}")
                print(f"      file: [{where}]  orphaned ledger rows: {oi} invoice, {op} payment")

                if not commit:
                    continue
                if src and src.name:
                    dst = TEST_EMAILS / src.name
                    if src.resolve() != dst.resolve():
                        shutil.move(str(src), str(dst))
                    moved.append(fn)
                else:
                    missing.append(fn)
                cur.execute("UPDATE emails SET status = 'fetched', error = NULL, updated_at = now() "
                            "WHERE filename = %s", (fn,))
                # Delete the orphaned ledger rows so the reprocess records cleanly (no live record to lose).
                cur.execute(f"DELETE {_ORPH_INV}", (fn,)); hist_cleared += cur.rowcount
                cur.execute(f"DELETE {_ORPH_PAY}", (fn,)); hist_cleared += cur.rowcount

            if not commit:
                print("\nDRY-RUN — no changes. Re-run with --commit to requeue, then run classify_emails.py.")
                return

        conn.commit()
        print("\n=== SUMMARY ===")
        print(f"  files moved to test_emails/: {len(moved)}")
        print(f"  files missing (set to 'fetched' anyway): {len(missing)}")
        for m in missing:
            print(f"      missing file: {m}")
        print(f"  orphaned ledger rows cleared: {hist_cleared}")
        print("\nNext:  python classify_emails.py   (reprocesses through the FULL save pipeline)")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back DB changes (any files already moved stay moved; rerun is safe).")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
