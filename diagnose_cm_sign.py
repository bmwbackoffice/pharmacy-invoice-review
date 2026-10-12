import database
conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM credit_memos ORDER BY id DESC LIMIT 10")
        for r in cur.fetchall():
            print(r)
finally:
    conn.close()
