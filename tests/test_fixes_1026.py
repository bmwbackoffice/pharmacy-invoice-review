"""Fixes Joel picked from the 2026-10-26 review: statement lines found in QuickBooks clear themselves, duplicate
settling also needs the copies' dates within 10 days, schema statements only run when they change (and never
queue behind a busy table), the dashboard puts the money tiles first and marks stale stores, the menu moves
(Learnings to Setup › Rules, Flags + Change requests to the user menu, a Menu button on narrow windows), and the
Records page shows one page at a time."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone

import _base  # noqa: F401
import database as db
import review_app
import web_db
from _base import ce
from test_db_rollback import RollbackTestCase

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class StatementLinesInQuickBooks(RollbackTestCase):
    """5 'missing' statement lines were in QuickBooks all along: HealthSource x2 (the statement says 'HealthSource
    Distributors'), Chester x2 (credits; QuickBooks keeps them without the minus sign). Lifeseed #808 is in
    QuickBooks for $585.36, not the statement's $906.00, so it stays for a person."""

    def setUp(self):
        super().setUp()
        c = self.cur
        c.execute("INSERT INTO emails (filename, source, status, received_at) "
                  "VALUES ('ZZTEST statement.eml', 'gmail', 'processed', now())")
        c.execute("INSERT INTO qb_import_batches (filename, uploaded_by) VALUES ('zz.xlsx', 'zz') RETURNING id")
        self.batch = c.fetchone()[0]

    def _line(self, vendor, number, day, amount):
        self.cur.execute(
            "INSERT INTO statement_lines (statement_email, statement_file, vendor, invoice_number, invoice_date, amount, "
            "status, created_at, updated_at) VALUES ('ZZTEST statement.eml', 'ZZTEST statement.eml', %s, %s, %s, %s, "
            "'missing', now(), now()) RETURNING id", (vendor, number, day, amount))
        return self.cur.fetchone()[0]

    def _qb(self, qb_type, number, name, day, amount):
        self.cur.execute("INSERT INTO qb_register (batch_id, qb_store, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, "
                         "qb_amount) VALUES (%s, 'Maple Pharmacy', %s, %s, %s, %s, %s, %s)",
                         (self.batch, qb_type, day, number, name, name, amount))

    def _status(self, lid):
        self.cur.execute("SELECT status, dismissed_by FROM statement_lines WHERE id = %s", (lid,))
        return self.cur.fetchone()

    def test_lines_in_quickbooks_clear_and_a_different_amount_stays(self):
        d = date.today() - timedelta(days=20)
        spelled = self._line("Zz HealthSource Distributors", "ZZWSTLC-1110", d, 172.96)
        self._qb("Bill", "ZZWSTLC-1110", "Zz HealthSource", d, 172.96)
        credit = self._line("Zz Chester Plastic & Paper Sales", "ZZCC1414", d, -22.50)
        self._qb("Credit", "ZZCC1414", "Zz Chester Plastic & Paper Goods", d, 22.50)
        other_amount = self._line("Zz Lifeseed Supplements", "ZZ808", d, 906.00)
        self._qb("Bill", "ZZ808", "Zz lifeseed", d, 585.36)
        unrelated = self._line("Zz Alpha Vendor", "ZZ555X", d, 100.00)
        self._qb("Bill", "ZZ555X", "Zz Omega Supply", d - timedelta(days=30), 100.00)
        counts = db.reconcile_statement_lines()
        self.assertGreaterEqual(counts["in_quickbooks"], 2)
        self.assertEqual(self._status(spelled), ("dismissed", "auto: found in QuickBooks"))
        self.assertEqual(self._status(credit)[0], "dismissed", "a credit, kept without its minus sign in QuickBooks")
        self.assertEqual(self._status(other_amount)[0], "missing", "QuickBooks has another amount: a person decides")
        self.assertEqual(self._status(unrelated)[0], "missing", "same number, but another vendor and date")
        self.cur.execute("SELECT new_value FROM edit_log WHERE table_name = 'statement_lines' AND record_id = %s",
                         (spelled,))
        self.assertIn("in QuickBooks", self.cur.fetchone()[0])


class SettleNeedsCloseDates(RollbackTestCase):
    """dup_settle compared store, amount, vendor and number, never the copies' dates."""

    def setUp(self):
        super().setUp()
        self._saved_std, ce.VENDOR_STD = ce.VENDOR_STD, {"lookup": {}, "zz": True}   # non-empty: no reload
        ce._DEDUPE_VENDOR_CACHE.clear()
        self.n = 0

    def tearDown(self):
        ce.VENDOR_STD = self._saved_std
        ce._DEDUPE_VENDOR_CACHE.clear()
        super().tearDown()

    def _copy(self, number, day, entered, gid):
        self.n += 1
        fn = f"ZZTEST settle {gid} {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename) VALUES (%s)", (fn,))
        self.cur.execute(
            "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, invoice_date, "
            "entered_in_qb, entered_qb_at, duplicate_suspect, duplicate_ignored, skip_qb_import, duplicate_group_id) "
            "VALUES (%s, 'Zz Settle Vendor', 'Refuah Pharmacy', %s, 4321.09, %s, %s, %s, %s, FALSE, FALSE, %s)",
            (fn, number, day, entered, datetime.now(timezone.utc) if entered else None, not entered, gid))

    def _result(self, gid):
        import dup_settle
        out = dup_settle.settle_duplicate_sets(dry_run=True, verbose=False)
        return ([r for r in out["resolved"] if r[0] == gid], [w for g, _l, w in out["left"] if g == gid])

    def test_copies_a_month_apart_are_left_for_a_person(self):
        self._copy("ZZ797208", "2026-08-10", True, "ZZGROUPFAR")
        self._copy("ZZ797203", "2026-09-10", False, "ZZGROUPFAR")
        resolved, left = self._result("ZZGROUPFAR")
        self.assertEqual(resolved, [])
        self.assertTrue(left and "dates" in left[0])

    def test_copies_days_apart_still_settle(self):
        self._copy("ZZ797208", "2026-08-10", True, "ZZGROUPNEAR")
        self._copy("ZZ797203", "2026-08-13", False, "ZZGROUPNEAR")
        resolved, _left = self._result("ZZGROUPNEAR")
        self.assertEqual(len(resolved), 1)


class SchemaOnlyWhenChanged(unittest.TestCase):
    """init_db's 82 ALTER TABLEs ran on every start; with a run holding the tables, the web app queued behind it.
    A fake database stands in: it remembers the stamp and can be "busy" (LockNotAvailable) for a few tries."""

    class FakeDB:
        def __init__(self):
            self.stamp, self.busy, self.log = None, 0, []

    class Cur:
        def __init__(self, fdb):
            self.fdb, self.res = fdb, None

        def execute(self, sql, params=None):
            from psycopg2 import errors
            self.fdb.log.append(sql)
            if "to_regclass('schema_stamp')" in sql:
                self.res = (self.fdb.stamp is not None,)
            elif sql.startswith("SELECT fingerprint"):
                self.res = (self.fdb.stamp,)
            elif sql == "ZZ SCHEMA" and self.fdb.busy > 0:
                self.fdb.busy -= 1
                raise errors.LockNotAvailable("busy")
            elif sql.startswith("INSERT INTO schema_stamp"):
                self.fdb.stamp = params[1]

        def fetchone(self):
            return self.res

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def __init__(self, fdb):
            self.fdb = fdb

        def cursor(self, *a, **k):
            return SchemaOnlyWhenChanged.Cur(self.fdb)

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def setUp(self):
        self._saved = (db.connect, db.SCHEMA_RETRY_SECONDS)
        self.fdb = self.FakeDB()
        db.connect = lambda: self.Conn(self.fdb)
        db.SCHEMA_RETRY_SECONDS = 0

    def tearDown(self):
        db.connect, db.SCHEMA_RETRY_SECONDS = self._saved

    def _runs(self):
        return self.fdb.log.count("ZZ SCHEMA")

    def test_runs_once_then_skips_until_the_text_changes(self):
        self.assertTrue(db.apply_schema("zz", "ZZ SCHEMA"))
        self.assertTrue(db.apply_schema("zz", "ZZ SCHEMA"))
        self.assertEqual(self._runs(), 1, "unchanged: no schema statement at all")
        self.assertTrue(db.apply_schema("zz", "ZZ SCHEMA", "-- changed"))
        self.assertEqual(self._runs(), 2)

    def test_a_busy_table_is_retried_then_given_up(self):
        self.fdb.busy = 2
        self.assertTrue(db.apply_schema("zz", "ZZ SCHEMA"), "the third try gets the lock")
        self.assertEqual(self._runs(), 3)
        self.fdb.stamp, self.fdb.busy = None, 99
        self.assertFalse(db.apply_schema("zz", "ZZ SCHEMA"), "carries on with the schema it has")
        self.assertTrue(all(s.startswith("SET LOCAL lock_timeout") for s in self.fdb.log
                            if "lock_timeout" in s), "never waits long for a lock")


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class Pages(unittest.TestCase):

    def _client(self, role="admin"):
        from starlette.testclient import TestClient
        fake = {"id": -424242, "username": "zz-fixes", "display_name": "Zz Fixes", "role": role,
                "active": True, "password_hash": "x"}
        self._saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id)
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        cl = TestClient(review_app.app)
        cl.post("/login", data={"username": "zz-fixes", "password": "x"})
        return cl

    def tearDown(self):
        web_db.get_user, web_db.verify_password, web_db.get_user_by_id = self._saved

    def test_dashboard_money_tiles_first(self):
        html = self._client().get("/dashboard").text
        order = [html.index(f'<div class="dashlabel">{label}</div>') for label in
                 ("In QuickBooks twice?", "QB corrections", "Possible duplicates", "Statement exceptions",
                  "Review queue", "QB entry", "Flags")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Database backup", html, "System health shows the nightly backup")

    def test_menu(self):
        html = self._client().get("/dashboard").text
        nav = html[html.index('<nav id="topnav">'):html.index("</nav>")]
        self.assertNotIn('href="/flags"', nav)
        self.assertNotIn('href="/requests"', nav)
        self.assertNotIn('href="/learnings"', nav, "Learnings is a Setup › Rules tab now")
        user_menu = html[html.index('class="right navgroup usermenu"'):]
        for href in ('href="/flags"', 'href="/requests"', 'href="/logout"'):
            self.assertIn(href, user_menu[:1500])
        self.assertIn('class="navtoggle"', html, "the narrow-window Menu button")
        learn = self._client().get("/learnings").text
        self.assertIn('class="setup-tabs"', learn)
        self.assertIn('href="/learnings"', learn[learn.index('class="setup-tabs"'):])

    def test_stale_store_marked_only_with_bills_waiting(self):
        now = datetime.now(timezone.utc)
        sample = [{"store": "Zz Old Waiting", "last_at": now - timedelta(days=12), "count": 3, "ready": 4},
                  {"store": "Zz Old Nothing Waiting", "last_at": now - timedelta(days=12), "count": 3, "ready": 0},
                  {"store": "Zz Recent", "last_at": now - timedelta(days=2), "count": 3, "ready": 9},
                  {"store": "Zz Never", "last_at": None, "count": 0, "ready": 2}]
        saved = web_db.last_qb_entry_by_store
        web_db.last_qb_entry_by_store = lambda *a, **k: [dict(s) for s in sample]
        try:
            html = self._client().get("/dashboard").text
        finally:
            web_db.last_qb_entry_by_store = saved

        def row(store):
            i = html.index(store)
            return html[i:html.index("</tr>", i)]
        self.assertIn("12 days ago", row("Zz Old Waiting"))
        self.assertIn("no entry yet", row("Zz Never"))
        self.assertNotIn("qb-stale", row("Zz Old Nothing Waiting"))
        self.assertNotIn("qb-stale", row("Zz Recent"))

    def test_records_page_by_page(self):
        cl = self._client()
        rows, total = web_db.list_all_records(page=1, page_size=5)
        self.assertLessEqual(len(rows), 5)
        self.assertGreater(total, 5)
        rows2, _ = web_db.list_all_records(page=2, page_size=5)
        self.assertFalse({(r["record_type"], r["record_id"]) for r in rows}
                         & {(r["record_type"], r["record_id"]) for r in rows2}, "no row on two pages")
        html = cl.get("/browse").text
        self.assertIn("Per page:", html)
        self.assertNotIn("max 2000", html)
        self.assertLess(len(html), 1_500_000, "one page, not 2,000 rows")


if __name__ == "__main__":
    unittest.main()
