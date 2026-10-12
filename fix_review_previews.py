"""One-off (2026-10-07): give open review items that have NO source document a preview.

Before the 2026-10-07 pipeline fix, a record flagged by the vendor/account check, or a held sales
order released into review, reached the queue with no review_pending/ document, and its review row
pointed at the WHOLE source email (answering one such item reprocessed every record in that email).
For each open row with an empty file_path whose filename is its source email, this script finds the
record's own document:

  1. Released sales order  -> rebuild it from the source email using the held order's snapshot
                              (source_document / source_pages / rotations), exactly as the pipeline would.
  2. Filed at flag time    -> copy the document the pipeline saved under invoice_files/ for that record
                              (matched by its generated 'Store - Vendor - Invoice # - Amount' name; never
                              a file a recorded record points to; newest copy wins).
  3. Otherwise             -> if the source email has exactly ONE document attachment, copy that whole
                              attachment as a PREVIEW ONLY (it may hold other records too).

Rows that get their OWN document (1 or 2) become normal split items: filename + file_path point at the
new review_pending/ file, so answering reprocesses just that record. Per source email this is
all-or-nothing: if any row of an email only gets a preview (3) or nothing, every row of that email
keeps reprocessing the whole email and only gets file_path (the preview). Internal notes and uploads
are keyed by review_key, so they stay attached. Nothing is deleted; every row change is logged to
edit_log as 'fix_review_previews'.

    python fix_review_previews.py            # dry run: builds documents in a temp folder, writes nothing
    python fix_review_previews.py --commit   # copy documents into review_pending/ and update the rows
"""
import hashlib
import io
import re
import shutil
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

import psycopg2.extras

import classify_emails as ce
import database

COMMIT = "--commit" in sys.argv
ACTOR = "fix_review_previews"
BASE = Path(__file__).parent
INVOICE_FILES = BASE / "invoice_files"
REVIEW_PENDING = BASE / "review_pending"
DOC_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _source_path(name):
    for d in ("processed_emails", "review_pending", "test_emails"):
        p = BASE / d / (name or "")
        if name and p.exists():
            return p
    return None


def _read_email(name):
    src = _source_path(name)
    if src is None:
        return None
    text, atts, html = ce.read_email_content(src)
    cid = {a["content_id"]: (a.get("data"), a.get("mimetype")) for a in atts if a.get("content_id")}
    return text, ce.expand_zip_attachments(atts), html, cid


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan_sales_order(cur, row, out_dir):
    """Mode 1: rebuild a released sales order's document from its snapshot attribution."""
    cur.execute(
        "SELECT id, snapshot FROM pending_sales_orders WHERE status = 'released' AND source_file = %s "
        "AND norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
        "AND upper(btrim(number)) = upper(btrim(%s)) ORDER BY id DESC LIMIT 1",
        (row["source_email"], row["vendor_matched"], row["invoice_number"]))
    so = cur.fetchone()
    if not so:
        return None
    inv = dict((so["snapshot"] or {}).get("invoice") or {})
    email = _read_email(row["source_email"])
    if email is None:
        return {"note": f"held order #{so['id']}: source email not on disk"}
    text, atts, html, cid = email
    sd = (inv.get("source_document") or "").strip().lower()
    if sd and sd != "email_body" and ce._match_attachment(sd, atts) is None:
        return {"note": f"held order #{so['id']}: {inv.get('source_document')!r} not in the email"}
    store, vendor, number, amount, date_iso = ce._record_fields("invoice", inv)
    path = ce._write_record_document(inv, store, vendor, number, amount, date_iso, out_dir,
                                     atts, text, html, cid)
    if not path:
        return {"note": f"held order #{so['id']}: document could not be rebuilt"}
    return {"mode": "own", "path": Path(path), "how": f"rebuilt from held sales order #{so['id']}"}


def _page_fingerprints(pdf_bytes):
    """One fingerprint per PDF page from its embedded image data (falling back to its content
    stream). Splitting, rotating, dropping or reordering pages keeps each page's images byte-for-byte,
    so a page filed for a record can be traced back to the source attachment it came from."""
    from pypdf import PdfReader
    fps = []
    for page in PdfReader(io.BytesIO(pdf_bytes)).pages:
        h = hashlib.sha256()
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        images = 0
        for name in sorted(xobjects):
            obj = xobjects[name].get_object()
            if obj.get("/Subtype") == "/Image":
                h.update(getattr(obj, "_data", b"") or obj.get_data())
                images += 1
        if not images:
            contents = page.get_contents()
            h.update(contents.get_data() if contents is not None else b"")
        fps.append(h.hexdigest())
    return fps


def _pages_come_from_email(path, row):
    """True when every page of a filed PDF appears among the PDF pages of the row's own source email."""
    if Path(path).suffix.lower() != ".pdf":
        return False
    email = _read_email(row["source_email"])
    if email is None:
        return False
    source_pages = set()
    for att in email[1]:
        if ce._effective_media_type(att) == "application/pdf":
            try:
                source_pages.update(_page_fingerprints(att["data"]))
            except Exception:
                pass
    try:
        filed = _page_fingerprints(Path(path).read_bytes())
    except Exception:
        return False
    return bool(filed) and all(fp in source_pages for fp in filed)


def plan_filed_copy(row, referenced):
    """Mode 2: the document the pipeline filed under invoice_files/ when the record was flagged.
    Identical copies (repeat runs of the same email) are interchangeable. When the copies DIFFER —
    the same invoice filed from different emails — a copy is used only if its pages are traced back
    to this row's own source email."""
    prefix = ce.build_invoice_filename(row["store_matched"], row["vendor_matched"],
                                       row["invoice_number"], row["amount"], None)[:170]
    cands = []
    for p in INVOICE_FILES.glob("*"):
        stem = p.stem
        if (p.suffix.lower() in DOC_EXTS and p.name not in referenced
                and (stem == prefix or stem.startswith(prefix + " - ") or stem.startswith(prefix + "__"))):
            cands.append(p)
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if len({_sha(p) for p in cands}) == 1:
        how = f"filed copy {cands[0].name}" + (f" (newest of {len(cands)}, all identical)" if len(cands) > 1 else "")
        return {"mode": "own", "path": cands[0], "how": how, "copy": True}
    for p in cands:
        if _pages_come_from_email(p, row):
            return {"mode": "own", "path": p, "copy": True,
                    "how": f"filed copy {p.name} (of {len(cands)} differing copies, pages traced to this email)"}
    return None


def plan_whole_attachment(row, out_dir):
    """Mode 3: the email's single document attachment, as a preview only."""
    email = _read_email(row["source_email"])
    if email is None:
        return None
    _text, atts, _html, _cid = email
    docs = [a for a in atts if ce.MEDIA_TYPE_TO_EXT.get(ce._effective_media_type(a)) in DOC_EXTS]
    if len(docs) != 1:
        return None
    att = docs[0]
    ext = ce.MEDIA_TYPE_TO_EXT.get(ce._effective_media_type(att)) or ".pdf"
    stem = ce.build_invoice_filename(row["store_matched"], row["vendor_matched"],
                                     row["invoice_number"], row["amount"], None) + " (full email attachment)"
    dest = ce._unique_path(out_dir, stem, ext)
    dest.write_bytes(att["data"])
    return {"mode": "preview", "path": dest, "how": f"whole attachment {att['filename']} (preview only)"}


def main():
    conn = database.connect()
    conn.autocommit = False
    created = []                      # files written into review_pending/ (removed again on failure)
    scratch = Path(tempfile.mkdtemp(prefix="review_previews_"))
    out_dir = REVIEW_PENDING if COMMIT else scratch
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, filename, source_email, review_key, vendor_matched, store_matched, "
                        "invoice_number, amount FROM review_queue "
                        "WHERE coalesce(file_path, '') = '' AND filename = source_email ORDER BY source_email, id")
            rows = cur.fetchall()
            cur.execute("SELECT file_path FROM invoices WHERE coalesce(file_path,'') <> '' "
                        "UNION ALL SELECT file_path FROM credit_memos WHERE coalesce(file_path,'') <> '' "
                        "UNION ALL SELECT file_path FROM payments WHERE coalesce(file_path,'') <> ''")
            referenced = {re.split(r"[\\/]", r["file_path"])[-1] for r in cur.fetchall()}

            groups = OrderedDict()
            for row in rows:
                plan = plan_sales_order(cur, row, out_dir)
                note = None
                if plan is not None and not plan.get("mode"):
                    note, plan = plan["note"], None          # a held order matched but couldn't be rebuilt
                if plan is None:
                    plan = plan_filed_copy(row, referenced) or plan_whole_attachment(row, out_dir)
                    if plan and note:
                        plan["how"] += f"; {note}"
                if plan is None:
                    plan = {"note": note or "no document found"}
                if COMMIT and plan.get("mode") and not plan.get("copy"):
                    created.append(plan["path"])             # written straight into review_pending/
                groups.setdefault(row["source_email"], []).append((row, plan))

            n_split = n_preview = n_none = 0
            for email, items in groups.items():
                convert = all(p.get("mode") == "own" for _r, p in items)
                print(f"\n{email}  ({len(items)} item(s); "
                      f"{'each becomes its own split item' if convert else 'preview only, whole-email reprocess kept'})")
                for row, plan in items:
                    label = f"  #{row['id']} {row['vendor_matched']} #{row['invoice_number']} ${row['amount']}"
                    if not plan.get("mode"):
                        n_none += 1
                        print(f"{label}: LEFT AS-IS — {plan.get('note')}")
                        continue
                    src = plan["path"]
                    if COMMIT and plan.get("copy"):
                        base = re.sub(r"__\d+$", "", src.stem)
                        dest = ce._unique_path(REVIEW_PENDING, base, src.suffix)
                        shutil.copy2(src, dest)
                        created.append(dest)
                    else:
                        dest = src
                    new_name = dest.name if convert else row["filename"]
                    new_path = str(dest.resolve()) if COMMIT else f"review_pending/{dest.name}"
                    print(f"{label}: {plan['how']} -> {('filename + file_path' if convert else 'file_path')} "
                          f"= {Path(new_path).name}")
                    if convert:
                        n_split += 1
                    else:
                        n_preview += 1
                    if not COMMIT:
                        continue
                    if convert:
                        cur.execute("UPDATE review_queue SET filename = %s, file_path = %s WHERE id = %s "
                                    "AND review_key = %s AND filename = source_email "
                                    "AND coalesce(file_path,'') = ''",
                                    (new_name, new_path, row["id"], row["review_key"]))
                    else:
                        cur.execute("UPDATE review_queue SET file_path = %s WHERE id = %s AND review_key = %s "
                                    "AND coalesce(file_path,'') = ''",
                                    (new_path, row["id"], row["review_key"]))
                    if cur.rowcount != 1:
                        raise RuntimeError(f"review row #{row['id']} changed since it was read — nothing "
                                           f"committed; rerun after the current classify run finishes")
                    changes = [("file_path", None, new_path)]
                    if convert:
                        changes.insert(0, ("filename", row["filename"], new_name))
                    for field, old, new in changes:
                        cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                                    "changed_by) VALUES ('review_queue', %s, %s, %s, %s, %s)",
                                    (row["id"], field, old, new, ACTOR))
        if COMMIT:
            conn.commit()
        else:
            conn.rollback()
        print(f"\n{'COMMITTED' if COMMIT else 'DRY RUN (nothing written)'}: {n_split} item(s) converted to "
              f"their own split document, {n_preview} given a preview only, {n_none} left as-is.")
        if not COMMIT and (n_split or n_preview):
            print("Run again with --commit to apply.")
    except Exception:
        conn.rollback()
        for f in created:
            try:
                Path(f).unlink()
            except OSError:
                pass
        raise
    finally:
        conn.close()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
