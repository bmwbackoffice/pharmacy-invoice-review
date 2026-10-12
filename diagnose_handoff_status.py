"""READ-ONLY. Final handoff check: confirms the vendor-data fixes (which don't show up in a code
grep) actually landed -- the PAS Packaging/Pharmacy Automation merge, the Abbott/Abbot Nutrition
merge, and whether the vendor-name-correction backfill (apply_vendor_name_corrections.py) has
been run yet."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print("=== PAS Packaging / Pharmacy Automation ===")
        cur.execute("""SELECT id, standard_vendor_name, aliases, name_madison FROM vendor_name_map
                       WHERE standard_vendor_name ILIKE '%%PAS PACKAGING%%'
                          OR standard_vendor_name ILIKE '%%Pharmacy Automation%%'
                          OR aliases ILIKE '%%Pharmacy Automation%%'""")
        for r in cur.fetchall(): print(r)

        print("\n=== Abbott / Abbot Nutrition ===")
        cur.execute("""SELECT id, standard_vendor_name, aliases FROM vendor_name_map
                       WHERE standard_vendor_name ILIKE '%%Abbot%%'""")
        for r in cur.fetchall(): print(r)

        print("\n=== vendor_account_assignment orphans (ids 91/92/634 from earlier this session) ===")
        cur.execute("SELECT * FROM vendor_account_assignment WHERE id IN (91,92,634)")
        for r in cur.fetchall(): print(r)

        print("\n=== Bordova Innovations @ Lakewood Rx (should be filled if the one-time backfill ran) ===")
        cur.execute("SELECT standard_vendor_name, name_lakewood_rx FROM vendor_name_map "
                    "WHERE standard_vendor_name ILIKE '%%Bordova%%'")
        for r in cur.fetchall(): print(r)

        print("\n=== recent vendor_name_map edit_log entries (last 20) ===")
        cur.execute("SELECT * FROM edit_log WHERE table_name = 'vendor_name_map' "
                    "ORDER BY changed_at DESC LIMIT 20")
        for r in cur.fetchall(): print(r)
finally:
    conn.close()
