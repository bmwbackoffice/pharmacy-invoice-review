"""Is id 1276 actually derived from the SAME source email as id 2993 (just processed
earlier), rather than being unrelated legacy data? Check emails table around the
Sept 1 add-date, review/ignored history for H002863/Vita Vocal, and whether
'Maple Scanner - 1a04558eab429b07.eml' shows any sign of being processed more than once."""
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== The emails row id 1276's email_filename actually points to ===")
        cur.execute("""
            SELECT filename, source, gmail_message_id, subject, from_address, received_at,
                   email_type, status, run_id, date_added, updated_at
            FROM emails
            WHERE filename = 'Maple - Vita Vocal Health - H002863 - $1,465.00 - 8.27.2026.pdf'
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== Full invoices row for id 1276 (every column) ===")
        cur.execute("SELECT * FROM invoices WHERE id = 1276")
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== Full invoices row for id 2993 (every column) ===")
        cur.execute("SELECT * FROM invoices WHERE id = 2993")
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== edit_log entries mentioning either record/filename ===")
        cur.execute("""
            SELECT table_name, record_id, field, old_value, new_value, changed_by, changed_at
            FROM edit_log
            WHERE record_id IN ('1276','2993')
               OR old_value ILIKE '%%H002863%%' OR new_value ILIKE '%%H002863%%'
               OR old_value ILIKE '%%Vita Vocal%%' OR new_value ILIKE '%%Vita Vocal%%'
            ORDER BY changed_at
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))

        print("\n=== review_queue / ignored_reviews mentioning H002863 or Vita Vocal ===")
        for table in ("review_queue", "ignored_reviews"):
            cur.execute(f"""
                SELECT * FROM {table}
                WHERE snapshot::text ILIKE '%%H002863%%' OR snapshot::text ILIKE '%%Vita Vocal%%'
            """)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            print(f"-- {table}: {len(rows)} row(s) --")
            for r in rows:
                print(dict(zip(cols, r)))

        print("\n=== runs around 2026-08-27 .. 2026-09-02 ===")
        cur.execute("""
            SELECT run_id, started_at, finished_at, n_emails, n_results, n_review
            FROM runs WHERE started_at BETWEEN '2026-08-27' AND '2026-09-02'
            ORDER BY started_at
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            print(dict(zip(cols, r)))
finally:
    conn.close()
