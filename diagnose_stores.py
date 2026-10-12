import database
conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT store_matched FROM invoices WHERE store_matched IS NOT NULL ORDER BY 1")
        for r in cur.fetchall():
            print(r)
finally:
    conn.close()
