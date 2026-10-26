"""Settle duplicate sets that QuickBooks already decides (2026-10-20, Joel).

A duplicate set = the live (not ignored) copies sharing a duplicate_group_id, where at least one copy is
still flagged. A set is SETTLE-ABLE when every copy has the same store, the same amount and the same
vendor, and the invoice numbers are equal or only a misread apart (one character added / dropped / a
look-alike swapped -- classify_emails._one_edit_apart), and (since 2026-10-26) the copies' dates are within
SETTLE_MAX_DAYS of each other. Nothing monetary differs, so the only question
is how many times the bill is in QuickBooks:

  * QuickBooks register (the bills saved from QB export uploads, qb_register) covers the bill's date:
      - exactly ONE matching QB bill -> resolved: keep the entered copy (the one whose number matches
        QuickBooks when there's a choice), ignore the rest (web_db.keep_and_ignore_others).
      - TWO or more -> 'entered twice' (invoices.qb_twice note): listed at the top of the Duplicates
        page until one bill is deleted in QuickBooks and a new export is uploaded.
  * No export covers it (or the export has no matching bill): go by the app's own "entered" marks --
      - exactly ONE copy marked entered -> resolved as above.
      - two or more marked entered -> 'entered twice?' -- check QuickBooks (same queue).
  * No copy entered at all -> not settled here (the normal duplicate review).

Runs at the end of every classify run and every QuickBooks export upload; resolve_entered_duplicates.py
runs it by hand (dry-run by default).
"""
from datetime import timedelta

import psycopg2.extras

import classify_emails as ce
import database

WHO = "auto: same bill, already in QuickBooks"
# The copies' own dates must agree this closely too (2026-10-26, Joel: yes). Before, a set settled on store,
# amount, vendor and a near number alone, so two real bills months apart could be taken for one.
SETTLE_MAX_DAYS = 10


def _dates_close(copies):
    """True when every copy has a readable date and they're all within SETTLE_MAX_DAYS of each other."""
    days = [ce._loose_date(m["invoice_date"]) for m in copies]
    if not days or any(d is None for d in days):
        return False
    return (max(days) - min(days)).days <= SETTLE_MAX_DAYS


def _num_close(a, b):
    ka, kb = ce._num_key(a), ce._num_key(b)
    return bool(ka) and (ka == kb or ce._one_edit_apart(ka, kb))


def _qb_bills(cur, store, amount, rdate, numbers, std_vendor):
    """(covered_at, [register rows]) -- QB bills for this store + amount within 10 days whose number is
    equal/near one of `numbers` and whose vendor resolves to the same standard vendor (or the number is
    exact). covered_at = when the latest export spanning this store + date was uploaded (None = none)."""
    if not rdate:
        return None, []
    cur.execute("SELECT max(uploaded_at) AS at FROM qb_register_coverage "
                "WHERE qb_store = %s AND %s BETWEEN date_from AND date_to", (store, rdate))
    covered = cur.fetchone()["at"]
    cur.execute("SELECT id, qb_num, qb_name, qb_std_vendor, qb_amount, qb_date FROM qb_register "
                "WHERE qb_store = %s AND qb_type ILIKE 'bill' AND abs(qb_amount - %s) < 0.005 "
                "AND qb_date BETWEEN %s AND %s",
                (store, amount, rdate - timedelta(days=10), rdate + timedelta(days=10)))
    hits = []
    for b in cur.fetchall():
        exact = any(ce._num_key(b["qb_num"]) == ce._num_key(n) for n in numbers)
        near = any(_num_close(b["qb_num"], n) for n in numbers)
        same_vendor = bool(std_vendor and b["qb_std_vendor"] and ce._norm(b["qb_std_vendor"]) == ce._norm(std_vendor))
        if exact or (near and same_vendor):
            hits.append(b)
    return covered, hits


def settle_duplicate_sets(actor=WHO, dry_run=False, verbose=True):
    """Returns {"resolved": [...], "twice": [...], "left": [...]} (see module doc)."""
    import web_db                                   # function-level: web_db imports classify_emails too
    out = {"resolved": [], "twice": [], "left": []}
    if not ce.VENDOR_STD:                           # web app / QB upload: load before opening the txn
        ce.VENDOR_STD = ce.load_vendor_standardization()
    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, vendor_matched, standard_vendor, invoice_number, invoice_date, amount,
                       store_matched, entered_in_qb, entered_qb_at, duplicate_group_id, qb_twice
                  FROM invoices
                 WHERE duplicate_ignored IS NOT TRUE AND ignored_at IS NULL AND skip_qb_import IS NOT TRUE
                   AND duplicate_group_id IN (
                       SELECT duplicate_group_id FROM invoices
                        WHERE (duplicate_suspect IS TRUE OR qb_twice IS NOT NULL)
                          AND duplicate_ignored IS NOT TRUE AND ignored_at IS NULL
                          AND duplicate_group_id IS NOT NULL)""")
            groups = {}
            for r in cur.fetchall():
                groups.setdefault(r["duplicate_group_id"], []).append(r)

            for gid, live in sorted(groups.items()):
                first = live[0]
                label = (f"{first['vendor_matched']} #{first['invoice_number']} ${float(first['amount'] or 0):,.2f} "
                         f"{first['store_matched']}")
                if len(live) < 2:
                    if first["qb_twice"] and not dry_run:   # its partner was ignored meanwhile -> clear
                        cur.execute("UPDATE invoices SET qb_twice = NULL WHERE id = %s", (first["id"],))
                    continue
                why = None
                if len({ce._norm(m["store_matched"]) for m in live}) != 1:
                    why = "stores differ"
                elif len({round(float(m["amount"] or 0), 2) for m in live}) != 1 or not first["amount"]:
                    why = "amounts differ"
                elif not all(_num_close(m["invoice_number"], first["invoice_number"]) for m in live):
                    why = "invoice numbers differ"
                elif not all(ce._vendors_related(m["vendor_matched"], first["vendor_matched"]) for m in live):
                    why = "vendors differ"
                elif not _dates_close(live):
                    why = f"dates more than {SETTLE_MAX_DAYS} days apart (or unreadable)"
                entered = [m for m in live if m["entered_in_qb"]]
                if not why and not entered:
                    why = "no copy entered in QuickBooks yet"
                if why:
                    out["left"].append((gid, label, why))
                    continue

                rdate = ce._loose_date(first["invoice_date"])
                numbers = [m["invoice_number"] for m in live]
                std_vendor = next((m["standard_vendor"] for m in live if m["standard_vendor"]), None)
                covered, bills = _qb_bills(cur, first["store_matched"], first["amount"], rdate, numbers, std_vendor)
                if covered:
                    # The export is a snapshot from its upload time: a copy marked entered AFTER that upload
                    # whose number isn't one of the export's bills may be a second entry the export can't see.
                    later = [m for m in entered if m["entered_qb_at"] and m["entered_qb_at"] > covered
                             and not any(ce._num_key(m["invoice_number"]) == ce._num_key(b["qb_num"]) for b in bills)]
                    n_in_qb = len(bills) + len(later) if bills else len(entered)
                    source = "the QuickBooks export" + (" plus entries marked after it" if bills and later else "")
                else:
                    n_in_qb, source = len(entered), "the app's entered marks (no QuickBooks export covers this date)"

                if n_in_qb >= 2:
                    nums = ", ".join(sorted({str(b["qb_num"]) for b in bills} |
                                            {str(m["invoice_number"]) for m in entered if not bills}))
                    note = (f"Entered twice in QuickBooks ({nums}), per {source} -- delete one bill in QuickBooks, "
                            f"then keep one copy here (or upload a fresh QuickBooks export and it clears itself).")
                    if covered and bills and later and len(bills) == 1:
                        note = (f"Possibly entered twice: the QuickBooks export of {covered:%m/%d/%Y} has "
                                f"#{bills[0]['qb_num']} once, and #{', #'.join(str(m['invoice_number']) for m in later)} "
                                f"was marked entered after that export. Upload a fresh QuickBooks export to confirm "
                                f"(if QuickBooks has it once, this clears itself).")
                    out["twice"].append((gid, label, note))
                    if not dry_run:
                        cur.execute("UPDATE invoices SET qb_twice = %s WHERE id = ANY(%s)",
                                    (note, [m["id"] for m in live]))
                    continue
                if n_in_qb == 0:
                    out["left"].append((gid, label, "marked entered, but the QuickBooks export has no such bill"))
                    continue
                # Entered once: keep the entered copy whose number QuickBooks has, else the first entered.
                keep = entered[0]
                if bills:
                    qn = ce._num_key(bills[0]["qb_num"])
                    keep = next((m for m in entered if ce._num_key(m["invoice_number"]) == qn), keep)
                others = [("invoice", m["id"]) for m in live if m["id"] != keep["id"]]
                out["resolved"].append((gid, label, keep["id"], [o[1] for o in others], source))
                if not dry_run:
                    cur.execute("UPDATE invoices SET qb_twice = NULL WHERE id = ANY(%s)", ([m["id"] for m in live],))
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()
    if not dry_run:
        for gid, label, keep_id, other_ids, source in out["resolved"]:
            web_db.keep_and_ignore_others("invoice", keep_id, [("invoice", i) for i in other_ids], actor)
    if verbose:
        for gid, label, keep_id, other_ids, source in out["resolved"]:
            print(f"  resolved: {label}: kept id={keep_id}, ignored {other_ids} (in QuickBooks once, per {source})")
        for gid, label, note in out["twice"]:
            print(f"  ENTERED TWICE: {label}: {note}")
    return out
