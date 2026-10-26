"""Re-reads every UNFI invoice's saved PDF for its two key fields, to find the two known misreads:
  - the bill # taken from "COVERSHEET#" instead of "DOCUMENT # INV#";
  - the amount taken from the "Merchandise summary" instead of "NET TOTAL ... <---- PLEASE PAY".

  python check_unfi_invoices.py            -> report only (one short AI read per UNFI invoice)
  python check_unfi_invoices.py --fix      -> corrects records NOT yet entered in QuickBooks only; records
                                              already in QuickBooks are listed and left as is"""
import base64
import io
import json
import os
import re
import sys
from pathlib import Path

import httpx
import psycopg2.extras
from anthropic import Anthropic

import classify_emails as ce
import database

ROOT = Path(__file__).parent
FIX = "--fix" in sys.argv
PROMPT = ("This is a UNFI (United Natural Foods) invoice, possibly scanned sideways or upside down. Reply with JSON "
          "only: {\"document_number\": the number in the DOCUMENT # box (after 'INV#', digits only), "
          "\"coversheet_number\": the number after 'COVERSHEET#', \"please_pay\": the NET TOTAL amount next to "
          "'<---- PLEASE PAY' at the bottom (a number), \"date\": the invoice DATE as YYYY-MM-DD}. Use null for "
          "anything you can't read.")


def pdf_bytes(rec):
    fp = rec.get("file_path") or ""
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", fp)
    if m:
        from drive_uploader import DriveUploader, NUM_RETRIES
        svc = DriveUploader().service
        name = svc.files().get(fileId=m.group(1), fields="name").execute(num_retries=NUM_RETRIES)["name"]
        for d in ("invoice_files", "review_pending"):
            if (ROOT / d / name).is_file():
                return (ROOT / d / name).read_bytes()
        return svc.files().get_media(fileId=m.group(1)).execute(num_retries=NUM_RETRIES)
    p = Path(fp)
    return p.read_bytes() if p.is_file() else None


def main():
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        print("ANTHROPIC_API_KEY is not set.")
        sys.exit(1)
    tmo = httpx.Timeout(120.0, connect=15.0)
    client = (Anthropic(api_key=key, timeout=tmo, http_client=httpx.Client(verify=str(ce.CA_BUNDLE), timeout=tmo))
              if ce.CA_BUNDLE.exists() else Anthropic(api_key=key, timeout=tmo))
    conn = database.connect()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, store_matched, vendor_matched, invoice_number, invoice_date, amount, entered_in_qb, file_path "
                "FROM invoices WHERE (vendor_matched ILIKE '%%unfi%%' OR vendor_matched ILIKE '%%united natural%%') "
                "AND duplicate_ignored IS NOT TRUE ORDER BY invoice_date, id")
    recs = cur.fetchall()
    print(f"{len(recs)} UNFI invoice(s) to check\n")
    wrong = 0
    for r in recs:
        data = pdf_bytes(r)
        if not data or not data[:5] == b"%PDF-":
            print(f"  id {r['id']}: no PDF found -- skipped")
            continue
        msg = client.messages.create(model=ce.MODEL, max_tokens=300, messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": PROMPT}]}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            got = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        except Exception:
            print(f"  id {r['id']}: couldn't read the answer -- skipped")
            continue
        doc = re.sub(r"\D", "", str(got.get("document_number") or ""))
        pay = got.get("please_pay")
        num_bad = bool(doc) and doc != re.sub(r"\D", "", r["invoice_number"] or "")
        try:
            amt_bad = pay is not None and abs(float(pay) - float(r["amount"] or 0)) >= 0.005
        except (TypeError, ValueError):
            amt_bad = False
        if not (num_bad or amt_bad):
            print(f"  id {r['id']}: OK  (#{r['invoice_number']}, ${float(r['amount'] or 0):,.2f})")
            continue
        wrong += 1
        what = []
        if num_bad:
            what.append(f"# {r['invoice_number']} -> {doc}")
        if amt_bad:
            what.append(f"amount ${float(r['amount'] or 0):,.2f} -> ${float(pay):,.2f}")
        tag = "in QB -- left as is" if r["entered_in_qb"] else ("fixed" if FIX else "would fix")
        print(f"  id {r['id']} {r['store_matched']} {r['invoice_date']}: {'; '.join(what)}   [{tag}]")
        if FIX and not r["entered_in_qb"]:          # in-QB ones are left alone (Joel, 2026-10-21)
            import web_db
            if num_bad:
                web_db.update_record_field("invoice", r["id"], "number", doc, "joel (check_unfi_invoices.py)")
            if amt_bad:
                web_db.update_record_field("invoice", r["id"], "amount", str(float(pay)), "joel (check_unfi_invoices.py)")
    print(f"\n{wrong} with a wrong # or amount." + ("" if FIX else " Run with --fix to correct the ones not in QuickBooks (in-QB ones are left as is)."))
    conn.close()


if __name__ == "__main__":
    main()
