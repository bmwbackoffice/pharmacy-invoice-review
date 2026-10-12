"""
Why does TopRx (a "missing invoices" statement exception) show in the Review
queue if TopRx is excluded from QuickBooks? Checking its actual vendor_name_map
flags -- exclude_from_qb and exclude_from_statement_recon are independent
switches (see database.py's unified-exclusion comment), so this checks which
one(s) are actually set for TopRx, and whether the statement line's own vendor
text even matches a known spelling. Read-only.
"""
import database

conn = database.connect()
cur = conn.cursor()

print("=== vendor_name_map rows matching 'TopRx' (name or alias) ===")
cur.execute("""
    SELECT id, standard_vendor_name, aliases, exclude_from_qb, exclude_from_statement_recon
    FROM vendor_name_map
    WHERE standard_vendor_name ILIKE %s OR aliases ILIKE %s
""", ('%toprx%', '%toprx%'))
for r in cur.fetchall():
    print("   ", r)

print("\n=== vendor_exclusion_spellings view, TopRx-like rows ===")
cur.execute("""
    SELECT name, exclude_from_qb, exclude_from_statement_recon
    FROM vendor_exclusion_spellings
    WHERE name ILIKE %s
""", ('%toprx%',))
for r in cur.fetchall():
    print("   ", r)

print("\n=== Raw vendor text on the open statement_lines rows for TopRx ===")
cur.execute("""
    SELECT DISTINCT vendor, status, count(*) OVER (PARTITION BY vendor, status)
    FROM statement_lines
    WHERE vendor ILIKE %s
""", ('%toprx%',))
for r in cur.fetchall():
    print("   ", r)

cur.close()
conn.close()
