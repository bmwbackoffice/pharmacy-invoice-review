"""
Save the pending unmatched rows of a QuickBooks upload made BEFORE the app kept them — the Oct 8,
2026 upload (batch 6) — as if they'd been kept at upload. A pending row waits for a later email: when
that invoice is recorded, it's marked entered in QB on arrival instead of being entered a second time.

The app didn't keep that file, so pass the same export again:

    python backfill_qb_unmatched.py "C:\\Users\\joel scher\\Downloads\\All 7 stores.xlsx"           # dry run
    python backfill_qb_unmatched.py "C:\\Users\\joel scher\\Downloads\\All 7 stores.xlsx" --apply   # save
      --list      also print every row it would keep
      --batch N   the upload to attach the rows to (default: the latest upload of a file with this name)

It first checks the file IS that upload (same number of Bill/Credit rows; --force skips the check).
Then, for every Bill/Credit row:
  - exactly matches a record NOT yet marked entered (store + standard vendor + number + amount — the
    upload's own auto-match test; e.g. its email arrived after the upload): saved as resolved, and the
    record is marked entered in QB (logged to edit_log), just as a new email is on arrival;
  - was shown for review at upload and confirmed (or is still waiting for review): skipped;
  - anything else, including review rows that were rejected, is kept or skipped by the SAME rule a new
    upload now uses (qb_match.UnmatchedKeeper): dated on/after the app's start and within 120 days of
    the upload, not a document the app already has in any status, and not already saved.

DRY RUN BY DEFAULT — prints what it would do and changes nothing. --apply writes it all in one
transaction (rolled back on any error) and keeps a copy of the file in qb_imports\\. Safe to re-run:
rows already saved are skipped.
"""
import argparse
import os
from collections import Counter

import psycopg2.extras

import classify_emails as ce
import database
import qb_match as qm


def _money(v):
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return "(no amount)"


def _in_pool(r):
    """A record the upload's matching looks at: not entered, not excluded, not ignored as a duplicate."""
    return not r["entered_in_qb"] and not r["skip_qb_import"] and not r["duplicate_ignored"]


def _exact_matches(keeper, it):
    """Every record (any status) the upload's Tier-1 test calls this row's document: same store +
    standard vendor + number + amount."""
    return [r for r in keeper.records.get((it["table"], it["store"], qm._num_bucket(it["num"])), ())
            if qm._std_eq(it["std"], r["standard_vendor"]) and qm._num_eq(it["num"], r["number"])
            and ce._amounts_equal(it["amount"], r["amount"])]


def _describe(it):
    day = f"{it['date']:%m/%d/%Y}" if it["date"] else "(no date)"
    return (f"{it['store']:<18} {day:<10}  {it['type']:<6} {str(it['name'] or '')[:30]:<30} "
            f"#{qm._num_str(it['num']) or '(none)':<16} {_money(it['amount']):>12}")


def _insert(cur, batch_id, it, resolved_to=None):
    rtype, rid = resolved_to or (None, None)
    cur.execute("INSERT INTO qb_unmatched_rows (batch_id, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, "
                "qb_amount, qb_store, status, resolved_record_type, resolved_record_id, resolved_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END)",
                (batch_id, it["type"], it["date"], qm._num_str(it["num"]) or None, it["name"], it["std"],
                 it["amount"], it["store"], "resolved" if rid else "pending", rtype, rid, bool(rid)))


def main():
    ap = argparse.ArgumentParser(description="Save the pending unmatched rows of an earlier QuickBooks upload.")
    ap.add_argument("file", help="the QuickBooks export (.xlsx) that was uploaded")
    ap.add_argument("--batch", type=int, help="upload id (default: latest upload of a file with this name)")
    ap.add_argument("--apply", action="store_true", help="save (default: dry run)")
    ap.add_argument("--list", action="store_true", help="print every row it would keep")
    ap.add_argument("--force", action="store_true", help="skip the same-file check")
    args = ap.parse_args()
    if not os.environ.get("DATABASE_URL"):
        print("backfill_qb_unmatched needs DATABASE_URL (DB mode). Nothing to do.")
        return
    with open(args.file, "rb") as f:
        data = f.read()
    rows = qm._parse_rows(data)
    std = ce.load_vendor_standardization()   # before the transaction, as process_qb_export does

    conn = database.connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if args.batch:
                cur.execute("SELECT * FROM qb_import_batches WHERE id = %s", (args.batch,))
            else:
                cur.execute("SELECT * FROM qb_import_batches WHERE filename = %s ORDER BY id DESC LIMIT 1",
                            (os.path.basename(args.file),))
            batch = cur.fetchone()
            if not batch:
                print("No QuickBooks upload of a file with this name — pass --batch N (the upload's id).")
                conn.rollback()
                return
            when = batch["uploaded_at"]
            print(f"QuickBooks upload #{batch['id']}: \"{batch['filename']}\", uploaded by {batch['uploaded_by']} "
                  f"on {when:%m/%d/%Y %I:%M %p}.")
            n_file = sum(1 for r in rows if str(r["type"] or "").strip().lower() in ("bill", "credit"))
            if n_file != batch["n_rows"]:
                print(f"File check: this file has {n_file:,} Bill/Credit rows but that upload had "
                      f"{batch['n_rows'] or 0:,} — it isn't the same export.")
                if not args.force:
                    print("Nothing changed. (--force runs it anyway.)")
                    conn.rollback()
                    return
            else:
                print(f"File check: {n_file:,} Bill/Credit rows, the same as that upload.")

            cur.execute("SELECT qb_store, qb_type, qb_date, qb_num, qb_name, qb_amount, status "
                        "FROM qb_match_candidates WHERE batch_id = %s ORDER BY id", (batch["id"],))
            review = {}
            for c in cur.fetchall():
                review.setdefault(qm.unmatched_row_key(c["qb_store"], c["qb_type"], c["qb_date"], c["qb_num"],
                                                       c["qb_name"], c["qb_amount"]), []).append(c["status"])
            keeper = qm.UnmatchedKeeper(cur, when.date())

            keep, resolve, near, twins, skipped, taken = [], [], [], [], Counter(), set()
            for row in rows:
                qtype = str(row["type"]).strip().lower() if row["type"] is not None else ""
                if qtype == "bill":
                    table, rtype = "invoices", "invoice"
                    amount = abs(qm._num(row["amount"])) if qm._num(row["amount"]) is not None else None
                elif qtype == "credit":
                    table, rtype = "credit_memos", "credit_memo"
                    amount = qm._num(row["amount"])
                else:
                    continue
                it = {"table": table, "rtype": rtype, "type": row["type"], "date": qm._parse_date(row["date"]),
                      "num": row["num"], "name": row["name"], "amount": amount, "store": row["store"],
                      "std": ce.resolve_standard_vendor(row["name"], std_tables=std)}
                # One QB row is one document. If a matching record is already entered (the upload marked
                # it, or a person did), the row is used up: another not-entered match is a second copy of
                # that record, which is reported, not marked entered.
                exact = _exact_matches(keeper, it)
                entered = [r for r in exact if r["entered_in_qb"] or (table, r["id"]) in taken]
                live = [r for r in exact if _in_pool(r) and (table, r["id"]) not in taken]
                if live and entered:
                    twins.append((it, entered[0], live))
                elif live:
                    taken.add((table, live[0]["id"]))
                    resolve.append((it, live[0]))
                    continue
                statuses = review.get(qm.unmatched_row_key(it["store"], it["type"], it["date"], it["num"],
                                                          it["name"], it["amount"]))
                status = statuses.pop(0) if statuses else None
                if status in ("confirmed", "pending"):
                    skipped["shown for review at upload and " + ("confirmed" if status == "confirmed"
                                                                   else "still waiting for review")] += 1
                    continue
                why = keeper.skip_reason(table, it["store"], it["type"], it["date"], it["num"], it["name"],
                                         it["std"], amount)
                if why == "in the app" and not exact:
                    on_file = keeper.record_on_file(table, it["store"], it["std"], it["num"], amount)
                    if _in_pool(on_file):
                        near.append((it, on_file))
                if why:
                    skipped[why] += 1
                else:
                    keep.append(it)

            since = f"{keeper.since:%m/%d/%Y}"
            wording = {
                "older": f"dated before {since} (QuickBooks history from before the app)",
                "in the app": "already in the app as a record (entered, excluded or ignored), same document",
                "already saved": "already saved as unmatched rows",
                "no date": "no date in the export",
            }
            print(f"\nOf its {n_file:,} rows:")
            print(f"  {len(keep):>6,}  to keep as pending unmatched rows (dated {since} or later, no record in the app)")
            print(f"  {len(resolve):>6,}  match a record not yet marked entered: saved as resolved, and the record "
                  f"is marked entered in QB")
            for why, n in skipped.most_common():
                print(f"  {n:>6,}  skipped: {wording.get(why, why)}")

            if keep:
                by_store = Counter(it["store"] for it in keep)
                by_month = Counter(f"{it['date']:%Y-%m}" for it in keep)
                print("\nRows to keep, by store: " + ", ".join(f"{s} {n}" for s, n in sorted(by_store.items())))
                print("               by month: " + ", ".join(f"{m} {n}" for m, n in sorted(by_month.items())))
                shown = keep if args.list else keep[:15]
                print(f"{'Every row' if args.list else 'First 15'} to keep:")
                for it in shown:
                    print("    " + _describe(it))
            if resolve:
                print("\nRecords to mark entered in QB (QuickBooks already has them):")
                for it, rec in resolve:
                    print(f"    {_describe(it)}  ->  {it['rtype'].replace('_', ' ')} #{rec['id']}")
            if twins:
                print(f"\n{len(twins)} record(s) look like a second copy of a record already entered in QB (same store, "
                      f"vendor, number and amount). Not changed; check them and ignore the copy as a duplicate:")
                for it, first, copies in twins:
                    label = it["rtype"].replace("_", " ")
                    print(f"    {_describe(it)}  entered: {label} #{first['id']}  copy: "
                          + ", ".join(f"#{r['id']}" for r in copies))
            if near:
                print(f"\n{len(near)} row(s) look like a record that isn't marked entered, but don't match it exactly "
                      f"(same number; vendor or amount differs). Not saved; the next QuickBooks upload shows them "
                      f"for review:")
                for it, rec in near[:20]:
                    print(f"    {_describe(it)}  ~  {it['rtype'].replace('_', ' ')} #{rec['id']} "
                          f"({_money(rec['amount'])})")

            if not args.apply:
                print("\nDry run — nothing changed. Re-run with --apply to save these rows.")
                conn.rollback()
                return
            actor = f"{batch['uploaded_by'] or 'someone'} via QB unmatched backfill"
            for it in keep:
                _insert(cur, batch["id"], it)
            for it, rec in resolve:
                cur.execute(f"UPDATE {it['table']} SET entered_in_qb = TRUE, entered_qb_by = %s, "
                            f"entered_qb_at = now() WHERE id = %s AND entered_in_qb = FALSE", (actor, rec["id"]))
                if cur.rowcount != 1:
                    raise RuntimeError(f"{it['table']} #{rec['id']} changed while this ran — nothing saved; run it again.")
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES (%s,%s,'entered_in_qb','false',%s,%s)",
                    (it["table"], rec["id"],
                     f"true (found in QuickBooks upload #{batch['id']} of {when:%m/%d/%Y} — {it['type']} "
                     f"Num={qm._num_str(it['num'])} Amount={it['amount']} Date={it['date']}; saved by the "
                     f"unmatched-rows backfill)", actor))
                _insert(cur, batch["id"], it, resolved_to=(it["rtype"], rec["id"]))
        conn.commit()
        print(f"\n--apply: saved {len(keep):,} pending unmatched row(s) and marked {len(resolve)} record(s) entered "
              f"in QB. They're on QB reconcile › Pending unmatched rows.")
        path = qm.keep_upload_copy(batch["id"], batch["filename"], data)
        if path:
            print(f"Kept a copy of the file: {path}")
    except Exception:
        conn.rollback()
        print("backfill_qb_unmatched: ERROR — rolled back, nothing changed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
