"""Round 3: check ignored_reviews (dismissed review-queue items) and tracked_reviews
(answered items) for EuroPharma, since invoice_history has 1 row but invoices/
review_queue are both empty -- a flagged-then-dismissed/answered item would explain
that shape. Read-only."""
import database

conn = database.connect()
cur = conn.cursor()

print("=== ignored_reviews: europharma / 0800123 ===")
cur.execute("""
    SELECT id, source_email, vendor_matched, invoice_number, filename, question,
           ignore_reason, ignored_by, ignored_at
    FROM ignored_reviews
    WHERE source_email ILIKE %s OR vendor_matched ILIKE %s OR invoice_number ILIKE %s
""", ('%europharma%', '%europharma%', '%0800123%'))
rows = cur.fetchall()
print(f"{len(rows)} row(s)")
for r in rows:
    print("   ", r)

print("\n=== tracked_reviews: europharma / 0800123 ===")
cur.execute("""
    SELECT id, filename, source_email, vendor_matched, invoice_number,
           original_question, answer_given, tracked_by, tracked_at, status
    FROM tracked_reviews
    WHERE source_email ILIKE %s OR vendor_matched ILIKE %s OR invoice_number ILIKE %s
""", ('%europharma%', '%europharma%', '%0800123%'))
rows = cur.fetchall()
print(f"{len(rows)} row(s)")
for r in rows:
    print("   ", r)

print("\n=== runs since 2026-09-22 (to see how many runs had a chance to touch this) ===")
cur.execute("""
    SELECT run_id, started_at, finished_at FROM runs
    WHERE run_id >= '20260922' ORDER BY run_id LIMIT 30
""")
rows = cur.fetchall()
print(f"{len(rows)} row(s)")
for r in rows:
    print("   ", r)

cur.close()
conn.close()
