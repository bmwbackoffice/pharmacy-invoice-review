"""
READ-ONLY report on the review signals that depend on the AI's reading of a document, so they
can be checked against what actually happened after real classify runs:

  sales orders       is_sales_order -> held; superseded (its invoice arrived: the call was right),
                     released (no invoice within the hold window), still held. Lists released orders
                     whose invoice turned up AFTER release (check those for a double entry).
  payment reminders  is_payment_reminder -> auto-resolved (invoice already on file) vs. recorded
                     waiting for its invoice (oldest first).
  printed totals     total_source -> 'stated' / 'derived' / not reported, per record type, and the
                     vendors whose totals were derived (the amount check sends those to review unless
                     the vendor trusts line-item totals in Setup). Saved only since 2026-10-08.
  not-received       records/review items whose duplicate reason cites not-received/short markup.
  open review items  what the review queue is waiting on, by kind of reason.

    python verify_model_signals.py              # last 30 days
    python verify_model_signals.py --days 90

Changes nothing. DB mode only (needs DATABASE_URL).
"""
import os
import sys

import psycopg2.extras

import database

# Review-reason fragments written by the deterministic checks (classify_emails apply_* gates).
_REASON_KINDS = [
    ("amount added up from line items", "calculated from line items"),
    ("no usable amount", "No usable amount was found"),
    ("no date", "found — needs a human to confirm or enter the date"),
    ("store not identified", "Store could not be identified"),
    ("store rule (Setup)", "Setup › Store rules"),
    ("vendor not in Vendor names", "No standardized vendor mapping found"),
    ("vendor has no account", "has no QuickBooks account assigned"),
    ("multiple versions", "Multiple versions of invoice"),
]


def _days():
    if "--days" in sys.argv:
        try:
            return max(1, int(sys.argv[sys.argv.index("--days") + 1]))
        except (IndexError, ValueError):
            pass
    return 30


def main():
    if not os.environ.get("DATABASE_URL"):
        print("verify_model_signals needs DATABASE_URL (DB mode). Nothing to do.")
        return
    days = _days()
    since = f"now() - interval '{days} days'"
    conn = database.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            print(f"Model-dependent review signals, last {days} day(s)\n")

            # -- sales orders --
            cur.execute(f"SELECT status, count(*) AS n FROM pending_sales_orders WHERE held_at >= {since} "
                        f"GROUP BY status ORDER BY status")
            counts = {r["status"]: r["n"] for r in cur.fetchall()}
            print("Sales orders (is_sales_order): "
                  + (", ".join(f"{n} {s}" for s, n in counts.items()) if counts else "none held"))
            if counts.get("superseded"):
                print(f"  superseded = its invoice arrived during the hold (the sales-order call was right).")
            cur.execute(f"SELECT p.id, p.vendor_matched, p.number, p.amount, p.release_after "
                        f"  FROM pending_sales_orders p WHERE p.status = 'released' AND p.held_at >= {since} "
                        f" ORDER BY p.id")
            late = []
            for o in cur.fetchall():
                num = "".join((o["number"] or "").split()).lower()
                if not num:
                    continue
                cur.execute("SELECT invoice_number, date_added, email_filename FROM invoices "
                            " WHERE norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
                            "   AND lower(regexp_replace(coalesce(invoice_number,''), '\\s', '', 'g')) LIKE %s "
                            "   AND lower(regexp_replace(coalesce(invoice_number,''), '\\s', '', 'g')) <> %s "
                            "   AND duplicate_ignored IS NOT TRUE AND date_added >= %s::date",
                            (o["vendor_matched"] or "", num + "%", num, o["release_after"]))
                for inv in cur.fetchall():
                    late.append((o, inv))
            if late:
                print(f"  {len(late)} released order(s) whose invoice arrived later under another number — "
                      f"check for a double entry:")
                for o, inv in late:
                    print(f"    order {o['vendor_matched']} #{o['number']} (released) -> invoice "
                          f"#{inv['invoice_number']} recorded {inv['date_added']}")

            # -- payment reminders --
            cur.execute(f"SELECT count(*) AS n FROM invoices WHERE date_added >= ({since})::date "
                        f"AND duplicate_reason LIKE 'Payment reminder for invoice #%%already on file%%'")
            resolved = cur.fetchone()["n"]
            waiting = database.list_open_reminder_records()
            print(f"\nPayment reminders (is_payment_reminder): {resolved} auto-resolved (invoice already on file); "
                  f"{len(waiting)} recorded and still waiting for their invoice (any age)")
            if waiting:
                cur.execute("SELECT id, date_added FROM invoices WHERE id = ANY(%s)", ([w["id"] for w in waiting],))
                added = {r["id"]: r["date_added"] for r in cur.fetchall()}
                for w in sorted(waiting, key=lambda w: added.get(w["id"]) or ""):
                    print(f"    #{w['id']} {w['vendor_matched']} invoice #{w['invoice_number'] or '?'} "
                          f"(recorded {added.get(w['id'])}{', entered in QB' if w['entered_in_qb'] else ''})")

            # -- total_source --
            print("\nPrinted totals (total_source) — recorded since the column was added (2026-10-08; records "
                  "saved earlier that day show as 'not reported'):")
            for table, label in (("invoices", "invoices"), ("credit_memos", "credit memos"), ("payments", "payments")):
                cur.execute(f"SELECT coalesce(total_source, 'not reported') AS s, count(*) AS n FROM {table} "
                            f" WHERE date_added >= greatest(({since})::date, '2026-10-08'::date) GROUP BY 1 ORDER BY 1")
                rows = cur.fetchall()
                print(f"  {label}: " + (", ".join(f"{r['n']} {r['s']}" for r in rows) if rows else "none yet"))
            cur.execute(f"SELECT vendor_matched, count(*) AS n FROM invoices WHERE total_source = 'derived' "
                        f"  AND date_added >= greatest(({since})::date, '2026-10-08'::date) "
                        f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15")
            derived = cur.fetchall()
            if derived:
                print("  recorded with a derived total (trusted vendor, or answered in review): "
                      + ", ".join(f"{r['vendor_matched']} {r['n']}" for r in derived))

            # -- not-received markup --
            cur.execute(f"SELECT count(*) AS n FROM invoices WHERE date_added >= ({since})::date "
                        f"AND duplicate_reason LIKE '%%not-received/short%%'")
            nr_rec = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM review_queue WHERE question LIKE '%%not-received/short%%'")
            nr_q = cur.fetchone()["n"]
            print(f"\nNot-received / short-shipped markup cited: {nr_rec} recorded invoice(s), "
                  f"{nr_q} open review item(s)")

            # -- open review items --
            cur.execute("SELECT question FROM review_queue")
            questions = [r["question"] or "" for r in cur.fetchall()]
            print(f"\nOpen review items: {len(questions)}")
            other = len(questions)
            for label, frag in _REASON_KINDS:
                n = sum(1 for q in questions if frag in q)
                if n:
                    print(f"  {n:>4}  {label}")
            other = sum(1 for q in questions if not any(frag in q for _l, frag in _REASON_KINDS))
            print(f"  {other:>4}  other (the AI's own question)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
