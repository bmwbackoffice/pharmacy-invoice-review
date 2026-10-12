"""READ-ONLY. Checks whether vendor_account_assignment also has an orphaned 'Pharmacy
Automation' row, same issue as the just-found 'Abbott' one, so both merges can be cleaned up
together."""
import psycopg2.extras
import database

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM vendor_account_assignment WHERE standard_vendor_name ILIKE %s "
                    "OR standard_vendor_name ILIKE %s", ("%pharmacy automation%", "%pas packaging%"))
        for r in cur.fetchall():
            print(r)
finally:
    conn.close()
