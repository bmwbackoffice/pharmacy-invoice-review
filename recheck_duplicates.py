"""
Re-check SAVED invoices and payments for duplicates with today's matching rules.

Records keep the duplicate status they got when they were saved, so a copy that slipped past an
older version of the duplicate check (or arrived before a backfill) is never looked at again. This
walks every live record in arrival order and reports the copies today's rules would have caught.

DRY RUN BY DEFAULT — prints what it finds and changes nothing.

    python recheck_duplicates.py              # report only
    python recheck_duplicates.py --apply      # flag the missed copies for review (see below)
    python recheck_duplicates.py --include-legacy   # also check old-system (Excel-migrated) records

What it reports, per document (same vendor + invoice/reference number):
  missed duplicate  - two live copies with the same amount from DIFFERENT emails, neither flagged.
                      Today's pipeline would auto-ignore the later one.
  missed revision   - same vendor + number, a different amount, neither flagged. Today's pipeline
                      would flag the later one as a possible (revised) duplicate.
  recorded twice    - the same document recorded twice from the SAME email with the same amount.
  (payments only)   - no reference number: same vendor + amount + payment date from different
                      emails, flagged as possible only (today's pipeline never auto-ignores those).
A pair where BOTH copies are already entered in QuickBooks is listed as "check QuickBooks for a
double entry" and is never changed.

--apply marks the LATER copy of each finding as a possible duplicate (duplicate_suspect + a
"[Re-check ...]" reason + a shared group id), so it shows on Possible duplicates for a person to
keep or ignore. It never ignores, deletes, or un-flags anything, and skips any copy already entered
in QuickBooks. Every change is logged to edit_log (changed_by 'recheck_duplicates'). Excluded-vendor
(skip-QB) invoices are left out, as on the Possible duplicates page.

DB mode only (needs DATABASE_URL). Read-only unless --apply.
"""
import os
import secrets
import sys
from datetime import date

import psycopg2.extras

import classify_emails as ce
import database
from classify_emails import _amounts_equal, _norm, _applied_invoice_from_notes

ACTOR = "recheck_duplicates"


def _money(v):
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return "(no amount)"


def _load(cur, include_legacy):
    legacy = "" if include_legacy else " AND r.legacy_migrated IS NOT TRUE"
    # Each record's ORIGINAL email: the ledger knows it for reprocessed split files.
    cur.execute("SELECT source_file, source_email_id FROM invoice_history WHERE source_email_id IS NOT NULL "
                "UNION SELECT source_file, source_email_id FROM payment_history WHERE source_email_id IS NOT NULL")
    origin = {f: e for f, e in cur.fetchall() if f}
    cur.execute("SELECT r.id, r.email_filename, r.vendor_matched, r.invoice_number AS number, r.amount, "
                "       r.invoice_date AS record_date, r.store_matched, r.duplicate_suspect, r.duplicate_group_id, "
                "       r.duplicate_reason, r.entered_in_qb, NULL::text AS applied, "
                "       (r.notes LIKE '%%Payment reminder for invoice #%%' "
                "        AND r.notes LIKE '%%NO matching invoice is on file%%') AS reminder "
                "  FROM invoices r WHERE r.duplicate_ignored IS NOT TRUE AND r.skip_qb_import IS NOT TRUE"
                + legacy + " ORDER BY r.id")
    invoices = cur.fetchall()
    cur.execute("SELECT r.id, r.email_filename, r.vendor_matched, r.reference_number AS number, r.amount, "
                "       r.payment_date AS record_date, r.store_matched, r.duplicate_suspect, r.duplicate_group_id, "
                "       r.duplicate_reason, r.entered_in_qb, r.invoice_number AS applied, r.notes, "
                "       FALSE AS reminder "
                "  FROM payments r WHERE r.duplicate_ignored IS NOT TRUE" + legacy + " ORDER BY r.id")
    payments = cur.fetchall()
    # Same notes-fallback as the live pipeline: if the applied invoice was only stated in free-text
    # notes (not the structured invoice_number column), extract it so two payments sharing a
    # reference # but settling DIFFERENT invoices aren't mistaken for revisions of each other.
    for p in payments:
        if not p["applied"]:
            p["applied"] = _applied_invoice_from_notes(p.get("notes"))
    for r in invoices + payments:
        r["email"] = origin.get(r["email_filename"]) or r["email_filename"]
    return invoices, payments


def _same_payment(a, b):
    """Payments that a shared reference # still keeps apart: both state a different applied invoice."""
    return not (_norm(a["applied"]) and _norm(b["applied"]) and _norm(a["applied"]) != _norm(b["applied"]))


def find(records, kind):
    """Compare each record with the EARLIER live copies of the same document, the way the pipeline's
    duplicate check does at save time. Returns findings: dicts with earlier, later, what."""
    findings = []
    groups = {}
    for r in records:
        # The pipeline's own vendor key: STANDARD name via Vendor names, else the printed spelling
        # (classify_emails._dedupe_vendor_key; main() loads the vendor tables first).
        v, n = ce._dedupe_vendor_key(r["vendor_matched"]), _norm(r["number"])
        if not v:
            continue
        if n:
            key = (v, n)
        elif kind == "payment" and r["amount"] is not None and _norm(r["record_date"]):
            key = (v, "", round(float(r["amount"]), 2), _norm(r["record_date"]))   # no-reference payment
        else:
            continue
        for prior in groups.get(key, []):
            if r["duplicate_suspect"] or prior["duplicate_suspect"]:
                continue                     # already flagged — on Possible duplicates now
            if kind == "payment" and not _same_payment(prior, r):
                continue
            what = _classify(kind, prior, r, has_number=bool(n))
            if what:
                # A payment reminder recorded before its invoice arrived: the reminder is the copy to
                # drop, whichever came first (the pipeline now does this itself when the invoice arrives).
                target = prior if prior.get("reminder") else r
                if prior.get("reminder") or r.get("reminder"):
                    what = "payment reminder + its invoice"
                findings.append({"kind": kind, "earlier": prior, "later": r, "what": what, "target": target})
                break                         # one finding per later copy
        groups.setdefault(key, []).append(r)
    return findings


def _classify(kind, prior, r, has_number):
    """What today's duplicate check would make of `r` given the earlier live copy `prior` — mirroring
    classify_emails.check_duplicate (invoices) / check_duplicate_payment (payments) — or None."""
    same_email = prior["email"] == r["email"]
    same_file = prior["email_filename"] == r["email_filename"]
    same_amount = _amounts_equal(prior["amount"], r["amount"])
    if not has_number:                        # payment with no reference: vendor + amount + date
        return None if same_email else "missed possible (no reference #)"
    if same_email:
        if same_amount:
            # A different file is a redundant re-record (auto-ignored today). The SAME file twice
            # isn't caught by today's check at all, but it's still one document recorded twice.
            return "recorded twice" if not same_file else "recorded twice (same file)"
        # Same email, different amount: a revision candidate for invoices; for payments the
        # reference check treats the same email as the same payment batch -> not flagged.
        return "missed revision" if kind == "invoice" else None
    if same_amount:
        if kind == "invoice" and _norm(prior["store_matched"]) != _norm(r["store_matched"]):
            return "missed duplicate (different stores)"
        return "missed duplicate"
    return "missed revision"


def _describe(f):
    a, b, t = f["earlier"], f["later"], f["target"]
    num = b["number"] or "(no #)"
    s = (f"  {f['what']:<34} {f['kind']:<7} {b['vendor_matched']} #{num}: "
         f"#{a['id']} {_money(a['amount'])} ({a['email_filename']})  vs  "
         f"#{b['id']} {_money(b['amount'])} ({b['email_filename']})")
    if a["entered_in_qb"] and b["entered_in_qb"]:
        s += "\n      BOTH entered in QuickBooks — check QuickBooks for a double entry (not changed)."
    elif t["entered_in_qb"]:
        s += f"\n      #{t['id']} (the copy --apply would flag) is already entered in QuickBooks (not changed)."
    elif t is a:
        s += f"\n      --apply flags #{t['id']}, the payment reminder (keep the invoice itself)."
    return s


def _apply(cur, f):
    """Flag the finding's target copy (the later one, or the payment reminder) as a possible
    duplicate. Returns True when a row was changed."""
    t = f["target"]
    other = f["earlier"] if t is f["later"] else f["later"]
    if t["entered_in_qb"]:
        return False
    table = "invoices" if f["kind"] == "invoice" else "payments"
    gid = other["duplicate_group_id"] or t["duplicate_group_id"] or secrets.token_hex(4).upper()
    label = "Invoice" if f["kind"] == "invoice" else "Payment ref"
    reason = (f"[Re-check {date.today():%m/%d/%Y}] {label} #{t['number'] or '(none)'} from {t['vendor_matched']} "
              f"for {_money(t['amount'])} matches another copy from {other['email_filename']} "
              f"({_money(other['amount'])}) — {f['what']}; keep one and ignore the other.")
    # '' is the insert-time default for duplicate_group_id, so it counts as unset (coalesce alone left
    # a '' group in place — fixed 2026-10-08). duplicate_of stays as it is: like the pipeline, it's only
    # set when a copy is actually ignored as a duplicate of another.
    cur.execute(f"UPDATE {table} SET duplicate_suspect = TRUE, "
                f"duplicate_group_id = CASE WHEN coalesce(duplicate_group_id, '') = '' THEN %s "
                f"                          ELSE duplicate_group_id END, "
                f"duplicate_reason = CASE WHEN coalesce(duplicate_reason, '') = '' THEN %s "
                f"                        ELSE duplicate_reason || ' ' || %s END "
                f"WHERE id = %s AND duplicate_suspect IS NOT TRUE AND entered_in_qb IS NOT TRUE",
                (gid, reason, reason, t["id"]))
    if cur.rowcount != 1:
        return False
    cur.execute(f"UPDATE {table} SET duplicate_group_id = %s WHERE id = %s AND coalesce(duplicate_group_id, '') = ''",
                (gid, other["id"]))
    cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s, %s, 'duplicate_suspect', 'False', %s, %s)",
                (table, t["id"], f"True ({f['what']} of #{other['id']})", ACTOR))
    return True


def main():
    if not os.environ.get("DATABASE_URL"):
        print("recheck_duplicates needs DATABASE_URL (DB mode). Nothing to do.")
        return
    apply = "--apply" in sys.argv
    include_legacy = "--include-legacy" in sys.argv
    ce.VENDOR_STD = ce.load_vendor_standardization()   # standard-name vendor matching, as in a classify run
    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            invoices, payments = _load(cur, include_legacy)
            findings = find(invoices, "invoice") + find(payments, "payment")
            print(f"Checked {len(invoices)} live invoice(s) and {len(payments)} live payment(s)"
                  f"{' (including old-system records)' if include_legacy else ''}.")
            if not findings:
                print("Nothing missed — every copy today's rules would catch is already flagged or ignored.")
                conn.rollback()
                return
            by_what = {}
            for f in findings:
                by_what.setdefault(f["what"], 0)
                by_what[f["what"]] += 1
            print(f"{len(findings)} finding(s): " + ", ".join(f"{n} {w}" for w, n in sorted(by_what.items())))
            for f in findings:
                print(_describe(f))
            if not apply:
                print("\nDry run — nothing changed. Re-run with --apply to flag the later copies as possible "
                      "duplicates for review (copies already entered in QuickBooks are left alone).")
                conn.rollback()
                return
            changed = sum(1 for f in findings if _apply(cur, f))
        conn.commit()
        print(f"\n--apply: flagged {changed} record(s) as possible duplicates — review them on Possible duplicates.")
    except Exception:
        conn.rollback()
        print("recheck_duplicates: ERROR — rolled back, nothing changed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
