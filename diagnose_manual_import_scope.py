"""Scope: how many emails rows were created via the manual-import path with no
gmail_message_id, and for how many of those does the SAME underlying Gmail
message (extracted from the linked invoice/payment/credit_memo's gmail_link)
also appear as a separately-processed 'Maple Scanner - <id>.eml' row?
This tells us whether Vita Vocal Health is a one-off or a wider backlog."""
import re
import database

conn = database.connect()
try:
    with conn.cursor() as cur:
        print("=== emails rows: source='manual' AND gmail_message_id IS NULL ===")
        cur.execute("""
            SELECT filename, status, run_id, date_added
            FROM emails
            WHERE source = 'manual' AND gmail_message_id IS NULL
            ORDER BY date_added
        """)
        cols = [d[0] for d in cur.description]
        manual_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        print(f"Total: {len(manual_rows)}")
        for r in manual_rows:
            print(r)

        print("\n=== For each, pull gmail_link from whichever table references this filename ===")
        suspects = []
        for r in manual_rows:
            fn = r["filename"]
            found = None
            for table in ("invoices", "credit_memos", "payments"):
                cur.execute(f"SELECT id, gmail_link FROM {table} WHERE email_filename = %s", (fn,))
                row = cur.fetchone()
                if row and row[1]:
                    found = (table, row[0], row[1])
                    break
            if not found:
                continue
            table, rec_id, link = found
            m = re.search(r'#all/([0-9a-fA-F]+)$', link or "")
            if not m:
                continue
            msg_id = m.group(1)
            suspects.append((fn, table, rec_id, msg_id))

        print(f"Resolved {len(suspects)} manual records to a Gmail message id")

        print("\n=== Checking each for a second, separately-processed 'Maple Scanner - <id>.eml' row ===")
        hits = []
        for fn, table, rec_id, msg_id in suspects:
            scanner_name = f"Maple Scanner - {msg_id}.eml"
            cur.execute("SELECT filename, status, date_added FROM emails WHERE filename = %s", (scanner_name,))
            row = cur.fetchone()
            if row:
                hits.append((fn, table, rec_id, msg_id, row))

        print(f"\n>>> {len(hits)} confirmed REPROCESSED-TWICE case(s) out of {len(suspects)} manual records checked <<<")
        for fn, table, rec_id, msg_id, row in hits:
            print(f"- {table} id={rec_id}: '{fn}' (msg {msg_id}) ALSO processed as '{row[0]}' (status={row[1]}, date_added={row[2]})")
finally:
    conn.close()
