"""Fill in the short item memo (2026-10-30) on bills NOT yet entered in QuickBooks, for vendors ticked
"Add item memo" in Setup > Vendor names. New bills get it when they're read; this covers bills already
in the app. Reads each bill's local PDF copy in invoice_files/ and asks the AI for the item label.
Entered, kept-out and set-aside bills are never touched.
  python backfill_item_memos.py                       -> list what it would fill (makes AI calls, saves nothing)
  python backfill_item_memos.py --commit              -> save the memos (logged)
  python backfill_item_memos.py --tick "Promotise Inc" --commit
        -> also ticks "Add item memo" for that vendor first (default: Promotise Inc, once)"""
import base64
import sys
from pathlib import Path

import psycopg2.extras

import classify_emails as ce
import database

COMMIT = "--commit" in sys.argv
TICK = "Promotise Inc"
if "--tick" in sys.argv:
    TICK = sys.argv[sys.argv.index("--tick") + 1]
FILES = Path(__file__).parent / "invoice_files"

INSTRUCTION = ("From this vendor bill, give ONLY a short label of what was bought, for a QuickBooks memo: the "
               "product name(s) as printed on the line items, in the document's own wording but WITHOUT colors, "
               "sizes, quantities, logos, imprint or other details ('Classic Click Pen purple with white trim, "
               "Madison Logo' -> 'Classic Click Pen'; 'Lab Coat Color: Teal Blue Sizes 3 x 38' -> 'Lab Coat'). "
               "Leave out set-up charges, ink/color changes, reorder or surcharge lines, shipping, tax and fees. "
               "Several different products: join with ' - '; more than three: the first three then ' - ...'. "
               "The same product in several colors is ONE product. Max about 60 characters. "
               "Answer with the label only, or NONE if no line items are shown.")


def find_pdf(r):
    num = (r["invoice_number"] or "").strip()
    if not num:
        return None
    word = (r["standard_vendor"] or r["vendor_matched"] or "").split()[0].lower()
    hits = [p for p in FILES.glob(f"* - {num} - *.pdf") if word in p.name.lower()]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def ask(pdf):
    data = base64.b64encode(pdf.read_bytes()).decode()
    resp = ce._create_message(None, model=ce.MODEL, max_tokens=2000, system="You read vendor bills.",
                              messages=[{"role": "user", "content": [
                                  {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                                  "data": data}},
                                  {"type": "text", "text": INSTRUCTION}]}],
                              timeout=120)
    text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip().strip('"')
    return "" if text.upper() == "NONE" else ce._clean_item_memo(text)


database.init_db()   # adds the new columns if the web app hasn't been restarted yet
conn = database.connect()
conn.autocommit = False
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
if TICK:
    cur.execute("SELECT add_item_memo FROM vendor_name_map WHERE standard_vendor_name = %s", (TICK,))
    row = cur.fetchone()
    if not row:
        print(f"No vendor named '{TICK}' in Vendor names.")
    elif not row["add_item_memo"]:
        print(f"{'Ticking' if COMMIT else 'Would tick'} Add item memo for {TICK}.")
        if COMMIT:
            cur.execute("UPDATE vendor_name_map SET add_item_memo = TRUE WHERE standard_vendor_name = %s", (TICK,))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "SELECT 'vendor_name_map', id, 'add_item_memo', 'False', 'True', "
                        "'joel (backfill_item_memos.py)' FROM vendor_name_map WHERE standard_vendor_name = %s", (TICK,))
cur.execute("""
    SELECT i.id, i.invoice_number, i.vendor_matched, i.standard_vendor, i.store_matched, i.amount
      FROM invoices i
      JOIN vendor_name_map v ON v.standard_vendor_name = i.standard_vendor
     WHERE (v.add_item_memo OR v.standard_vendor_name = %s)
       AND i.entered_in_qb IS NOT TRUE AND i.skip_qb_import IS NOT TRUE AND i.duplicate_ignored IS NOT TRUE
       AND COALESCE(btrim(i.item_memo), '') = ''
     ORDER BY i.id""", (TICK or "",))
rows = cur.fetchall()
print(f"{len(rows)} bill(s) not yet in QuickBooks without an item memo.")
n = 0
for r in rows:
    pdf = find_pdf(r)
    label = f"  rec {r['id']}  {r['standard_vendor']}  #{r['invoice_number']}  {r['store_matched']}  ${r['amount']}"
    if not pdf:
        print(label + "  -> no local PDF found, skipped"); continue
    try:
        memo = ask(pdf)
    except Exception as e:
        print(label + f"  -> AI call failed: {e}"); continue
    print(label + f"  -> '{memo or '(none)'}'")
    if memo and COMMIT:
        cur.execute("UPDATE invoices SET item_memo = %s WHERE id = %s", (memo, r["id"]))
        cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('invoices', %s, 'item_memo', NULL, %s, 'joel (backfill_item_memos.py)')", (r["id"], memo))
        n += 1
conn.commit() if COMMIT else conn.rollback()
print(f"\n{'Saved' if COMMIT else 'Would save'} {n if COMMIT else 'the memos above'}." + ("" if COMMIT else " Run with --commit."))
