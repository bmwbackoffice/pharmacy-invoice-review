"""READ-ONLY. Pulls every payment (and, in case it's relevant, invoice) tied to reference
number ARC_2TG28BJLQ8AU, plus its edit_log history, to see why a duplicate flag Joel already
resolved manually a couple days ago is showing up again now."""
import psycopg2.extras
import database

REF = "ARC_2TG28BJLQ8AU"

conn = database.connect()
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        print(f"=== payments with reference_number = {REF!r} ===")
        cur.execute("SELECT * FROM payments WHERE reference_number = %s ORDER BY id", (REF,))
        rows = cur.fetchall()
        for r in rows:
            print(r)
            print()
        print(f"Total: {len(rows)}")

        if rows:
            ids = [r["id"] for r in rows]
            print(f"\n=== edit_log for payments {ids} ===")
            cur.execute(
                "SELECT * FROM edit_log WHERE table_name = 'payments' AND record_id = ANY(%s) "
                "ORDER BY id", (ids,))
            for r in cur.fetchall():
                print(r)

        print(f"\n=== emails referencing this reference number (by filename match, loose) ===")
        cur.execute(
            "SELECT filename, gmail_message_id, source, status, run_id FROM emails "
            "WHERE filename ILIKE %s", (f"%{REF}%",))
        for r in cur.fetchall():
            print(r)
finally:
    conn.close()
