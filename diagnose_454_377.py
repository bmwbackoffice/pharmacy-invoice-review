"""READ-ONLY. Checks the two carried-over records:
  id=454 (Smiling Herbs $0.00) -- genuine zero-dollar doc, or broken extraction?
  id=377 (Zahler PDF) -- no local source file; is the source .eml still archived so the
          attachment can be re-extracted locally instead of re-downloaded from Drive?
Looks the ids up in invoices, payments, credit_memos and review_queue (ids are per-table).
Writes nothing."""
import email, email.policy
from pathlib import Path
import psycopg2.extras
import database

ROOT = Path(__file__).parent
TABLES = ["invoices", "payments", "credit_memos", "review_queue"]

def show_email(cur, fn):
    if not fn:
        return
    cur.execute("SELECT filename, status, subject, from_address, received_at, gmail_link, error "
                "FROM emails WHERE filename=%s", (fn,))
    r = cur.fetchone()
    print("   email row:", dict(r) if r else "(none)")
    for d in ("processed_emails", "review_pending", "test_emails"):
        p = ROOT / d / fn
        if p.exists():
            print(f"   source .eml FOUND in {d}/")
            m = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
            for part in m.walk():
                if part.get_filename():
                    print(f"     attachment: {part.get_filename()} ({len(part.get_payload(decode=True) or b'')} bytes)")
                elif part.get_content_type() == "text/plain":
                    print("     body:", " ".join(part.get_content().split())[:400])
            return
    print("   source .eml NOT found locally")

conn = database.connect()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
for rid in (454, 377):
    print("=" * 90, f"\nid = {rid}")
    for t in TABLES:
        cur.execute(f"SELECT * FROM {t} WHERE id=%s", (rid,))
        r = cur.fetchone()
        if not r:
            continue
        print(f"-- {t}:")
        for k, v in r.items():
            if v not in (None, "", False):
                print(f"   {k}: {v}")
        fp = r.get("file_path")
        if fp:
            print("   file_path exists locally:", (ROOT / fp).exists() or Path(fp).exists())
        show_email(cur, r.get("email_filename"))
conn.close()
