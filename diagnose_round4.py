"""
Round 4, read-only, final check. Round 3 showed BOTH Lantev flags trace to the SAME
supposed prior: "MDS400 918 INVOICES - 1a0ca5f1d84c583c.eml" (via check_duplicate's
'store_preferred' path), but find_live_invoice() couldn't locate it as a live invoice
(the reason text lacks the "already entered in QuickBooks" suffix that would print if
it HAD been found-but-entered) -- and round 2 already showed invoice_history has ZERO
rows for Lantev #9IB1317/#9IB1318 right now, even though check_duplicate() can only
return a non-'none' status by matching a history row that existed AT THE TIME.

Working theory: invoice_history is fully rebuilt (DELETE + reinsert) each classify run
(database.save_history()), so a history row used to set a flag in one run can vanish by
the time of a later run's rebuild if that run's fresh history list didn't include it --
leaving the flag permanently orphaned with nothing left in the system to show what it
was actually flagged against.

This checks: does ANY invoice row -- live, ignored, or entered, any vendor/number --
still exist with email_filename = 'MDS400 918 INVOICES - 1a0ca5f1d84c583c.eml'? If not,
that source email's records are completely gone from `invoices`, which would confirm
there's nothing left anywhere in the system these two flags could be resolved against.

Run with the same Python env / DATABASE_URL as the web app:
    python diagnose_round4.py
"""
import database

conn = database.connect()
cur = conn.cursor()

SRC = "MDS400 918 INVOICES - 1a0ca5f1d84c583c.eml"

print("=" * 70)
print(f"Any invoices row with email_filename = {SRC!r} ?")
print("=" * 70)
cur.execute("""
    SELECT id, vendor_matched, invoice_number, amount, store_matched,
           duplicate_suspect, duplicate_ignored, entered_in_qb
    FROM invoices WHERE email_filename = %s
""", (SRC,))
rows = cur.fetchall()
if not rows:
    print("NONE -- no invoice row (live, ignored, or entered) traces to that source email at all.")
else:
    for r in rows:
        print(r)

print()
print("=" * 70)
print("Any emails row for that filename at all (was it ever even fetched)?")
print("=" * 70)
cur.execute("SELECT filename, status, error, run_id, updated_at FROM emails WHERE filename = %s", (SRC,))
r = cur.fetchone()
print(r if r else "NO emails row either -- this filename doesn't exist anywhere in the current DB.")

print()
print("=" * 70)
print("Any invoice_history rows at all with this source_file (any vendor/number)?")
print("=" * 70)
cur.execute("SELECT vendor_matched, invoice_number, invoice_amount FROM invoice_history WHERE source_file = %s", (SRC,))
hist = cur.fetchall()
print(hist if hist else "NONE.")

conn.close()
