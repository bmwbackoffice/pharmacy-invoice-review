"""Settle open duplicate sets by how many times the bill is in QuickBooks (dup_settle -- the same thing
every classify run and every QuickBooks export upload now does on its own).

  in QuickBooks once  -> keep the entered copy, ignore the rest (resolved)
  in QuickBooks twice -> marked "entered twice" at the top of the Duplicates page
  anything else (amount / store / vendor differs, nothing entered yet) -> left for a person

    python resolve_entered_duplicates.py            # report only
    python resolve_entered_duplicates.py --commit   # do it
"""
import sys
import dup_settle

commit = "--commit" in sys.argv[1:]
print(f"=== {'COMMIT' if commit else 'DRY-RUN'} ===")
res = dup_settle.settle_duplicate_sets(dry_run=not commit, verbose=True)
print(f"\n{'Resolved' if commit else 'Would resolve'}: {len(res['resolved'])} set(s); "
      f"entered twice in QuickBooks: {len(res['twice'])}; left for a person: {len(res['left'])}")
for gid, label, why in res["left"]:
    print(f"  left: {label}: {why}")
if not commit:
    print("\nDRY-RUN -- nothing changed. Re-run with --commit.")
