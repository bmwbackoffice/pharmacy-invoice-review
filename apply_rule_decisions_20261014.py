"""
Joel's decisions on the 2026-10-14 rule-collision analysis: the data side (the code side is in
classify_emails / web_db / review_app). Dry run by default; --apply writes. Every change is logged to
edit_log as 'claude (asked by joel)'. Safe to run again: each step checks before it changes anything.

    python apply_rule_decisions_20261014.py            # show what would change
    python apply_rule_decisions_20261014.py --apply    # make the changes

1. One vendor, two Setup rows with opposite Exclude ticks: merged into the row that has the QuickBooks
   names and account, with Exclude ticked; the other row's spellings become aliases, and that row goes.
   (Until 2026-10-14 these were excluded only because the exclusion matched names that merely
   contained an excluded name; it now matches whole names.)
2. Spellings, so "Ignore payment confirmations" catches JCP&L and NJNG the way the AI writes them.
3. Exclude from QB: Institute for Advanced Judaic Studies (its learning says to ignore it) and NJ Natural
   Gas (Joel: both utilities ignored). Then the exclusion backfill (records not yet entered).
4. Standard Process back on QB Entry: 3 Refuah bills kept out because 'Anda' is inside 'Standard Process'.
5. The 7 utility payments recorded before "Ignore payment confirmations" was ticked: set aside.
6. The learning about store-to-store transfers is deleted (Joel: outdated; the exclusion wins).
7. 3M's store note: 382 Route 59 Ste 276 is the shared back office, not Maple.
"""
import sys

import psycopg2.extras

import classify_emails as ce
import web_db

APPLY = "--apply" in sys.argv
WHO = "claude (asked by joel)"
STORE_COLS = ["name_3m", "name_madison", "name_maple", "name_refuah", "name_westgate", "name_bmw",
              "name_lakewood_rx"]
FLAGS = ["exclude_from_qb", "exclude_from_statement_recon", "trust_line_item_totals",
         "ignore_payment_confirmations"]

MERGES = [                                    # (row kept, rows merged into it)
    ("Parmed Pharmaceuticals", ["Parmed"]),
    ("Auburn Pharmaceuticals", ["Auburn"]),
    ("Blupax Pharmaceuticals", ["BluPax", "BluPax Pharma"]),
    ("Masters Pharmaceuticals", ["Masters"]),
    ("Jersey Central Power & Light", ["Jersey Central Power and Light"]),
]
SPELLINGS = [
    ("Jersey Central Power & Light", ["JCP&L", "Jersey Central Power & Light (JCP&L)"]),
    ("NJ Natural Gas", ["NJNG (New Jersey Natural Gas)"]),
]
EXCLUDE = ["Institute for Advanced Judaic Studies", "NJ Natural Gas"]
STANDARD_PROCESS = [2277, 3112, 3965]
UTILITY_PAYMENTS = [60, 170, 171, 961, 962, 963, 964]
# Found by its text: every classify run re-saves the learnings under new ids (#4648 at 11:00 was #4720 by noon).
TRANSFER_LEARNING = "These are records of inventory transferred between stores"
OLD_3M = ("(382 Route 59 = Maple Pharmacy/NY; 1091 River Ave = 3M Pharmacy/NJ)",
          "(59 Route 59 Ste 143, Monsey = Maple Pharmacy/NY; 1091 River Ave, Lakewood = 3M Pharmacy/NJ; "
          "382 Route 59 Ste 276 is the shared back-office address and doesn't decide between them)")
OLD_3M_TAIL = ("if there's truly no address to disambiguate, set store_confidence to low and needs_human_review "
               "to true rather than guessing.",
               "if there's truly no address to disambiguate, look at the bigger picture (what was billed, and "
               "the [HISTORY] line when there is one); if that doesn't settle it, set store_confidence to low and "
               "needs_human_review to true rather than guessing.")


def _row(cur, name):
    cur.execute("SELECT * FROM vendor_name_map WHERE standard_vendor_name = %s", (name,))
    return cur.fetchone()


def _spellings(r):
    names = [r["standard_vendor_name"]]
    names += [a.strip() for a in (r.get("aliases") or "").splitlines() if a.strip()]
    names += [r[c] for c in STORE_COLS if (r.get(c) or "").strip()]
    out, seen = [], set()
    for n in names:
        if n.strip().lower() not in seen:
            seen.add(n.strip().lower())
            out.append(n.strip())
    return out


def merge_rows(cur):
    print("\n1. Merge duplicate vendor rows (Exclude ticked on the kept row)")
    keys_after = set()
    for keeper, merged in MERGES:
        k = _row(cur, keeper)
        if not k:
            print(f"   !! '{keeper}' not found, skipped")
            continue
        for mname in merged:
            m = _row(cur, mname)
            if not m:
                print(f"   '{mname}' -> '{keeper}': already merged")
                continue
            present = {s.lower() for s in _spellings(k)}
            add = [s for s in _spellings(m) if s.lower() not in present]
            flags = [f for f in FLAGS if (m.get(f) or f == "exclude_from_qb") and not k.get(f)]
            cur.execute("SELECT standard_account_name FROM vendor_account_assignment WHERE standard_vendor_name = %s",
                        (mname,))
            acct = cur.fetchone()
            print(f"   '{mname}' (row {m['id']}) -> '{keeper}' (row {k['id']}): aliases + {add or 'nothing'}; "
                  f"tick {flags or 'nothing'}; delete row {m['id']}"
                  + (f"; !! its account '{acct['standard_account_name']}' is left as is" if acct else ""))
            if APPLY:
                aliases = [a.strip() for a in (k.get("aliases") or "").splitlines() if a.strip()] + add
                sets, vals = ["aliases = %s"], ["\n".join(aliases) or None]
                for f in flags:
                    sets.append(f"{f} = TRUE")
                cur.execute(f"UPDATE vendor_name_map SET {', '.join(sets)}, edited_by = %s, edited_at = now() "
                            f"WHERE id = %s", vals + [WHO, k["id"]])
                if add:
                    web_db._ref_log(cur, "vendor_name_map", k["id"], "aliases", k.get("aliases"),
                                    "\n".join(aliases), WHO)
                for f in flags:
                    web_db._ref_log(cur, "vendor_name_map", k["id"], f, "False", "True", WHO)
                cur.execute("DELETE FROM vendor_name_map WHERE id = %s", (m["id"],))
                web_db._ref_log(cur, "vendor_name_map", m["id"], "standard_vendor_name", mname, None,
                                f"{WHO} (merged into '{keeper}')")
                k = _row(cur, keeper)
            else:
                k = dict(k, aliases="\n".join(filter(None, [(k.get("aliases") or "").strip()] + add)),
                         **{f: True for f in flags})
        keys_after |= {ce._norm_vendor_std_key(s) for s in _spellings(k)}
    return keys_after


def add_spellings(cur):
    print("\n2. Spellings for 'Ignore payment confirmations'")
    keys = set()
    for std, names in SPELLINGS:
        r = _row(cur, std)
        if not r:
            print(f"   !! '{std}' not found, skipped")
            continue
        present = {s.lower() for s in _spellings(r)}
        for n in names:
            if n.lower() in present:
                print(f"   '{n}' is already a spelling of '{std}'")
            else:
                print(f"   '{n}' -> '{std}'")
                if APPLY:
                    web_db.attach_vendor_spelling(std, n, None, WHO)
            keys.add(ce._norm_vendor_std_key(n))
        keys |= {ce._norm_vendor_std_key(s) for s in _spellings(r)}
    return keys


def exclude(cur):
    print("\n3. Exclude from QuickBooks")
    keys = set()
    for name in EXCLUDE:
        r = _row(cur, name)
        if not r:
            print(f"   !! '{name}' not found, skipped")
            continue
        print(f"   '{name}' (row {r['id']}): " + ("already excluded" if r["exclude_from_qb"] else "tick Exclude"))
        if APPLY and not r["exclude_from_qb"]:
            web_db.exclude_vendor(name, WHO)
        keys |= {ce._norm_vendor_std_key(s) for s in _spellings(r)}
    return keys


def backfill(cur, future_keys):
    print("\n   Records not yet entered that become kept out of QB:")
    keys = ce._excluded_vendor_keys() | future_keys
    for table in ("invoices", "credit_memos"):
        cur.execute(f"SELECT id, vendor_matched, store_matched, amount FROM {table} "
                    f"WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE AND coalesce(vendor_matched, '') <> ''")
        for r in cur.fetchall():
            if ce._norm_vendor_std_key(r["vendor_matched"]) in keys:
                print(f"     {table} {r['id']}: {r['vendor_matched']} / {r['store_matched']} / ${r['amount']}")
    if APPLY:
        print(f"   backfill flipped {ce.reapply_excluded_vendor_backfill()} record(s)")


def standard_process(cur):
    print("\n4. Standard Process back on QB Entry")
    assert not ce.is_excluded_vendor("Standard Process"), "the whole-name exclusion code isn't in place"
    for rid in STANDARD_PROCESS:
        cur.execute("SELECT vendor_matched, store_matched, invoice_number, amount, skip_qb_import, entered_in_qb, "
                    "ignored_by, notes FROM invoices WHERE id = %s", (rid,))
        r = cur.fetchone()
        if not r or (r["vendor_matched"] or "").strip().lower() != "standard process":
            print(f"   !! invoice {rid} is not a Standard Process bill, skipped")
            continue
        if not r["skip_qb_import"]:
            print(f"   #{r['invoice_number']} ${r['amount']}: already on QB Entry")
            continue
        if r["entered_in_qb"] or r["ignored_by"]:
            print(f"   !! #{r['invoice_number']}: entered or set aside by a person, left alone")
            continue
        print(f"   #{r['invoice_number']} {r['store_matched']} ${r['amount']}: back on QB Entry")
        if APPLY:
            note = ("[Back on QB Entry 2026-10-14: it was kept out because the excluded vendor 'Anda' is inside "
                    "'Standard Process'; exclusions now match whole names.]")
            notes = ((r["notes"] or "") + " " + note).strip()
            cur.execute("UPDATE invoices SET skip_qb_import = FALSE, notes = %s, edited_at = now() WHERE id = %s",
                        (notes, rid))
            web_db._ref_log(cur, "invoices", rid, "skip_qb_import", "True", "False", WHO)
            web_db._ref_log(cur, "invoices", rid, "notes", r["notes"], notes, WHO)


def utility_payments(cur):
    print("\n5. Set aside the utility payment confirmations recorded before the setting")
    for pid in UTILITY_PAYMENTS:
        cur.execute("SELECT vendor_matched, store_matched, amount, payment_date, entered_in_qb, skip_qb_import, "
                    "ignored_by, notes FROM payments WHERE id = %s", (pid,))
        r = cur.fetchone()
        v = (r or {}).get("vendor_matched") or ""
        if not r or not any(w in v.lower() for w in ("jersey central", "njng", "natural gas")):
            print(f"   !! payment {pid} is not a JCP&L / NJNG payment, skipped")
            continue
        label = f"payment {pid}: {v} / {r['store_matched']} / ${r['amount']} / {r['payment_date'] or 'no date'}"
        if r["entered_in_qb"]:
            print(f"   !! {label}: entered in QuickBooks, left alone")
            continue
        if r["skip_qb_import"]:
            print(f"   {label}: already set aside")
            continue
        print(f"   {label}: set aside")
        if APPLY:
            web_db.set_record_ignored("payment", pid, True, WHO)
            note = ("[Set aside 2026-10-14: a utility payment confirmation, and the vendor is set to Ignore "
                    "payment confirmations (Joel).]")
            notes = ((r["notes"] or "") + " " + note).strip()
            cur.execute("UPDATE payments SET notes = %s WHERE id = %s", (notes, pid))
            web_db._ref_log(cur, "payments", pid, "notes", r["notes"], notes, WHO)


def transfer_learning(cur):
    print("\n6. Delete the store-to-store transfer learning")
    cur.execute("SELECT id, learned_rule FROM learnings WHERE learned_rule LIKE %s", (TRANSFER_LEARNING + "%",))
    rows = cur.fetchall()
    if not rows:
        print("   already deleted")
        return
    if len(rows) > 1:
        print(f"   !! {len(rows)} learnings start that way, skipped")
        return
    r = rows[0]
    print(f"   learning {r['id']}: \"{r['learned_rule'][:90]}…\"")
    if APPLY:
        web_db.delete_learning_web(r["learned_rule"], WHO)


def note_3m(cur):
    print("\n7. 3M's store note")
    cur.execute("SELECT id, notes FROM stores WHERE store_name = '3M Pharmacy'")
    r = cur.fetchone()
    if not r:
        print("   !! 3M Pharmacy not found")
        return
    notes = r["notes"] or ""
    new = notes.replace(*OLD_3M).replace(*OLD_3M_TAIL)
    if new == notes:
        print("   already updated" if OLD_3M[1] in notes else "   !! the expected text isn't there, skipped")
        return
    print(f"   {OLD_3M[0]}\n   -> {OLD_3M[1]}")
    if APPLY:
        web_db.edit_reference("stores", r["id"], {"notes": new}, WHO)


def main():
    print(("APPLYING" if APPLY else "DRY RUN (nothing is changed; --apply to make the changes)")
          + " — Joel's 2026-10-14 rule decisions")
    conn = web_db.connect()                       # autocommit, like the app's own Setup edits
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            keys = merge_rows(cur)
            keys |= add_spellings(cur)
            keys |= exclude(cur)
            backfill(cur, keys)
            standard_process(cur)
            utility_payments(cur)
            transfer_learning(cur)
            note_3m(cur)
    finally:
        conn.close()
    print("\nDone." if APPLY else "\nDry run only. Run again with --apply to make these changes.")


if __name__ == "__main__":
    main()
