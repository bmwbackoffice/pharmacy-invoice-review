"""Read-only check: why does email #2567348 (KayCo / Kenover Marketing) show
Type '-' and Run '-' with disposition 'No action'? Confirms whether it was
ever actually processed, or just ingested and left alone."""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT filename, source, gmail_message_id, subject, from_address,
                   received_at, email_type, status, error, run_id, date_added, updated_at
            FROM emails
            WHERE subject ILIKE %s OR from_address ILIKE %s
            ORDER BY received_at DESC
        """, ('%2567348%', '%kayco%'))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        if not rows:
            print("No matching email row found.")
        for r in rows:
            print("-" * 60)
            for c, v in zip(cols, r):
                print(f"  {c}: {v!r}")
finally:
    conn.close()
