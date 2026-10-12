"""
Read-only audit: find vendor_name_map rows whose 'aliases' field would be
affected by changing the alias parser from splitting on [newline OR comma]
to splitting on newline only (the fix for the EnlivenHealth (FDS, Inc.)
bug, where a comma INSIDE one alias breaks it into two garbage fragments).

For every row with at least one comma inside a line, prints:
  - the standard vendor name
  - the raw line containing the comma
  - what the OLD parser would have produced from that line (comma-split)
  - what the NEW parser (newline-only) would produce (the line as one alias)

Nothing is written to the database. Run with the same Python environment /
DATABASE_URL the web app itself uses, e.g.:
    python audit_vendor_alias_commas.py
"""
import re
import database

conn = database.connect()
flagged = []
with conn.cursor() as cur:
    cur.execute("SELECT standard_vendor_name, aliases FROM vendor_name_map "
                "WHERE aliases IS NOT NULL AND aliases <> '' ORDER BY lower(standard_vendor_name)")
    rows = cur.fetchall()
conn.close()

for std, aliases in rows:
    for line in (aliases or "").splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        old_fragments = [f.strip() for f in re.split(r"[\n,]+", line) if f.strip()]
        flagged.append((std, line, old_fragments))

print(f"Checked {len(rows)} vendor_name_map rows. {len(flagged)} line(s) contain a comma:\n")
for std, line, old_fragments in flagged:
    print(f"- Standard vendor: {std}")
    print(f"    Line as stored:        {line!r}")
    print(f"    OLD parser would split it into: {old_fragments}")
    print(f"    NEW parser keeps it as one alias: {line!r}")
    print()

if not flagged:
    print("No comma-containing alias lines found — the newline-only fix is safe with no follow-up needed.")
