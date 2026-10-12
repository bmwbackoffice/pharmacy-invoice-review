"""READ-ONLY. Costco 20751521485459184 (7/29/2026) shows twice at $214.77 on /qb from the same
email, plus a $219.07 version on disk. Dumps every record/review row carrying that number so we
can tell two genuine documents from a duplicate. Writes nothing."""
import psycopg2.extras
import database

NUM = "20751521485459184"
conn = database.connect()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
skip = {"raw_json", "extracted_json"}
for t, numcol in (("invoices", "invoice_number"), ("credit_memos", "invoice_number"),
                  ("payments", "invoice_number"), ("review_queue", "invoice_number")):
    try:
        cur.execute(f"SELECT * FROM {t} WHERE {numcol} LIKE %s ORDER BY id", (f"%{NUM[-8:]}%",))
    except Exception as e:
        conn.rollback(); print(f"-- {t}: skipped ({e.__class__.__name__})"); continue
    for r in cur.fetchall():
        print(f"\n-- {t} id={r['id']}")
        for k, v in r.items():
            if v not in (None, "", False) and k not in skip:
                print(f"   {k}: {v}")
cur.execute("SELECT filename, status, subject, received_at FROM emails WHERE filename ILIKE %s "
            "OR subject ILIKE %s", ("%costco%", "%costco%"))
print("\n=== Costco emails")
for r in cur.fetchall():
    print("  ", dict(r))
conn.close()
