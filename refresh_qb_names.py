"""
ONE-OFF (idempotent, safe to re-run) -- catches up records that kept an OLD QuickBooks vendor /
account name after a store spelling was edited in Setup (Vendor Names / Account Names).
Going forward this happens automatically whenever a store spelling is edited (web_db.edit_reference)
or corrected from a QB reconciliation upload. Only records NOT yet entered in QB are touched;
every change is logged to edit_log.

    python refresh_qb_names.py            # report only (dry run)
    python refresh_qb_names.py --commit   # write
"""
import sys
import web_db


def main():
    commit = "--commit" in sys.argv[1:]
    print(f"=== refresh_qb_names [{'COMMIT' if commit else 'DRY-RUN'}] ===\n")
    conn = web_db.database.connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for map_table, label in (("vendor_name_map", "QB Vendor"), ("account_name_map", "QB Account")):
                changes = web_db.refresh_qb_names(cur, map_table, "one-time QB name refresh",
                                                  dry_run=not commit)
                print(f"{label}: {len(changes)} record(s) {'updated' if commit else 'would change'}")
                for table, rid, store, old, new in changes:
                    print(f"  {table} #{rid} @ {store}: {old!r} -> {new!r}")
                print()
        conn.commit() if commit else conn.rollback()
    finally:
        conn.close()
    if not commit:
        print("DRY-RUN -- nothing written. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
