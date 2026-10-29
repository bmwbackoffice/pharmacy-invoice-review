"""
Database layer for the review web app (Phase 2B).

ADDITIVE ONLY. Reuses database.connect() and the existing tables written by the
CLI pipeline (emails, invoices, payments, credit_memos, review_queue, learnings,
invoice_history). It adds — via ALTER ... IF NOT EXISTS / CREATE ... IF NOT
EXISTS run at web startup, never by the CLI — a `users` table, an `edit_log`
table, audit columns, and an `all_records` view for browsing. The CLI's
behavior is unchanged: it never references the new columns/tables.

Skip-QB and duplicate re-evaluation reuse the SAME rules as the pipeline
(classify_emails.is_excluded_vendor / check_duplicate) — no second copy.
"""

import datetime
import json
import os
import re
import secrets
import string
import time

import bcrypt
import psycopg2
import psycopg2.extras

import database  # reuse connect() and existing helpers

# Host prefix for the internal short links. Config/env so moving to the cloud at
# Phase 2C is a one-line change (set LINK_BASE_URL to the cloud domain) — the codes
# stay stable, only the host changes. Empty → the caller's LAN base_url is used.
LINK_BASE_URL = os.environ.get("LINK_BASE_URL", "").rstrip("/")
_CODE_ALPHABET = string.ascii_letters + string.digits   # base62


def connect():
    """A fresh connection with autocommit ON. Without this, a psycopg2
    connection opens a transaction on its first SELECT and holds that MVCC
    snapshot until commit/rollback — so a long-lived or pooled connection would
    keep serving stale reads and never see rows the pipeline commits after the
    page loaded. Autocommit ends each statement's transaction immediately, so
    every request reflects the latest committed data with no restart.
    Multi-statement writes that need atomicity flip autocommit off locally
    (see update_record_field)."""
    conn = database.connect()
    conn.autocommit = True
    return conn


# --------------------------------------------------------------------------- #
# Additive schema (users, edit_log, audit columns, browse view)
# --------------------------------------------------------------------------- #

WEB_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL DEFAULT 'reviewer',   -- 'admin' | 'reviewer'
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS edit_log (
    id          SERIAL PRIMARY KEY,
    table_name  TEXT,
    record_id   INTEGER,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT,
    changed_by  TEXT,
    changed_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS reviewed_by TEXT;
ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS assigned_to TEXT;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS display_name TEXT;

-- Per-user UI preferences (e.g. QB-entry column view/order + CSV export layout).
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences JSONB DEFAULT '{}';
-- Read-only users who may still make changes on a few pages (2026-10-21): comma list of WRITABLE_PAGES keys.
ALTER TABLE users ADD COLUMN IF NOT EXISTS edit_pages TEXT;

ALTER TABLE invoices ADD COLUMN IF NOT EXISTS entered_qb_by TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS entered_qb_at TIMESTAMPTZ;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS edited_by TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;

ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS entered_qb_by TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS entered_qb_at TIMESTAMPTZ;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS edited_by TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;

ALTER TABLE payments ADD COLUMN IF NOT EXISTS edited_by TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;
-- Payments are always QB-entered (never excluded); they get the same entered flag + audit.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS entered_in_qb BOOLEAN DEFAULT FALSE;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS entered_qb_by TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS entered_qb_at TIMESTAMPTZ;

-- Audit trail for a MANUAL duplicate-ignore override (who/when), mirroring entered_qb_by/at.
-- Set by web_db.set_duplicate_ignored(); the CLI never reads these.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS duplicate_ignored_by TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS duplicate_ignored_at TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS duplicate_ignored_by TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS duplicate_ignored_at TIMESTAMPTZ;

-- Old-system (migrate_from_excel) records that never came from a real email. Set TRUE by
-- purge_non_email_records.py --archive; excluded from the Records list + Emails ledger. Default
-- FALSE, so a no-op until archiving is used. (The CLI never reads this column.)
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS legacy_migrated BOOLEAN DEFAULT FALSE;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS legacy_migrated BOOLEAN DEFAULT FALSE;
ALTER TABLE payments     ADD COLUMN IF NOT EXISTS legacy_migrated BOOLEAN DEFAULT FALSE;
ALTER TABLE emails       ADD COLUMN IF NOT EXISTS legacy_migrated BOOLEAN DEFAULT FALSE;

-- Free-form notes/comments on a review item. Keyed by the item's review_key (source email +
-- vendor + number), which survives reprocessing and re-flags.
CREATE TABLE IF NOT EXISTS review_notes (
    id          SERIAL PRIMARY KEY,
    review_key  TEXT NOT NULL,
    note        TEXT NOT NULL,
    author      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_review_notes_key ON review_notes (review_key);

-- Dismissed ("ignored") review items. Web-only: ignoring an item snapshots the full
-- review_queue row here and DELETEs it from the queue — instant, no reprocess/API call.
-- The snapshot allows an exact restore back into the queue (under its old id when that's still
-- free, else a new one). The CLI never touches this table.
CREATE TABLE IF NOT EXISTS ignored_reviews (
    id             SERIAL PRIMARY KEY,
    snapshot       JSONB NOT NULL,            -- the full review_queue row, for exact restore
    source_email   TEXT, vendor_matched TEXT, store_matched TEXT,
    invoice_number TEXT, filename TEXT, question TEXT, run_id TEXT,  -- denormalized for the list view
    ignore_reason  TEXT,
    ignored_by     TEXT, ignored_at TIMESTAMPTZ DEFAULT now()
);

-- Opt-in outcome tracking for answered review items: keep the item visible in the
-- Tracked tab so a reviewer can confirm HOW their answer resolved (from the normal
-- reprocess results already in the DB — no extra API call) before writing a rule.
-- Web-only; the CLI never touches it.
CREATE TABLE IF NOT EXISTS tracked_reviews (
    id SERIAL PRIMARY KEY,
    filename TEXT, source_email TEXT,
    vendor_matched TEXT, store_matched TEXT, invoice_number TEXT,
    original_question TEXT, answer_given TEXT,
    tracked_by TEXT, tracked_at TIMESTAMPTZ DEFAULT now(),
    status TEXT DEFAULT 'open'          -- 'open' | 'done'
);
-- The item's review key (source email + vendor + number) at track time. A re-flagged split item
-- gets a NEW filename (…__2) but keeps this key, so the outcome follows the item (list_tracked).
ALTER TABLE tracked_reviews ADD COLUMN IF NOT EXISTS review_key TEXT;

-- Internal short links (private URL shortener) so record links fit in QuickBooks memo
-- fields. Deduped by target_url (same URL reuses its code). Nothing leaves the app —
-- /l/<code> just 302s to the stored target (email/records gated by login; see review_app).
CREATE TABLE IF NOT EXISTS links (
    id          SERIAL PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    target_url  TEXT UNIQUE NOT NULL,
    kind        TEXT,                   -- 'file' | 'email' | 'records'
    created_at  TIMESTAMPTZ DEFAULT now()
);
-- Fast dedupe-by-URL for the batch short-link resolver (redundant with the UNIQUE
-- above, but explicit / IF NOT EXISTS so it's safe on existing DBs).
CREATE UNIQUE INDEX IF NOT EXISTS links_target_url_key ON links (target_url);

-- User-submitted "Flag for review" on a specific record. Distinct from the AI review_queue
-- (which holds records the classifier flagged): this is a human saying "please look at this".
-- `snapshot` freezes the record's field values at flag time so an investigation has stable
-- context even if the record is later edited. `status` lifecycle: pending -> dismissed /
-- approved_for_fix (admin actions). An "Investigate" run is a separate `investigations` row.
CREATE TABLE IF NOT EXISTS flagged_reviews (
    id           SERIAL PRIMARY KEY,
    record_type  TEXT NOT NULL,            -- 'invoice' | 'credit_memo' | 'payment'
    record_id    INTEGER NOT NULL,
    snapshot     JSONB NOT NULL,           -- frozen record fields at flag time (see add_flag)
    note         TEXT,                     -- the submitter's free-text description (UNTRUSTED)
    submitted_by TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    status       TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'dismissed' | 'approved_for_fix'
    resolved_by  TEXT,
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_flagged_reviews_status ON flagged_reviews (status, created_at DESC);

-- One admin-triggered, READ-ONLY automated Claude Code investigation of a flag. The wrapper
-- (investigate.py) — NOT the agent — writes every column here; the agent has no DB access.
-- status lifecycle: running -> completed | failed. cost/usage captured from `claude -p` JSON.
CREATE TABLE IF NOT EXISTS investigations (
    id            SERIAL PRIMARY KEY,
    flag_id       INTEGER REFERENCES flagged_reviews(id) ON DELETE CASCADE,  -- OR request_id (one of)
    status        TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'completed' | 'failed'
    triggered_by  TEXT,
    started_at    TIMESTAMPTZ DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    findings      TEXT,                    -- the agent's summary (on completed)
    error         TEXT,                    -- reason (on failed/timeout)
    num_turns     INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      NUMERIC,
    duration_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_investigations_flag ON investigations (flag_id, started_at DESC);

CREATE OR REPLACE VIEW all_records AS
    SELECT 'invoice' AS record_type, i.id AS record_id, i.email_filename,
           i.store_matched, i.vendor_matched, i.invoice_number AS number,
           i.invoice_date AS record_date, i.amount, i.skip_qb_import,
           i.needs_review, i.duplicate_suspect, i.entered_in_qb,
           i.file_path, i.gmail_link, i.date_added, i.notes,
           e.status AS email_status, e.run_id, e.gmail_message_id,
           i.duplicate_ignored, i.duplicate_of,
           e.from_address AS sender, e.received_at, i.legacy_migrated,
           i.duplicate_group_id,
           i.ignored_by, i.ignored_at
      FROM invoices i LEFT JOIN emails e ON e.filename = i.email_filename
    UNION ALL
    SELECT 'credit_memo', c.id, c.email_filename, c.store_matched, c.vendor_matched,
           c.credit_memo_number, c.credit_memo_date, c.amount, c.skip_qb_import,
           c.needs_review, NULL::boolean, c.entered_in_qb,
           c.file_path, c.gmail_link, c.date_added, c.notes,
           e.status, e.run_id, e.gmail_message_id,
           FALSE, NULL::text,
           e.from_address, e.received_at, c.legacy_migrated,
           NULL::text,
           c.ignored_by, c.ignored_at
      FROM credit_memos c LEFT JOIN emails e ON e.filename = c.email_filename
    UNION ALL
    SELECT 'payment', p.id, p.email_filename, p.store_matched, p.vendor_matched,
           p.reference_number, p.payment_date, p.amount, p.skip_qb_import,
           p.needs_review, p.duplicate_suspect, NULL::boolean,
           p.file_path, p.gmail_link, p.date_added, p.notes,
           e.status, e.run_id, e.gmail_message_id,
           p.duplicate_ignored, p.duplicate_of,
           e.from_address, e.received_at, p.legacy_migrated,
           p.duplicate_group_id,
           p.ignored_by, p.ignored_at
      FROM payments p LEFT JOIN emails e ON e.filename = p.email_filename
    UNION ALL
    -- In-review items surfaced in Browse marked 'In review' (record_id = review_queue.id;
    -- link to /review/<id>). record_date cast to text to match the invoice arm's type.
    -- LEFT JOIN emails on the source email so sender/received populate here too.
    SELECT 'review', rq.id, rq.source_email, rq.store_matched, rq.vendor_matched,
           rq.invoice_number, rq.date_flagged::text, rq.amount, NULL::boolean, TRUE,
           NULL::boolean, NULL::boolean, rq.file_path, rq.gmail_link, rq.date_flagged, rq.question,
           'In review', rq.run_id, NULL::text, FALSE, NULL::text,
           e.from_address, e.received_at, FALSE,
           rq.duplicate_group_id,
           NULL::text, NULL::timestamptz
      FROM review_queue rq LEFT JOIN emails e ON e.filename = rq.source_email
    UNION ALL
    -- Held sales orders (2026-10-14, Joel: a held order "should show up in record search, with a label that it's
    -- held"): record_type 'sales_order', record_id = pending_sales_orders.id, email_status 'Held', notes = when it's
    -- recorded. Only while held; a released order is a normal invoice record, a superseded one has its invoice.
    SELECT 'sales_order', so.id, coalesce(so.source_email, so.source_file), so.store_matched, so.vendor_matched,
           so.number, so.order_date, so.amount, NULL::boolean, FALSE,
           NULL::boolean, NULL::boolean, so.file_path, so.gmail_link, so.held_at::date,
           'Held sales order: recorded on ' || to_char(so.release_after, 'MM/DD/YY') || ' if no invoice comes first',
           'Held', NULL::text, NULL::text, FALSE, NULL::text,
           e.from_address, e.received_at, FALSE,
           NULL::text,
           NULL::text, NULL::timestamptz
      FROM pending_sales_orders so LEFT JOIN emails e ON e.filename = coalesce(so.source_email, so.source_file)
     WHERE so.status = 'held';

-- Per-user notifications (2026-10-21): shown on the user's dashboard until dismissed. First use: a change
-- request's status changed by an admin -> its submitter is told.
CREATE TABLE IF NOT EXISTS user_notifications (
    id          SERIAL PRIMARY KEY,
    username    TEXT NOT NULL,
    message     TEXT NOT NULL,
    link        TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    read_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_user_notifications_unread ON user_notifications (username, read_at);

-- App change/feature requests: same review lifecycle as flags (pending -> dismiss/approve ->
-- optional read-only investigation -> build -> done), but NOT tied to any accounting record.
CREATE TABLE IF NOT EXISTS change_requests (
    id           SERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,          -- the submitter's free-text ask (UNTRUSTED, like a flag note)
    category     TEXT,                   -- 'bug' | 'feature' | 'change' — self-selected, informational
    submitted_by TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | dismissed | approved_for_build | done
    resolved_by  TEXT,
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_change_requests_status ON change_requests (status, created_at DESC);

-- Generalize investigations to serve a flag OR a change request (same runner, different prompt).
ALTER TABLE investigations ALTER COLUMN flag_id DROP NOT NULL;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS request_id INTEGER
    REFERENCES change_requests(id) ON DELETE CASCADE;
DO $$ BEGIN
    ALTER TABLE investigations ADD CONSTRAINT investigations_one_subject
        CHECK ((flag_id IS NOT NULL)::int + (request_id IS NOT NULL)::int = 1);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_investigations_request ON investigations (request_id, started_at DESC);

-- /no-live-copy: invoice/payment groups whose copies are ALL duplicate-ignored (none entered, none
-- skip-QB) -- a bill that will never show for entry. A row here = a person checked the group and
-- confirmed it should stay ignored, so it stops counting on the dashboard. Keyed like the page groups.
CREATE TABLE IF NOT EXISTS no_live_copy_confirmed (
    record_type  TEXT NOT NULL,          -- 'invoice' | 'payment'
    vendor_key   TEXT NOT NULL,          -- _duplicate_vendor_keyer() of the vendor
    number_key   TEXT NOT NULL,          -- classify_emails._norm() of the number
    confirmed_by TEXT,
    confirmed_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (record_type, vendor_key, number_key)
);
"""


def init_web_schema():
    database.init_db()  # ensure base tables exist first
    # Only when WEB_DDL changed, with a short lock wait (database.apply_schema, 2026-10-26): it replaces the
    # all_records view and alters invoices / payments, which used to queue behind a classify run at every restart.
    database.apply_schema("web", WEB_DDL)


# --------------------------------------------------------------------------- #
# Users / auth
# --------------------------------------------------------------------------- #

def hash_password(pw):
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(pw, hashed):
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), (hashed or "").encode("ascii"))
    except (ValueError, TypeError):
        return False


# 'viewer' = read-only (2026-10-12): every page, no changes (review_app._read_only_guard).
ROLES = ("admin", "reviewer", "viewer")
ROLE_LABELS = {"admin": "admin", "reviewer": "reviewer", "viewer": "read-only"}


def create_user(username, password, display_name=None, role="reviewer"):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name, role) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
                (username, hash_password(password), display_name or username, role))
        conn.commit()
    finally:
        conn.close()


def set_user_active(user_id, active):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET active = %s WHERE id = %s", (active, user_id))
        conn.commit()
    finally:
        conn.close()


def get_user(username):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            return cur.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def get_user_prefs(user_id):
    """The user's UI preferences dict (JSONB). {} when none set yet."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT preferences FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return (row[0] if row and row[0] else {}) or {}
    finally:
        conn.close()


def set_user_pref(user_id, key, value):
    """Merge one preference key into the user's preferences JSONB (leaves other keys
    untouched). Additive/web-only — the CLI never reads user preferences."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET preferences = COALESCE(preferences, '{}'::jsonb) || %s::jsonb "
                        "WHERE id = %s", (json.dumps({key: value}), user_id))
        conn.commit()
    finally:
        conn.close()


_USER_SORT = {"username": "username", "display_name": "display_name", "role": "role",
              "active": "active", "created_at": "created_at"}


def list_users(sort=None, direction=None):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            order = _order_clause(sort, direction, _USER_SORT, "username")
            cur.execute(f"SELECT id, username, display_name, role, active, created_at, edit_pages "
                        f"FROM users {order}")
            return cur.fetchall()
    finally:
        conn.close()


# Pages a read-only user can be allowed to change things on (Users > Edit). key -> (label, path prefixes that
# the page itself and its buttons post to).
WRITABLE_PAGES = {
    "qb_doubles": ("In QuickBooks twice?", ("/qb-reconcile/doubles",)),
    "qb_corrections": ("QB corrections", ("/qb-corrections",)),
    "correspondence": ("Correspondence", ("/correspondence",)),
}


def user_edit_pages(user):
    """The WRITABLE_PAGES keys this read-only user may change things on."""
    return [k for k in (str((user or {}).get("edit_pages") or "")).split(",") if k.strip() in WRITABLE_PAGES]


def user_can_write_path(user, path):
    """True when a READ-ONLY user is allowed to make changes at this path (other roles: always True)."""
    if not user or user.get("role") != "viewer":
        return True
    return any(path.startswith(pre) for k in user_edit_pages(user) for pre in WRITABLE_PAGES[k.strip()][1])


def set_user_edit_pages(user_id, keys, admin_username):
    val = ",".join(k for k in keys if k in WRITABLE_PAGES) or None
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT edit_pages FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            old = row[0] if row else None
            if (old or None) != val:
                cur.execute("UPDATE users SET edit_pages = %s WHERE id = %s", (val, user_id))
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                            "VALUES ('users',%s,'edit_pages',%s,%s,%s)", (user_id, old, val, admin_username))
        conn.commit()
    finally:
        conn.close()


def update_user(user_id, admin_username, display_name=None, role=None, active=None,
                new_password=None):
    """Admin edit of a user: display name, role (admin/reviewer), active flag,
    and/or a password reset. Only changed fields are written and each is logged to
    edit_log (the password VALUE is never logged — just that it was reset)."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            u = cur.fetchone()
            if not u:
                raise ValueError("user not found")

            def _log(field, old, new):
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, "
                            "new_value, changed_by) VALUES ('users',%s,%s,%s,%s,%s)",
                            (user_id, field, old, new, admin_username))

            dn = (display_name or "").strip()
            if display_name is not None and dn != (u["display_name"] or ""):
                cur.execute("UPDATE users SET display_name = %s WHERE id = %s",
                            (dn or None, user_id))
                _log("display_name", u["display_name"], dn)
            if role in ROLES and role != u["role"]:
                cur.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
                _log("role", u["role"], role)
            if active is not None and bool(active) != bool(u["active"]):
                cur.execute("UPDATE users SET active = %s WHERE id = %s", (active, user_id))
                _log("active", str(u["active"]), str(active))
            if new_password:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                            (hash_password(new_password), user_id))
                _log("password", None, "(reset)")
        conn.commit()
    finally:
        conn.close()


def seed_admin(username, password):
    """Create the first admin if no users exist yet."""
    if not username or not password:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO users (username, password_hash, display_name, role, active) "
                    "VALUES (%s,%s,%s,'admin',TRUE)",
                    (username, hash_password(password), username))
                print(f"Seeded admin user '{username}'.")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #

def _order_clause(sort, direction, allowed, default):
    """A SAFE 'ORDER BY ...' fragment. `allowed` whitelists sort-key -> column, so
    the user-supplied sort/direction can never inject SQL. Falls back to `default`
    (a raw SQL order expression) when the sort key isn't recognized."""
    col = allowed.get(sort)
    if not col:
        return f"ORDER BY {default}"
    d = "ASC" if str(direction).lower() == "asc" else "DESC"
    return f"ORDER BY {col} {d} NULLS LAST"


_REVIEW_SORT = {"flagged": "flagged_at", "vendor": "vendor_matched", "store": "store_matched",
                "number": "invoice_number", "amount": "amount", "run": "run_id"}


def _review_filter_clause(run_id, store, vendor, search, assigned=None):
    """Shared WHERE fragment for the review-queue filters (run / store / vendor /
    assigned-to / free-text search). Returns (sql_fragment, params). Parameterized —
    safe. `assigned` may be a username, or the sentinel "__unassigned__"."""
    clause, params = "", []
    if run_id:
        clause += " AND run_id = %s"; params.append(run_id)
    if store:
        clause += " AND store_matched = %s"; params.append(store)
    if vendor:
        clause += " AND vendor_matched = %s"; params.append(vendor)
    if assigned == "__unassigned__":
        clause += " AND (assigned_to IS NULL OR assigned_to = '')"
    elif assigned:
        clause += " AND assigned_to = %s"; params.append(assigned)
    if search:
        clause += (" AND (vendor_matched ILIKE %s OR store_matched ILIKE %s "
                   "OR invoice_number ILIKE %s OR question ILIKE %s)")
        like = f"%{search}%"
        params += [like, like, like, like]
    return clause, params


def list_open_reviews(run_id=None, sort=None, direction=None, store=None, vendor=None,
                      search=None, include_answered=False, assigned=None):
    """Items in the review queue. By default only those still needing an answer;
    pass include_answered=True to also include ANSWERED-but-not-yet-reprocessed rows
    (an answered item leaves the queue only once a classify run actually resolves it).
    Optional store / vendor / assigned-to / free-text search filters. Pending-answer
    items sort first by default so the work list stays at the top; the column-sort
    whitelist still applies."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            clause = "TRUE"
            params = []
            if not include_answered:
                clause += " AND (answer IS NULL OR answer = '')"
            frag, fparams = _review_filter_clause(run_id, store, vendor, search, assigned)
            clause += frag; params += fparams
            default_order = ("(CASE WHEN answer IS NULL OR answer = '' THEN 0 ELSE 1 END), id")
            order = _order_clause(sort, direction, _REVIEW_SORT, default_order)
            # Join the source email's REAL Gmail receive time (emails.received_at) for display.
            # Wrapped in a subquery aliased back to "review_queue" so the existing WHERE/ORDER BY
            # fragments (which reference unqualified columns like run_id) stay valid + unambiguous
            # — emails.run_id would otherwise collide with review_queue.run_id once joined.
            cur.execute(f"""
                SELECT * FROM (
                    SELECT rq.*, e.received_at AS email_received_at
                    FROM review_queue rq
                    LEFT JOIN emails e ON e.filename = rq.source_email
                ) review_queue
                WHERE {clause} {order}
            """, params)
            return cur.fetchall()
    finally:
        conn.close()


def review_filter_options(run_id=None):
    """{'stores': [...], 'vendors': [...]} — DISTINCT non-empty store_matched /
    vendor_matched currently in the review queue (respecting the run filter if given),
    for the filter dropdowns."""
    run_clause = " AND run_id = %s" if run_id else ""
    p = [run_id] if run_id else []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT store_matched FROM review_queue "
                        f"WHERE store_matched IS NOT NULL AND store_matched <> ''{run_clause} "
                        f"ORDER BY store_matched", p)
            stores = [r[0] for r in cur.fetchall()]
            cur.execute(f"SELECT DISTINCT vendor_matched FROM review_queue "
                        f"WHERE vendor_matched IS NOT NULL AND vendor_matched <> ''{run_clause} "
                        f"ORDER BY vendor_matched", p)
            vendors = [r[0] for r in cur.fetchall()]
            return {"stores": stores, "vendors": vendors}
    finally:
        conn.close()


def statement_filter_options(run_id=None):
    """DISTINCT vendor currently open on statement_lines (status IN ('missing','amount_mismatch')),
    for the Statements page vendor filter. No store — statement_lines carries none."""
    run_clause = " AND first_seen_run = %s" if run_id else ""
    p = [run_id] if run_id else []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT vendor FROM statement_lines "
                        f"WHERE status IN ('missing','amount_mismatch') "
                        f"AND vendor IS NOT NULL AND vendor <> ''{run_clause} "
                        f"ORDER BY vendor", p)
            return {"vendors": [r[0] for r in cur.fetchall()]}
    finally:
        conn.close()


def answered_count(run_id=None, store=None, vendor=None, search=None, assigned=None):
    """Count of currently-ANSWERED (pending-reprocess) rows matching the same filters,
    for the 'Show answered (N)' toggle label."""
    frag, params = _review_filter_clause(run_id, store, vendor, search, assigned)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM review_queue "
                        f"WHERE answer IS NOT NULL AND answer <> ''{frag}", params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def _stmt_money(v):
    """$1,234.50 for statement-exception messages (None -> '(none)')."""
    if v is None:
        return "(none)"
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _stmt_date(d):
    """m/d/yy for statement-exception messages, or 'no date'."""
    if not d:
        return "no date"
    try:
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except AttributeError:
        return str(d)


def _statement_group_row(statement_file, rows):
    """Turn ALL open exception lines of ONE statement into a SINGLE review-queue-shaped dict
    (kind='statement'), so a 5-missing-invoice statement is one queue row listing every
    invoice — not five separate rows. Not stored in review_queue; surfaced at read time."""
    rep = rows[0]
    vendors = {(r.get("vendor") or "").strip() for r in rows if (r.get("vendor") or "").strip()}
    vendor = next(iter(vendors)) if len(vendors) == 1 else "(multiple vendors)"
    missing = [r for r in rows if r.get("status") == "missing"]
    mismatch = [r for r in rows if r.get("status") == "amount_mismatch"]
    n_missing, n_mismatch, n = len(missing), len(mismatch), len(rows)

    def _one_missing(r):
        return (f"{r.get('invoice_number') or '—'} - {_stmt_money(r.get('amount'))} - "
                f"{_stmt_date(r.get('invoice_date'))}")

    def _one_mismatch(r):
        return (f"{r.get('invoice_number') or '—'} - statement {_stmt_money(r.get('amount'))} "
                f"vs recorded {_stmt_money(r.get('recorded_amount'))} - "
                f"{_stmt_date(r.get('invoice_date'))}")

    segs = []
    if missing:
        segs.append("Missing invoice(s):\n" + "\n".join(_one_missing(r) for r in missing))
    if mismatch:
        segs.append("Amount mismatch:\n" + "\n".join(_one_mismatch(r) for r in mismatch))
    prefix = "" if vendor == "(multiple vendors)" else f"{vendor} — "
    question = f"Statement {statement_file or '—'} — {prefix}\n" + "\n\n".join(segs)

    if n_missing and n_mismatch:
        status, status_label = "mixed", f"{n_missing} missing, {n_mismatch} mismatch"
    elif n_mismatch:
        status = "amount_mismatch"
        status_label = "Amount mismatch" if n_mismatch == 1 else f"{n_mismatch} amount mismatches"
    else:
        status = "missing"
        status_label = "Missing invoice" if n_missing == 1 else f"{n_missing} missing invoices"

    return {
        "kind": "statement", "id": rep.get("id"),
        "line_ids": [r.get("id") for r in rows], "line_count": n,
        "n_missing": n_missing, "n_mismatch": n_mismatch,
        "status": status, "status_label": status_label,
        "vendor_matched": vendor, "store_matched": "",
        "invoice_number": f"{n} invoice(s)", "amount": None, "recorded_amount": None,
        # All lines in a group are assigned together, so the first row's value represents the group.
        "assigned_to": rep.get("assigned_to"),
        "question": question,
        "source_email": rep.get("statement_email"), "statement_file": statement_file,
        "gmail_link": rep.get("gmail_link"), "flagged_at": rep.get("created_at"),
        "date_flagged": None, "run_id": rep.get("first_seen_run"), "answer": None,
        "note_count": 0, "has_pending_upload": False,
    }


def list_open_statement_exceptions(run_id=None, vendor=None, search=None, assigned=None):
    """Open statement-reconciliation exceptions (status 'missing' / 'amount_mismatch', not
    dismissed, not excluded_vendor), grouped into ONE review-queue row PER statement file —
    every open line on that statement listed inside it. Read-only; UNIONed into the Review
    queue at read time. store filter is handled by the caller (statement lines carry no store).
    `assigned` mirrors the regular queue's filter: '__unassigned__' = unassigned only, any other
    truthy value = that assignee only. ('excluded_vendor' lines never match the status filter.)"""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            clause, params = "sl.status IN ('missing','amount_mismatch')", []
            if run_id:
                clause += " AND sl.first_seen_run = %s"; params.append(run_id)
            if vendor:
                clause += " AND sl.vendor ILIKE %s"; params.append(f"%{vendor}%")
            if search:
                like = f"%{search}%"
                clause += " AND (sl.vendor ILIKE %s OR sl.invoice_number ILIKE %s)"
                params += [like, like]
            if assigned == "__unassigned__":
                clause += " AND (sl.assigned_to IS NULL OR sl.assigned_to = '')"
            elif assigned:
                clause += " AND sl.assigned_to = %s"; params.append(assigned)
            cur.execute(
                f"""SELECT sl.id, sl.statement_email, sl.statement_file, sl.vendor,
                           sl.invoice_number, sl.invoice_date, sl.amount, sl.recorded_amount,
                           sl.status, sl.first_seen_run, sl.created_at, sl.assigned_to, e.gmail_link
                    FROM statement_lines sl
                    LEFT JOIN emails e ON e.filename = sl.statement_file
                    WHERE {clause}
                    ORDER BY sl.statement_file, sl.status, sl.vendor, sl.invoice_number""",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    # Group the fetched lines by statement file (dict preserves first-seen order) → one row each.
    groups = {}
    for r in rows:
        groups.setdefault(r.get("statement_file"), []).append(r)
    return [_statement_group_row(f, g) for f, g in groups.items()]


def dismiss_statement_lines(line_ids, username):
    """Batch-dismiss MANY statement exception lines (one grouped Review row = a whole
    statement's open lines): status='dismissed' (reconcile skips dismissed), each change
    audited in edit_log. Skips already-dismissed ids. Returns the number of rows updated."""
    ids = [int(x) for x in (line_ids or []) if x is not None]
    if not ids:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM statement_lines "
                        "WHERE id = ANY(%s) AND status <> 'dismissed'", (ids,))
            rows = cur.fetchall()
            if not rows:
                return 0
            upd_ids = [r[0] for r in rows]
            cur.execute("UPDATE statement_lines SET status='dismissed', dismissed_by=%s, "
                        "dismissed_at=now(), updated_at=now() WHERE id = ANY(%s)",
                        (username, upd_ids))
            for lid, old in rows:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('statement_lines', %s, 'status', %s, 'dismissed', %s)",
                    (lid, old, username))
        conn.commit()
        return len(upd_ids)
    finally:
        conn.close()


def assign_statement_lines(line_ids, assigned_to, username):
    """Set (or clear, with assigned_to='') who should work ALL open lines on one statement (one
    grouped Review row = a whole statement). Audited in edit_log per line, same pattern as
    assign_review()."""
    ids = [int(x) for x in (line_ids or []) if x is not None]
    if not ids:
        return 0
    new = (assigned_to or "").strip() or None
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, assigned_to FROM statement_lines WHERE id = ANY(%s)", (ids,))
            rows = cur.fetchall()
            cur.execute("UPDATE statement_lines SET assigned_to = %s, updated_at = now() "
                        "WHERE id = ANY(%s)", (new, ids))
            for lid, old in rows:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('statement_lines', %s, 'assigned_to', %s, %s, %s)",
                    (lid, old, new, username))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def dismiss_statement_line(line_id, username):
    """Resolve/dismiss one statement exception: status='dismissed' (won't reappear; reconcile
    skips dismissed). Audited in edit_log. Returns True if a row was updated."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM statement_lines WHERE id = %s", (line_id,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("UPDATE statement_lines SET status='dismissed', dismissed_by=%s, "
                        "dismissed_at=now(), updated_at=now() WHERE id=%s", (username, line_id))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES ('statement_lines', %s, 'status', %s, 'dismissed', %s)",
                (line_id, row[0], username))
        conn.commit()
        return True
    finally:
        conn.close()


def list_runs():
    """All classify runs, newest first, with counts — for the run filter dropdown."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT run_id, started_at, finished_at, n_emails, n_results, n_review "
                        "FROM runs ORDER BY started_at DESC")
            return cur.fetchall()
    finally:
        conn.close()


def get_review(review_id):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT rq.*, e.received_at AS email_received_at "
                "FROM review_queue rq LEFT JOIN emails e ON e.filename = rq.source_email "
                "WHERE rq.id = %s", (review_id,))
            return cur.fetchone()
    finally:
        conn.close()


def list_email_siblings(source_email):
    """Records from the SAME source email that are NOT in the review queue — i.e.
    anything already recorded to results: clean records, and excluded-vendor
    invoices/credit memos recorded but skipped from QB. Read-only context shown as
    a breadcrumb on the review item (flagged records aren't in results, so they
    never appear here)."""
    if not source_email:
        return []
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT record_type, record_id, vendor_matched, number, amount, "
                        "skip_qb_import, entered_in_qb FROM all_records "
                        "WHERE email_filename = %s AND record_type <> 'sales_order' "
                        "ORDER BY record_type, number", (source_email,))
            return cur.fetchall()
    finally:
        conn.close()


def list_review_notes(review_key):
    """Notes on a review item, oldest first. Keyed by the STABLE review_key (source email +
    vendor + number) so they survive BOTH the queue's DELETE+re-INSERT (which changes row ids)
    AND a reprocess that regenerates the display filename (store corrected / suffix bumped)."""
    if not review_key:
        return []
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, note, author, created_at FROM review_notes "
                        "WHERE review_key = %s ORDER BY created_at, id", (review_key,))
            return cur.fetchall()
    finally:
        conn.close()


def add_review_note(review_key, note, author):
    note = (note or "").strip()
    if not review_key or not note:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO review_notes (review_key, note, author) VALUES (%s,%s,%s)",
                        (review_key, note, author))
        conn.commit()
    finally:
        conn.close()


def delete_review_note(note_id, username):
    """Admin-only delete of one internal note (mirrors delete_learning_web) — logs an
    edit_log audit row keyed by table 'review_notes' + the note id."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT note FROM review_notes WHERE id = %s", (note_id,))
            row = cur.fetchone()
            cur.execute("DELETE FROM review_notes WHERE id = %s", (note_id,))
            if cur.rowcount:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('review_notes', %s, 'note', %s, NULL, %s)",
                    (note_id, row[0] if row else None, username))
        conn.commit()
    finally:
        conn.close()


def add_manual_attachment(review_filename, file_path, original_name, username):
    """Record a reviewer-uploaded PDF/image against a review item (keyed by its stable
    split filename). The CLI reads UNCONSUMED rows on the next run and appends them to
    that record's reprocess. Web-only insert; the file itself is saved by the caller."""
    if not review_filename or not file_path:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO manual_attachments (review_filename, file_path, "
                        "original_name, uploaded_by) VALUES (%s,%s,%s,%s)",
                        (review_filename, file_path, original_name, username))
        conn.commit()
    finally:
        conn.close()


def list_manual_attachments(review_filename):
    """All manual attachments on a review item (newest first) for the item page — incl.
    consumed ones (shown greyed), so the reviewer sees what's been used."""
    if not review_filename:
        return []
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, file_path, original_name, uploaded_by, uploaded_at, consumed_at "
                        "FROM manual_attachments WHERE review_filename = %s ORDER BY id DESC",
                        (review_filename,))
            return cur.fetchall()
    finally:
        conn.close()


def pending_manual_filenames():
    """Set of review filenames that have at least one UNCONSUMED manual attachment —
    i.e. files uploaded but not yet used by a reprocess. Drives the 'Files uploaded —
    pending reprocess' status (same pattern as the note-count badge query)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT review_filename FROM manual_attachments "
                        "WHERE consumed_at IS NULL")
            return {r[0] for r in cur.fetchall() if r[0]}
    finally:
        conn.close()


def get_review_note_counts():
    """Map of review_key (stable source-email+vendor+number identity) -> number of internal notes,
    for the queue-list badge. Read-only; a SEPARATE query, independent of the
    review_queue query (never JOIN into list_open_reviews — review_notes.id would
    collide with that function's default ORDER BY id)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT review_key, count(*) FROM review_notes GROUP BY review_key")
            return {k: c for k, c in cur.fetchall()}
    finally:
        conn.close()


def _json_default(o):
    """Serialize the odd types in a review_queue row (datetime, Decimal) for JSONB."""
    return str(o)


def ignore_review(review_id, username, reason=None):
    """Dismiss a review item: snapshot the FULL review_queue row into ignored_reviews
    and DELETE it from the queue — instant, no reprocess/API call. One transaction,
    logged to edit_log. The split file in review_pending/ is intentionally LEFT in
    place so the ignored item's preview + restore still work (the queue is DB-driven;
    a row is never rebuilt from that file). Returns True if an item was ignored."""
    reason = (reason or "").strip() or None
    conn = connect()
    conn.autocommit = False        # snapshot + delete + log must be atomic
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM review_queue WHERE id = %s", (review_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False
            snap = dict(row)
            cur.execute(
                "INSERT INTO ignored_reviews (snapshot, source_email, vendor_matched, "
                "store_matched, invoice_number, filename, question, run_id, ignore_reason, ignored_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (psycopg2.extras.Json(snap, dumps=lambda o: json.dumps(o, default=_json_default)),
                 snap.get("source_email"), snap.get("vendor_matched"), snap.get("store_matched"),
                 snap.get("invoice_number"), snap.get("filename"), snap.get("question"),
                 snap.get("run_id"), reason, username))
            cur.execute("DELETE FROM review_queue WHERE id = %s", (review_id,))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES ('review_queue', %s, 'ignored', %s, %s, %s)",
                (review_id, snap.get("filename"), (f"reason: {reason}" if reason else "ignored"),
                 username))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_ignored_reviews():
    """Ignored items, newest-first, for the Ignored view. Flagged date + amount + gmail
    link come out of the JSON snapshot; the rest are denormalized columns. email_received_at
    is Gmail's own authoritative receive time for the source email (joined in live, not from
    the snapshot) — the timing that matches what shows up searching regular Gmail."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT ir.id, ir.source_email, ir.vendor_matched, ir.store_matched, ir.invoice_number, "
                "ir.filename, ir.question, ir.run_id, ir.ignore_reason, ir.ignored_by, ir.ignored_at, "
                "ir.snapshot->>'flagged_at' AS flagged_at, ir.snapshot->>'date_flagged' AS date_flagged, "
                "ir.snapshot->>'amount' AS amount, ir.snapshot->>'gmail_link' AS gmail_link, "
                "e.received_at AS email_received_at "
                "FROM ignored_reviews ir LEFT JOIN emails e ON e.filename = ir.source_email "
                "ORDER BY ir.id DESC")
            return cur.fetchall()
    finally:
        conn.close()


def ignored_count():
    """Number of ignored review items — for the header/nav label."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ignored_reviews")
            return cur.fetchone()[0]
    finally:
        conn.close()


def restore_ignored_review(ignored_id, username):
    """Restore an ignored item back into the review queue from its snapshot, under its old id
    when nothing has taken it (queue ids are kept across runs, so links to it work again), else a
    new one. One transaction, logged to edit_log. Returns True if a row was restored."""
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT snapshot, filename FROM ignored_reviews WHERE id = %s", (ignored_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False
            snap = dict(row["snapshot"] or {})     # JSONB -> dict
            old_id = snap.pop("id", None)
            if old_id is not None:
                cur.execute("SELECT 1 FROM review_queue WHERE id = %s", (old_id,))
                if cur.fetchone() is None:
                    snap["id"] = old_id             # free -> restore under its old id
            cols = list(snap.keys())
            if cols:
                collist = ", ".join(cols)           # keys are real review_queue columns (SELECT *)
                placeholders = ", ".join(["%s"] * len(cols))
                cur.execute(f"INSERT INTO review_queue ({collist}) VALUES ({placeholders})",
                            [snap[c] for c in cols])
            cur.execute("DELETE FROM ignored_reviews WHERE id = %s", (ignored_id,))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES ('review_queue', %s, 'restored', %s, 'restored to queue', %s)",
                (ignored_id, row.get("filename"), username))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def submit_review_answer(review_id, answer, notes_to_learn, username):
    """Write Answer + Notes to Learn back onto the row EXACTLY as the CLI Phase-1
    resolve loop expects (it reads `answer` / `notes_to_learn`). Also stamps the
    audit fields. We do NOT reprocess here — the next classify_emails.py run does."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE review_queue SET answer = %s, notes_to_learn = %s, "
                "reviewed_by = %s, reviewed_at = now() WHERE id = %s",
                (answer, notes_to_learn or None, username, review_id))
        conn.commit()
    finally:
        conn.close()


def assign_review(review_id, assigned_to, username):
    """Set (or clear, with assigned_to='') who should work this review item.
    Persists across classify_emails.py runs — carried through the queue's
    delete+reinsert the same way Notes/Duplicate Group already are — until
    the item is answered/resolved or reassigned."""
    new = (assigned_to or "").strip() or None
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT assigned_to FROM review_queue WHERE id = %s", (review_id,))
            row = cur.fetchone()
            old = row[0] if row else None
            cur.execute("UPDATE review_queue SET assigned_to = %s WHERE id = %s",
                        (new, review_id))
            _ref_log(cur, "review_queue", review_id, "assigned_to", old, new, username)
        conn.commit()
    finally:
        conn.close()


def track_review(review_id, answer, username):
    """Opt-in: capture an answered review item into tracked_reviews so its OUTCOME can
    be confirmed in the Tracked tab after it reprocesses. Snapshots the display fields +
    the answer given (the live outcome is DERIVED later from the results tables — no
    extra API call). Skips if this item (same filename, or same review key — a re-flag
    renames the file) already has an OPEN tracked row (no dupes)."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM review_queue WHERE id = %s", (review_id,))
            row = cur.fetchone()
            if not row:
                return
            fn, key = row.get("filename"), row.get("review_key")
            if fn or key:
                cur.execute("SELECT 1 FROM tracked_reviews WHERE status = 'open' "
                            "AND (filename = %s OR (review_key IS NOT NULL AND review_key = %s))", (fn, key))
                if cur.fetchone():
                    return
            cur.execute(
                "INSERT INTO tracked_reviews (filename, source_email, vendor_matched, "
                "store_matched, invoice_number, original_question, answer_given, tracked_by, review_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (fn, row.get("source_email"), row.get("vendor_matched"), row.get("store_matched"),
                 row.get("invoice_number"), row.get("question"), answer, username, key))
        conn.commit()
    finally:
        conn.close()


# A tracked row's review key: the one saved at track time, else the same formula the classifier
# uses (classify_emails._stable_review_key) for rows tracked before the column existed.
_TRACK_KEY_SQL = (
    "coalesce(t.review_key, CASE WHEN coalesce(norm_vendor_dedupe(t.vendor_matched), '') = '' "
    "AND coalesce(lower(btrim(t.invoice_number)), '') = '' THEN t.source_email "
    "ELSE t.source_email || '::' || coalesce(norm_vendor_dedupe(t.vendor_matched), '') || '::' "
    "|| coalesce(lower(btrim(t.invoice_number)), '') END)")

# The recorded copy of a tracked item: same vendor + number, filed under its source email or under
# any file the ledgers say came from that email (a reprocessed split file, e.g. …__2.pdf). Live
# copies before ignored ones.
_TRACK_RECORD_SQL = """
    SELECT m.email_filename, m.record_type, m.record_id, m.amount, m.store_matched,
           m.entered_in_qb, m.skip_qb_import
      FROM all_records m
     WHERE m.record_type IN ('invoice', 'credit_memo', 'payment')
       AND norm_vendor_dedupe(m.vendor_matched) = norm_vendor_dedupe(%(vendor)s)
       AND lower(btrim(coalesce(m.number, ''))) = lower(btrim(%(number)s))
       AND ({where})
     ORDER BY m.duplicate_ignored IS TRUE, m.record_id DESC
     LIMIT 1"""
_TRACK_SAME_EMAIL = ("m.email_filename = %(email)s OR m.email_filename IN ("
                     "SELECT source_file FROM invoice_history WHERE source_email_id = %(email)s "
                     "UNION SELECT source_file FROM payment_history WHERE source_email_id = %(email)s)")


def list_tracked(status="open"):
    """Tracked items with their LIVE outcome, derived read-only from existing tables
    (no API call). Matched by the item's split `filename` first, then by its review key —
    a re-flagged split item is saved under a NEW filename (…__2) but keeps its key:
      * in review_queue with an answer         -> 'pending'      (awaiting next reprocess)
      * in review_queue, answer cleared         -> 'still_flagged' (+ the current question)
      * a recorded (non-review) record exists   -> 'resolved'     (+ amount/store/entered/skip):
        under the filename, else same vendor + number under its source email or any file the
        ledgers trace back to that email
      * neither                                 -> 'no_record'    (dropped/ignored); `elsewhere`
        notes when the same vendor + number IS recorded, from another email
    """
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT t.*, {_TRACK_KEY_SQL} AS track_key FROM tracked_reviews t "
                        "WHERE t.status = %s ORDER BY t.id DESC", (status,))
            tracked = cur.fetchall()
            files = list({t["filename"] for t in tracked if t["filename"]})
            keys = list({t["track_key"] for t in tracked if t["track_key"]})
            queue, queue_by_key, recs = {}, {}, {}
            if files or keys:
                cur.execute("SELECT filename, review_key, answer, question FROM review_queue "
                            "WHERE filename = ANY(%s) OR review_key = ANY(%s) ORDER BY id DESC",
                            (files, keys))
                for r in cur.fetchall():
                    queue.setdefault(r["filename"], r)
                    if r.get("review_key"):
                        queue_by_key.setdefault(r["review_key"], r)
            if files:
                cur.execute("SELECT email_filename, record_type, amount, store_matched, "
                            "entered_in_qb, skip_qb_import FROM all_records "
                            "WHERE record_type NOT IN ('review', 'sales_order') AND email_filename = ANY(%s)", (files,))
                for r in cur.fetchall():
                    recs.setdefault(r["email_filename"], r)   # first match wins
            for t in tracked:
                t["_by_key"] = t["_elsewhere"] = None
                fn = t.get("filename")
                if queue.get(fn) or queue_by_key.get(t.get("track_key")) or recs.get(fn):
                    continue
                if not ((t.get("vendor_matched") or "").strip() and (t.get("invoice_number") or "").strip()):
                    continue
                args = {"vendor": t["vendor_matched"], "number": t["invoice_number"],
                        "email": t.get("source_email") or ""}
                cur.execute(_TRACK_RECORD_SQL.format(where=_TRACK_SAME_EMAIL), args)
                t["_by_key"] = cur.fetchone()
                if t["_by_key"] is None:
                    cur.execute(_TRACK_RECORD_SQL.format(where="TRUE"), args)
                    t["_elsewhere"] = cur.fetchone()
    finally:
        conn.close()

    for t in tracked:
        fn = t.get("filename")
        q = queue.get(fn) or queue_by_key.get(t.get("track_key"))
        by_key, elsewhere = t.pop("_by_key", None), t.pop("_elsewhere", None)
        rec = recs.get(fn) or by_key
        if elsewhere:
            t["elsewhere_email"] = elsewhere.get("email_filename")
        if q is not None:
            if (q.get("answer") or "").strip():
                t["outcome"] = "pending"
                t["current_question"] = q.get("question")
            else:
                t["outcome"] = "still_flagged"
                t["current_question"] = q.get("question")
        elif rec is not None:
            t["outcome"] = "resolved"
            t["rec_type"] = rec.get("record_type")
            t["rec_amount"] = rec.get("amount")
            t["rec_store"] = rec.get("store_matched")
            t["rec_entered"] = rec.get("entered_in_qb")
            t["rec_skip"] = rec.get("skip_qb_import")
        else:
            t["outcome"] = "no_record"
    return tracked


def tracked_open_count():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tracked_reviews WHERE status = 'open'")
            return cur.fetchone()[0]
    finally:
        conn.close()


def mark_tracked_done(tracked_id, username):
    """Close a tracked item (status='done') — removes it from the open Tracked list.
    Logged to edit_log."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tracked_reviews SET status = 'done' WHERE id = %s AND status <> 'done'",
                        (tracked_id,))
            if cur.rowcount:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('tracked_reviews', %s, 'status', 'open', 'done', %s)",
                    (tracked_id, username))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# QB entry
# --------------------------------------------------------------------------- #

_QB_SORT = {"store": "store_matched", "vendor": "vendor_matched", "number": "number",
            "date": "record_date", "amount": "amount", "sales_tax": "sales_tax",
            "ship": "ship_charge", "entered": "entered_in_qb"}


def _missing_info_sql(datecol, alias="r"):
    """SQL text listing what a record lacks for QuickBooks — 'store', 'date', 'amount' (comma-joined,
    '' when nothing is missing). The same three things the store/date/amount review checks catch on
    NEW records; records saved before those checks existed can still lack them (2026-10-08)."""
    return (f"concat_ws(', ', "
            f"CASE WHEN coalesce(btrim({alias}.store_matched), '') = '' "
            f"       OR lower(btrim({alias}.store_matched)) = 'unknown' THEN 'store' END, "
            f"CASE WHEN coalesce(btrim({alias}.{datecol}), '') = '' THEN 'date' END, "
            f"CASE WHEN {alias}.amount IS NULL OR {alias}.amount <= 0 THEN 'amount' END)")


def missing_info_counts():
    """How many records still waiting for QuickBooks entry lack a store, date or amount, per page:
    {'qb': n (invoices + credit memos on /qb), 'payments': n}. Same base as each page's default view:
    not entered, not skip-QB, not an ignored duplicate."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            out = {}
            for key, sql in (
                ("qb", f"SELECT (SELECT count(*) FROM invoices r WHERE r.entered_in_qb IS NOT TRUE "
                       f"AND r.skip_qb_import IS NOT TRUE AND r.duplicate_ignored IS NOT TRUE "
                       f"AND {_missing_info_sql('invoice_date')} <> '') + "
                       f"(SELECT count(*) FROM credit_memos r WHERE r.entered_in_qb IS NOT TRUE "
                       f"AND r.skip_qb_import IS NOT TRUE AND {_missing_info_sql('credit_memo_date')} <> '')"),
                ("payments", f"SELECT count(*) FROM payments r WHERE r.entered_in_qb IS NOT TRUE "
                             f"AND r.skip_qb_import IS NOT TRUE AND r.duplicate_ignored IS NOT TRUE "
                             f"AND {_missing_info_sql('payment_date')} <> ''")):
                cur.execute(sql)
                out[key] = cur.fetchone()[0]
            return out
    finally:
        conn.close()


def list_qb_ready(entered="no", run_id=None, email_filename=None, store=None,
                  sort=None, direction=None, dup=None, ignored=None, group=None,
                  vendor=None, search=None, date_from=None, date_to=None,
                  amount_min=None, amount_max=None, record_type=None, needs_action=None,
                  amount=None, action=None, missing=None):
    """Non-skip invoices + credit memos (credit shown negative). Ordered newest
    first. Includes the possible-duplicate + entered flags, audit info, the source
    email's run_id (joined from emails) for the run filter, and sales_tax/ship_charge
    (invoices only; NULL for credit memos) for the QuickBooks CSV export.
    dup: 'yes' = only possible-duplicates, 'all' = include possible-duplicates (no
    filter), else (default, or 'no') = hide possible-duplicates (new default, mirrors
    how duplicate_ignored is already hidden by default below).
    ignored: '1' also shows manually/auto-ignored duplicates (hidden by default, as before).
    group: filter to one duplicate_group_id cluster."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Per-table WHERE keeps the skip/entered/store filters pushed down; the
            # run filter applies to the joined emails row (e).
            where = "WHERE (r.skip_qb_import IS NOT TRUE)"
            params = []
            # entered: "no" (default) = only not-yet-entered; "yes" = only entered; "all" = both.
            if entered == "yes":
                where += " AND (r.entered_in_qb IS TRUE)"
            elif entered != "all":
                where += " AND (r.entered_in_qb IS NOT TRUE)"
            if store:
                where += " AND r.store_matched = %s"; params.append(store)
            if run_id:
                where += " AND e.run_id = %s"; params.append(run_id)
            if email_filename:
                where += " AND r.email_filename = %s"; params.append(email_filename)
            # The possible/ignored-duplicate flags live on invoices (credit memos are
            # always FALSE), so filter them on the unioned result (q). REAL duplicates
            # (duplicate_ignored) are ALWAYS excluded from QB entry.
            # REAL duplicates (duplicate_ignored) are hidden from QB entry by default; ignored='1'
            # reveals them (parity with /browse + /payments).
            outer_clauses = []
            if ignored != "1":
                outer_clauses.append("q.duplicate_ignored IS NOT TRUE")
            if dup == "yes":
                outer_clauses.append("q.duplicate_suspect IS TRUE")
            elif dup != "all":
                # default, or dup == "no": hide possible duplicates from QB entry
                outer_clauses.append("q.duplicate_suspect IS NOT TRUE")
            outer_params = []
            if group:
                outer_clauses.append("q.duplicate_group_id = %s"); outer_params.append(group)
            # General QB-entry filters, applied on the unioned/aliased result q (which
            # normalizes vendor_matched/number/record_date/amount/record_type across
            # invoice + credit_memo), so they work regardless of record type.
            if vendor:
                outer_clauses.append("q.vendor_matched ILIKE %s"); outer_params.append(f"%{vendor}%")
            if search:
                outer_clauses.append("(q.vendor_matched ILIKE %s OR q.number ILIKE %s OR q.email_filename ILIKE %s)")
                outer_params += [f"%{search}%"] * 3
            if date_from:
                outer_clauses.append("q.record_date >= %s"); outer_params.append(date_from)
            if date_to:
                outer_clauses.append("q.record_date <= %s"); outer_params.append(date_to)
            # Exact amount (default single-field search) takes precedence over min/max range.
            # Rounded numeric compare so 548.4 == 548.40 and float noise never defeats it.
            if amount not in (None, ""):
                outer_clauses.append("ROUND(q.amount::numeric, 2) = ROUND(%s::numeric, 2)")
                outer_params.append(amount)
            else:
                if amount_min not in (None, ""):
                    outer_clauses.append("q.amount >= %s"); outer_params.append(amount_min)
                if amount_max not in (None, ""):
                    outer_clauses.append("q.amount <= %s"); outer_params.append(amount_max)
            if record_type:
                outer_clauses.append("q.record_type = %s"); outer_params.append(record_type)
            # "Needs action" = rows still showing the add-vendor/assign-account pill (no QB
            # account resolved); "Complete" = the inverse. Same condition that decides the pill.
            # Blank ('') counts as no account, like the pill and the dashboard count (2026-10-29: the
            # "QB setup needed" tile said 26 while Needs action listed none -- their accounts were '').
            if needs_action == "yes":
                outer_clauses.append("COALESCE(btrim(q.qb_account), '') = ''")
            elif needs_action == "no":
                outer_clauses.append("COALESCE(btrim(q.qb_account), '') <> ''")
            # Finer split of "needs action" by WHICH fix the Account-cell pill asks for — the
            # SAME conditions qb.html uses to choose the pill (blank qb_account/standard_vendor
            # treated as '' so an empty string counts as unset, matching the template's `not`).
            #   add_vendor     = no QB account AND no standard vendor (a vendor IS named)
            #   assign_account = no QB account BUT the vendor is already standardized
            #   none           = QB account populated (nothing but the entry checkbox needed)
            # Combines (ANDs) with needs_action + every other filter.
            if action == "add_vendor":
                outer_clauses.append("COALESCE(q.qb_account,'') = '' AND COALESCE(q.standard_vendor,'') = '' "
                                     "AND COALESCE(q.vendor_matched,'') <> ''")
            elif action == "assign_account":
                outer_clauses.append("COALESCE(q.qb_account,'') = '' AND COALESCE(q.standard_vendor,'') <> ''")
            elif action == "none":
                outer_clauses.append("COALESCE(q.qb_account,'') <> ''")
            # missing='1': only records lacking a store, date or amount (_missing_info_sql).
            if missing == "1":
                outer_clauses.append("q.missing_info <> ''")
            outer = ("WHERE " + " AND ".join(outer_clauses)) if outer_clauses else ""
            cur.execute(f"""
                SELECT record_type, record_id, email_filename, store_matched, vendor_matched,
                       number, record_date, amount, sales_tax, ship_charge, duplicate_suspect,
                       duplicate_ignored, duplicate_group_id, duplicate_reason, duplicate_of,
                       entered_in_qb, entered_qb_by, entered_qb_at,
                       file_path, gmail_link, run_id, class, qb_vendor, qb_account, standard_vendor,
                       missing_info
                  FROM (
                    SELECT 'invoice' AS record_type, r.id AS record_id, r.email_filename,
                           r.store_matched, r.vendor_matched, r.invoice_number AS number,
                           r.invoice_date AS record_date, r.amount, r.sales_tax, r.ship_charge,
                           r.duplicate_suspect, r.duplicate_ignored, r.duplicate_group_id,
                           r.duplicate_reason, r.duplicate_of, r.entered_in_qb,
                           r.entered_qb_by, r.entered_qb_at, r.file_path, r.gmail_link, e.run_id,
                           r.class, r.qb_vendor, r.qb_account, r.standard_vendor,
                           {_missing_info_sql('invoice_date')} AS missing_info
                      FROM invoices r LEFT JOIN emails e ON e.filename = r.email_filename
                      {where}
                    UNION ALL
                    SELECT 'credit_memo', r.id, r.email_filename, r.store_matched, r.vendor_matched,
                           r.credit_memo_number, r.credit_memo_date, -abs(r.amount), NULL, NULL,
                           FALSE, FALSE, NULL, NULL, NULL, r.entered_in_qb, r.entered_qb_by, r.entered_qb_at,
                           r.file_path, r.gmail_link, e.run_id, r.class, r.qb_vendor, r.qb_account, r.standard_vendor,
                           {_missing_info_sql('credit_memo_date')}
                      FROM credit_memos r LEFT JOIN emails e ON e.filename = r.email_filename
                      {where}
                  ) q
                {outer}
                {_order_clause(sort, direction, _QB_SORT, "record_date DESC NULLS LAST, number")}
            """, params + params + outer_params)  # same params for both UNION arms, then outer
            return cur.fetchall()
    finally:
        conn.close()


def set_entered_in_qb(record_type, record_id, entered, username):
    table = {"credit_memo": "credit_memos", "payment": "payments"}.get(record_type, "invoices")
    conn = connect()
    try:
        with conn.cursor() as cur:
            if entered:
                cur.execute(
                    f"UPDATE {table} SET entered_in_qb = TRUE, entered_qb_by = %s, "
                    f"entered_qb_at = now() WHERE id = %s", (username, record_id))
            else:
                cur.execute(
                    f"UPDATE {table} SET entered_in_qb = FALSE, entered_qb_by = NULL, "
                    f"entered_qb_at = NULL WHERE id = %s", (record_id,))
        conn.commit()
    finally:
        conn.close()


def set_record_ignored(record_type, record_id, ignored, username):
    """Manual per-record exclude-from-QB toggle (any record type). Reuses skip_qb_import -- the
    same flag vendor-exclusion sets automatically -- but stamps ignored_by/ignored_at so the UI can
    tell a manual ignore apart from a vendor-rule skip and only offer Restore for the manual case.
    Logged to edit_log for audit parity with set_duplicate_ignored."""
    if record_type not in ("invoice", "credit_memo", "payment"):
        return
    table = {"credit_memo": "credit_memos", "payment": "payments"}.get(record_type, "invoices")
    conn = connect()
    try:
        with conn.cursor() as cur:
            if ignored:
                cur.execute(
                    f"UPDATE {table} SET skip_qb_import = TRUE, ignored_by = %s, "
                    f"ignored_at = now() WHERE id = %s", (username, record_id))
            else:
                cur.execute(
                    f"UPDATE {table} SET skip_qb_import = FALSE, ignored_by = NULL, "
                    f"ignored_at = NULL WHERE id = %s", (record_id,))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (table, record_id, "skip_qb_import (manual ignore)",
                 str(not ignored), str(ignored), username))
        conn.commit()
    finally:
        conn.close()


def set_duplicate_ignored(record_type, record_id, ignored, username, duplicate_of=None):
    """Manual override of a duplicate flag (invoices/payments only). ignored=True marks the
    record as a confirmed duplicate to exclude from QB (same effect as an auto-detected one — it
    drops out of /qb via the existing 'duplicate_ignored IS NOT TRUE' clause). ignored=False
    clears the ignore (restores it to a normal record). Logged to edit_log for audit parity.

    Side effect of ignored=True: the duplicate decision is now made, so any surviving
    (non-ignored) sibling has its duplicate_suspect flag CLEARED — it's the kept copy, no
    longer a "possible duplicate" needing review, so it stops showing the dup? pill /
    "ignore this one" and drops off the flagged views. Siblings are matched both by shared
    duplicate_group_id AND by shared vendor + number (the actual duplicate identity), so
    this works even for the many rows that never got a group id stamped — and so ignoring
    EITHER side of a duplicate set (the flagged copy or the likely original) flips it. The
    sibling's duplicate_group_id is left intact so the Group link still works."""
    table = {"invoice": "invoices", "payment": "payments"}.get(record_type)
    if not table:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            if ignored:
                # If the reviewer linked this to an original (duplicate_of = its email_filename),
                # tie BOTH into a shared duplicate_group_id — reuse the original's if it has one,
                # else mint a short id and stamp it on the original too — so a manually-linked
                # pair becomes a filterable/visible group, same as a code-detected one.
                group_id = None
                if duplicate_of:
                    cur.execute(f"SELECT id, duplicate_group_id FROM {table} "
                                f"WHERE email_filename = %s ORDER BY id DESC LIMIT 1", (duplicate_of,))
                    orig = cur.fetchone()
                    if orig:
                        group_id = orig[1]
                        if not group_id:
                            import secrets
                            group_id = secrets.token_hex(4).upper()
                            cur.execute(f"UPDATE {table} SET duplicate_group_id = %s WHERE id = %s",
                                        (group_id, orig[0]))
                cur.execute(
                    f"UPDATE {table} SET duplicate_ignored = TRUE, duplicate_suspect = TRUE, "
                    f"duplicate_of = COALESCE(%s, duplicate_of), "
                    f"duplicate_reason = COALESCE(duplicate_reason, 'Manually marked as duplicate'), "
                    f"duplicate_group_id = COALESCE(%s, duplicate_group_id), "
                    f"duplicate_ignored_by = %s, duplicate_ignored_at = now() "
                    f"WHERE id = %s RETURNING duplicate_group_id",
                    (duplicate_of, group_id, username, record_id))
                row = cur.fetchone()
                final_group_id = row[0] if row else None
                # The decision is made: this record is the discard. In a 2-copy set the ONE
                # surviving (non-ignored) sibling is now the kept copy, so clear its suspect flag
                # (drops off /qb + /browse + /duplicates). But ONLY when this ignore resolves the
                # cluster down to a SINGLE non-ignored survivor: in a 3+-copy set, ignoring one
                # flagged copy must NOT blanket-clear the OTHER still-unresolved flagged copies —
                # they each still need their own explicit review, and silently un-flagging them
                # would drop a genuine duplicate off /duplicates while it stays live + unentered
                # in /qb (a double-entry risk). Match siblings by shared group id OR shared
                # vendor+number (the actual duplicate identity /duplicates groups on).
                num_col = "invoice_number" if table == "invoices" else "reference_number"
                cluster_match = (
                    f"((%s IS NOT NULL AND s.duplicate_group_id = %s) "
                    f" OR EXISTS (SELECT 1 FROM {table} d WHERE d.id = %s "
                    f"            AND norm_vendor_dedupe(d.vendor_matched) = norm_vendor_dedupe(s.vendor_matched) "
                    f"            AND d.{num_col} IS NOT NULL AND d.{num_col} <> '' "
                    f"            AND d.{num_col} = s.{num_col}))")
                # How many NON-ignored records remain in this cluster (this one is now ignored,
                # so it's excluded). Only auto-clear the survivor when exactly one remains.
                cur.execute(
                    f"SELECT count(*) FROM {table} s "
                    f"WHERE s.duplicate_ignored IS NOT TRUE AND {cluster_match}",
                    (final_group_id, final_group_id, record_id))
                remaining = cur.fetchone()[0]
                if remaining <= 1:
                    cur.execute(
                        f"UPDATE {table} s SET duplicate_suspect = FALSE "
                        f"WHERE s.id <> %s AND s.duplicate_ignored IS NOT TRUE "
                        f"  AND s.duplicate_suspect IS TRUE AND {cluster_match}",
                        (record_id, final_group_id, final_group_id, record_id))
            else:
                cur.execute(
                    f"UPDATE {table} SET duplicate_ignored = FALSE, "
                    f"duplicate_ignored_by = NULL, duplicate_ignored_at = NULL "
                    f"WHERE id = %s", (record_id,))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (table, record_id, "duplicate_ignored", str(not ignored), str(ignored), username))
        conn.commit()
    finally:
        conn.close()


def clear_duplicate_flag(record_type, record_id, username):
    """Dismiss a possible-duplicate flag: it's NOT actually a duplicate — set
    duplicate_suspect=FALSE (and clear the reason) so the record is normal again and drops off
    the Duplicates page. Leaves duplicate_ignored alone (it's already FALSE for a suspect).
    Audited in edit_log. Invoices/payments only."""
    table = {"invoice": "invoices", "payment": "payments"}.get(record_type)
    if not table:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET duplicate_suspect = FALSE, duplicate_reason = NULL "
                f"WHERE id = %s", (record_id,))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (table, record_id, "duplicate_suspect", "True", "False", username))
        conn.commit()
    finally:
        conn.close()


# (auto_resolve_store_vs_vendor_duplicates() — a one-time tool to flip the historical store-copy vs
# vendor-copy duplicate pairs — was deleted 2026-10-08: it was never run (Joel chose "prefer the store
# copy going forward", which check_duplicate does for new copies) and nothing called it.)


def set_kept_number(record_type, record_id, number, username):
    """Give the copy kept on Possible duplicates a corrected bill number (2026-10-20, Joel: keep copy 1
    but with copy 2's number). Only for a copy NOT entered in QuickBooks. Updates the record and its
    duplicate-ledger row (so later copies match the corrected number) and logs both. Deliberately skips
    update_record_field's duplicate re-check: the copy that had this number was just set aside as this
    one's duplicate, so the re-check would flag the kept copy against it. Returns True when changed."""
    number = (number or "").strip()
    if record_type not in ("invoice", "payment") or not number:
        return False
    table = _TABLE[record_type]
    col = EDITABLE[record_type]["number"]
    ledger, lnum, lamt = (("invoice_history", "invoice_number", "invoice_amount") if record_type == "invoice"
                          else ("payment_history", "reference_number", "amount"))
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (record_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("record not found")
            if row.get("entered_in_qb"):
                raise ValueError("This copy is already entered in QuickBooks; its number can't be changed here.")
            old = row.get(col)
            if (old or "") == number:
                conn.rollback()
                return False
            cur.execute(f"UPDATE {table} SET {col} = %s, edited_by = %s, edited_at = now() WHERE id = %s",
                        (number, username, record_id))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (table, record_id, col, old, number,
                                                       f"{username} (kept on Possible duplicates)"))
            cur.execute(f"SELECT id, {lnum} AS n FROM {ledger} WHERE source_file = %s "
                        f"AND lower(coalesce({lnum}, '')) = lower(coalesce(%s, '')) "
                        f"AND abs(coalesce({lamt}, 0) - coalesce(%s, 0)) < 0.005",
                        (row.get("email_filename"), old, row.get("amount")))
            for h in cur.fetchall():
                cur.execute(f"UPDATE {ledger} SET {lnum} = %s WHERE id = %s", (number, h["id"]))
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                            "VALUES (%s,%s,%s,%s,%s,%s)", (ledger, h["id"], lnum, h["n"], number,
                                                           f"{username} (kept on Possible duplicates)"))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def keep_and_ignore_others(kept_type, kept_id, others, username):
    """Resolve an entire duplicate SET in one action: keep kept_type/kept_id as the
    correct copy (clearing its own duplicate_suspect flag, if it had one — e.g. the
    user picked a non-default column as the keeper), and mark every record in
    `others` (list of (record_type, record_id) tuples) as a confirmed duplicate,
    excluded from QB, linked back to the kept record via duplicate_of. Mirrors
    clicking 'Ignore this one' on each of the others individually, but atomic and
    one click. Invoices/payments only, same as set_duplicate_ignored.

    The kept copy takes over the "entered in QuickBooks" mark of a copy set aside here, when the
    two match in amount and store (2026-10-14, Joel: "if the information is basically the same,
    meaning no financial impact"). Before, keeping a copy that wasn't marked entered over one that
    was left the bill on QB Entry as not entered, ready to be entered twice. When the entered copy's
    amount or store differs, nothing carries over (QuickBooks needs fixing first) and the result
    says so. Returns {"carried": bool, "differs": [{"amount", "store"} of each such entered copy]}."""
    tables = {"invoice": "invoices", "payment": "payments"}
    kept_table = tables.get(kept_type)
    kept, entered_others = None, []
    if kept_table:
        conn = connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SELECT email_filename, amount, store_matched, entered_in_qb FROM {kept_table} "
                            f"WHERE id = %s", (kept_id,))
                kept = cur.fetchone()
                for other_type, other_id in others:
                    if other_type not in tables:
                        continue
                    cur.execute(f"SELECT amount, store_matched FROM {tables[other_type]} "
                                f"WHERE id = %s AND entered_in_qb IS TRUE AND duplicate_ignored IS NOT TRUE",
                                (other_id,))
                    row = cur.fetchone()
                    if row:
                        entered_others.append(row)
        finally:
            conn.close()
    kept_email = kept["email_filename"] if kept else None
    for other_type, other_id in others:
        set_duplicate_ignored(other_type, other_id, True, username, duplicate_of=kept_email)
    if kept_table:
        clear_duplicate_flag(kept_type, kept_id, username)
    result = {"carried": False, "differs": []}
    if not kept or kept["entered_in_qb"] or not entered_others:
        return result

    def _same(o):
        try:
            same_amount = abs(float(o["amount"]) - float(kept["amount"])) < 0.005
        except (TypeError, ValueError):
            same_amount = False
        return same_amount and ((o["store_matched"] or "").strip().lower()
                                == (kept["store_matched"] or "").strip().lower())

    if any(_same(o) for o in entered_others):
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {kept_table} SET entered_in_qb = TRUE, entered_qb_by = %s, "
                            f"entered_qb_at = now() WHERE id = %s AND entered_in_qb IS NOT TRUE",
                            (username, kept_id))
                if cur.rowcount:
                    cur.execute(
                        "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES (%s,%s,'entered_in_qb','False','True',%s)",
                        (kept_table, kept_id, f"{username} (kept over a copy already entered in QuickBooks)"))
            conn.commit()
        finally:
            conn.close()
        result["carried"] = True
    else:
        result["differs"] = [{"amount": o["amount"], "store": o["store_matched"]} for o in entered_others]
    return result


# --------------------------------------------------------------------------- #
# Payments (dedicated tab — always QB-entered, never excluded/duplicate-checked)
# --------------------------------------------------------------------------- #

_PAYMENT_SORT = {"vendor": "p.vendor_matched", "store": "p.store_matched",
                 "amount": "p.amount", "date": "p.payment_date",
                 "reference": "p.reference_number", "entered": "p.entered_in_qb",
                 "date_added": "p.date_added"}


def _money_or_none(v):
    """'$1,091.33' / '1091.33' -> 1091.33; blank or unreadable -> None (the filter is then skipped)."""
    s = str(v or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s) if s else None
    except ValueError:
        return None


def payment_vendor_names():
    """Distinct payment vendors, for the Payments vendor filter's suggestions."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT vendor_matched FROM payments WHERE coalesce(btrim(vendor_matched),'') <> '' "
                        "ORDER BY 1")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def list_payments(store=None, entered=None, run_id=None, date_from=None, date_to=None,
                  sort="date_added", direction="desc", dup=None, show_ignored=False, group=None,
                  show_skipped=False, missing=None, q=None, vendor=None, reference=None,
                  applied=None, amount_min=None, amount_max=None, added_from=None, added_to=None):
    """Payment records for the Payments tab, joined to emails for the run filter.
    Filters: store, entered/not-entered, run, payment-date range, and possible-duplicate
    (dup: 'yes' = flagged, 'all' = include flagged (no filter), else default/'no' = hide
    flagged). Auto-ignored REAL duplicates are
    HIDDEN by default (they're never entered) but reachable with show_ignored=True.
    skip_qb_import payments (reference-only — e.g. a per-invoice remittance breakdown that
    repeats cash already captured by the real bank receipt) are likewise HIDDEN by default so
    they don't clutter the to-enter queue, reachable with show_skipped=True."""
    clauses, params = ["TRUE"], []
    if not show_ignored:
        clauses.append("p.duplicate_ignored IS NOT TRUE")
    if not show_skipped:
        clauses.append("p.skip_qb_import IS NOT TRUE")
    if store:
        clauses.append("p.store_matched = %s"); params.append(store)
    if entered == "yes":
        clauses.append("p.entered_in_qb IS TRUE")
    elif entered == "no":
        clauses.append("p.entered_in_qb IS NOT TRUE")
    if dup == "yes":
        clauses.append("p.duplicate_suspect IS TRUE")
    elif dup != "all":
        # default, or dup == "no": hide possible duplicates from QB entry
        clauses.append("p.duplicate_suspect IS NOT TRUE")
    if group:
        clauses.append("p.duplicate_group_id = %s"); params.append(group)
    if run_id:
        clauses.append("e.run_id = %s"); params.append(run_id)
    if missing == "1":            # only payments lacking a store, date or amount
        clauses.append(f"{_missing_info_sql('payment_date', 'p')} <> ''")
    # Range on payment_date, but fall back to date_added for rows whose payment_date
    # is null/non-ISO — so a date filter never silently drops a payment. date_added is
    # always set by the pipeline, so the effective date is never null in practice.
    eff_date = ("COALESCE(CASE WHEN p.payment_date ~ '^\\d{4}-\\d{2}-\\d{2}$' "
                "THEN p.payment_date::date END, p.date_added)")
    if date_from:
        clauses.append(f"{eff_date} >= %s"); params.append(date_from)
    if date_to:
        clauses.append(f"{eff_date} <= %s"); params.append(date_to)
    # Search fields (2026-10-12). Text matches ignore case; reference and applied-invoice numbers also
    # ignore punctuation and spaces, so '75227825' finds 'REF-7522 7825'.
    alnum = "regexp_replace(lower(coalesce({}, '')), '[^a-z0-9]', '', 'g')"
    if q and q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(p.vendor_matched ILIKE %s OR p.reference_number ILIKE %s OR p.invoice_number ILIKE %s "
                       "OR p.store_matched ILIKE %s OR p.email_filename ILIKE %s OR p.notes ILIKE %s "
                       "OR CAST(p.amount AS TEXT) LIKE %s)")
        params.extend([like] * 6 + [f"%{_money_or_none(q) if _money_or_none(q) is not None else q.strip()}%"])
    if vendor and vendor.strip():
        clauses.append("p.vendor_matched ILIKE %s"); params.append(f"%{vendor.strip()}%")
    for value, col in ((reference, "p.reference_number"), (applied, "p.invoice_number")):
        key = re.sub(r"[^a-z0-9]", "", (value or "").lower())
        if key:
            clauses.append(f"{alnum.format(col)} LIKE %s"); params.append(f"%{key}%")
    lo, hi = _money_or_none(amount_min), _money_or_none(amount_max)
    if lo is not None:
        clauses.append("p.amount >= %s"); params.append(lo - 0.005)
    if hi is not None:
        clauses.append("p.amount <= %s"); params.append(hi + 0.005)
    if added_from:
        clauses.append("p.date_added >= %s"); params.append(added_from)
    if added_to:
        clauses.append("p.date_added <= %s"); params.append(added_to)
    where = "WHERE " + " AND ".join(clauses)
    order = _order_clause(sort, direction, _PAYMENT_SORT, "p.date_added DESC NULLS LAST")
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT p.id AS record_id, p.email_filename, p.vendor_matched, p.store_matched,
                       p.amount, p.payment_date, p.reference_number, p.invoice_number AS applied_invoice,
                       p.file_path, p.gmail_link,
                       p.entered_in_qb, p.entered_qb_by, p.entered_qb_at,
                       p.skip_qb_import, p.ignored_by, p.ignored_at,
                       p.duplicate_suspect, p.duplicate_ignored, p.duplicate_of,
                       p.duplicate_reason, p.duplicate_group_id, e.run_id,
                       {_missing_info_sql('payment_date', 'p')} AS missing_info
                  FROM payments p LEFT JOIN emails e ON e.filename = p.email_filename
                  {where}
                  {order}
            """, params)
            return cur.fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Editing records (+ audit log + rule re-evaluation)
# --------------------------------------------------------------------------- #

# UI field -> DB column, per record type. Only these may be edited.
EDITABLE = {
    "invoice": {"vendor": "vendor_matched", "store": "store_matched",
                "number": "invoice_number", "date": "invoice_date",
                "amount": "amount", "sales_tax": "sales_tax", "ship_charge": "ship_charge"},
    "credit_memo": {"vendor": "vendor_matched", "store": "store_matched",
                    "number": "credit_memo_number", "date": "credit_memo_date", "amount": "amount"},
    "payment": {"vendor": "vendor_matched", "store": "store_matched",
                "number": "reference_number", "date": "payment_date", "amount": "amount"},
}
_TABLE = {"invoice": "invoices", "credit_memo": "credit_memos", "payment": "payments"}
_NUMERIC = {"amount", "sales_tax", "ship_charge"}


def get_record(record_type, record_id):
    """Full row for one record, ENRICHED with the source email (sender/received_at/
    run_id/email_status/gmail_message_id) and its classify-run row (started/finished).
    Keeps all the raw table columns (r.*) the edit page relies on — just adds keys.
    Handles record_type 'review' (from review_queue) as well."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if record_type == "review":
                cur.execute(
                    "SELECT rq.*, e.from_address AS sender, e.received_at, e.status AS email_status, "
                    "e.gmail_message_id, ru.started_at AS run_started_at, ru.finished_at AS run_finished_at "
                    "FROM review_queue rq "
                    "LEFT JOIN emails e ON e.filename = rq.source_email "
                    "LEFT JOIN runs ru ON ru.run_id = rq.run_id WHERE rq.id = %s", (record_id,))
                return cur.fetchone()
            table = _TABLE.get(record_type)
            if not table:
                return None
            cur.execute(
                f"SELECT r.*, e.from_address AS sender, e.received_at, e.run_id, "
                f"e.status AS email_status, e.gmail_message_id, "
                f"ru.started_at AS run_started_at, ru.finished_at AS run_finished_at "
                f"FROM {table} r "
                f"LEFT JOIN emails e ON e.filename = r.email_filename "
                f"LEFT JOIN runs ru ON ru.run_id = e.run_id WHERE r.id = %s", (record_id,))
            return cur.fetchone()
    finally:
        conn.close()


def get_edit_log(record_type, record_id):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM edit_log WHERE table_name = %s AND record_id = %s "
                        "ORDER BY changed_at DESC", (_TABLE[record_type], record_id))
            return cur.fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Flag for review (user-submitted) + admin-triggered investigations.
# --------------------------------------------------------------------------- #

_FLAG_STATUSES = {"pending", "dismissed", "approved_for_fix"}
# Change requests add a 4th 'done' state (flags don't track completion; requests are a build backlog).
_REQUEST_STATUSES = {"pending", "dismissed", "approved_for_build", "done"}


def _record_snapshot(record_type, rec):
    """Freeze the record's relevant fields at flag time, so an investigation has stable context
    even if the record is later edited. Numbers/date column names vary by record type."""
    num_col = {"invoice": "invoice_number", "credit_memo": "credit_memo_number",
               "payment": "reference_number", "review": "invoice_number"}.get(record_type)
    date_col = {"invoice": "invoice_date", "credit_memo": "credit_memo_date",
                "payment": "payment_date", "review": "date_flagged"}.get(record_type)

    def _f(k):
        v = rec.get(k)
        return float(v) if v is not None else None

    return {
        "record_type": record_type, "record_id": rec.get("id"),
        "vendor": rec.get("vendor_matched"), "store": rec.get("store_matched"),
        "number": rec.get(num_col), "date": str(rec.get(date_col) or "") or None,
        "amount": _f("amount"), "sales_tax": _f("sales_tax"), "ship_charge": _f("ship_charge"),
        "duplicate_suspect": bool(rec.get("duplicate_suspect")),
        "duplicate_ignored": bool(rec.get("duplicate_ignored")),
        "entered_in_qb": bool(rec.get("entered_in_qb")),
        "source_file": rec.get("file_path"),
        "email_filename": rec.get("email_filename"),
    }


def add_flag(record_type, record_id, note, submitted_by):
    """Create a pending flag for one record, snapshotting its current field values."""
    rec = get_record(record_type, record_id)
    if not rec:
        raise ValueError("record not found")
    snap = _record_snapshot(record_type, rec)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO flagged_reviews (record_type, record_id, snapshot, note, submitted_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (record_type, record_id, psycopg2.extras.Json(snap), note, submitted_by))
            fid = cur.fetchone()[0]
        conn.commit()
        return fid
    finally:
        conn.close()


def add_duplicate_set_flag(records, note, submitted_by):
    """Flag a whole possible-duplicate SET for review in one entry. Unlike add_flag
    (one record, one flag), this embeds every record's frozen field snapshot into a
    single flagged_reviews row — snap['duplicate_set'] — so an investigation (which has
    no DB access, only what's in this snapshot) can actually see every copy's
    store/vendor/amount/date side by side instead of just one record's own fields.
    The FIRST record in `records` is used as the flag's primary record_type/record_id
    (for linking/display) and its own fields are ALSO kept flat at the top level, same
    shape as add_flag's snapshot, so the /flags page's existing summary line (which
    reads s.vendor/s.amount/etc.) keeps working unchanged."""
    recs = []
    for rtype, rid in records:
        rec = get_record(rtype, rid)
        if rec:
            recs.append((rtype, rid, rec))
    if not recs:
        raise ValueError("no valid records in set")
    primary_type, primary_id, primary_rec = recs[0]
    snap = _record_snapshot(primary_type, primary_rec)
    snap["duplicate_set"] = [_record_snapshot(t, r) for t, _, r in recs]
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO flagged_reviews (record_type, record_id, snapshot, note, "
                "submitted_by) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (primary_type, primary_id, psycopg2.extras.Json(snap), note, submitted_by))
            fid = cur.fetchone()[0]
        conn.commit()
        return fid
    finally:
        conn.close()


def list_flags(username=None, is_admin=False):
    """Flags newest first, each with its LATEST investigation (status/findings/cost) for the list.
    Admin sees all; a non-admin sees only flags they submitted."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where, params = "", []
            if not is_admin:
                where = "WHERE f.submitted_by = %s"
                params = [username]
            cur.execute(f"""
                SELECT f.*,
                       i.id AS inv_id, i.status AS inv_status, i.findings AS inv_findings,
                       i.error AS inv_error, i.started_at AS inv_started, i.finished_at AS inv_finished,
                       i.cost_usd AS inv_cost, i.input_tokens AS inv_in, i.output_tokens AS inv_out,
                       i.num_turns AS inv_turns, i.triggered_by AS inv_by
                  FROM flagged_reviews f
                  LEFT JOIN LATERAL (
                      SELECT * FROM investigations WHERE flag_id = f.id
                       ORDER BY started_at DESC LIMIT 1
                  ) i ON TRUE
                  {where}
                 ORDER BY f.created_at DESC
            """, params)
            return cur.fetchall()
    finally:
        conn.close()


def get_flag(flag_id):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_reviews WHERE id = %s", (flag_id,))
            return cur.fetchone()
    finally:
        conn.close()


def build_fix_prompt(f):
    """Assemble a copy-paste-ready Claude Code fix prompt for a flag row (as returned by
    list_flags — i.e. the flag plus its .snapshot dict and the latest investigation joined).
    PURE TEXT, no side effects: this only produces what Joel would otherwise hand-write — it does
    NOT trigger any fix. Contents: a one-line title, the record snapshot, the submitter's note, any
    completed investigation findings, and the standard closing instruction used throughout this
    project."""
    s = f.get("snapshot") or {}
    rt = (f.get("record_type") or "record").replace("_", " ")

    def m(v):
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return None

    def dt(v):
        return str(v)[:16] if v else ""

    num = s.get("number")
    lines = []
    # 1) one-line title: record type · vendor · store · #number · amount
    title = " · ".join(x for x in [
        rt.title(), s.get("vendor") or "—", s.get("store"),
        ("#" + str(num)) if num else None, m(s.get("amount")),
    ] if x)
    lines.append("Fix needed — " + title)
    lines.append("")
    # 2) snapshot
    lines.append("RECORD SNAPSHOT")

    def row(label, val):
        if val not in (None, "", []):
            lines.append(f"- {label}: {val}")

    row("Type", rt)
    row("Vendor", s.get("vendor"))
    row("Store", s.get("store"))
    row("Number", ("#" + str(num)) if num else None)
    row("Date", s.get("date"))
    row("Amount", m(s.get("amount")))
    row("Sales tax", m(s.get("sales_tax")))
    row("Shipping", m(s.get("ship_charge")))
    row("Entered in QB", "yes" if s.get("entered_in_qb") else "no")
    if s.get("duplicate_ignored"):
        row("Duplicate", "ignored")
    elif s.get("duplicate_suspect"):
        row("Duplicate", "possible duplicate")
    row("Source file", s.get("source_file"))
    row("Source email", s.get("email_filename"))
    # Review-queue flags live at /review/<id>; live records at /records/<type>/<id>.
    _rt, _rid = f.get("record_type"), f.get("record_id")
    row("Record page", f"/review/{_rid}" if _rt == "review" else f"/records/{_rt}/{_rid}")
    # 3) submitter's note
    lines.append("")
    who = f.get("submitted_by") or "someone"
    when = dt(f.get("created_at"))
    lines.append(f"FLAGGED BY {who}" + (f" ON {when}" if when else "") + ":")
    lines.append((f.get("note") or "(no note provided)").strip())
    # 4) investigation findings, if one completed
    if f.get("inv_status") == "completed" and (f.get("inv_findings") or "").strip():
        lines.append("")
        fin = dt(f.get("inv_finished"))
        lines.append("AUTOMATED INVESTIGATION FINDINGS" + (f" ({fin})" if fin else "") + ":")
        lines.append(f["inv_findings"].strip())
    # 5) closing instruction (matches how Joel closes every prompt in this project)
    lines.append("")
    lines.append("---")
    lines.append("Please investigate and implement an appropriate fix. "
                 "Update PROJECT_STATUS.md and report back.")
    return "\n".join(lines)


def set_flag_status(flag_id, status, admin):
    """Admin action: 'dismissed' or 'approved_for_fix' (or back to 'pending')."""
    if status not in _FLAG_STATUSES:
        raise ValueError(f"invalid flag status: {status}")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE flagged_reviews SET status = %s, resolved_by = %s, "
                "resolved_at = (CASE WHEN %s = 'pending' THEN NULL ELSE now() END) WHERE id = %s",
                (status, admin, status, flag_id))
        conn.commit()
    finally:
        conn.close()


def any_investigation_running():
    """True if an investigation is currently 'running' (one-at-a-time guard, DB-enforced)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM investigations WHERE status = 'running' LIMIT 1")
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_investigation(flag_id=None, request_id=None, triggered_by=None):
    """Insert a 'running' investigation for EXACTLY ONE subject — a flag or a change request —
    and return its id. The investigations_one_subject CHECK enforces the exclusivity."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO investigations (flag_id, request_id, triggered_by) "
                "VALUES (%s, %s, %s) RETURNING id",
                (flag_id, request_id, triggered_by))
            iid = cur.fetchone()[0]
        conn.commit()
        return iid
    finally:
        conn.close()


def fail_stale_investigations(older_than_seconds):
    """Mark any still-'running' investigation older than N seconds as 'failed' — used on startup
    / before enqueue so a run interrupted by an app restart or crash never sits 'running' forever.
    Returns how many it swept."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE investigations SET status = 'failed', finished_at = now(), "
                "error = COALESCE(error, 'interrupted (app restart or crash) — no result recorded') "
                "WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)",
                (older_than_seconds,))
            return cur.rowcount
    finally:
        conn.commit()
        conn.close()


def finish_investigation(inv_id, status, findings=None, error=None, usage=None):
    """Mark an investigation completed/failed and store findings/error + usage. `usage` is an
    optional dict with num_turns/input_tokens/output_tokens/cost_usd/duration_ms."""
    u = usage or {}
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE investigations SET status = %s, findings = %s, error = %s, "
                "finished_at = now(), num_turns = %s, input_tokens = %s, output_tokens = %s, "
                "cost_usd = %s, duration_ms = %s WHERE id = %s AND status = 'running'",
                (status, findings, error, u.get("num_turns"), u.get("input_tokens"),
                 u.get("output_tokens"), u.get("cost_usd"), u.get("duration_ms"), inv_id))
        conn.commit()
    finally:
        conn.close()
    # (Only a still-running row is finished: a run a person stopped or deleted is never overwritten.)


def stop_investigation(inv_id, username, detail=""):
    """Mark a running investigation stopped by a person (status 'failed', error "Stopped by …").
    Logged. Returns True if it was running."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE investigations SET status = 'failed', finished_at = now(), error = %s "
                        "WHERE id = %s AND status = 'running'",
                        (f"Stopped by {username}" + (f" ({detail})" if detail else ""), inv_id))
            changed = cur.rowcount > 0
            if changed:
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                            "VALUES ('investigations', %s, 'status', 'running', 'stopped', %s)", (inv_id, username))
        conn.commit()
        return changed
    finally:
        conn.close()


def delete_investigation(inv_id, username):
    """Delete one investigation record entirely (admins). Refuses a running one -- stop it first. The
    flag or request it belonged to is untouched. Logged with a short summary of what was deleted.
    Returns (ok, message)."""
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (inv_id,))
            inv = cur.fetchone()
            if not inv:
                conn.rollback()
                return (False, "That investigation no longer exists.")
            if inv["status"] == "running":
                conn.rollback()
                return (False, "That investigation is still running — stop it first.")
            subject = f"flag {inv['flag_id']}" if inv.get("flag_id") else f"request {inv.get('request_id')}"
            summary = (f"{subject}, {inv['status']}, started {inv['started_at']:%Y-%m-%d %H:%M}"
                       + (f", ${float(inv['cost_usd']):.4f}" if inv.get("cost_usd") is not None else ""))
            cur.execute("DELETE FROM investigations WHERE id = %s", (inv_id,))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('investigations', %s, 'deleted', %s, 'deleted', %s)", (inv_id, summary, username))
        conn.commit()
        return (True, "Investigation deleted.")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Change requests — same review lifecycle as flags, but about the APP itself,
# not tied to any accounting record. Functions mirror the flag ones above.
# --------------------------------------------------------------------------- #

def add_change_request(title, description, category, submitted_by):
    """Create a pending change request. Returns its id."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO change_requests (title, description, category, submitted_by) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                ((title or "").strip(), (description or "").strip(),
                 (category or "").strip() or None, submitted_by))
            rid = cur.fetchone()[0]
        conn.commit()
        return rid
    finally:
        conn.close()


def get_change_request(request_id):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM change_requests WHERE id = %s", (request_id,))
            return cur.fetchone()
    finally:
        conn.close()


def list_change_requests(username=None, is_admin=False):
    """Change requests newest first, each with its LATEST investigation joined (same shape as
    list_flags). Admin sees all; a non-admin sees only requests they submitted."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where, params = "", []
            if not is_admin:
                where = "WHERE r.submitted_by = %s"
                params = [username]
            cur.execute(f"""
                SELECT r.*,
                       i.id AS inv_id, i.status AS inv_status, i.findings AS inv_findings,
                       i.error AS inv_error, i.started_at AS inv_started, i.finished_at AS inv_finished,
                       i.cost_usd AS inv_cost, i.input_tokens AS inv_in, i.output_tokens AS inv_out,
                       i.num_turns AS inv_turns, i.triggered_by AS inv_by
                  FROM change_requests r
                  LEFT JOIN LATERAL (
                      SELECT * FROM investigations WHERE request_id = r.id
                       ORDER BY started_at DESC LIMIT 1
                  ) i ON TRUE
                  {where}
                 ORDER BY r.created_at DESC
            """, params)
            return cur.fetchall()
    finally:
        conn.close()


def set_request_status(request_id, status, admin):
    """Admin action: dismissed | approved_for_build | done (or back to pending)."""
    if status not in _REQUEST_STATUSES:
        raise ValueError(f"invalid change-request status: {status}")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title, submitted_by, status FROM change_requests WHERE id = %s", (request_id,))
            before = cur.fetchone()
            cur.execute(
                "UPDATE change_requests SET status = %s, resolved_by = %s, "
                "resolved_at = (CASE WHEN %s = 'pending' THEN NULL ELSE now() END) WHERE id = %s",
                (status, admin, status, request_id))
            # Tell the submitter (2026-10-21), unless nothing changed or they made the change themselves.
            if before and before[1] and before[2] != status and before[1] != admin:
                words = {"approved_for_build": "approved for build", "done": "done ✓",
                         "dismissed": "dismissed", "pending": "put back to pending"}.get(status, status)
                cur.execute("INSERT INTO user_notifications (username, message, link) VALUES (%s, %s, %s)",
                            (before[1], f"Your change request \"{before[0]}\" was marked {words} by {admin}.",
                             "/requests"))
        conn.commit()
    finally:
        conn.close()


def list_notifications(username):
    """Unread notifications for one user, newest first."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, message, link, created_at FROM user_notifications "
                        "WHERE username = %s AND read_at IS NULL ORDER BY created_at DESC LIMIT 50", (username,))
            return cur.fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def dismiss_notifications(username, notification_id=None):
    """Mark one (or, with no id, all) of this user's notifications read."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            if notification_id:
                cur.execute("UPDATE user_notifications SET read_at = now() WHERE id = %s AND username = %s "
                            "AND read_at IS NULL", (notification_id, username))
            else:
                cur.execute("UPDATE user_notifications SET read_at = now() WHERE username = %s AND read_at IS NULL",
                            (username,))
    finally:
        conn.close()


def build_request_prompt(r):
    """Copy-paste-ready Claude Code prompt for an approved change request (as returned by
    list_change_requests — the row plus its latest investigation joined). PURE TEXT, triggers
    nothing: the title, category, submitter, the full description, any completed investigation
    findings, and the same standard closing instruction build_fix_prompt uses."""
    def dt(v):
        return str(v)[:16] if v else ""
    lines = []
    cat = (r.get("category") or "").strip()
    lines.append("Change request — " + (r.get("title") or "(untitled)")
                 + (f" [{cat}]" if cat else ""))
    lines.append("")
    who = r.get("submitted_by") or "someone"
    when = dt(r.get("created_at"))
    lines.append(f"SUBMITTED BY {who}" + (f" ON {when}" if when else "") + ":")
    lines.append((r.get("description") or "(no description provided)").strip())
    if r.get("inv_status") == "completed" and (r.get("inv_findings") or "").strip():
        lines.append("")
        fin = dt(r.get("inv_finished"))
        lines.append("AUTOMATED INVESTIGATION FINDINGS" + (f" ({fin})" if fin else "") + ":")
        lines.append(r["inv_findings"].strip())
    lines.append("")
    lines.append("---")
    lines.append("Please investigate and implement an appropriate fix. "
                 "Update PROJECT_STATUS.md and report back.")
    return "\n".join(lines)


_QB_CORRECTION_FIELDS = ("amount", "number", "date", "vendor", "store", "sales_tax", "ship_charge")
_QB_FIELD_LABEL = {"amount": "Amount", "number": "Bill #", "date": "Date", "vendor": "Vendor", "store": "Store",
                   "sales_tax": "Sales tax", "ship_charge": "Shipping"}


def _same_value(a, b):
    if a in (None, "") and b in (None, ""):
        return True
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a or "").strip().lower() == str(b or "").strip().lower()


def _note_qb_correction(cur, record_type, record_id, ui_field, row, old, new, username):
    """Open (or update) the QuickBooks correction for this record + field. Several edits to one field keep
    ONE open item, from the value QuickBooks has (the first old value) to the latest; editing it back to
    that value closes the item by itself."""
    num_col = EDITABLE.get(record_type, {}).get("number")
    cur.execute("SELECT id, old_value FROM qb_corrections WHERE record_type=%s AND record_id=%s AND field=%s "
                "AND done_at IS NULL ORDER BY id DESC LIMIT 1", (record_type, record_id, ui_field))
    open_row = cur.fetchone()
    new_s = None if new is None else str(new)
    if open_row:
        if _same_value(open_row["old_value"], new_s):
            cur.execute("UPDATE qb_corrections SET done_at = now(), done_by = %s, note = 'changed back -- nothing to do' "
                        "WHERE id = %s", (username, open_row["id"]))
        else:
            cur.execute("UPDATE qb_corrections SET new_value=%s, changed_by=%s, changed_at=now() WHERE id=%s",
                        (new_s, username, open_row["id"]))
        return
    if _same_value(old, new_s):
        return
    cur.execute("INSERT INTO qb_corrections (record_type, record_id, field, old_value, new_value, store, vendor, number, "
                "changed_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (record_type, record_id, ui_field, None if old is None else str(old), new_s,
                 row.get("store_matched"), row.get("vendor_matched"), row.get(num_col) if num_col else None, username))


def list_qb_corrections(show="open"):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = "WHERE done_at IS NULL" if show == "open" else ("WHERE done_at IS NOT NULL" if show == "done" else "")
            cur.execute(f"SELECT * FROM qb_corrections {where} ORDER BY coalesce(done_at, changed_at) DESC, id DESC LIMIT 500")
            rows = cur.fetchall()
        for r in rows:
            r["field_label"] = _QB_FIELD_LABEL.get(r["field"], r["field"])
        return rows
    finally:
        conn.close()


def qb_correction_done(correction_id, username, note=None):
    """Someone made the change in QuickBooks: the record takes the new value now, and the item closes."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM qb_corrections WHERE id = %s AND done_at IS NULL", (correction_id,))
            c = cur.fetchone()
    finally:
        conn.close()
    if not c:
        return False
    update_record_field(c["record_type"], c["record_id"], c["field"], c["new_value"] or "",
                        f"{username} (QB correction done)", qb_confirmed=True)
    _close_qb_correction(correction_id, username, note)
    return True


def _close_qb_correction(correction_id, username, note=None):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE qb_corrections SET done_at = now(), done_by = %s, note = coalesce(%s, note) "
                        "WHERE id = %s AND done_at IS NULL", (username, (note or "").strip() or None, correction_id))
        conn.commit()
    finally:
        conn.close()


def resolve_qb_corrections_from_register():
    """After a QuickBooks export upload (and when the page opens): an open correction whose bill shows the
    NEW value in an export uploaded after the change (same store, related vendor, date within 10 days, bill #
    = old or new) is applied to the record and closed ("confirmed by QuickBooks export"). One that still shows
    the old value stays open with a note. Store / vendor changes are left for "Done in QB". Returns the
    number closed."""
    import classify_emails as ce
    if not ce.VENDOR_STD:
        ce.VENDOR_STD = ce.load_vendor_standardization()
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT c.*, i.invoice_date, i.amount AS rec_amount, i.invoice_number AS rec_number "
                        "  FROM qb_corrections c JOIN invoices i ON c.record_type = 'invoice' AND i.id = c.record_id "
                        " WHERE c.done_at IS NULL AND c.field IN ('amount', 'number', 'date')")
            items = cur.fetchall()
            if not items:
                return 0
            cur.execute("SELECT qb_store, date_from, date_to, uploaded_at FROM qb_register_coverage")
            coverage = cur.fetchall()
            cur.execute("SELECT qb_store, qb_date, qb_num, qb_name, qb_std_vendor, qb_amount FROM qb_register "
                        "WHERE qb_type ILIKE 'bill'")
            bills = cur.fetchall()
    finally:
        conn.close()
    closed = 0
    for c in items:
        try:
            d = datetime.datetime.strptime(str(c["invoice_date"] or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            d = None
        newer = [v for v in coverage if ce._norm(v["qb_store"]) == ce._norm(c["store"])
                 and v["uploaded_at"] and c["changed_at"] and v["uploaded_at"] > c["changed_at"]
                 and d and v["date_from"] and v["date_to"] and v["date_from"] <= d <= v["date_to"]]
        if not newer:
            continue
        nums = {ce._num_core(x) for x in (c["number"], c["rec_number"], c["new_value"] if c["field"] == "number" else None) if x}
        cands = [b for b in bills if ce._norm(b["qb_store"]) == ce._norm(c["store"])
                 and ce._vendors_related(b["qb_name"], c["vendor"] or "")
                 and (not d or not b["qb_date"] or abs((b["qb_date"] - d).days) <= 10)
                 and (not nums or ce._num_core(b["qb_num"]) in nums)]
        def shows_new(b):
            if c["field"] == "amount":
                return _same_value(b["qb_amount"], c["new_value"])
            if c["field"] == "number":
                return ce._num_core(b["qb_num"]) == ce._num_core(c["new_value"])
            return b["qb_date"] and str(b["qb_date"]) == str(c["new_value"])[:10]
        if any(shows_new(b) for b in cands):
            update_record_field(c["record_type"], c["record_id"], c["field"], c["new_value"] or "",
                                "QuickBooks export (QB correction confirmed)", qb_confirmed=True)
            _close_qb_correction(c["id"], "QuickBooks export", "confirmed by a newer QuickBooks export")
            closed += 1
    return closed


def qb_corrections_open_count():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM qb_corrections WHERE done_at IS NULL")
            return cur.fetchone()[0]
    finally:
        conn.close()


def update_record_field(record_type, record_id, ui_field, new_value, username, qb_confirmed=False):
    """Update one whitelisted field, log old→new, and (if vendor/number changed)
    re-evaluate skip_qb_import + Possible Duplicate using the pipeline's rules.

    A record already entered in QuickBooks is NOT changed (2026-10-21, Joel: changing it would break the
    match with QuickBooks): the edit becomes a pending QB correction instead, and the record takes the new
    value only once QuickBooks has it -- a newer QuickBooks export shows it, or someone clicks "Done in QB"
    (qb_confirmed=True). Returns "pending" in that case, else None."""
    import classify_emails as ce  # reuse is_excluded_vendor + check_duplicate

    table = _TABLE[record_type]
    col = EDITABLE.get(record_type, {}).get(ui_field)
    if not col:
        raise ValueError(f"Field '{ui_field}' is not editable on a {record_type}")

    value = None if new_value in (None, "") else new_value
    if col in _NUMERIC and value is not None:
        try:
            value = float(value)
        except ValueError:
            raise ValueError(f"{ui_field} must be a number")

    conn = connect()
    conn.autocommit = False  # this edit + its log + re-eval must be one transaction
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (record_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("record not found")
            old = row.get(col)
            if row.get("entered_in_qb") and ui_field in _QB_CORRECTION_FIELDS and not qb_confirmed:
                _note_qb_correction(cur, record_type, record_id, ui_field, row, old, value, username)
                conn.commit()
                return "pending"

            cur.execute(f"UPDATE {table} SET {col} = %s, edited_by = %s, edited_at = now() "
                        f"WHERE id = %s", (value, username, record_id))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, "
                        "new_value, changed_by) VALUES (%s,%s,%s,%s,%s,%s)",
                        (table, record_id, col, str(old) if old is not None else None,
                         str(value) if value is not None else None, username))

            # A store edit reaches the record's duplicate-ledger row too (2026-10-14). The ledger kept the old store:
            # Abbott #622312457 was moved BMW -> Maple here on 9/23, and a later copy under Maple was flagged as "already
            # recorded to BMW" (a store conflict with a copy entered in QuickBooks) instead of being a plain duplicate.
            if ui_field == "store" and record_type in ("invoice", "payment"):
                ledger, num_col, amt_col = (("invoice_history", "invoice_number", "invoice_amount") if record_type == "invoice"
                                            else ("payment_history", "reference_number", "amount"))
                rec_num = row.get("invoice_number") if record_type == "invoice" else row.get("reference_number")
                cur.execute(f"SELECT id, store_matched FROM {ledger} WHERE source_file = %s "
                            f"AND lower(coalesce({num_col}, '')) = lower(coalesce(%s, '')) "
                            f"AND abs(coalesce({amt_col}, 0) - coalesce(%s, 0)) < 0.005",
                            (row.get("email_filename"), rec_num, row.get("amount")))
                for h in cur.fetchall():
                    if (h["store_matched"] or "") != (value or ""):
                        cur.execute(f"UPDATE {ledger} SET store_matched = %s WHERE id = %s", (value, h["id"]))
                        cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                                    "VALUES (%s,%s,'store_matched',%s,%s,%s)",
                                    (ledger, h["id"], h["store_matched"], value, f"{username} (store edited on its record)"))

            # Re-evaluate derived flags when the inputs to them change.
            if record_type in ("invoice", "credit_memo") and ui_field == "vendor":
                skip = ce.is_excluded_vendor(value)
                cur.execute(f"UPDATE {table} SET skip_qb_import = %s WHERE id = %s",
                            (skip, record_id))
            if record_type == "invoice" and ui_field in ("vendor", "number"):
                cur.execute("SELECT * FROM invoices WHERE id = %s", (record_id,))
                inv = cur.fetchone()
                fn = inv.get("email_filename") or ""
                # History excluding this record's OWN source email (by either id) so
                # it can't duplicate itself; check_duplicate self-excludes too.
                cur.execute("SELECT vendor_matched, invoice_number, invoice_amount, source_file, "
                            "source_email_id FROM invoice_history "
                            "WHERE source_file <> %s AND (source_email_id IS NULL OR source_email_id <> %s)",
                            (fn, fn))
                history = [dict(vendor_matched=h["vendor_matched"], invoice_number=h["invoice_number"],
                                invoice_amount=float(h["invoice_amount"]) if h["invoice_amount"] is not None else None,
                                source_file=h["source_file"], source_email_id=h["source_email_id"])
                           for h in cur.fetchall()]
                status, reason, dup_of, _prior = ce.check_duplicate(
                    {"vendor_matched": inv.get("vendor_matched"),
                     "invoice_number": inv.get("invoice_number"),
                     "invoice_amount": float(inv["amount"]) if inv.get("amount") is not None else None},
                    history, source_id=fn)
                cur.execute("UPDATE invoices SET duplicate_suspect = %s, duplicate_reason = %s, "
                            "duplicate_ignored = %s, duplicate_of = %s WHERE id = %s",
                            (status in ("duplicate", "possible"), reason,
                             status == "duplicate", dup_of if status == "duplicate" else None,
                             record_id))

            # Live payment duplicate re-eval (parity with invoices) when the inputs to
            # check_duplicate_payment change: vendor, reference (ui 'number'), or amount.
            if record_type == "payment" and ui_field in ("vendor", "number", "amount"):
                cur.execute("SELECT * FROM payments WHERE id = %s", (record_id,))
                p = cur.fetchone()
                fn = p.get("email_filename") or ""
                cur.execute("SELECT vendor_matched, reference_number, amount, payment_date, "
                            "source_file, source_email_id FROM payment_history "
                            "WHERE source_file <> %s AND (source_email_id IS NULL OR source_email_id <> %s)",
                            (fn, fn))
                phist = [dict(vendor_matched=h["vendor_matched"], reference_number=h["reference_number"],
                              amount=float(h["amount"]) if h["amount"] is not None else None,
                              payment_date=h["payment_date"], source_file=h["source_file"],
                              source_email_id=h["source_email_id"]) for h in cur.fetchall()]
                status, reason, dup_of, _prior = ce.check_duplicate_payment(
                    {"vendor_matched": p.get("vendor_matched"),
                     "reference_number": p.get("reference_number"),
                     "payment_amount": float(p["amount"]) if p.get("amount") is not None else None,
                     "payment_date": p.get("payment_date")},
                    phist, source_id=fn)
                cur.execute("UPDATE payments SET duplicate_suspect = %s, duplicate_reason = %s, "
                            "duplicate_ignored = %s, duplicate_of = %s WHERE id = %s",
                            (status in ("duplicate", "possible"), reason,
                             status == "duplicate", dup_of if status == "duplicate" else None,
                             record_id))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Browse / filter (merged Records + History ledger tab)
# --------------------------------------------------------------------------- #

def _excluded_vendor_sql(mode, col):
    """WHERE fragment for the QuickBooks-excluded-vendors filter, matching the pipeline's
    is_excluded_vendor rule exactly: the vendor spellings on records and in the ledger that
    is_excluded_vendor says yes to (a WHOLE-name match since 2026-10-14; it used to be "contains,
    either direction", which caught Standard Process through 'Anda') are listed into the SQL.
    mode: 'exclude' (hide excluded), 'only' (keep only excluded), else '' (include all). No params
    needed."""
    if mode not in ("exclude", "only"):
        return ""
    import classify_emails as ce
    from psycopg2 import sql as pgsql
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT vendor_matched FROM all_records WHERE coalesce(vendor_matched, '') <> '' "
                        "UNION SELECT DISTINCT vendor_matched FROM invoice_history "
                        " WHERE coalesce(vendor_matched, '') <> ''")
            keys = ce._excluded_vendor_keys()
            hits = sorted({v.strip().lower() for (v,) in cur.fetchall() if ce._norm_vendor_std_key(v) in keys})
            listed = pgsql.Literal(hits).as_string(cur) if hits else None
    finally:
        conn.close()
    is_excl = (f"(lower(btrim(coalesce({col}, ''))) = ANY({listed}::text[]))" if listed else "FALSE")
    return is_excl if mode == "only" else f"NOT {is_excl}"


_RECORDS_SORT = {"date_added": "date_added", "vendor": "vendor_matched",
                 "store": "store_matched", "amount": "amount", "number": "number",
                 "record_type": "record_type", "date": "record_date",
                 "status": "email_status", "entered": "entered_in_qb",
                 "received": "received_at", "sender": "sender"}


def list_all_records(store=None, vendor=None, record_type=None, status=None,
                     entered=None, date_from=None, date_to=None, search=None,
                     sort="date_added", direction="desc", run_id=None, email_filename=None,
                     excluded="include", dup=None, ignored=None, group=None,
                     amount_min=None, amount_max=None, amount=None, page=None, page_size=None):
    """The rows of all_records matching the filters, sorted. Without page_size: the first 2,000 (a list). With
    page_size (the Records page, 2026-10-26: it rendered up to 2,000 rows, a 3.6 MB page): (one page of rows,
    total matching), so every record is reachable a page at a time."""
    # Archived old-system rows (purge_non_email_records.py --archive) are hidden here.
    clauses, params = ["legacy_migrated IS NOT TRUE"], []
    if store:
        clauses.append("store_matched = %s"); params.append(store)
    if vendor:
        clauses.append("vendor_matched ILIKE %s"); params.append(f"%{vendor}%")
    if record_type:
        clauses.append("record_type = %s"); params.append(record_type)
    if status:
        clauses.append("email_status = %s"); params.append(status)
    if run_id:
        clauses.append("run_id = %s"); params.append(run_id)
    if email_filename:
        clauses.append("email_filename = %s"); params.append(email_filename)
    if entered == "yes":
        clauses.append("entered_in_qb IS TRUE")
    elif entered == "no":
        clauses.append("entered_in_qb IS NOT TRUE")
    if date_from:
        clauses.append("date_added >= %s"); params.append(date_from)
    if date_to:
        clauses.append("date_added <= %s"); params.append(date_to)
    # Exact amount (default) wins over min/max range; rounded numeric compare avoids float bugs.
    if amount not in (None, ""):
        clauses.append("ROUND(amount::numeric, 2) = ROUND(%s::numeric, 2)"); params.append(amount)
    else:
        if amount_min not in (None, ""):
            clauses.append("amount >= %s"); params.append(amount_min)
        if amount_max not in (None, ""):
            clauses.append("amount <= %s"); params.append(amount_max)
    if search:
        clauses.append("(vendor_matched ILIKE %s OR number ILIKE %s OR email_filename ILIKE %s)")
        params += [f"%{search}%"] * 3
    # Possible-duplicate filter (mirrors /qb's dup=); credit_memo/review rows carry NULL
    # duplicate_suspect, so 'yes' naturally excludes them and 'no' keeps them.
    if dup == "yes":
        clauses.append("duplicate_suspect IS TRUE")
        # Keep excluded-vendor INVOICES out of the duplicate view (they never enter QB, so
        # there's nothing to protect) — matching the /duplicates working queue. Payments and
        # every other record type are unaffected.
        clauses.append(f"NOT (record_type = 'invoice' AND {_excluded_vendor_sql('only', 'vendor_matched')})")
    elif dup == "no":
        clauses.append("duplicate_suspect IS NOT TRUE")
    # Auto/manually-ignored REAL duplicates are HIDDEN by default (same convention as /qb and
    # /payments); ignored='1' shows them.
    if ignored != "1":
        clauses.append("duplicate_ignored IS NOT TRUE")
    # NOTE: `group` is handled inside the connection block below — it needs a lookup to widen
    # the match to the cluster's vendor+number (so review-queue rows, which can never carry a
    # duplicate_group_id, are pulled in too).
    excl = _excluded_vendor_sql(excluded, "vendor_matched")
    if excl:
        clauses.append(excl)

    order = _RECORDS_SORT.get(sort, "date_added")
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"

    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if group:
                # A Group link should surface EVERY record for the same document. The review arm now
                # exposes rq.duplicate_group_id (credit_memo still NULL), so this bootstrap SELECT can
                # find the flagged item's own gid even when no live invoice/payment carries it (the
                # history-only-prior case — otherwise step 1 found nothing and the link died). Then
                # resolve the group to its cluster's vendor+number and widen the match to that too,
                # mirroring how list_duplicate_candidates() clusters (by vendor+number, not the
                # sparsely-stamped group id). Not scoped by store — a store mismatch between same
                # vendor+number records is itself sometimes the problem worth seeing.
                cur.execute("SELECT vendor_matched, number FROM all_records "
                            "WHERE duplicate_group_id = %s LIMIT 1", (group,))
                g = cur.fetchone()
                if g and (g.get("vendor_matched") or g.get("number")):
                    clauses.append("(duplicate_group_id = %s OR "
                                   "(norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) AND number = %s))")
                    params += [group, g.get("vendor_matched"), g.get("number")]
                else:
                    # Unknown/old group id -> degrade safely to today's exact-match-only behavior.
                    clauses.append("duplicate_group_id = %s"); params.append(group)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            if page_size:
                cur.execute(f"SELECT count(*) AS n FROM all_records {where}", params)
                total = cur.fetchone()["n"]
                size = max(1, int(page_size))
                last = max(1, -(-total // size))
                try:
                    page = max(1, min(int(page or 1), last))
                except (TypeError, ValueError):
                    page = 1
                # record_type + record_id break ties (many rows share a date), so no row shows on two pages.
                cur.execute(f"SELECT * FROM all_records {where} "
                            f"ORDER BY {order} {direction} NULLS LAST, record_type, record_id DESC "
                            f"LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
                return cur.fetchall(), total
            cur.execute(f"SELECT * FROM all_records {where} "
                        f"ORDER BY {order} {direction} NULLS LAST LIMIT 2000", params)
            return cur.fetchall()
    finally:
        conn.close()


_VENDOR_TABLES = {"at": 0.0, "tables": None}
_VENDOR_TABLES_TTL = 60   # seconds — Setup edits show up within a minute


def _vendor_tables():
    """The classifier's vendor standardization tables (classify_emails.load_vendor_standardization),
    loaded here because the web app never loads the classifier's VENDOR_STD. Cached briefly; {} if
    they can't be read (callers then fall back to printed spellings)."""
    import classify_emails as ce
    now = time.time()
    if _VENDOR_TABLES["tables"] is None or now - _VENDOR_TABLES["at"] > _VENDOR_TABLES_TTL:
        try:
            _VENDOR_TABLES["tables"] = ce.load_vendor_standardization() or {}
        except Exception:
            _VENDOR_TABLES["tables"] = {}
        _VENDOR_TABLES["at"] = now
    return _VENDOR_TABLES["tables"]


def _duplicate_vendor_keyer():
    """vendor -> duplicate-identity key, the SAME one the pipeline's duplicate checks use
    (classify_emails._dedupe_vendor_key: the vendor's standard name via Vendor names — exact, then
    fuzzy — else the printed spelling; legal suffixes stripped). Memoized per call."""
    import classify_emails as ce
    tables, memo = _vendor_tables(), {}

    def key(vendor):
        v = vendor or ""
        if v not in memo:
            std = ce.resolve_standard_vendor(v, std_tables=tables) if (tables and v.strip()) else None
            memo[v] = ce._norm_vendor_dedupe(std or v)
        return memo[v]
    return key


def vendor_mapping_doubt(vendor_matched, standard_vendor):
    """True when a record's STANDARD vendor isn't what its matched vendor name is listed as in Setup ›
    Vendor names — i.e. the standard name came from a fuzzy guess, or from an older mapping that no
    longer agrees (the R & T Distributors → L&R Distributors case). False when the matched name is the
    standard name or one of its listed spellings, however it's capitalized or punctuated — which is
    most records (2026-10-12: the old letter-for-letter rule flagged 964, almost all harmless)."""
    import classify_emails as ce
    vendor, std = (vendor_matched or "").strip(), (standard_vendor or "").strip()
    if not vendor or not std:
        return False
    k = ce._norm_vendor_std_key
    if k(vendor) == k(std):
        return False
    tables = _vendor_tables()
    if not tables:
        return False                                  # can't tell — don't cry wolf
    listed = tables.get("lookup", {}).get(k(vendor))
    return not (listed and k(listed) == k(std))


def qb_twice_notes():
    """{invoice id: note} for live invoices whose duplicate set is in QuickBooks twice (dup_settle)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, qb_twice FROM invoices WHERE qb_twice IS NOT NULL "
                        "AND duplicate_ignored IS NOT TRUE AND ignored_at IS NULL")
            return {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def list_duplicate_candidates():
    """Duplicate 'sets' for the working queue. Each set groups every still-live (non-ignored)
    invoice/payment sharing the same vendor + document number — i.e. the copies of one
    document — where at least one copy is still flagged (duplicate_suspect, not ignored).

    EXCLUDED-VENDOR INVOICES are left OUT of duplicate review (they never enter QB anyway, so
    there's nothing to protect); payments are ALWAYS included, excluded-vendor or not. Grouping
    is by (record_type, vendor, number) — NOT duplicate_group_id — because most flagged rows never
    had a group id stamped. The vendor is the duplicate checks' own key (_duplicate_vendor_keyer:
    STANDARD name, 2026-10-12), so a set shows every copy the pipeline matched — before, 'MaxiHealth'
    scans and the 'Maxi Health' vendor copy they duplicated landed in different sets, and keeping a
    scan left the vendor copy live. The exception is a flagged payment with NO reference
    number: it can't cluster by number, so its set is its shared duplicate_group_id. Returns
    [{key, record_type, records:[...], original_email}], most-recently-flagged set first; within
    a set the non-suspect 'original' comes first, then flagged copies oldest→newest so the whole
    set (2, 3, … copies) renders together on one row."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pull every non-ignored record sharing a type + number with at least one qualifying
            # flagged copy; the vendor is matched in Python below (standard names don't exist in
            # SQL), and a group only counts when it holds a qualifying suspect itself. A flagged
            # INVOICE/credit copy only qualifies when it
            # would actually enter QB (skip_qb_import IS NOT TRUE) — so records kept out of QB (any
            # Setup-excluded vendor, which apply_vendor_exclusion already marks skip_qb_import=TRUE,
            # AND per-record skips like the multi-store cost-split invoices) don't clutter the queue:
            # there's nothing to protect. skip_qb_import is a strict superset of the old vendor-level
            # exclusion check, so this loses no coverage. Payments are ALWAYS included (never excluded).
            cur.execute(f"""
                SELECT m.* FROM all_records m
                WHERE m.record_type IN ('invoice','payment')
                  AND m.duplicate_ignored IS NOT TRUE
                  AND m.number IS NOT NULL AND m.number <> ''
                  AND EXISTS (
                    SELECT 1 FROM all_records f
                    WHERE f.record_type = m.record_type
                      AND f.number = m.number
                      AND f.duplicate_suspect IS TRUE AND f.duplicate_ignored IS NOT TRUE
                      AND (f.record_type = 'payment' OR f.skip_qb_import IS NOT TRUE)
                  )
            """)
            rows = cur.fetchall()
            # Also pull the ALREADY-IGNORED copies of those same documents, so a cluster whose
            # siblings were all ignored still shows the full side-by-side comparison — the live
            # suspect no longer appears "orphaned" alone. These are context columns (rendered
            # "Ignored", with a Restore action); they're never re-ignored and never create a new
            # all-ignored cluster on their own.
            nums = list({r["number"] for r in rows if r.get("number")})
            ignored_rows = []
            if nums:
                cur.execute(
                    "SELECT m.* FROM all_records m "
                    "WHERE m.record_type IN ('invoice','payment') "
                    "  AND m.duplicate_ignored IS TRUE AND m.number = ANY(%s)", (nums,))
                ignored_rows = cur.fetchall()
            # Payments with NO reference number can't cluster by number, so a flagged one (a "no
            # reference # to confirm" possible match, from the pipeline or recheck_duplicates.py) never
            # showed here — hidden from /payments by default AND absent from this page. Those sets are
            # built from the shared duplicate_group_id instead (2026-10-08); every member is included,
            # live and ignored, whatever its number.
            cur.execute(
                "SELECT m.* FROM all_records m "
                " WHERE m.record_type = 'payment' AND m.duplicate_group_id IN ("
                "   SELECT f.duplicate_group_id FROM all_records f "
                "    WHERE f.record_type = 'payment' AND coalesce(f.number, '') = '' "
                "      AND f.duplicate_suspect IS TRUE AND f.duplicate_ignored IS NOT TRUE "
                "      AND coalesce(f.duplicate_group_id, '') <> '')")
            no_ref_rows = cur.fetchall()
            # Every copy sharing a duplicate group with a qualifying live suspect, WHATEVER its number:
            # the misread-number check (2026-10-12) groups copies whose numbers differ ('93G4240' and
            # its misread '9304240', Lantev 10/13), and grouping by number alone left such a copy alone
            # in its own one-record set, which isn't shown (2026-10-14).
            cur.execute(
                "SELECT m.* FROM all_records m "
                " WHERE m.record_type IN ('invoice','payment') AND coalesce(m.duplicate_group_id, '') <> '' "
                "   AND m.duplicate_group_id IN ("
                "     SELECT f.duplicate_group_id FROM all_records f "
                "      WHERE f.record_type IN ('invoice','payment') "
                "        AND f.duplicate_suspect IS TRUE AND f.duplicate_ignored IS NOT TRUE "
                "        AND (f.record_type = 'payment' OR f.skip_qb_import IS NOT TRUE) "
                "        AND coalesce(f.duplicate_group_id, '') <> '')")
            group_rows = cur.fetchall()
            # The invoice each payment settles (payments.invoice_number, when the remittance states
            # it). The payment duplicate check uses it — one shared reference can pay several
            # invoices — so show it beside the reference number.
            pay_ids = [r["record_id"] for r in rows + ignored_rows + no_ref_rows + group_rows
                       if r["record_type"] == "payment"]
            applied = {}
            if pay_ids:
                cur.execute("SELECT id, invoice_number FROM payments WHERE id = ANY(%s)", (pay_ids,))
                applied = {r["id"]: r["invoice_number"] for r in cur.fetchall()}
            for r in rows + ignored_rows + no_ref_rows + group_rows:
                r["applied_invoice"] = applied.get(r["record_id"]) if r["record_type"] == "payment" else None
            # Why each copy was flagged, and whether it's already entered in QuickBooks — shown on the page
            # so a flag about a bill already in QuickBooks says so (2026-10-14). The all_records view
            # carries neither for payments (entered_in_qb is NULL there), so both come from the tables.
            extra = {}
            for table, rtype in (("invoices", "invoice"), ("payments", "payment")):
                ids = [r["record_id"] for r in rows + ignored_rows + no_ref_rows + group_rows
                       if r["record_type"] == rtype]
                if ids:
                    cur.execute(f"SELECT id, duplicate_reason, entered_in_qb FROM {table} WHERE id = ANY(%s)", (ids,))
                    extra.update({(rtype, x["id"]): x for x in cur.fetchall()})
            for r in rows + ignored_rows + no_ref_rows + group_rows:
                x = extra.get((r["record_type"], r["record_id"])) or {}
                r["duplicate_reason"] = x.get("duplicate_reason")
                r["entered_in_qb"] = bool(x.get("entered_in_qb"))
    finally:
        conn.close()

    # Group by the duplicate checks' own vendor key (standard name, legal suffixes stripped). Plain
    # .lower() once split 'Authentic Health' / 'Authentic Health Inc' into SINGLETON sets (2026-10-09),
    # and the printed spelling split 'MaxiHealth' / 'Maxi Health' (2026-10-12).
    vendor_key = _duplicate_vendor_keyer()

    def _key(r):
        return (r["record_type"], vendor_key(r.get("vendor_matched")), r.get("number"))

    def _qualifies(r):          # a live suspect that would actually matter for QB entry
        return (r.get("duplicate_suspect") and not r.get("duplicate_ignored")
                and (r["record_type"] == "payment" or not r.get("skip_qb_import")))

    groups = {}
    for r in rows:
        groups.setdefault(_key(r), []).append(r)
    # The SQL matched only type + number; keep the groups whose own vendor has a qualifying suspect.
    groups = {k: recs for k, recs in groups.items() if any(_qualifies(r) for r in recs)}
    # Append ignored siblings ONLY to clusters that already qualify (a live suspect exists) —
    # matched on the full type+vendor+number key, so a shared number under a different vendor
    # is ignored. Never spawns a new (all-ignored) cluster.
    for r in ignored_rows:
        k = _key(r)
        if k in groups:
            groups[k].append(r)
    # One set per duplicate GROUP as well (2026-10-14): number-based sets whose copies share a group
    # id are merged (same record type), and a group's other copies join the set, whatever their
    # number. The misread-number check groups '93G4240' with its misread '9304240'; by number alone the
    # misread copy sat in a one-record set that's never shown, and the real pair looked settled.
    parent = {k: k for k in groups}

    def _root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    first_set = {}                       # (record type, group id) -> the first set holding it
    for k, recs in groups.items():
        for r in recs:
            g = (r.get("duplicate_group_id") or "").strip()
            if g:
                other = first_set.setdefault((k[0], g), k)
                if other != k:
                    parent[_root(k)] = _root(other)
    merged = {}
    for k, recs in groups.items():
        merged.setdefault(_root(k), []).extend(recs)
    groups = merged
    shown = {(r["record_type"], r["record_id"]) for recs in groups.values() for r in recs}
    owner = {}
    for k, recs in groups.items():
        for r in recs:
            if (r.get("duplicate_group_id") or "").strip():
                owner.setdefault((r["record_type"], r["duplicate_group_id"]), k)
    for r in group_rows:
        k = owner.get((r["record_type"], r["duplicate_group_id"]))
        if k and (r["record_type"], r["record_id"]) not in shown:
            groups[k].append(r)
            shown.add((r["record_type"], r["record_id"]))
    # No-reference payment sets, keyed by their shared group id ("~group" can't be a vendor key, which
    # is lowercase vendor text). A record already shown in a number-based set isn't repeated.
    for r in no_ref_rows:
        if (r["record_type"], r["record_id"]) not in shown:
            groups.setdefault(("payment", "~group", r["duplicate_group_id"]), []).append(r)
    out = []
    for key, recs in groups.items():
        # Never invoices and payments in one set (Joel, 2026-10-14). Every set is built per record type, so this is
        # only a guard against a future change: anything of another type is left out.
        recs = [r for r in recs if r["record_type"] == key[0]]
        # A duplicate SET needs ≥2 copies to compare. A lone record (a suspect whose sibling is gone,
        # or a stale flag) self-matches the SQL EXISTS but has nothing to compare against — don't
        # surface it as a one-record "set".
        if len(recs) < 2:
            continue
        # Order: live original(s) first, then live suspects, then ignored context copies last.
        recs.sort(key=lambda x: (1 if x.get("duplicate_ignored") else 0,
                                 1 if x.get("duplicate_suspect") else 0,
                                 str(x.get("date_added") or "")))
        original = (next((x for x in recs
                          if not x.get("duplicate_suspect") and not x.get("duplicate_ignored")), None)
                    or next((x for x in recs if not x.get("duplicate_ignored")), None) or recs[0])
        latest_flag = max((str(x.get("date_added") or "") for x in recs
                           if x.get("duplicate_suspect") and not x.get("duplicate_ignored")), default="")
        _mark_set_differences(recs)
        out.append({
            "key": "|".join(str(k) for k in key),
            "record_type": key[0],
            "records": recs,
            "original_email": original.get("email_filename") if original else None,
            "_sort": latest_flag,
        })
    out.sort(key=lambda g: g["_sort"], reverse=True)
    return out


def _mark_set_differences(recs):
    """Where a duplicate set's copies disagree, so the page can color it (2026-10-14, Joel: the colors
    for differences in bill numbers were gone). Copies with different numbers share a set since that
    day (a misread number; a sales order beside its invoice), and nothing showed which characters
    differ. Sets on each record:
      number_vs   — the number to highlight this copy's number against, character by character
                    (the diff_against filter, as on QB match review), or None when it agrees;
      store_diff / date_diff — this copy's store / date is the odd one out (red, like the amounts).
    A value counts as odd one out when the set disagrees and no other copy shares it."""
    def norm(v):
        return " ".join(str(v or "").lower().split())

    def odd_ones(values):
        if len(set(values)) <= 1:
            return [False] * len(values)
        return [values.count(v) == 1 for v in values]

    for r, odd in zip(recs, odd_ones([norm(r.get("store_matched")) for r in recs])):
        r["store_diff"] = odd
    for r, odd in zip(recs, odd_ones([norm(r.get("record_date")) for r in recs])):
        r["date_diff"] = odd
    numbers = [norm(r.get("number")) for r in recs]
    if len(set(numbers)) <= 1:
        for r in recs:
            r["number_vs"] = None
        return
    # The reference: the number most copies share (ties: the first copy's, the likely original).
    ref = max(numbers, key=lambda n: (numbers.count(n), -numbers.index(n)))
    ref_shown = next(r.get("number") for r, n in zip(recs, numbers) if n == ref)
    first_other = next(r.get("number") for r, n in zip(recs, numbers) if n != ref)
    for r, n in zip(recs, numbers):
        r["number_vs"] = (first_other if n == ref else ref_shown) or ""


def orphaned_history_records():
    """Read-only drill-down for the reconcile check: the invoice_history / payment_history rows that
    never became a live invoices / payments row (matched by normalized vendor + number). These are
    'extracted into the ledger but never recorded' — likely a recovery that silently failed. Returns
    a list of dicts {kind, vendor, number, amount, date, source_file}."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT 'invoice' AS kind, h.vendor_matched AS vendor, h.invoice_number AS number, "
                "       h.invoice_amount AS amount, h.invoice_date AS date, h.source_file, "
                "       e.status AS email_status "
                "  FROM invoice_history h LEFT JOIN emails e ON e.filename = h.source_file "
                " WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS ("
                "   SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number) "
                "     AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched)) "
                "UNION ALL "
                "SELECT 'payment', h.vendor_matched, h.reference_number, h.amount, h.payment_date, "
                "       h.source_file, e.status "
                "  FROM payment_history h LEFT JOIN emails e ON e.filename = h.source_file "
                " WHERE coalesce(h.reference_number,'') <> '' AND NOT EXISTS ("
                "   SELECT 1 FROM payments p WHERE lower(p.reference_number)=lower(h.reference_number) "
                "     AND norm_vendor_dedupe(p.vendor_matched)=norm_vendor_dedupe(h.vendor_matched)) "
                "ORDER BY email_status, date NULLS LAST")
            return cur.fetchall()
    finally:
        conn.close()


# duplicate_ignored_by values written by scripts rather than a person clicking Ignore (NULL is the
# pipeline itself). Anything else -- 'joel', 'js', 'Amy' -- is a person.
_SCRIPT_IGNORE_MARKERS = ("claude", "cleanup", "backfill", "recheck", "auto", "diagnosis")


def _ignored_by_person(ignored_by):
    who = (ignored_by or "").strip().lower()
    return bool(who) and not any(m in who for m in _SCRIPT_IGNORE_MARKERS)


def no_live_copy_groups():
    """Invoice and payment groups -- same vendor (standard-name duplicate key, as /duplicates uses) and
    same normalized number -- where EVERY copy is duplicate_ignored, none is entered in QuickBooks and
    none is skip_qb_import: a bill that will never show for entry. Happens when each copy is ignored as a
    duplicate of another (Lake Distribution #1051 x5), or after a store-conflict supersede whose survivor
    was later ignored too (Lantev #9GH5739, Garden of Life #52099183) -- found 2026-10-12.
    Each group: {record_type, vendor, number, vendor_key, number_key, copies, by_person, confirmed}.
    by_person = some copy was ignored by a person (probably deliberate); confirmed = a person checked
    it on /no-live-copy (no_live_copy_confirmed). Newest groups first. Read-only.
    Numbers are compared the way the duplicate checks compare them (2026-10-26, Joel): without punctuation, and
    a group isn't listed when the bill has a copy that's live, entered or kept out of QuickBooks under a number a
    misread apart or the same once a tag is dropped (_num_core), for the same vendor and amount. Before, the page
    listed Reisman's #R205029-IN and #R204977-IN, whose kept copies are #R205029 and #R204977."""
    import classify_emails as ce
    vkey = _duplicate_vendor_keyer()
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT 'invoice' AS record_type, id, vendor_matched, invoice_number AS number, amount, "
                "       store_matched, invoice_date AS doc_date, email_filename, duplicate_ignored, "
                "       entered_in_qb, skip_qb_import, duplicate_ignored_by, duplicate_reason "
                "  FROM invoices WHERE coalesce(btrim(invoice_number), '') <> '' "
                "UNION ALL "
                "SELECT 'payment', id, vendor_matched, reference_number, amount, store_matched, "
                "       payment_date, email_filename, duplicate_ignored, entered_in_qb, skip_qb_import, "
                "       duplicate_ignored_by, duplicate_reason "
                "  FROM payments WHERE coalesce(btrim(reference_number), '') <> ''")
            rows = cur.fetchall()
            confirmed = {}
            cur.execute("SELECT to_regclass('no_live_copy_confirmed') IS NOT NULL AS ok")
            if cur.fetchone()["ok"]:
                cur.execute("SELECT * FROM no_live_copy_confirmed")
                confirmed = {(r["record_type"], r["vendor_key"], r["number_key"]): r for r in cur.fetchall()}
    finally:
        conn.close()
    groups = {}
    for r in rows:
        k = (r["record_type"], vkey(r["vendor_matched"]), ce._num_key(r["number"]))
        if k[1] and k[2]:
            groups.setdefault(k, []).append(r)
    settled = {}                         # (type, vendor key) -> copies that are live, entered or kept out of QB
    for k, copies in groups.items():
        settled.setdefault(k[:2], []).extend(
            c for c in copies if not c["duplicate_ignored"] or c["entered_in_qb"] or c["skip_qb_import"])

    def has_twin(k, copies):
        """The bill has a settled copy under a near number: a misread apart, or the same once a tag is dropped."""
        return any(ce._amounts_equal(c["amount"], o["amount"])
                   and (ce._one_edit_apart(k[2], ce._num_key(o["number"]))
                        or ce._num_core(c["number"]) == ce._num_core(o["number"]))
                   for c in copies for o in settled.get(k[:2], []))

    out = []
    for k, copies in groups.items():
        if not all(c["duplicate_ignored"] for c in copies) \
                or any(c["entered_in_qb"] or c["skip_qb_import"] for c in copies) or has_twin(k, copies):
            continue
        copies.sort(key=lambda c: c["id"])
        # Confirmations made before numbers were compared without punctuation are keyed by the number as written.
        conf = confirmed.get(k) or next((confirmed[(k[0], k[1], ce._norm(c["number"]))] for c in copies
                                         if (k[0], k[1], ce._norm(c["number"])) in confirmed), None)
        out.append({"record_type": k[0], "vendor_key": k[1], "number_key": conf["number_key"] if conf else k[2],
                    "vendor": copies[-1]["vendor_matched"], "number": copies[-1]["number"],
                    "copies": copies,
                    "by_person": any(_ignored_by_person(c["duplicate_ignored_by"]) for c in copies),
                    "confirmed": conf})
    out.sort(key=lambda g: -g["copies"][-1]["id"])
    return out


def no_live_copy_count():
    """Dashboard number: groups with no live copy that no person ignored or confirmed."""
    return sum(1 for g in no_live_copy_groups() if not g["by_person"] and not g["confirmed"])


def confirm_no_live_copy(record_type, vendor_key, number_key, username, confirm=True):
    """A person checked a no-live-copy group: confirm it stays ignored (stops counting), or undo that.
    Logged to edit_log (record_id 0; the group is named in the values)."""
    if record_type not in ("invoice", "payment") or not vendor_key or not number_key:
        return
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            if confirm:
                cur.execute("INSERT INTO no_live_copy_confirmed (record_type, vendor_key, number_key, "
                            "confirmed_by) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (record_type, vendor_key, number_key, username))
            else:
                cur.execute("DELETE FROM no_live_copy_confirmed WHERE record_type=%s AND vendor_key=%s "
                            "AND number_key=%s", (record_type, vendor_key, number_key))
            if cur.rowcount:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('no_live_copy_confirmed', 0, %s, %s, %s, %s)",
                    (f"{record_type} {vendor_key} #{number_key}",
                     "open" if confirm else "confirmed", "confirmed" if confirm else "open", username))
        conn.commit()
    finally:
        conn.close()


def qb_batch_auto_matches(batch_id, search=""):
    """The records one QuickBooks upload marked entered by itself, because a QuickBooks bill matched them
    exactly (2026-10-14, Joel looking for Bened Life #125057: auto-matches were on no page; Pending unmatched lists
    only QB rows that matched nothing, and QB match review only the doubtful ones). Found by the mark the upload
    leaves: entered_qb_by '<uploader> via QB reconcile import', stamped within the hour after the upload. `search`
    narrows by vendor or number. Returns (batch, [{record_type, record_id, store_matched, vendor_matched, number,
    amount, record_date, entered_in_qb}]), store then vendor order; (None, []) for an unknown batch."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM qb_import_batches WHERE id = %s", (batch_id,))
            batch = cur.fetchone()
            if not batch:
                return None, []
            actor = f"{batch['uploaded_by']} via QB reconcile import"
            arms = []
            for rtype, table, num, dcol in (("invoice", "invoices", "invoice_number", "invoice_date"),
                                            ("credit_memo", "credit_memos", "credit_memo_number", "credit_memo_date"),
                                            ("payment", "payments", "reference_number", "payment_date")):
                arms.append(f"SELECT '{rtype}' AS record_type, id AS record_id, store_matched, vendor_matched, "
                            f"{num} AS number, amount, {dcol} AS record_date, entered_in_qb FROM {table} "
                            f"WHERE entered_qb_by = %(actor)s AND entered_qb_at >= %(at)s - interval '1 minute' "
                            f"AND entered_qb_at < %(at)s + interval '1 hour'")
            where = ""
            if (search or "").strip():
                where = " WHERE vendor_matched ILIKE %(q)s OR number ILIKE %(q)s"
            cur.execute("SELECT * FROM (" + " UNION ALL ".join(arms) + ") m" + where +
                        " ORDER BY store_matched, lower(vendor_matched), number",
                        {"actor": actor, "at": batch["uploaded_at"], "q": f"%{(search or '').strip()}%"})
            return batch, cur.fetchall()
    finally:
        conn.close()


def qb_list_batches(limit=50):
    """Past QB-reconcile import batches, newest first (for the history table)."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM qb_import_batches ORDER BY uploaded_at DESC LIMIT %s", (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def qb_batch(batch_id):
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM qb_import_batches WHERE id = %s", (batch_id,))
            return cur.fetchone()
    finally:
        conn.close()


def qb_list_candidates(batch_id=None, status="pending"):
    """Tier-2 doubtful candidates with the QB-side fields AND the linked app record's own fields
    (vendor/number/amount/date/store) joined in, for side-by-side display."""
    clauses, params = ["c.status = %s"], [status]
    if batch_id:
        clauses.append("c.batch_id = %s"); params.append(batch_id)
    where = "WHERE " + " AND ".join(clauses)
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT c.*,
                       COALESCE(i.vendor_matched, cm.vendor_matched)        AS rec_vendor,
                       COALESCE(i.standard_vendor, cm.standard_vendor)       AS rec_std_vendor,
                       COALESCE(i.invoice_number, cm.credit_memo_number)     AS rec_number,
                       COALESCE(i.amount, cm.amount)                         AS rec_amount,
                       COALESCE(i.invoice_date::text, cm.credit_memo_date::text) AS rec_date,
                       COALESCE(i.store_matched, cm.store_matched)           AS rec_store,
                       COALESCE(i.entered_in_qb, cm.entered_in_qb)           AS rec_entered
                FROM qb_match_candidates c
                LEFT JOIN invoices i      ON c.record_type = 'invoice'     AND i.id  = c.record_id
                LEFT JOIN credit_memos cm ON c.record_type = 'credit_memo' AND cm.id = c.record_id
                {where}
                ORDER BY c.created_at DESC, c.id DESC""", params)
            return cur.fetchall()
    finally:
        conn.close()


def qb_pending_count():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM qb_match_candidates WHERE status = 'pending'")
            return cur.fetchone()[0]
    finally:
        conn.close()


def qb_confirm_candidate(candidate_id, username, right="bill"):
    """Confirm a doubtful match: mark the linked record entered_in_qb=TRUE (audited) and the
    candidate 'confirmed'. Atomic. Returns True if a pending candidate was acted on."""
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM qb_match_candidates WHERE id = %s AND status = 'pending'",
                        (candidate_id,))
            c = cur.fetchone()
            if not c:
                conn.rollback(); return False
            table = "invoices" if c["record_type"] == "invoice" else "credit_memos"
            actor = f"{username} via QB reconcile confirm"
            cur.execute(f"UPDATE {table} SET entered_in_qb = TRUE, entered_qb_by = %s, "
                        f"entered_qb_at = now() WHERE id = %s", (actor, c["record_id"]))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s,%s,'entered_in_qb','false',%s,%s)",
                (table, c["record_id"],
                 f"true (confirmed QB match — {c['qb_type']} Num={c['qb_num']} Amount={c['qb_amount']})",
                 actor))
            cur.execute("UPDATE qb_match_candidates SET status = 'confirmed', decided_by = %s, "
                        "decided_at = now() WHERE id = %s", (username, candidate_id))
            # Confirmed, but QuickBooks has a different amount than the bill (2026-10-22, Joel): QuickBooks needs
            # updating -> a QB correction (QuickBooks' amount -> the bill's), closed by Done in QB or a newer export.
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (c["record_id"],))
            rec = cur.fetchone() or {}
            if right == "qb" and c["record_type"] in ("invoice", "credit_memo") and c.get("qb_amount") is not None \
                    and rec.get("amount") is not None and not _same_value(rec["amount"], c["qb_amount"]):
                # QuickBooks has the right amount: the app's record takes it (logged), nothing to fix in QB.
                cur.execute(f"UPDATE {table} SET amount = %s, edited_by = %s, edited_at = now() WHERE id = %s",
                            (c["qb_amount"], actor, c["record_id"]))
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                            "VALUES (%s,%s,'amount',%s,%s,%s)",
                            (table, c["record_id"], str(rec["amount"]), str(c["qb_amount"]),
                             f"{actor} (QuickBooks has the right amount)"))
            elif (c["record_type"] in ("invoice", "credit_memo") and rec.get("amount") is not None
                    and c.get("qb_amount") is not None and not _same_value(rec["amount"], c["qb_amount"])):
                num_col = EDITABLE.get(c["record_type"], {}).get("number")
                cur.execute("SELECT 1 FROM qb_corrections WHERE record_type=%s AND record_id=%s AND field='amount' "
                            "AND done_at IS NULL", (c["record_type"], c["record_id"]))
                if not cur.fetchone():
                    cur.execute("INSERT INTO qb_corrections (record_type, record_id, field, old_value, new_value, store, "
                                "vendor, number, changed_by, note) VALUES (%s,%s,'amount',%s,%s,%s,%s,%s,%s,%s)",
                                (c["record_type"], c["record_id"], str(c["qb_amount"]), str(rec["amount"]),
                                 rec.get("store_matched"), rec.get("vendor_matched"),
                                 rec.get(num_col) if num_col else None, f"{username} via QB match review",
                                 "QuickBooks has a different amount than the bill (found on QB match review)"))
        conn.commit()
        return True
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def qb_reject_candidate(candidate_id, username):
    """Reject a doubtful match: candidate 'rejected' only — the record is left untouched.
    connect() is autocommit so the UPDATE already persists; the explicit commit keeps this correct
    (and symmetric with qb_confirm_candidate) if the connection's autocommit ever changes."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE qb_match_candidates SET status = 'rejected', decided_by = %s, "
                        "decided_at = now() WHERE id = %s AND status = 'pending'",
                        (username, candidate_id))
            ok = cur.rowcount > 0
        conn.commit()
        return ok
    finally:
        conn.close()


def _qb_bill_key(b):
    d = b["qb_date"].isoformat() if b.get("qb_date") else ""
    parts = [str(b.get("qb_store") or ""), d, str(b.get("qb_num") or ""), str(b.get("qb_name") or ""),
             f"{float(b.get('qb_amount') or 0):.2f}"]
    if str(b.get("qb_type") or "").strip().lower() == "credit":
        parts.append("credit")          # bills keep their old keys, so earlier "resolved" marks still apply
    return "|".join(parts)


def _amount_digits(amount):
    """An amount's digits without trailing zeros ($819.00 and $81,900.00 -> '819'): amounts that differ only by
    where the decimal point sits share it."""
    return str(round(abs(float(amount)) * 100)).rstrip("0") or "0"


def _decimal_slip(x, y):
    """10, 100 or 1000 when one amount is the other with the decimal point moved, else None."""
    lo, hi = sorted([abs(float(x)), abs(float(y))])
    return next((10 ** k for k in (1, 2, 3) if lo and abs(lo * 10 ** k - hi) < 0.006), None)


def _digit_slip(x, y):
    """True when two amounts differ in one digit, or by two neighbouring digits swapped ($1,071.27 / $1,071.57)."""
    a, b = f"{abs(float(x)):.2f}", f"{abs(float(y)):.2f}"
    if len(a) != len(b):
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                              and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])


def qb_double_entries(show="open"):
    """Bills that may be in QuickBooks twice, from the saved QuickBooks register (every bill of every export
    upload -- qb_register). A pair = two bills, same store, same amount, invoice dates within 10 days, the
    same vendor (standard name, or one name inside the other), and the same invoice number -- or numbers
    one character apart (a misread: added/dropped digit, look-alike swapped), or no number on one of them
    and the same date. Since 2026-10-26 also the same bill with two different amounts (kind amount_differs),
    and a pair the app marked entered twice shows once, on its QuickBooks pair. show: 'open' (default),
    'dismissed', 'all'. Returns [{key, a, b, why, strength, dismissed}] strongest first, newest first."""
    import classify_emails as ce
    if not ce.VENDOR_STD:
        ce.VENDOR_STD = ce.load_vendor_standardization()
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Bills AND credits (2026-10-23); a bill is only ever paired with a bill, a credit with a credit.
            cur.execute("SELECT qb_store, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, qb_amount FROM qb_register "
                        "WHERE (qb_type ILIKE 'bill' OR qb_type ILIKE 'credit') "
                        "  AND qb_amount IS NOT NULL AND abs(qb_amount) >= 0.005")
            rows_all = cur.fetchall()
            bills = [b for b in rows_all if str(b["qb_type"]).strip().lower() == "bill"]
            cur.execute("SELECT * FROM qb_double_dismissed")     # with outcome / app_changes once they exist
            dismissed = {r["pair_key"]: r for r in cur.fetchall()}
    finally:
        conn.close()
    groups = {}
    for b in rows_all:
        groups.setdefault((str(b["qb_type"]).strip().lower(), round(abs(float(b["qb_amount"])), 2)), []).append(b)
    out, seen = [], set()
    for grp in groups.values():
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                a, b = grp[i], grp[j]
                da, db = a["qb_date"], b["qb_date"]
                if da and db and abs((da - db).days) > 10:
                    continue
                sa, sb = ce._norm(a["qb_std_vendor"]), ce._norm(b["qb_std_vendor"])
                if not ((sa and sa == sb) or ce._vendors_related(a["qb_name"], b["qb_name"])):
                    continue
                na, nb = ce._num_key(a["qb_num"]), ce._num_key(b["qb_num"])
                if ce._norm(a["qb_store"]) != ce._norm(b["qb_store"]):
                    # Two stores' QuickBooks files (Joel 2026-10-20): only the SAME bill -- vendor, invoice
                    # number, date and amount all equal -- since different stores do get look-alike bills.
                    if not (na and na == nb and da and da == db):
                        continue
                    why, strength, kind = (f"same bill in two stores' QuickBooks ({a['qb_store']} and {b['qb_store']}) "
                                           f"-- same vendor, number, date and amount"), 3, "two_stores"
                elif na and nb:
                    if na == nb:
                        why, strength, kind = "same invoice number", 3, "same_number"
                    elif ce._one_edit_apart(na, nb):
                        why, strength, kind = "invoice numbers one character apart (likely a misread)", 2, "misread"
                    elif da and db and da == db:
                        # 2026-10-21: one bill read under two unrelated numbers (UNFI INV 93055097 vs its
                        # coversheet 33625479, both keyed in). Same store/vendor/amount AND same date: check it.
                        why, strength, kind = "same vendor, amount and date, but different numbers", 1, "same_day"
                    else:
                        continue
                elif da and db and da == db:
                    why, strength, kind = "same date, no invoice number on " + ("both" if not na and not nb else "one"), 1, "no_number"
                else:
                    continue
                key = " || ".join(sorted([_qb_bill_key(a), _qb_bill_key(b)]))
                if key in seen:
                    continue
                seen.add(key)
                first, second = sorted([a, b], key=lambda x: (x["qb_date"] or datetime.date.min, str(x["qb_num"])))
                out.append({"key": key, "a": first, "b": second, "why": why, "strength": strength,
                            "kind": kind, "dismissed": dismissed.get(key),
                            "credit": str(a.get("qb_type") or "").strip().lower() == "credit"})
    # One document keyed in once as a BILL and once as a CREDIT (2026-10-23, Joel). Stricter than the rules
    # above: same store, same vendor, same amount (sign ignored), the SAME bill number (present on both) and
    # the SAME date.
    by_amt = {}
    for b in rows_all:
        by_amt.setdefault(round(abs(float(b["qb_amount"])), 2), []).append(b)
    for grp in by_amt.values():
        bl = [x for x in grp if str(x["qb_type"]).strip().lower() == "bill"]
        cr = [x for x in grp if str(x["qb_type"]).strip().lower() == "credit"]
        for a in bl:
            for b in cr:
                na, nb = ce._num_key(a["qb_num"]), ce._num_key(b["qb_num"])
                if not (na and na == nb and a["qb_date"] and a["qb_date"] == b["qb_date"]
                        and ce._norm(a["qb_store"]) == ce._norm(b["qb_store"])):
                    continue
                sa, sb = ce._norm(a["qb_std_vendor"]), ce._norm(b["qb_std_vendor"])
                if not ((sa and sa == sb) or ce._vendors_related(a["qb_name"], b["qb_name"])):
                    continue
                key = " || ".join(sorted([_qb_bill_key(a), _qb_bill_key(b)]))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"key": key, "a": a, "b": b, "strength": 3, "kind": "bill_credit",
                            "dismissed": dismissed.get(key),
                            "why": "same document entered once as a bill and once as a credit", "credit": False})
    # The same bill with two different amounts (2026-10-26, Joel: "it's all the same info besides the amount.
    # That's a red flag in itself"): Vita Vocal H002875 $819.00 on 8/2 and 002875 $81,900.00 on 9/2, both in
    # Refuah's QuickBooks. A number a misread apart isn't enough on its own here -- a vendor's next bill is often
    # one look-alike digit away (Jams RX REFUH-50536 / 50538) -- so besides the same store, type and vendor a
    # pair needs one of: the same bill number and dates within 10 days (a revised bill entered twice, or an
    # amount mistyped); a decimal slip, one amount 10, 100 or 1000 times the other, with the number equal or a
    # misread apart and dates within 45 days; or amounts one digit apart, the number a misread apart, same date.
    # Candidates come from three small buckets (same number / same digits / same day), never the whole store.
    buckets, num_of = {}, {}
    for b in rows_all:
        nk = num_of[id(b)] = ce._num_key(b["qb_num"])
        if not nk:
            continue
        t, st = str(b["qb_type"]).strip().lower(), ce._norm(b["qb_store"])
        buckets.setdefault(("num", t, st, nk), []).append(b)
        buckets.setdefault(("digits", t, st, _amount_digits(b["qb_amount"])), []).append(b)
        if b["qb_date"]:
            buckets.setdefault(("day", t, st, b["qb_date"]), []).append(b)
    for grp in buckets.values():
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                a, b = grp[i], grp[j]
                na, nb = num_of[id(a)], num_of[id(b)]
                if not (na == nb or ce._one_edit_apart(na, nb)):
                    continue
                x, y = abs(float(a["qb_amount"])), abs(float(b["qb_amount"]))
                if abs(x - y) < 0.005 or not (a["qb_date"] and b["qb_date"]):
                    continue                     # same amount: the checks above
                sa, sb = ce._norm(a["qb_std_vendor"]), ce._norm(b["qb_std_vendor"])
                if not ((sa and sa == sb) or ce._vendors_related(a["qb_name"], b["qb_name"])):
                    continue
                days, factor = abs((a["qb_date"] - b["qb_date"]).days), _decimal_slip(x, y)
                if factor and days <= 45:
                    note = f"One amount is {factor:,} times the other: a misplaced decimal point?"
                elif na == nb and days <= 10:
                    note = "Same bill #, different amounts: a revised bill entered twice, or an amount mistyped?"
                elif days == 0 and _digit_slip(x, y):
                    note = "Amounts one digit apart on the same day: a typo?"
                else:
                    continue
                key = " || ".join(sorted([_qb_bill_key(a), _qb_bill_key(b)]))
                if key in seen:
                    continue
                seen.add(key)
                first, second = sorted([a, b], key=lambda r: (r["qb_date"], str(r["qb_num"])))
                out.append({"key": key, "a": first, "b": second, "why": "same bill, different amounts",
                            "strength": 2, "kind": "amount_differs", "note": note, "dismissed": dismissed.get(key),
                            "credit": str(a.get("qb_type") or "").strip().lower() == "credit"})
    # The app's own history (2026-10-21, Joel): copies of one bill (a duplicate set) where TWO OR MORE were
    # marked entered in QuickBooks -- even if one was set aside later -- were probably keyed in twice. Listed
    # until a QuickBooks export uploaded AFTER the later entry, covering the bill's date, shows the bill at
    # most once; then it's settled and drops off by itself. When the export itself shows the two bills (a pair
    # above), they're shown once, on that pair, with the app's records (2026-10-26: Maxi Health, Zahler and
    # Lantev each had two boxes, one to resolve on each).
    qb_pairs = list(out)
    for p in _app_entered_twice(bills, ce):
        qb_pair = next((q for q in qb_pairs if _pair_matches_records(q, p, ce)), None)
        if qb_pair is None:
            p["dismissed"] = dismissed.get(p["key"])
            out.append(p)
            continue
        qa, qb = qb_pair["a"], qb_pair["b"]
        if "record_id" in qa:
            continue                     # already carries another app pair's records
        ra, rb = (p["a"], p["b"]) if _qb_same_bill(qa, p["a"], ce) and _qb_same_bill(qb, p["b"], ce) else (p["b"], p["a"])
        extra = ("record_id", "entered_by", "entered_at", "set_aside")
        qb_pair["a"] = dict(qa, **{k: ra[k] for k in extra})
        qb_pair["b"] = dict(qb, **{k: rb[k] for k in extra})
        qb_pair["note"] = " ".join(n for n in (qb_pair.get("note"), "Both copies are marked entered in the app.") if n)
    out = [p for p in out if show == "all" or (show == "open") == (not p["dismissed"])]
    out.sort(key=lambda p: (-p["strength"], -(p["a"]["qb_date"] or datetime.date.min).toordinal()))
    return out


def _pair_matches_records(pair, app_pair, ce):
    """A QuickBooks pair whose two bills are the two app records of an 'entered twice' pair, either way round."""
    if pair.get("credit") or pair["kind"] == "bill_credit":
        return False                      # the app's pairs are invoices
    qa, qb, ra, rb = pair["a"], pair["b"], app_pair["a"], app_pair["b"]
    return ((_qb_same_bill(qa, ra, ce) and _qb_same_bill(qb, rb, ce))
            or (_qb_same_bill(qa, rb, ce) and _qb_same_bill(qb, ra, ce)))


def _qb_same_bill(bill, rec, ce):
    """A QuickBooks register bill that is this app record's bill: same store, vendor related, amount within
    $1.00 (a bill keyed as $718.00 for $718.08), date within 10 days, and the number equal, one character off,
    equal after dropping a tag (R204977-IN), or missing on either side."""
    if ce._norm(bill["qb_store"]) != ce._norm(rec["qb_store"]):
        return False
    try:
        if abs(float(bill["qb_amount"]) - float(rec["qb_amount"])) > 1.0:
            return False
    except (TypeError, ValueError):
        return False
    if bill["qb_date"] and rec["qb_date"] and abs((bill["qb_date"] - rec["qb_date"]).days) > 10:
        return False
    sb, sr = ce._norm(bill.get("qb_std_vendor")), ce._norm(rec.get("_std"))
    if not ((sb and sb == sr) or ce._vendors_related(bill["qb_name"], rec["qb_name"])):
        return False
    nb, nr = ce._num_key(bill["qb_num"]), ce._num_key(rec["qb_num"])
    if not nb or not nr:
        return True
    return nb == nr or ce._one_edit_apart(nb, nr) or ce._num_core(bill["qb_num"]) == ce._num_core(rec["qb_num"])


def _app_entered_twice(bills, ce):
    """Pairs for the 'twice' page from the app's own marks: in one duplicate group, 2+ invoices marked
    entered in QB (set-aside copies included). Skipped once a later QuickBooks export of that store covers
    the bill's date and holds it at most once."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, store_matched, vendor_matched, invoice_number, invoice_date, amount, "
                        "       entered_qb_by, entered_qb_at, duplicate_ignored, duplicate_group_id "
                        "  FROM invoices WHERE entered_in_qb IS TRUE AND coalesce(duplicate_group_id, '') <> '' "
                        "   AND duplicate_group_id IN (SELECT duplicate_group_id FROM invoices WHERE entered_in_qb IS TRUE "
                        "                               AND coalesce(duplicate_group_id, '') <> '' "
                        "                             GROUP BY duplicate_group_id HAVING count(*) >= 2) "
                        " ORDER BY duplicate_group_id, entered_qb_at NULLS FIRST, id")
            rows = cur.fetchall()
            cur.execute("SELECT qb_store, date_from, date_to, uploaded_at FROM qb_register_coverage")
            coverage = cur.fetchall()
    finally:
        conn.close()
    groups = {}
    for r in rows:
        groups.setdefault(r["duplicate_group_id"], []).append(r)
    # Also copies that were never put in one duplicate set (2026-10-21: UNFI Maple $206.45, one bill read
    # as 93055097 and 33625479, both marked entered, no set): two entered invoices with the same store,
    # standard vendor, amount and date. Keyed by their ids, so "Not a duplicate" sticks.
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, store_matched, vendor_matched, invoice_number, invoice_date, amount, "
                        "       entered_qb_by, entered_qb_at, duplicate_ignored, duplicate_group_id "
                        "  FROM invoices WHERE entered_in_qb IS TRUE AND amount IS NOT NULL AND abs(amount) >= 0.005 "
                        "   AND coalesce(invoice_date, '') <> '' AND coalesce(store_matched, '') <> ''")
            pool = {}
            for r in cur.fetchall():
                k = (ce._norm(r["store_matched"]), ce._dedupe_vendor_key(r["vendor_matched"] or ""),
                     round(float(r["amount"]), 2), str(r["invoice_date"])[:10])
                if k[1]:
                    pool.setdefault(k, []).append(r)
    finally:
        conn.close()
    grouped_ids = {r["id"] for r in rows}
    for k, members in pool.items():
        loose = [m for m in members if m["id"] not in grouped_ids]
        if len(members) >= 2 and loose:
            groups["ids:" + ",".join(str(m["id"]) for m in members)] = members
    out = []
    for gid, members in groups.items():
        recs = []
        for m in members:
            try:
                d = datetime.datetime.strptime(str(m["invoice_date"] or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                d = None
            recs.append({"qb_store": m["store_matched"], "qb_name": m["vendor_matched"], "qb_num": m["invoice_number"],
                         "qb_date": d, "qb_amount": m["amount"], "record_id": m["id"],
                         "entered_by": m["entered_qb_by"], "entered_at": m["entered_qb_at"],
                         "set_aside": bool(m["duplicate_ignored"]),
                         "_std": (ce.VENDOR_STD.get("lookup", {}) or {}).get(ce._norm_vendor_std_key(m["vendor_matched"] or ""))
                                 if ce.VENDOR_STD else None})
        # A shared group id alone isn't enough (2026-10-21: a $624.54 Lantev bill and a $1,722.96 Ysf Trading
        # bill shared one). The two entered copies must look like ONE bill: same store, amount within $1,
        # dates within 10 days, related vendor.
        pair = None
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                x, y = recs[i], recs[j]
                if ce._norm(x["qb_store"]) != ce._norm(y["qb_store"]):
                    continue
                try:
                    if abs(float(x["qb_amount"]) - float(y["qb_amount"])) > 1.0:
                        continue
                except (TypeError, ValueError):
                    continue
                if x["qb_date"] and y["qb_date"] and abs((x["qb_date"] - y["qb_date"]).days) > 10:
                    continue
                if not ((x["_std"] and x["_std"] == y["_std"]) or ce._vendors_related(x["qb_name"], y["qb_name"])):
                    continue
                pair = (x, y)
                break
            if pair:
                break
        if not pair:
            continue
        a, b = pair
        recs = [a, b]
        last_entry = max((r["entered_at"] for r in recs if r["entered_at"]), default=None)
        # Settled by a newer export? It must be uploaded after the later entry and cover the bill's date.
        newer = [c for c in coverage if ce._norm(c["qb_store"]) == ce._norm(a["qb_store"])
                 and (last_entry is None or (c["uploaded_at"] and c["uploaded_at"] > last_entry))
                 and a["qb_date"] and c["date_from"] and c["date_to"] and c["date_from"] <= a["qb_date"] <= c["date_to"]]
        in_qb = [bl for bl in bills if _qb_same_bill(bl, a, ce)]
        if newer and len(in_qb) <= 1:
            continue                     # the latest export shows it once: resolved by itself
        if newer:
            note = f"Newer export shows it {len(in_qb)}×."
        else:
            when = last_entry.strftime("%m/%d").lstrip("0") if last_entry else "the 2nd entry"
            note = f"Clears itself when a {web_db_store_label(a['qb_store'])} export uploaded after {when} shows it once."
        out.append({"key": f"app|{gid}|" + ",".join(str(r["record_id"]) for r in recs), "a": a, "b": b,
                    "why": "marked entered in QuickBooks twice in the app", "strength": 3, "kind": "app_marks",
                    "note": note, "dismissed": None})
    return out


def web_db_store_label(store):
    try:
        return store_label(store) or (store or "this store's")
    except Exception:
        return store or "this store's"


def qb_double_dismiss(pair_key, username, note=None, restore=False):
    conn = connect()
    try:
        with conn.cursor() as cur:
            if restore:
                cur.execute("DELETE FROM qb_double_dismissed WHERE pair_key = %s", (pair_key,))
            else:
                cur.execute("INSERT INTO qb_double_dismissed (pair_key, note, dismissed_by) VALUES (%s,%s,%s) "
                            "ON CONFLICT (pair_key) DO NOTHING", (pair_key, (note or "").strip() or None, username))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('qb_double_dismissed', NULL, %s, NULL, %s, %s)",
                        ("restored" if restore else "not a duplicate", pair_key[:200], username))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# In QB twice?: the app's own copy of each pair's bill, which bill to keep, and the report-back (2026-10-26,
# Joel: "if we have the record and there are discrepancies ... the system should say which one should be the
# correct one", and the person reports what QuickBooks kept "so the app updates accordingly").
# --------------------------------------------------------------------------- #

# Answers that change nothing in the app. The 'kept' ones (kept_a, kept_b, kept_a_fixed, kept_b_fixed) are worded
# per pair (_twice_options).
TWICE_ANSWERS = {"two_real": "Not a duplicate: two real bills",
                 "corrected": "Corrected a bill in QuickBooks",
                 "other": "Other"}
_TWICE_KEPT = ("kept_a", "kept_b", "kept_a_fixed", "kept_b_fixed")
_TWICE_FIELD_WORD = {"store": "store", "number": "bill #", "date": "date", "amount": "amount"}


def _twice_value(x, field, ce):
    """A bill's value for one compared field, normalized. The app's records are given the same shape."""
    if field == "store":
        return ce._norm(x.get("qb_store"))
    if field == "number":
        return ce._num_key(x.get("qb_num"))
    if field == "date":
        return x.get("qb_date")
    if field == "amount":
        try:
            return round(abs(float(x.get("qb_amount"))), 2)
        except (TypeError, ValueError):
            return None
    return "credit" if str(x.get("qb_type") or "").strip().lower() == "credit" else "bill"


def _twice_diff(a, b, ce):
    """The fields a pair's two bills differ in."""
    return [f for f in ("store", "number", "date", "amount", "type")
            if _twice_value(a, f, ce) != _twice_value(b, f, ce)]


def _twice_side(rec, a, b, diff, ce):
    """'a' or 'b': the bill this app record agrees with on the fields where the two bills differ. A tie goes to
    the bill with the record's number (Lantev #93G4241 $718.08 against 9304241 $718.08 and 93G4241 $718.00).
    None when that can't tell them apart."""
    sa = sum(_twice_value(rec, f, ce) == _twice_value(a, f, ce) for f in diff)
    sb = sum(_twice_value(rec, f, ce) == _twice_value(b, f, ce) for f in diff)
    if sa != sb:
        return "a" if sa > sb else "b"
    if "number" in diff:
        core, ca, cb = (ce._num_core(x.get("qb_num") or "") for x in (rec, a, b))
        if core and core == ca != cb:
            return "a"
        if core and core == cb != ca:
            return "b"
    return None


def _twice_same(field, x, y, ce):
    if field == "number":
        return ce._num_key(x) == ce._num_key(y)
    if field == "date":
        return str(x or "")[:10] == str(y or "")[:10]
    if field == "store":
        return ce._norm(x) == ce._norm(y)
    return _same_value(x, y)


def _twice_money(v):
    try:
        return f"${abs(float(v)):,.2f}"
    except (TypeError, ValueError):
        return "no amount"


def _twice_day(d):
    return f"{d.month}/{d.day}/{d:%y}" if d else "no date"


def _twice_show(field, value):
    """A field's value the way the page shows it."""
    if field == "store":
        return store_label(value) or "no store"
    if field == "number":
        return f"#{value}" if str(value or "").strip() else "no #"
    if field == "date":
        return _twice_day(value)
    return _twice_money(value)


def _twice_raw(x, field):
    return {"store": x.get("qb_store"), "number": x.get("qb_num"), "date": x.get("qb_date"),
            "amount": x.get("qb_amount")}.get(field)


def _twice_links(bill, rec, ce):
    """An app record is this bill's: _qb_same_bill, and both have a number (which it already requires to be close)
    or the dates are equal. A missing number alone isn't enough: Monsey View #1600728 of 9/3 would be the $320.00
    no-number bill of 9/10."""
    if not _qb_same_bill(bill, rec, ce):
        return False
    if ce._num_key(bill.get("qb_num")) and ce._num_key(rec.get("qb_num")):
        return True
    return bool(bill.get("qb_date") and bill["qb_date"] == rec.get("qb_date"))


def _twice_label(x, diff, ce):
    """How the page names one bill of a pair, by what tells it from the other: '#797203', 'the one in Maple',
    '#H002875 $819.00', 'the credit', 'the 8/27/26 one'."""
    parts = []
    if "type" in diff:
        parts.append("the credit" if _twice_value(x, "type", ce) == "credit" else "the bill")
    if "store" in diff:
        parts.append(f"the one in {store_label(x.get('qb_store')) or 'no store'}")
    if "number" in diff:
        parts.append(f"#{x['qb_num']}" if str(x.get("qb_num") or "").strip() else "the one with no #")
    if "amount" in diff:
        parts.append(_twice_money(x.get("qb_amount")))
    if "date" in diff and not parts:
        parts.append(f"the {_twice_day(x.get('qb_date'))} one")
    return " ".join(parts) or "one"


def qb_double_attach(pairs):
    """What the In QB twice? page and the report-back need on top of qb_double_entries' pairs (2026-10-26):
      records -- the app's records of the two bills (invoices for bills, credit memos for credits, matched like
                 _qb_same_bill), each with `side` (the bill it agrees with where the two differ, or None) and
                 `live` (not set aside or ignored; an excluded vendor's bill counts);
      diff    -- the fields the two bills differ in;
      suggest -- 'a' / 'b': the bill the app's live records agree with, the one to keep (None: not in the app,
                 the app has both, or it can't tell);
      fix     -- fields where the app sides with the other bill: the kept one needs them changed in QuickBooks;
      advice  -- one line saying so;  options -- the report-back choices (_twice_options).
    Changes the pairs in place and returns them."""
    import classify_emails as ce
    if not pairs:
        return pairs
    if not ce.VENDOR_STD:
        ce.VENDOR_STD = ce.load_vendor_standardization()
    lookup = (ce.VENDOR_STD or {}).get("lookup", {}) or {}
    want = sorted({round(abs(float(x["qb_amount"]))) + k for p in pairs for x in (p["a"], p["b"])
                   if x.get("qb_amount") is not None for k in (-1, 0, 1)}) or [-1]
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT 'invoice' AS record_type, id, store_matched, vendor_matched, standard_vendor, "
                "       invoice_number AS number, invoice_date AS rdate, amount, entered_in_qb, entered_qb_by, "
                "       entered_qb_at, duplicate_ignored, ignored_at, skip_qb_import "
                "  FROM invoices WHERE amount IS NOT NULL AND round(abs(amount)) = ANY(%s) "
                "UNION ALL "
                "SELECT 'credit_memo', id, store_matched, vendor_matched, standard_vendor, credit_memo_number, "
                "       credit_memo_date, amount, entered_in_qb, entered_qb_by, entered_qb_at, FALSE, ignored_at, "
                "       skip_qb_import "
                "  FROM credit_memos WHERE amount IS NOT NULL AND round(abs(amount)) = ANY(%s)", (want, want))
            rows = cur.fetchall()
    finally:
        conn.close()
    by_id, buckets = {}, {}
    for r in rows:
        rec = {"record_type": r["record_type"], "record_id": r["id"],
               "qb_type": "Credit" if r["record_type"] == "credit_memo" else "Bill",
               "qb_store": r["store_matched"], "qb_name": r["vendor_matched"], "qb_num": r["number"],
               "qb_date": ce._loose_date(r["rdate"]), "qb_amount": abs(float(r["amount"])),
               "_std": r["standard_vendor"] or lookup.get(ce._norm_vendor_std_key(r["vendor_matched"] or "")),
               "entered": bool(r["entered_in_qb"]), "entered_by": r["entered_qb_by"], "entered_at": r["entered_qb_at"],
               "set_aside": bool(r["duplicate_ignored"]), "ignored": bool(r["ignored_at"]),
               # an excluded vendor's bill (Parmed) isn't entered from the app, but its store / # still say which is right
               "excluded": bool(r["skip_qb_import"]) and not r["ignored_at"]}
        rec["live"] = not (rec["set_aside"] or rec["ignored"])
        by_id[(rec["record_type"], rec["record_id"])] = rec
        buckets.setdefault((rec["qb_type"].lower(), round(rec["qb_amount"])), []).append(rec)
    for p in pairs:
        a, b = p["a"], p["b"]
        p["diff"] = diff = _twice_diff(a, b, ce)
        found = []
        if p["kind"] == "app_marks":                  # the pair is the app's own two records
            for x, side in ((a, "a"), (b, "b")):
                rec = by_id.get(("invoice", x.get("record_id")))
                if rec:
                    found.append(dict(rec, side=side))
        else:
            seen = set()
            for x in (a, b):
                bill = dict(x, qb_amount=abs(float(x["qb_amount"])))
                n = round(bill["qb_amount"])
                for rec in [r for k in (n - 1, n, n + 1) for r in buckets.get((_twice_value(x, "type", ce), k), ())]:
                    rid = (rec["record_type"], rec["record_id"])
                    if rid not in seen and _twice_links(bill, rec, ce):
                        seen.add(rid)
                        found.append(dict(rec, side=_twice_side(rec, a, b, diff, ce)))
        found.sort(key=lambda r: (not r["live"], not r["entered"], r["record_id"]))
        p["records"] = found
        live = [r for r in found if r["live"]]
        sides = {r["side"] for r in live} - {None}
        s = sides.pop() if diff and len(sides) == 1 else None
        p["suggest"], p["fix"] = s, []
        if s:
            o = "b" if s == "a" else "a"
            ref = next(r for r in live if r["side"] == s)
            p["fix"] = [f for f in diff if f != "type"
                        and _twice_value(ref, f, ce) != _twice_value(p[s], f, ce)
                        and _twice_value(ref, f, ce) == _twice_value(p[o], f, ce)]
        p["advice"] = _twice_advice(p, ce)
        p["options"] = _twice_options(p, ce)
    return pairs


def _twice_fix_texts(p, ce):
    """For a suggestion with a fix: ('$718.08', 'its amount to $718.08', '#93G4241', '#9304241') -- what the app's
    record has where it sides with the other bill, and the two bills named by the other fields."""
    ref = next(r for r in p["records"] if r["live"] and r["side"] == p["suggest"])
    has = ", ".join(_twice_show(f, _twice_raw(ref, f)) for f in p["fix"])
    to = " and ".join(f"its {_TWICE_FIELD_WORD[f]} to {_twice_show(f, _twice_raw(ref, f))}" for f in p["fix"])
    rest = [f for f in p["diff"] if f not in p["fix"]] or p["diff"]
    o = "b" if p["suggest"] == "a" else "a"
    return has, to, _twice_label(p[p["suggest"]], rest, ce), _twice_label(p[o], rest, ce)


def _twice_advice(p, ce):
    """One line: what the app has of this bill, and which bill to keep."""
    recs, diff, s = p["records"], p["diff"], p["suggest"]
    live = [r for r in recs if r["live"]]
    if not recs:
        return "Not in the app."
    if s:
        o = "b" if s == "a" else "a"
        ls, lo = _twice_label(p[s], diff, ce), _twice_label(p[o], diff, ce)
        if p["kind"] == "app_marks":
            return f"The app kept {ls} and set the other copy aside. If QuickBooks has both, delete {lo}."
        if p["fix"]:
            has, to, ls, lo = _twice_fix_texts(p, ce)
            return f"The app's record matches {ls} but has {has}: keep {ls}, change {to}, and delete {lo}."
        return f"The app's record matches {ls}: keep it and delete {lo}."
    if p["kind"] == "app_marks":
        return "Both copies are live in the app: check QuickBooks, then report which one it has."
    if not live:
        what = ("set aside" if all(r["set_aside"] for r in recs) else
                "ignored" if not any(r["set_aside"] for r in recs) else "set aside or ignored")
        return f"The app's {'copy is' if len(recs) == 1 else 'copies are'} {what}."
    if not diff:
        return ("The app has it once: delete one of the two in QuickBooks." if len(live) == 1
                else f"The app has it {len(live)} times too.")
    if {"a", "b"} <= {r["side"] for r in live}:
        return "The app has both: check which one is right."
    return "The app's record doesn't match either one exactly."


def _twice_plan(p, side, ce, fixed=False):
    """What the app changes when QuickBooks kept bill `side` ('a' / 'b') of a pair: the app follows QuickBooks.
    Its live copy of the bill (one agreeing with the kept bill, else one agreeing with neither, else the deleted
    bill's) takes the kept bill's store / # / date / amount where the two bills differed, except the `fix` fields
    when `fixed` (the kept bill was changed in QuickBooks to the app's values). Live invoices agreeing with the
    deleted bill are set aside as its copies. Returns (keeper or None, steps): ("set", rec, field, value),
    ("aside", rec), or ("type", rec) for a bill / credit mix-up, which is left to a person."""
    other, kept = ("b" if side == "a" else "a"), p[side]
    live = [r for r in p.get("records") or () if r["live"]]
    pool = ([r for r in live if r["side"] == side] or [r for r in live if r["side"] is None]
            or [r for r in live if r["side"] == other])
    if not pool:
        return None, []
    keeper = pool[0]
    steps = [("aside", r) for r in live if r is not keeper and r["side"] == other and r["record_type"] == "invoice"]
    if _twice_value(keeper, "type", ce) != _twice_value(kept, "type", ce):
        return keeper, steps + [("type", keeper)]
    skip = set(p.get("fix") or ()) if fixed else set()
    for f in p["diff"]:
        if f == "type" or f in skip or _twice_value(kept, f, ce) in (None, ""):
            continue                     # a kept bill with no # (or date) leaves the app's alone
        if _twice_value(keeper, f, ce) != _twice_value(kept, f, ce):
            steps.append(("set", keeper, f, _twice_raw(kept, f)))
    return keeper, steps


def _twice_steps_text(steps, done=False):
    """A plan in words: 'sets aside rec 1240; changes rec 1234 to #797208; moves rec 1510 to Maple' (done: 'set
    aside ...; changed ...; moved ...')."""
    parts, changes = [], {}
    for st in steps:
        if st[0] == "aside":
            parts.append(f"{'set' if done else 'sets'} aside rec {st[1]['record_id']}")
        elif st[0] == "type":
            kind = "credit" if st[1]["record_type"] == "credit_memo" else "bill"
            parts.append(f"rec {st[1]['record_id']} is a {kind} in the app (change it by hand)")
        elif st[0] == "set" and st[2] == "store":
            parts.append(f"{'moved' if done else 'moves'} rec {st[1]['record_id']} to {_twice_show('store', st[3])}")
        elif st[0] == "set":
            changes.setdefault(st[1]["record_id"], []).append(_twice_show(st[2], st[3]))
    parts += [f"{'changed' if done else 'changes'} rec {rid} to {', '.join(v)}" for rid, v in changes.items()]
    return "; ".join(parts)


def _twice_options(p, ce):
    """The report-back choices: [{"value", "answer" (saved), "label" (shown: the answer plus what the app will
    change, or that it's what the app has)}], the app's pick first."""
    diff, s = p["diff"], p["suggest"]
    app_only = p["kind"] == "app_marks"
    opts = []

    def add(value, answer, side, fixed=False):
        text = _twice_steps_text(_twice_plan(p, side, ce, fixed=fixed)[1])
        tail = (f" (the app: {text})" if text
                else " ✓ as the app has it" if side == s and (fixed or not p["fix"]) else "")
        opts.append({"value": value, "answer": answer, "label": answer + tail})

    if not diff:
        add("kept_a", "QuickBooks has it once" if app_only else "Deleted the extra one in QuickBooks", "a")
    else:
        lab = {k: _twice_label(p[k], diff, ce) for k in ("a", "b")}
        if s and p["fix"]:
            _has, to, ls, lo = _twice_fix_texts(p, ce)
            add(f"kept_{s}_fixed", f"Kept {ls}, changed {to}, deleted {lo}", s, fixed=True)
        for side in ((s, "b" if s == "a" else "a") if s else ("a", "b")):
            o = "b" if side == "a" else "a"
            add(f"kept_{side}", f"QuickBooks has {lab[side]} only" if app_only else f"Kept {lab[side]}, deleted {lab[o]}",
                side)
    for value, answer in TWICE_ANSWERS.items():
        opts.append({"value": value, "answer": answer,
                     "label": answer + ("" if value == "two_real" else " (say what in the note)")})
    return opts


def _twice_report_columns(cur):
    """True once qb_double_dismissed has outcome / app_changes (the schema update reaches each database at its
    next start)."""
    cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'qb_double_dismissed' "
                "AND column_name IN ('outcome', 'app_changes')")
    return cur.fetchone()[0] == 2


def _twice_save(pair_key, outcome, answer, note, username):
    """Mark a pair resolved with a report-back answer. False when it already was (someone was faster)."""
    text = f"{answer}: {note}" if note else answer
    conn = connect()
    try:
        with conn.cursor() as cur:
            if _twice_report_columns(cur):
                cur.execute("INSERT INTO qb_double_dismissed (pair_key, note, dismissed_by, outcome) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (pair_key) DO NOTHING", (pair_key, text, username, outcome))
            else:
                cur.execute("INSERT INTO qb_double_dismissed (pair_key, note, dismissed_by) VALUES (%s,%s,%s) "
                            "ON CONFLICT (pair_key) DO NOTHING", (pair_key, text, username))
            if not cur.rowcount:
                return False
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('qb_double_dismissed', NULL, 'resolved', %s, %s, %s)", (pair_key[:200], text, username))
        conn.commit()
        return True
    finally:
        conn.close()


def _twice_save_changes(pair_key, text):
    conn = connect()
    try:
        with conn.cursor() as cur:
            if _twice_report_columns(cur):
                cur.execute("UPDATE qb_double_dismissed SET app_changes = %s WHERE pair_key = %s", (text, pair_key))
            else:
                cur.execute("UPDATE qb_double_dismissed SET note = note || %s WHERE pair_key = %s",
                            (f" [the app: {text}]", pair_key))
        conn.commit()
    finally:
        conn.close()


def _twice_apply(pair, side, fixed, username, ce):
    """Carry out _twice_plan for a 'kept' answer. The kept copy's changes are one transaction: the record, its
    duplicate-ledger row for store / # (as an edit on the record page and set_kept_number do), edit_log, and an
    open QB correction asking QuickBooks for that very value closes. No duplicate re-check: it would flag the kept
    copy against the copy set aside next. Then the deleted bill's copies are set aside (keep_and_ignore_others),
    and the pair's 'entered twice' note on Possible duplicates goes. Returns what changed, in words."""
    keeper, steps = _twice_plan(pair, side, ce, fixed=fixed)
    actor = f"{username} via In QB twice?"
    why = f"{actor} (QuickBooks kept {_twice_label(pair[side], pair['diff'], ce)})"
    sets = [st for st in steps if st[0] == "set"]
    if sets:
        rtype, rid = keeper["record_type"], keeper["record_id"]
        table, cols = _TABLE[rtype], EDITABLE[rtype]
        conn = connect()
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SELECT * FROM {table} WHERE id = %s FOR UPDATE", (rid,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"rec {rid} is gone; reload the page.")
                ledger = []
                if rtype == "invoice":
                    cur.execute("SELECT id, invoice_number, store_matched FROM invoice_history WHERE source_file = %s "
                                "AND lower(coalesce(invoice_number, '')) = lower(coalesce(%s, '')) "
                                "AND abs(coalesce(invoice_amount, 0) - coalesce(%s, 0)) < 0.005",
                                (row.get("email_filename"), row.get("invoice_number"), row.get("amount")))
                    ledger = cur.fetchall()
                for _step, _rec, f, value in sets:
                    col, old = cols[f], row.get(cols[f])
                    if f == "amount":
                        new = round(abs(float(value)), 2) * (-1 if old is not None and float(old) < 0 else 1)
                    elif f == "date":
                        new = value.isoformat() if value else None
                    else:
                        new = value
                    cur.execute(f"UPDATE {table} SET {col} = %s, edited_by = %s, edited_at = now() WHERE id = %s",
                                (new, actor, rid))
                    cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                                "VALUES (%s,%s,%s,%s,%s,%s)",
                                (table, rid, col, None if old is None else str(old), None if new is None else str(new), why))
                    lcol = {"number": "invoice_number", "store": "store_matched"}.get(f)
                    for h in (ledger if lcol else ()):
                        if (h[lcol] or "") != (new or ""):
                            cur.execute(f"UPDATE invoice_history SET {lcol} = %s WHERE id = %s", (new, h["id"]))
                            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                                        "changed_by) VALUES ('invoice_history',%s,%s,%s,%s,%s)",
                                        (h["id"], lcol, h[lcol], new, why))
                    cur.execute("SELECT id, new_value FROM qb_corrections WHERE record_type = %s AND record_id = %s "
                                "AND field = %s AND done_at IS NULL", (rtype, rid, f))
                    for c in cur.fetchall():
                        if _twice_same(f, c["new_value"], new, ce):
                            cur.execute("UPDATE qb_corrections SET done_at = now(), done_by = %s, note = %s WHERE id = %s",
                                        (username, "QuickBooks has it (reported on In QB twice?)", c["id"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    asides = [st[1] for st in steps if st[0] == "aside"]
    if asides:
        keep_and_ignore_others(keeper["record_type"], keeper["record_id"],
                               [("invoice", r["record_id"]) for r in asides], actor)
    ids = [r["record_id"] for r in pair.get("records") or () if r["record_type"] == "invoice"]
    if ids:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE invoices SET qb_twice = NULL WHERE id = ANY(%s) AND qb_twice IS NOT NULL", (ids,))
            conn.commit()
        finally:
            conn.close()
    text = _twice_steps_text(steps, done=True)
    return [text] if text else []


def qb_double_report(pair_key, answer, username, note=None):
    """The report-back on an In QB twice? pair (2026-10-26, Joel: "the user should report back so the app updates
    accordingly"): what QuickBooks kept. Marks the pair resolved with that answer, once (a second report of a
    resolved pair changes nothing), and a 'kept' answer makes the app follow QuickBooks (_twice_plan). A pair no
    longer in the saved register (a newer export) only gets the answer. Returns {"answer", "changes": [text],
    "found": bool, "already": True / the earlier resolution when it was resolved before}."""
    import classify_emails as ce
    if answer not in TWICE_ANSWERS and answer not in _TWICE_KEPT:
        raise ValueError("Pick what QuickBooks has now.")
    note = (note or "").strip() or None
    pair = next((p for p in qb_double_entries("all") if p["key"] == pair_key), None)
    if pair is None:
        text = TWICE_ANSWERS.get(answer) or "Extra bill deleted in QuickBooks"
        saved = _twice_save(pair_key, answer, text, note, username)
        return {"answer": text, "changes": [], "found": False, "already": None if saved else True}
    if pair.get("dismissed"):
        return {"answer": None, "changes": [], "found": True, "already": pair["dismissed"]}
    qb_double_attach([pair])
    opt = next((o for o in pair["options"] if o["value"] == answer), None)
    if opt is None:
        raise ValueError("That choice no longer fits this pair; reload the page.")
    if not _twice_save(pair_key, answer, opt["answer"], note, username):
        return {"answer": opt["answer"], "changes": [], "found": True, "already": True}
    changes = []
    if answer in _TWICE_KEPT:
        try:
            changes = _twice_apply(pair, answer[5], answer.endswith("_fixed"), username, ce)
        except Exception:
            qb_double_dismiss(pair_key, username, restore=True)      # back on the list: it wasn't carried out
            raise
        if changes:
            _twice_save_changes(pair_key, "; ".join(changes))
    return {"answer": opt["answer"], "changes": changes, "found": True, "already": None}


def qb_list_unmatched(status="pending"):
    """QB export rows that matched NO record at upload (frozen for later arrival), newest first
    within each store, with the source batch filename. status: 'pending' (default) | 'resolved' |
    'dismissed' | 'all'. Resolved rows also carry their linked record's vendor/number for display."""
    clauses, params = [], []
    if status and status != "all":
        clauses.append("u.status = %s"); params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT u.*, b.filename AS batch_filename
                FROM qb_unmatched_rows u
                LEFT JOIN qb_import_batches b ON b.id = u.batch_id
                {where}
                ORDER BY u.qb_store NULLS LAST, u.created_at DESC, u.id DESC""", params)
            return cur.fetchall()
    finally:
        conn.close()


def qb_unmatched_pending_count():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM qb_unmatched_rows WHERE status = 'pending'")
            return cur.fetchone()[0]
    finally:
        conn.close()


def qb_dismiss_unmatched(unmatched_id, username):
    """Mark a pending unmatched QB row 'dismissed' (it will never arrive as an email). No app
    record is touched; the decision is logged to edit_log. Returns True if a pending row was acted
    on. connect() is autocommit; the explicit commit keeps it correct if that ever changes."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE qb_unmatched_rows SET status = 'dismissed', resolved_at = now() "
                        "WHERE id = %s AND status = 'pending'", (unmatched_id,))
            ok = cur.rowcount > 0
            if ok:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                    "changed_by) VALUES ('qb_unmatched_rows',%s,'status','pending','dismissed',%s)",
                    (unmatched_id, username))
        conn.commit()
        return ok
    finally:
        conn.close()


def qb_restore_unmatched(unmatched_id, username):
    """Undo a dismissal: a 'dismissed' row goes back to 'pending' so it can resolve again.
    Logged. Returns True if a dismissed row was acted on."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE qb_unmatched_rows SET status = 'pending', resolved_at = NULL "
                        "WHERE id = %s AND status = 'dismissed'", (unmatched_id,))
            ok = cur.rowcount > 0
            if ok:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                    "changed_by) VALUES ('qb_unmatched_rows',%s,'status','dismissed','pending',%s)",
                    (unmatched_id, username))
        conn.commit()
        return ok
    finally:
        conn.close()


def set_notice(message, minutes, username):
    """Show a sitewide banner for `minutes` (None = until cleared). Delegates to database."""
    return database.set_notice(message, minutes, username)


def clear_notice(username):
    """End the current notice immediately (active_until = now()). Delegates to database."""
    return database.clear_notice(username)


def get_active_notice():
    """The current notice {message, active_until} or None — used both for the initial page
    render and the /api/notice polling endpoint. Delegates to database."""
    return database.get_active_notice()


QB_ENTRY_GAP_MINUTES = 60   # marks this close together count as one sitting at QuickBooks


def last_qb_entry_by_store(gap_minutes=QB_ENTRY_GAP_MINUTES):
    """For the dashboard (Joel, 2026-10-14: "the last date we made an import for each store … track every time
    someone clicks 'entered to QB'"): per store, when records were last marked entered in QuickBooks by a
    person, how many records that sitting covered, and how many are ready to enter now. A sitting is a run of
    marks with no gap longer than `gap_minutes`. Only QB entry records count, invoices and credit memos; payments
    were taken off (Joel: "too messy and unnecessary"). Automatic marks don't count either: the QB reconcile
    import / recheck / confirm / unmatched backfill ("… via …") and "auto-…" ones. Uses the records' own
    entered_qb_at / entered_qb_by, so a mark someone later undid isn't counted. Returns
    [{"store", "last_at", "count", "ready"}] in the stores' display order; last_at is None for a store with no
    entries in the last 180 days."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT store_matched, entered_qb_at FROM (
                    SELECT store_matched, entered_qb_at, entered_qb_by FROM invoices WHERE entered_in_qb IS TRUE
                    UNION ALL
                    SELECT store_matched, entered_qb_at, entered_qb_by FROM credit_memos WHERE entered_in_qb IS TRUE
                ) x
                WHERE entered_qb_at > now() - interval '180 days'
                  AND coalesce(btrim(entered_qb_by), '') <> ''
                  AND entered_qb_by NOT ILIKE '%% via %%' AND entered_qb_by NOT ILIKE 'auto%%'
                ORDER BY entered_qb_at DESC""")
            marks = cur.fetchall()
            # Ready to export now, per store (Joel, 2026-10-14): the dashboard's own QB entry conditions
            # (dashboard_counts), so the number matches what the QB entry page shows for that store.
            cur.execute("""
                SELECT lower(btrim(coalesce(store_matched, ''))), count(*) FROM (
                    SELECT store_matched FROM invoices
                     WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE
                       AND duplicate_ignored IS NOT TRUE AND duplicate_suspect IS NOT TRUE
                    UNION ALL
                    SELECT store_matched FROM credit_memos
                     WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE
                ) r GROUP BY 1""")
            ready = dict(cur.fetchall())
    finally:
        conn.close()
    by_store = {}
    for store, at in marks:
        by_store.setdefault((store or "").strip().lower(), []).append(at)
    out = []
    for name in store_names():
        times = by_store.get(name.strip().lower()) or []
        count = 0
        for i, at in enumerate(times):                # newest first: count back to the first long gap
            if i and (times[i - 1] - at).total_seconds() > gap_minutes * 60:
                break
            count += 1
        out.append({"store": name, "last_at": times[0] if times else None, "count": count,
                    "ready": ready.get(name.strip().lower(), 0)})
    return out


# Daily report (2026-10-26, Joel: "how many items are being processed on daily basis and its breakdown, like
# how many emails, how many of it were invoices, how many payments"). Periods offered on the page.
REPORT_DAY_CHOICES = (7, 14, 30, 60, 90)
# How the system read each email -> report column. Anything else (spam, unknown, unreadable, not read yet) is "other".
REPORT_EMAIL_TYPES = ("invoice", "payment", "statement", "shipment_notice", "correspondence")
# A person's "mark entered" click, as on the dashboard (last_qb_entry_by_store): QB reconcile matches ("… via …")
# and "auto-…" marks don't count.
_PERSON_ENTERED_SQL = ("coalesce(btrim(entered_qb_by), '') <> '' AND entered_qb_by NOT ILIKE '%% via %%' "
                       "AND entered_qb_by NOT ILIKE 'auto%%'")


def daily_report(days=14):
    """What came in and what was done with it, per day for the last `days` days (today included), for the Daily
    report page. Per day:
      emails      emails received (Gmail receive time; a manual upload on its upload day) -- total and by how the
                  system read them (REPORT_EMAIL_TYPES + "other"). A review document re-read under its own name has
                  an emails row with no source (save_email_communication): not an email, left out.
      bills       invoice records saved that day, copies set aside as duplicates left out; for_qb = of those, the
                  ones not kept out of QuickBooks (excluded vendors, "don't enter"); bills_amt = the bills' total.
      credits     credit memo records saved that day.  payments = payment records saved, duplicates left out.
      review      items the classify runs that day sent to the review queue (runs.n_review; an item flagged again
                  after a re-read counts again).
      entered     invoices + credit memos a person marked entered in QuickBooks that day (automatic marks left out,
                  as on the dashboard); entered_amt = the invoices' total.
      dups        invoices and payments set aside as duplicates that day, automatically or by a person (a copy set
                  aside when it was saved has no timestamp: its save day).
    Returns {"days", "start", "rows" (newest first), "totals", "stores"}; stores = per store for the period (bills,
    bills_amt, for_qb, entered, entered_amt) plus ready to export now and the last entry, as on the dashboard."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 120))
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)
    blank = {"emails": 0, **{t: 0 for t in REPORT_EMAIL_TYPES}, "other": 0, "bills": 0, "for_qb": 0,
             "bills_amt": 0.0, "credits": 0, "payments": 0, "review": 0, "entered": 0, "entered_amt": 0.0, "dups": 0}
    by_day = {start + datetime.timedelta(days=i): dict(blank) for i in range(days)}

    def put(d, **vals):
        row = by_day.get(d)
        if row is not None:
            for k, v in vals.items():
                row[k] += float(v) if isinstance(row[k], float) else int(v or 0)

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT date(coalesce(received_at, date_added::timestamptz)), coalesce(email_type, ''), count(*)
                             FROM emails
                            WHERE source IS NOT NULL AND coalesce(received_at, date_added::timestamptz) >= %s
                            GROUP BY 1, 2""", (start,))
            for d, etype, n in cur.fetchall():
                put(d, emails=n, **{etype if etype in REPORT_EMAIL_TYPES else "other": n})
            cur.execute("""SELECT date_added,
                                  count(*) FILTER (WHERE duplicate_ignored IS NOT TRUE),
                                  count(*) FILTER (WHERE duplicate_ignored IS NOT TRUE AND skip_qb_import IS NOT TRUE),
                                  coalesce(sum(amount) FILTER (WHERE duplicate_ignored IS NOT TRUE), 0)
                             FROM invoices WHERE date_added >= %s GROUP BY 1""", (start,))
            for d, n, n_qb, amt in cur.fetchall():
                put(d, bills=n, for_qb=n_qb, bills_amt=amt)
            cur.execute("SELECT date_added, count(*) FROM credit_memos WHERE date_added >= %s GROUP BY 1", (start,))
            for d, n in cur.fetchall():
                put(d, credits=n)
            cur.execute("""SELECT date_added, count(*) FROM payments
                            WHERE date_added >= %s AND duplicate_ignored IS NOT TRUE GROUP BY 1""", (start,))
            for d, n in cur.fetchall():
                put(d, payments=n)
            cur.execute("SELECT date(started_at), coalesce(sum(n_review), 0) FROM runs WHERE started_at >= %s GROUP BY 1",
                        (start,))
            for d, n in cur.fetchall():
                put(d, review=n)
            cur.execute(f"""SELECT date(entered_qb_at), count(*), coalesce(sum(amt), 0) FROM (
                                SELECT entered_qb_at, entered_qb_by, amount AS amt FROM invoices WHERE entered_in_qb IS TRUE
                                UNION ALL
                                SELECT entered_qb_at, entered_qb_by, 0 FROM credit_memos WHERE entered_in_qb IS TRUE) x
                             WHERE entered_qb_at >= %s AND {_PERSON_ENTERED_SQL} GROUP BY 1""", (start,))
            for d, n, amt in cur.fetchall():
                put(d, entered=n, entered_amt=amt)
            cur.execute("""SELECT date(coalesce(duplicate_ignored_at, date_added::timestamptz)), count(*) FROM (
                                SELECT duplicate_ignored_at, date_added FROM invoices WHERE duplicate_ignored IS TRUE
                                UNION ALL
                                SELECT duplicate_ignored_at, date_added FROM payments WHERE duplicate_ignored IS TRUE) x
                            WHERE coalesce(duplicate_ignored_at, date_added::timestamptz) >= %s GROUP BY 1""", (start,))
            for d, n in cur.fetchall():
                put(d, dups=n)
            cur.execute(f"""SELECT lower(btrim(coalesce(store_matched, ''))),
                                   count(*) FILTER (WHERE date_added >= %s AND duplicate_ignored IS NOT TRUE),
                                   coalesce(sum(amount) FILTER (WHERE date_added >= %s AND duplicate_ignored IS NOT TRUE), 0),
                                   count(*) FILTER (WHERE date_added >= %s AND duplicate_ignored IS NOT TRUE
                                                      AND skip_qb_import IS NOT TRUE),
                                   count(*) FILTER (WHERE entered_in_qb IS TRUE AND entered_qb_at >= %s AND {_PERSON_ENTERED_SQL}),
                                   coalesce(sum(amount) FILTER (WHERE entered_in_qb IS TRUE AND entered_qb_at >= %s
                                                                  AND {_PERSON_ENTERED_SQL}), 0)
                              FROM invoices GROUP BY 1""", (start,) * 5)
            store_rows = {r[0]: r[1:] for r in cur.fetchall()}
    finally:
        conn.close()

    rows = [{"day": d, **vals} for d, vals in sorted(by_day.items(), reverse=True)]
    totals = {k: sum(r[k] for r in rows) for k in blank}
    entry = {s["store"].strip().lower(): s for s in last_qb_entry_by_store()}
    stores = []
    for name in store_names():
        key = name.strip().lower()
        n, amt, n_qb, n_ent, ent_amt = store_rows.pop(key, (0, 0, 0, 0, 0))
        e = entry.get(key) or {}
        stores.append({"store": name, "bills": n, "bills_amt": float(amt), "for_qb": n_qb, "entered": n_ent,
                       "entered_amt": float(ent_amt), "ready": e.get("ready", 0), "last_at": e.get("last_at")})
    # Bills whose store isn't one of the stores in Setup (unknown, blank): one line, so the column totals add up.
    rest = [v for v in store_rows.values() if v[0] or v[3]]
    if rest:
        stores.append({"store": "", "bills": sum(v[0] for v in rest), "bills_amt": float(sum(v[1] for v in rest)),
                       "for_qb": sum(v[2] for v in rest), "entered": sum(v[3] for v in rest),
                       "entered_amt": float(sum(v[4] for v in rest)), "ready": None, "last_at": None})
    return {"days": days, "start": start, "rows": rows, "totals": totals, "stores": stores}


_SALES_TAX_BASE_SQL = """
    FROM invoices i
    LEFT JOIN vendor_name_map v ON v.standard_vendor_name = i.standard_vendor
   WHERE i.sales_tax IS NOT NULL AND i.sales_tax > 0.004
     AND i.duplicate_ignored IS NOT TRUE AND i.ignored_at IS NULL
"""


def list_sales_tax_bills(show="open", store=None):
    """Bills (invoices) that carry a sales-tax charge (2026-10-21, Joel). show='open': only vendors NOT
    marked OK to charge (vendor_name_map.sales_tax_ok); 'ok': only the OK'd vendors; 'all'. Duplicates
    kept out and manually ignored bills are left out. Returns [{vendor..., bills:[...], total_tax}] by
    vendor (standard name, else the name as read), most bills first."""
    clauses, params = [], []
    # OK'd = the vendor (all stores), the vendor at this bill's store (sales_tax_ok_stores), or the bill itself
    # (2026-10-23). Decided per bill below, after the vendor name is resolved.
    if store:
        clauses.append("i.store_matched = %s"); params.append(store)
    extra = "".join(" AND " + c for c in clauses)
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT i.id, i.store_matched, i.vendor_matched, i.standard_vendor, i.invoice_number,
                       i.invoice_date, i.amount, i.sales_tax, i.entered_in_qb, i.skip_qb_import,
                       i.email_filename, i.file_path, v.id AS vendor_map_id, v.sales_tax_ok, v.sales_tax_ok_by, v.sales_tax_ok_at,
                       i.sales_tax_ok_by AS bill_ok_by, i.sales_tax_ok_at AS bill_ok_at
                {_SALES_TAX_BASE_SQL}{extra}
                ORDER BY i.id DESC""", params)
            rows = cur.fetchall()
    finally:
        conn.close()
    # Excluded-vendor bills (skip_qb_import) never get a standard_vendor saved -- the standardization backfill
    # skips them -- so "Parmed" and "Parmed Pharmaceuticals" showed as two vendors (2026-10-23). Resolve the
    # standard name from Vendor names (names + aliases) here when the record has none.
    import classify_emails as ce
    if not ce.VENDOR_STD:
        ce.VENDOR_STD = ce.load_vendor_standardization()
    lookup = (ce.VENDOR_STD or {}).get("lookup") or {}
    map_ids, ok_info, store_ok = {}, {}, {}
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, standard_vendor_name, sales_tax_ok, sales_tax_ok_by, sales_tax_ok_at FROM vendor_name_map")
            for v in cur.fetchall():
                map_ids[v["standard_vendor_name"]] = v["id"]
                ok_info[v["standard_vendor_name"]] = v
            cur.execute("SELECT standard_vendor, store, ok_by, ok_at FROM sales_tax_ok_stores")
            for so in cur.fetchall():
                store_ok[(so["standard_vendor"], (so["store"] or "").strip().lower())] = so
    finally:
        conn.close()
    resolved_rows = []
    for r in rows:
        # Re-resolve when the record has no standard name OR one that's no longer in Vendor names (two Parmed
        # bills still carried an old standard name 'Parmed' after it was merged into 'Parmed Pharmaceuticals').
        if r["vendor_matched"] and (not r["standard_vendor"] or (ok_info and r["standard_vendor"] not in ok_info)):
            k = ce._norm_vendor_std_key(r["vendor_matched"])
            std = lookup.get(k) or ce._best_fuzzy_vendor_match(k, lookup)[0]   # same matching as the pipeline
            if not std:
                # Still nothing: a short form of exactly ONE standard name ('Parmed' -> 'Parmed Pharmaceuticals').
                rel = [n for n in ok_info if ce._vendors_related(r["vendor_matched"], n)]
                std = rel[0] if len(rel) == 1 else None
            if std:
                info = ok_info.get(std) or {}
                r["standard_vendor"] = std
                r["vendor_map_id"] = map_ids.get(std)
                r["sales_tax_ok"], r["sales_tax_ok_by"], r["sales_tax_ok_at"] = (
                    info.get("sales_tax_ok"), info.get("sales_tax_ok_by"), info.get("sales_tax_ok_at"))
        so = store_ok.get((r["standard_vendor"], (r["store_matched"] or "").strip().lower())) if r["standard_vendor"] else None
        r["store_ok_by"], r["store_ok_at"] = (so["ok_by"], so["ok_at"]) if so else (None, None)
        is_ok = bool(r.get("sales_tax_ok")) or bool(r.get("bill_ok_at")) or bool(so)
        if (show == "open" and is_ok) or (show == "ok" and not is_ok):
            continue
        resolved_rows.append(r)
    rows = resolved_rows
    groups = {}
    for r in rows:
        key = r["standard_vendor"] or (r["vendor_matched"] or "(no vendor)")
        g = groups.setdefault(key, {"vendor": key, "standard_vendor": r["standard_vendor"],
                                    "in_map": r["vendor_map_id"] is not None, "ok": bool(r["sales_tax_ok"]),
                                    "ok_by": r["sales_tax_ok_by"], "ok_at": r["sales_tax_ok_at"],
                                    "bills": [], "total_tax": 0.0})
        try:
            r["tax_pct"] = float(r["sales_tax"]) / (float(r["amount"]) - float(r["sales_tax"])) * 100 \
                if r["amount"] and float(r["amount"]) > float(r["sales_tax"]) else None
        except (TypeError, ValueError, ZeroDivisionError):
            r["tax_pct"] = None
        g["bills"].append(r)
        g["total_tax"] += float(r["sales_tax"] or 0)
    for g in groups.values():
        g["bills"].sort(key=lambda r: (ce._loose_date(r["invoice_date"]) or datetime.date.min, r["id"]), reverse=True)
    return sorted(groups.values(), key=lambda g: (-len(g["bills"]), g["vendor"].lower()))


def sales_tax_open_count():
    """Same bills as the page's 'open' list (vendor names resolved through aliases, 2026-10-23)."""
    try:
        return sum(len(g["bills"]) for g in list_sales_tax_bills("open"))
    except Exception:
        return 0


def set_store_sales_tax_ok(standard_vendor, store, ok, username):
    """OK a vendor's sales tax at one store only (or undo it). Logged."""
    if not standard_vendor or not store:
        raise ValueError("Pick a store.")
    conn = connect()
    try:
        with conn.cursor() as cur:
            if ok:
                cur.execute("INSERT INTO sales_tax_ok_stores (standard_vendor, store, ok_by) VALUES (%s,%s,%s) "
                            "ON CONFLICT (standard_vendor, store) DO NOTHING", (standard_vendor, store, username))
            else:
                cur.execute("DELETE FROM sales_tax_ok_stores WHERE standard_vendor = %s AND store = %s",
                            (standard_vendor, store))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('sales_tax_ok_stores', NULL, %s, %s, %s, %s)",
                        (f"{standard_vendor} @ {store}", str(not ok), str(bool(ok)), username))
        conn.commit()
    finally:
        conn.close()


def set_bill_sales_tax_ok(invoice_id, ok, username):
    """OK one bill's sales-tax charge (or undo it), leaving its vendor as is. Logged."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            if ok:
                cur.execute("UPDATE invoices SET sales_tax_ok_by = %s, sales_tax_ok_at = now() WHERE id = %s", (username, invoice_id))
            else:
                cur.execute("UPDATE invoices SET sales_tax_ok_by = NULL, sales_tax_ok_at = NULL WHERE id = %s", (invoice_id,))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('invoices',%s,'sales_tax_ok',%s,%s,%s)", (invoice_id, str(not ok), str(bool(ok)), username))
        conn.commit()
    finally:
        conn.close()


def set_vendor_sales_tax_ok(standard_vendor, ok, username):
    """Mark a vendor (vendor_name_map row) OK -- or not -- to charge sales tax. Logged."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE vendor_name_map SET sales_tax_ok = %s, sales_tax_ok_by = %s, "
                        "sales_tax_ok_at = now() WHERE standard_vendor_name = %s RETURNING id",
                        (bool(ok), username, standard_vendor))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"'{standard_vendor}' isn't in Setup > Vendor names yet -- add it there first")
            _ref_log(cur, "vendor_name_map", row[0], "sales_tax_ok", str(not ok), str(bool(ok)), username)
    finally:
        conn.close()


def dashboard_counts():
    """Live counts for the to-do dashboard — one number per 'needs action' queue, computed on
    each call (no caching; these are small COUNTs). Conditions match what each page shows. Also
    returns the review queue split by assignee (the only queue with per-user ownership)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            def n(sql, params=()):
                cur.execute(sql, params)
                return cur.fetchone()[0]
            d = {}
            d["review"] = n("SELECT count(*) FROM review_queue")
            cur.execute(
                "SELECT COALESCE(NULLIF(btrim(assigned_to), ''), '(unassigned)') AS who, count(*) "
                "FROM review_queue GROUP BY 1 "
                "ORDER BY (COALESCE(NULLIF(btrim(assigned_to), ''), '(unassigned)') = '(unassigned)'), "
                "count(*) DESC")
            d["review_by"] = [(r[0], r[1]) for r in cur.fetchall()]
            d["statement"] = n("SELECT count(*) FROM statement_lines "
                               "WHERE status IN ('missing', 'amount_mismatch')")
            # What the missing lines add up to, for the dashboard tile (2026-10-26, Joel).
            d["statement_missing_amt"] = float(n("SELECT coalesce(sum(amount), 0) FROM statement_lines "
                                                 "WHERE status = 'missing'"))
            _qb_inv = ("FROM invoices WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE "
                       "AND duplicate_ignored IS NOT TRUE AND duplicate_suspect IS NOT TRUE")
            d["qb_invoices"] = n(f"SELECT count(*) {_qb_inv}")
            d["qb_invoices_setup"] = n(f"SELECT count(*) {_qb_inv} "
                                       "AND (qb_account IS NULL OR btrim(qb_account) = '')")
            d["qb_credits"] = n("SELECT count(*) FROM credit_memos "
                                "WHERE entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE")
            d["payments"] = n("SELECT count(*) FROM payments WHERE entered_in_qb IS NOT TRUE "
                              "AND duplicate_ignored IS NOT TRUE AND duplicate_suspect IS NOT TRUE "
                              "AND skip_qb_import IS NOT TRUE")
            d["flags_pending"] = n("SELECT count(*) FROM flagged_reviews WHERE status = 'pending'")
            d["flags_approved"] = n("SELECT count(*) FROM flagged_reviews WHERE status = 'approved_for_fix'")
            d["requests_pending"] = n("SELECT count(*) FROM change_requests WHERE status = 'pending'")
            d["requests_approved"] = n("SELECT count(*) FROM change_requests WHERE status = 'approved_for_build'")
            d["investigations"] = n("SELECT count(*) FROM investigations WHERE status IN ('running', 'failed')")
            d["sales_orders"] = n("SELECT count(*) FROM pending_sales_orders WHERE status = 'held'")
            d["corr_open"] = n("SELECT count(*) FROM emails WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                               "AND coalesce(corr_status, 'open') = 'open' AND coalesce(status, '') <> 'fetched'")
            d["corr_follow_up"] = n("SELECT count(*) FROM emails WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                                    "AND corr_status = 'follow_up'")
            d["failed_emails"] = n("SELECT count(*) FROM emails WHERE status = 'error'")
            # Reconcile: rows that are in the dedup LEDGER (invoice_history / payment_history) but
            # never became a live invoices / payments row — the "extracted but never recorded"
            # condition that hid EuroPharma INV0800123 for 2 weeks. Matched by normalized vendor +
            # number (so a legal-suffix spelling drift doesn't create a false orphan).
            d["orphaned_history"] = n(
                "SELECT (SELECT count(*) FROM invoice_history h "
                "         WHERE coalesce(h.invoice_number,'') <> '' AND NOT EXISTS ("
                "           SELECT 1 FROM invoices i WHERE lower(i.invoice_number)=lower(h.invoice_number) "
                "             AND norm_vendor_dedupe(i.vendor_matched)=norm_vendor_dedupe(h.vendor_matched))) "
                "     + (SELECT count(*) FROM payment_history h "
                "         WHERE coalesce(h.reference_number,'') <> '' AND NOT EXISTS ("
                "           SELECT 1 FROM payments p WHERE lower(p.reference_number)=lower(h.reference_number) "
                "             AND norm_vendor_dedupe(p.vendor_matched)=norm_vendor_dedupe(h.vendor_matched)))")
            # Emails fetched but NEVER classified: no run ever touched them (run_id NULL) and they're
            # >1 day old (so a just-fetched email mid-cycle isn't flagged). This is the OTHER half of
            # the "record never recorded" bug class — a source email that silently never got processed
            # at all (the 42 unprocessed scanner docs). Surfaces a recurrence here instead of needing
            # someone to spot one blank row on /emails. 1-day grace mirrors the batch cadence.
            # Count via the SAME labeled CTE the /emails page uses, so the tile and the
            # ?disposition=never_classified page agree exactly (tile = the >1-day-aged subset). A
            # fetched/run_id-NULL email that nonetheless has records/review rows is NOT counted here
            # (the CTE's earlier CASE branches claim it); only truly untouched ones are.
            d["never_classified"] = n(
                _EMAILS_CTE + "SELECT count(*) FROM labeled WHERE disposition_key = 'never_classified' "
                "AND received_at < now() - interval '1 day'")
            d["qb_pending"] = n("SELECT count(*) FROM qb_match_candidates WHERE status = 'pending'")
    finally:
        conn.close()
    # Sets (not records) for /duplicates, and unmapped cards — via the existing helpers so the
    # numbers match those pages exactly.
    try:
        d["duplicates"] = len(list_duplicate_candidates())
    except Exception:
        d["duplicates"] = 0
    try:
        d["cards"] = sum(1 for c in list_seen_cards() if not c.get("store"))
    except Exception:
        d["cards"] = 0
    try:
        d["no_live_copy"] = no_live_copy_count()
    except Exception:
        d["no_live_copy"] = 0
    return d


def distinct_values(column):
    if column not in ("store_matched", "vendor_matched", "email_status"):
        return []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT {column} FROM all_records "
                        f"WHERE {column} IS NOT NULL AND {column} <> '' ORDER BY 1")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Emails ledger — one row per ingested email (from `emails`) with a derived
# DISPOSITION (what the system did with it) + the records it produced. Read-only.
# --------------------------------------------------------------------------- #

# disposition_key -> human label (also the filter dropdown). 'recorded' /
# 'informational' get a computed suffix in _email_disposition().
EMAIL_DISPOSITIONS = [
    ("recorded", "Recorded"), ("in_review", "In review"), ("ignored", "Dismissed"),
    ("held", "Held (sales order)"), ("informational", "No action / informational"),
    ("never_classified", "Never classified"), ("error", "Error / unreadable"),
]

# The counts CTE + labeling, reused by the page + count queries (no params — the
# only literal is status='held'). Aggregate once per table (not per email row).
_EMAILS_CTE = """
WITH inv AS (SELECT email_filename ef, count(*) n,
                    count(*) FILTER (WHERE skip_qb_import IS TRUE) ns FROM invoices GROUP BY email_filename),
     cm  AS (SELECT email_filename ef, count(*) n,
                    count(*) FILTER (WHERE skip_qb_import IS TRUE) ns FROM credit_memos GROUP BY email_filename),
     pay AS (SELECT email_filename ef, count(*) n FROM payments GROUP BY email_filename),
     rvq AS (SELECT source_email ef, count(*) n FROM review_queue GROUP BY source_email),
     ign AS (SELECT source_email ef, count(*) n FROM ignored_reviews GROUP BY source_email),
     hld AS (SELECT source_email ef, count(*) n FROM pending_sales_orders WHERE status='held' GROUP BY source_email),
labeled AS (
  SELECT e.filename, e.received_at, e.from_address, e.subject, e.email_type, e.status,
         e.run_id, e.gmail_link,
         COALESCE(inv.n,0) AS n_inv, COALESCE(inv.ns,0) AS n_inv_skip,
         COALESCE(cm.n,0)  AS n_cm,  COALESCE(cm.ns,0)  AS n_cm_skip,
         COALESCE(pay.n,0) AS n_pay, COALESCE(rvq.n,0) AS n_review,
         COALESCE(ign.n,0) AS n_ignored, COALESCE(hld.n,0) AS n_held,
         CASE
           WHEN e.status = 'error' THEN 'error'
           WHEN COALESCE(inv.n,0)+COALESCE(cm.n,0)+COALESCE(pay.n,0) > 0 THEN 'recorded'
           WHEN COALESCE(rvq.n,0) > 0 THEN 'in_review'
           WHEN COALESCE(ign.n,0) > 0 THEN 'ignored'
           WHEN COALESCE(hld.n,0) > 0 THEN 'held'
           -- Fetched but no run ever touched it AND it produced nothing: never classified at all
           -- (distinct from 'informational', which WAS classified and simply had no record).
           WHEN e.status = 'fetched' AND e.run_id IS NULL THEN 'never_classified'
           ELSE 'informational'
         END AS disposition_key
  FROM emails e
  LEFT JOIN inv ON inv.ef = e.filename
  LEFT JOIN cm  ON cm.ef  = e.filename
  LEFT JOIN pay ON pay.ef = e.filename
  LEFT JOIN rvq ON rvq.ef = e.filename
  LEFT JOIN ign ON ign.ef = e.filename
  LEFT JOIN hld ON hld.ef = e.filename
  WHERE e.legacy_migrated IS NOT TRUE          -- archived old-system stubs hidden from the ledger
    -- Real emails only: Gmail-fetched (has a message id) or a manually-dropped .eml. Excludes the
    -- stub rows created per split/reprocessed record (one _upsert_email keyed by a split .pdf name,
    -- not an email) so the ledger count reconciles to the Gmail "fetched" label. See §5z note.
    AND (e.gmail_message_id IS NOT NULL OR e.filename ILIKE '%%.eml')
)
"""


def _email_types():
    """Distinct email_type values present, for the filter dropdown."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT email_type FROM emails "
                        "WHERE email_type IS NOT NULL AND email_type <> '' ORDER BY 1")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _email_disposition(r):
    """Human disposition label from a labeled email row's counts + key."""
    key = r["disposition_key"]
    if key == "error":
        return "Error / unreadable"
    if key == "recorded":
        parts = []
        if r["n_inv"]:
            parts.append(f"{r['n_inv']} invoice" + ("s" if r["n_inv"] > 1 else ""))
        if r["n_cm"]:
            parts.append(f"{r['n_cm']} credit memo" + ("s" if r["n_cm"] > 1 else ""))
        if r["n_pay"]:
            parts.append(f"{r['n_pay']} payment" + ("s" if r["n_pay"] > 1 else ""))
        label = "Recorded — " + ", ".join(parts)
        n_records = r["n_inv"] + r["n_cm"] + r["n_pay"]
        n_skip = r["n_inv_skip"] + r["n_cm_skip"]      # payments never skip
        if n_records > 0 and n_skip == n_records:
            label += " (excluded — in Browse)"
        return label
    if key == "in_review":
        return "In review"
    if key == "ignored":
        return "Dismissed"
    if key == "held":
        return "Held (sales order)"
    if key == "never_classified":
        return "Never classified (fetched, no run)"
    et = (r["email_type"] or "").lower()               # informational
    if et == "shipment_notice":
        return "Shipment notice"
    if et == "statement":
        return "Statement"
    if "order" in et or "ack" in et:
        return "Order ack"
    if et == "correspondence":
        return "Correspondence"
    if et in ("spam", "none", "unknown", ""):
        return "No action"
    return "No action (informational)"


def list_emails(disposition=None, email_type=None, run_id=None, date_from=None, date_to=None,
                search=None, page=1, page_size=200):
    """One row per ingested email with its derived disposition, produced-record counts,
    and the records it produced (for linking). Returns (rows, total). Filters + paging
    applied in SQL; the disposition label is computed per row in Python."""
    clauses, params = [], []
    if disposition:
        clauses.append("disposition_key = %s"); params.append(disposition)
    if email_type:
        clauses.append("email_type = %s"); params.append(email_type)
    if run_id:
        clauses.append("run_id = %s"); params.append(run_id)
    if date_from:
        clauses.append("received_at::date >= %s"); params.append(date_from)
    if date_to:
        clauses.append("received_at::date <= %s"); params.append(date_to)
    if search:
        clauses.append("(subject ILIKE %s OR from_address ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    page = max(1, int(page)); page_size = max(1, min(int(page_size), 500))
    offset = (page - 1) * page_size

    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"{_EMAILS_CTE} SELECT count(*) AS n FROM labeled {where}", params)
            total = cur.fetchone()["n"]
            cur.execute(f"{_EMAILS_CTE} SELECT * FROM labeled {where} "
                        f"ORDER BY received_at DESC NULLS LAST, filename LIMIT %s OFFSET %s",
                        params + [page_size, offset])
            rows = cur.fetchall()

            # Attach the produced records (record_type + id) for the visible page only.
            fns = [r["filename"] for r in rows]
            by_email = {fn: [] for fn in fns}
            if fns:
                cur.execute(
                    "SELECT email_filename ef, 'invoice' AS rt, id FROM invoices WHERE email_filename = ANY(%s) "
                    "UNION ALL SELECT email_filename, 'credit_memo', id FROM credit_memos WHERE email_filename = ANY(%s) "
                    "UNION ALL SELECT email_filename, 'payment', id FROM payments WHERE email_filename = ANY(%s) "
                    "ORDER BY ef, rt, id", (fns, fns, fns))
                for rr in cur.fetchall():
                    by_email[rr["ef"]].append({"record_type": rr["rt"], "id": rr["id"]})
    finally:
        conn.close()

    for r in rows:
        r["disposition"] = _email_disposition(r)
        r["records"] = by_email.get(r["filename"], [])
    return rows, total


# --------------------------------------------------------------------------- #
# Learnings editor (SAME `learnings` table the pipeline injects on every run).
# The pipeline rewrites this table each run (DELETE + re-INSERT), which would
# reset any audit columns — so audit lives in edit_log (keyed by rule TEXT,
# because the pipeline's re-insert also changes row ids). Curated here, the
# rules simply take effect on the next classify_emails.py run.
# --------------------------------------------------------------------------- #

def _log(cur, field, old, new, username):
    cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                "changed_by) VALUES ('learnings', NULL, %s, %s, %s, %s)",
                (field, old, new, username))


def list_learnings():
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, learned_rule, date_added, from_email FROM learnings "
                        "ORDER BY learned_rule")
            rows = cur.fetchall()
            cur.execute("SELECT old_value, new_value, changed_by, changed_at FROM edit_log "
                        "WHERE table_name = 'learnings' ORDER BY changed_at")
            logs = cur.fetchall()
    finally:
        conn.close()
    created, edited = {}, {}
    for l in logs:  # ascending time → last write wins for 'edited'
        rule = l["new_value"]
        if not rule:
            continue
        if l["old_value"] is None and rule not in created:
            created[rule] = (l["changed_by"], l["changed_at"])
        edited[rule] = (l["changed_by"], l["changed_at"])
    for r in rows:
        r["created_by"], r["created_at"] = created.get(r["learned_rule"], (None, None))
        r["edited_by"], r["edited_at"] = edited.get(r["learned_rule"], (None, None))
    return rows


def add_learning_web(rule, username):
    rule = (rule or "").strip()
    if not rule:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO learnings (learned_rule, date_added, from_email) "
                        "VALUES (%s, %s, %s) ON CONFLICT (learned_rule) DO NOTHING",
                        (rule, datetime.date.today().isoformat(), f"web:{username}"))
            if cur.rowcount:
                _log(cur, "learned_rule", None, rule, username)
    finally:
        conn.close()


def edit_learning_web(old_rule, new_rule, username):
    new_rule = (new_rule or "").strip()
    if not new_rule or new_rule == old_rule:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM learnings WHERE learned_rule = %s", (new_rule,))
            if cur.fetchone():
                # target text already exists — merge by dropping the old row
                cur.execute("DELETE FROM learnings WHERE learned_rule = %s", (old_rule,))
            else:
                cur.execute("UPDATE learnings SET learned_rule = %s WHERE learned_rule = %s",
                            (new_rule, old_rule))
            _log(cur, "learned_rule", old_rule, new_rule, username)
    finally:
        conn.close()


def delete_learning_web(rule, username):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM learnings WHERE learned_rule = %s", (rule,))
            if cur.rowcount:
                _log(cur, "learned_rule", rule, None, username)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Reference tables (stores / vendor_name_map / sender_store_map / …).
# The tables + seed data live in database.py (created/seeded by init_db, which
# init_web_schema calls). The CLI only READS them, so their ids are STABLE —
# audit uses id-keyed edit_log PLUS on-row created_/edited_ columns. Curated
# here; the next classify_emails.py run reads the new values with no code change.
# --------------------------------------------------------------------------- #

_REF = {
    "stores":           {"unique": "store_name",
                         "cols": ["store_name", "display_name", "aliases", "address", "notes", "back_office"],
                         "bool_cols": {"back_office"},
                         "order": "sort_order, id"},
    "vendor_name_map": {"unique": "standard_vendor_name",
                         "cols": ["standard_vendor_name", "exclude_from_qb",
                                  "exclude_from_statement_recon", "trust_line_item_totals",
                                  "ignore_payment_confirmations", "sales_tax_ok",
                                  "aliases", "name_3m",
                                  "name_madison", "name_maple", "name_refuah",
                                  "name_westgate", "name_bmw", "name_lakewood_rx", "name_carepax"],
                         "bool_cols": {"exclude_from_qb", "exclude_from_statement_recon",
                                       "trust_line_item_totals", "ignore_payment_confirmations", "sales_tax_ok"},
                         "order": "lower(standard_vendor_name)"},
    "account_name_map": {"unique": "standard_account_name",
                          "cols": ["standard_account_name", "name_3m", "name_madison",
                                   "name_maple", "name_refuah", "name_westgate",
                                   "name_bmw", "name_lakewood_rx", "name_carepax"],
                          "order": "lower(standard_account_name)"},
    "vendor_account_assignment": {"unique": "standard_vendor_name",
                          "cols": ["standard_vendor_name", "standard_account_name"],
                          "order": "lower(standard_vendor_name)"},
    "sender_store_map": {"unique": "sender_email",
                         "cols": ["sender_email", "store_matched", "note"],
                         "order": "lower(sender_email)"},
    "contact_store_map": {"unique": "contact_email",
                          "cols": ["contact_email", "store_matched", "note"],
                          "order": "lower(contact_email)"},
    "card_store_map":   {"unique": "card_last4",
                         "cols": ["card_last4", "store_matched", "note"],
                         "order": "card_last4"},
    # Vendor-scoped store rules (classify_emails.apply_scoped_learning_gate). No UNIQUE key.
    "store_scope_rules": {"unique": None, "required": "reference",
                          "cols": ["reference", "store_matched", "applies_to", "vendors", "note"],
                          "order": "id"},
    # No single UNIQUE key (many rules may share a store/vendor); `class` is required.
    # Correspondence "always ignore" rules (database.apply_correspondence_rules). No UNIQUE key.
    "correspondence_rules": {"unique": None, "required": "action",
                             "cols": ["topic", "sender", "subject_contains", "recipients_contains",
                                      "outside_recipient_only", "action", "assign_to", "forward_to", "note"],
                             "bool_cols": {"outside_recipient_only"},
                             "order": "id"},
    # People correspondence gets forwarded to (the Correspondence page's pick-list).
    "forward_contacts": {"unique": "email", "cols": ["name", "email", "note"], "order": "name, email"},
    "class_rules":      {"unique": None, "required": "class",
                         "cols": ["store_matched", "vendor_matched", "account", "class", "note"],
                         "order": "id"},
}


def _ref_log(cur, table, record_id, field, old, new, username):
    cur.execute(
        "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (table, record_id, field,
         str(old) if old not in (None, "") else None,
         str(new) if new not in (None, "") else None, username))


# store_matched (lower/trimmed) -> per-store name_* column. Mirrors classify_emails._STORE_KEY,
# which is what set qb_vendor / qb_account when the record was first processed.
_QB_NAME_STORE_COL = {
    "maple pharmacy": "name_maple", "madison pharmacy": "name_madison",
    "refuah pharmacy": "name_refuah", "westgate pharmacy": "name_westgate",
    "3m pharmacy": "name_3m", "bmw": "name_bmw", "lakewood rx": "name_lakewood_rx", "carepax": "name_carepax",
}
_QB_NAME_TARGETS = {
    # map table:        (record column holding the standard name, record column to refresh)
    "vendor_name_map":  ("standard_vendor", "qb_vendor"),
    "account_name_map": ("standard_account", "qb_account"),
}


def refresh_qb_names(cur, map_table, username, standard_names=None, dry_run=False):
    """Re-derive the saved QuickBooks name (qb_vendor or qb_account) on invoices + credit memos
    that are NOT yet entered in QB, from the CURRENT per-store spelling in vendor_name_map /
    account_name_map — same rule as classify_emails.apply_vendor_standardization(): the store's
    name_* column if filled, else the standard name. Without this, editing a store spelling in
    Setup never reached records that were already processed (they kept the old snapshot).
    Records already entered in QB are never touched. Every change is logged to edit_log.
    standard_names: limit to these standard names (None = every name in the map).
    Returns [(table, record_id, store, old, new)]."""
    std_col, qb_col = _QB_NAME_TARGETS[map_table]
    key_col = "standard_vendor_name" if map_table == "vendor_name_map" else "standard_account_name"
    case_sql = "CASE lower(trim(r.store_matched)) " + " ".join(
        f"WHEN '{k}' THEN m.{c}" for k, c in _QB_NAME_STORE_COL.items()) + " END"
    new_expr = f"COALESCE(NULLIF(trim({case_sql}), ''), r.{std_col})"
    changes = []
    for table in ("invoices", "credit_memos"):
        sql = (f"SELECT r.id, r.store_matched, r.{qb_col} AS old_v, {new_expr} AS new_v "
               f"  FROM {table} r JOIN {map_table} m ON m.{key_col} = r.{std_col} "
               f" WHERE r.entered_in_qb IS NOT TRUE "
               f"   AND r.{qb_col} IS DISTINCT FROM {new_expr}")
        params = []
        if standard_names is not None:
            if not standard_names:
                continue
            sql += f" AND m.{key_col} = ANY(%s)"
            params.append(list(standard_names))
        cur.execute(sql, params)
        for rec in cur.fetchall():
            if isinstance(rec, dict):     # works with RealDictCursor or a plain cursor
                rid, store, old_v, new_v = rec["id"], rec["store_matched"], rec["old_v"], rec["new_v"]
            else:
                rid, store, old_v, new_v = rec
            changes.append((table, rid, store, old_v, new_v))
            if dry_run:
                continue
            cur.execute(f"UPDATE {table} SET {qb_col} = %s, edited_at = now() WHERE id = %s",
                        (new_v, rid))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (table, rid, qb_col, old_v, new_v, f"{username} (store name updated in Setup)"))
    return changes


def _ref_clean(table, values):
    """Whitelist + trim the incoming fields; sender_email is normalized lower and
    card_last4 is normalized to its trailing 4 digits."""
    m = _REF[table]
    bool_cols = m.get("bool_cols") or set()
    out = {}
    for c in m["cols"]:
        if c in bool_cols:
            # Checkbox: present+truthy = True, absent (unchecked) = False. ALWAYS emitted so an
            # edit can UNCHECK it (an unchecked box submits nothing, so it's simply absent here).
            raw = values.get(c)
            out[c] = str(raw).strip().lower() in ("1", "true", "on", "yes", "t") if raw is not None else False
            continue
        if c not in values:
            continue
        v = (values.get(c) or "").strip()
        if c == "sender_email":
            v = v.lower()
        elif c == "card_last4":
            digits = "".join(ch for ch in v if ch.isdigit())
            v = digits[-4:] if digits else ""
        out[c] = v or None
    return out


def list_reference(table):
    m = _REF[table]
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY {m['order']}")
            return cur.fetchall()
    finally:
        conn.close()


def store_names():
    """The canonical store_name values (in the stores table's display order) — the
    option list for any store-valued select input (Card→store, Sender→store, Cards
    assign). Keeps store values pickable, never mistyped."""
    return [r["store_name"] for r in list_reference("stores") if r.get("store_name")]


_STORE_LABEL_CACHE = {"at": 0, "map": {}}
_STORE_LABEL_TTL = 30   # seconds — reference table changes rarely; avoids a query per row


def store_display_map():
    """{canonical store_matched value -> display label}, label falls back to the
    canonical value itself when display_name is blank. Cached briefly so it's cheap
    to call per-row in a template (the stores table is tiny and changes rarely)."""
    now = time.time()
    if now - _STORE_LABEL_CACHE["at"] > _STORE_LABEL_TTL:
        _STORE_LABEL_CACHE["map"] = {r["store_name"]: (r.get("display_name") or r["store_name"])
                                     for r in list_reference("stores") if r.get("store_name")}
        _STORE_LABEL_CACHE["at"] = now
    return _STORE_LABEL_CACHE["map"]


def store_label(value):
    """Display label for a canonical store_matched value. Falls back to the value
    itself (never hides an unmapped/legacy value). Blank/None passes through."""
    if not value:
        return value
    return store_display_map().get(value, value)


def standard_vendor_names():
    """Standard vendor names — option list for the Vendor→account select."""
    return [r["standard_vendor_name"] for r in list_reference("vendor_name_map")
            if r.get("standard_vendor_name")]


def standard_account_names():
    """Standard account names — option list for the Vendor→account select."""
    return [r["standard_account_name"] for r in list_reference("account_name_map")
            if r.get("standard_account_name")]


# Per-store name_* columns shared by vendor_name_map and account_name_map, mapped to a
# short store label for display.
_STORE_NAME_COLS = [
    ("name_3m", "3M"), ("name_madison", "Madison"), ("name_maple", "Maple"),
    ("name_refuah", "Refuah"), ("name_westgate", "Westgate"), ("name_bmw", "BMW"),
    ("name_lakewood_rx", "Lakewood Rx"), ("name_carepax", "Carepax"),
]


def _store_name_pairs(row):
    """[(store_label, value)] for the non-empty per-store name_* columns of a
    vendor_name_map / account_name_map row."""
    out = []
    for col, label in _STORE_NAME_COLS:
        v = (row.get(col) or "").strip()
        if v:
            out.append((label, v))
    return out


_VENDOR_FILLER_WORDS = {"the", "and", "inc", "llc", "co", "corp", "company", "ltd", "of", "usa", "us", "group",
                        "pharmacy", "pharma", "pharmaceuticals", "pharmaceutical", "distributors", "distributor",
                        "distributing", "distribution", "supply", "supplies", "products", "services", "service",
                        "wholesale", "international", "enterprises", "trading", "health", "medical", "labs"}


def _vendor_words(key):
    """The meaningful words of a normalized vendor name (3+ letters, not filler like 'the', 'inc', 'pharma')."""
    return {w for w in re.split(r"[^a-z0-9]+", (key or "").lower()) if len(w) >= 3 and w not in _VENDOR_FILLER_WORDS}


def suggest_vendor_matches(raw, limit=5, floor=60):
    """Fuzzy candidates for a raw (unmatched) vendor spelling against existing vendor_name_map
    entries — scored against the standard name, EVERY alias, AND every per-store spelling column,
    since a new spelling might match a store's spelling rather than the canonical name. A human
    confirms, so the floor is lower than classify's 88% auto-apply threshold. Returns
    [{standard_vendor_name, score, matched_on:(label, spelling)}], best first. Reuses classify's
    normalization + scorer; does NOT touch VENDOR_FUZZY_THRESHOLD/MARGIN or auto-apply behavior."""
    import classify_emails as ce
    raw_key = ce._norm_vendor_std_key(raw or "")
    if not raw_key:
        return []

    def _score(a, b):
        if getattr(ce, "_rapidfuzz", None):
            return ce._rapidfuzz.token_sort_ratio(a, b)
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() * 100

    out = []
    for r in list_reference("vendor_name_map"):
        std = r.get("standard_vendor_name")
        if not std:
            continue
        spellings = [("standard name", std)]
        for alias in re.split(r"[\n,]+", r.get("aliases") or ""):
            if alias.strip():
                spellings.append(("alias", alias.strip()))
        for col, label in _STORE_NAME_COLS:
            v = (r.get(col) or "").strip()
            if v:
                spellings.append((f"{label} spelling", v))
        best, best_on = 0.0, None
        for label, sp in spellings:
            key = ce._norm_vendor_std_key(sp)
            sc = _score(raw_key, key)
            # 2026-10-29: letter-level similarity alone made "The Hire Mark" a 64% match for "THE HEALA".
            # Below 85%, a suggestion also needs a real word in common (3+ letters, not a filler word).
            if sc < 85 and not (_vendor_words(raw_key) & _vendor_words(key)):
                continue
            if sc > best:
                best, best_on = sc, (label, sp)
        if best >= floor:
            out.append({"standard_vendor_name": std, "score": round(best), "matched_on": best_on})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def attach_vendor_spelling(standard_vendor_name, raw, store, username):
    """Add a raw invoice spelling to an EXISTING vendor_name_map row so that spelling resolves to
    this standard vendor — into the store's per-store column when the store is known and that
    column is empty, else appended to aliases. Idempotent (a spelling already present is a no-op).
    Returns the column it wrote to, or None if nothing to do / row missing."""
    import classify_emails as ce
    raw = (raw or "").strip()
    if not raw or not standard_vendor_name:
        return None
    key = ce._STORE_KEY.get((store or "").strip().lower())
    store_col = f"name_{key}" if key else None
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM vendor_name_map WHERE standard_vendor_name = %s LIMIT 1",
                        (standard_vendor_name,))
            row = cur.fetchone()
            if not row:
                return None
            rl = raw.lower()
            # Already present anywhere on the row -> it resolves already; no-op. Split aliases on
            # NEWLINES ONLY (one alias per line) — NOT on commas: a comma is legitimate inside a single
            # vendor name (e.g. "EnlivenHealth (FDS, Inc.)"). The old comma-split shattered such an alias
            # so this presence check never matched the whole string, and each re-save appended ANOTHER
            # duplicate copy of it. (Mirrors the same fix in database.load_vendor_standardization.)
            present = [(row.get("standard_vendor_name") or "").lower()]
            present += [a.strip().lower() for a in (row.get("aliases") or "").splitlines() if a.strip()]
            present += [(row.get(c) or "").strip().lower() for c, _ in _STORE_NAME_COLS]
            if rl in present:
                return store_col or "aliases"
            if store_col and not (row.get(store_col) or "").strip():
                target = store_col
                cur.execute(f"UPDATE vendor_name_map SET {store_col} = %s, edited_by = %s, "
                            f"edited_at = now() WHERE id = %s", (raw, username, row["id"]))
            else:
                target = "aliases"
                aliases = (row.get("aliases") or "").strip()
                new_aliases = (aliases + "\n" + raw) if aliases else raw
                cur.execute("UPDATE vendor_name_map SET aliases = %s, edited_by = %s, "
                            "edited_at = now() WHERE id = %s", (new_aliases, username, row["id"]))
            cur.execute(
                "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                ("vendor_name_map", row["id"], target, None,
                 f"+ '{raw}' (matched to {standard_vendor_name})", username))
        conn.commit()
        return target
    finally:
        conn.close()


def vendor_lookup(term):
    """Read-only cross-table lookup for one search term across the three vendor-related Setup
    tables, so 'does this vendor have everything it needs' is answerable in a single glance
    instead of checking Vendor names, Vendor → account and Account names separately.

    Case-insensitive substring match against: vendor_name_map (standard name, aliases, every
    per-store name_*), vendor_account_assignment (standard vendor name), and account_name_map
    (standard account name + per-store name_*, in case the term is really an account spelling).

    Returns {"term", "vendors": [...], "accounts": [...]} where each vendor card carries its
    aliases, per-store names, whether it's in the name map at all, and its assigned account (or
    None → the caller offers an 'assign account' prefill link)."""
    term = (term or "").strip()
    if not term:
        return {"term": "", "vendors": [], "accounts": []}
    # 2026-10-21 (Joel): '&' found nothing. Each column is also compared with '&' read as 'and' and
    # spaces/punctuation ignored, so 'A&S', 'A & S' and 'A and S' all find each other.
    like = f"%{term}%"
    nterm = re.sub(r"[^a-z0-9]", "", term.lower().replace("&", "and"))
    nlike = f"%{nterm}%" if nterm else None

    def _m(col):
        """(sql, params) -- col matches the term as typed, or in its normalized form."""
        if not nlike:
            return f"{col} ILIKE %s", [like]
        return (f"({col} ILIKE %s OR regexp_replace(replace(lower(coalesce({col}, '')), '&', 'and'), "
                f"'[^a-z0-9]', '', 'g') LIKE %s)", [like, nlike])

    def _any(cols):
        parts, params = [], []
        for c in cols:
            q, p = _m(c)
            parts.append(q)
            params += p
        return " OR ".join(parts), params

    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Vendor names (standard name / aliases / any per-store name).
            where, params = _any(["standard_vendor_name", "aliases"] + [c for c, _ in _STORE_NAME_COLS])
            cur.execute(f"SELECT * FROM vendor_name_map WHERE {where} ORDER BY standard_vendor_name", params)
            name_rows = cur.fetchall()

            # Vendor → account assignments (by standard vendor name).
            where, params = _any(["standard_vendor_name"])
            cur.execute(
                "SELECT standard_vendor_name, standard_account_name FROM vendor_account_assignment "
                f"WHERE {where} ORDER BY standard_vendor_name", params)
            assign_rows = cur.fetchall()
            # Full assignment map so a name-map hit can show its account even when the term
            # didn't match the assignment row itself.
            cur.execute("SELECT standard_vendor_name, standard_account_name "
                        "FROM vendor_account_assignment")
            assign_by_vendor = {r["standard_vendor_name"]: r["standard_account_name"]
                                for r in cur.fetchall()}

            # Account names (the term might be an account spelling, not a vendor).
            where, params = _any(["standard_account_name"] + [c for c, _ in _STORE_NAME_COLS])
            cur.execute(f"SELECT * FROM account_name_map WHERE {where} ORDER BY standard_account_name", params)
            acct_rows = cur.fetchall()
    finally:
        conn.close()

    # One card per matching standard vendor — union of name-map hits and assignment hits.
    vendors = {}
    for r in name_rows:
        v = r.get("standard_vendor_name") or ""
        vendors[v] = {
            "standard_vendor_name": v,
            "aliases": (r.get("aliases") or "").strip(),
            "store_names": _store_name_pairs(r),
            "in_name_map": True,
            "assigned_account": assign_by_vendor.get(v),
        }
    for r in assign_rows:
        v = r.get("standard_vendor_name") or ""
        if v not in vendors:
            vendors[v] = {
                "standard_vendor_name": v,
                "aliases": "",
                "store_names": [],
                "in_name_map": False,
                "assigned_account": r.get("standard_account_name"),
            }
    accounts = [{
        "standard_account_name": r.get("standard_account_name") or "",
        "store_names": _store_name_pairs(r),
    } for r in acct_rows]
    return {
        "term": term,
        "vendors": sorted(vendors.values(), key=lambda d: d["standard_vendor_name"].lower()),
        "accounts": accounts,
    }


# --------------------------------------------------------------------------- #
# Internal short links (private) — compact /l/<code> URLs for QuickBooks memos.
# --------------------------------------------------------------------------- #

def _gen_code(n=7):
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def _link_code(target_url, kind):
    """The stable short code for a target URL — reused if the URL was seen before
    (dedupe by target_url), else a fresh unique base62 code. connect() is autocommit,
    so each statement commits on its own; a unique-violation re-selects (handles both a
    concurrent same-URL insert and a rare code collision)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM links WHERE target_url = %s", (target_url,))
            row = cur.fetchone()
            if row:
                return row[0]
            for _ in range(12):
                code = _gen_code()
                try:
                    cur.execute("INSERT INTO links (code, target_url, kind) VALUES (%s,%s,%s)",
                                (code, target_url, kind))
                    return code
                except psycopg2.errors.UniqueViolation:
                    cur.execute("SELECT code FROM links WHERE target_url = %s", (target_url,))
                    row = cur.fetchone()
                    if row:
                        return row[0]   # a concurrent insert added this URL
                    # else a code collision — loop and try a new code
            raise RuntimeError("could not allocate a unique short-link code")
    finally:
        conn.close()


def short_link_for(target_url, kind, base_url=None):
    """Return a compact internal short link (f"{base}/l/{code}") for `target_url`,
    upserting a stable code. Host precedence: env LINK_BASE_URL (set at Phase 2C to the
    cloud domain) wins, else the caller's `base_url` (the current LAN host). Empty
    target → "" so callers fall back to nothing."""
    if not target_url:
        return ""
    code = _link_code(target_url, kind)
    base = LINK_BASE_URL or (base_url or "").rstrip("/")
    return f"{base}/l/{code}"


def short_links_for(pairs, base_url=None):
    """Batch version of short_link_for: {target_url: short_url} for every (target_url,
    kind) pair, resolved in ONE db connection (one SELECT for the already-coded URLs +
    a single batch INSERT for any new ones). Used on the hot paths (QB page + CSV) so N
    rows cost one round-trip instead of ~3N connections. Same host precedence as
    short_link_for. Empty/blank target_urls are skipped."""
    base = LINK_BASE_URL or (base_url or "").rstrip("/")
    kinds = {}                                  # url -> kind (first seen wins)
    for url, kind in pairs:
        if url and url not in kinds:
            kinds[url] = kind
    if not kinds:
        return {}
    urls = list(kinds)
    codes = {}                                  # url -> code
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, target_url FROM links WHERE target_url = ANY(%s)", (urls,))
            for code, url in cur.fetchall():
                codes[url] = code
            missing = [u for u in urls if u not in codes]
            for _ in range(5):                  # bounded code-collision retry
                if not missing:
                    break
                used, rows = set(), []
                for u in missing:
                    code = _gen_code()
                    while code in used:
                        code = _gen_code()
                    used.add(code)
                    rows.append((code, u, kinds[u]))
                try:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO links (code, target_url, kind) VALUES %s "
                        "ON CONFLICT (target_url) DO NOTHING",
                        rows)
                except psycopg2.errors.UniqueViolation:
                    pass                        # a generated code collided — regenerate + retry
                # Authoritative: re-read whatever codes are now committed for these URLs.
                cur.execute("SELECT code, target_url FROM links WHERE target_url = ANY(%s)", (missing,))
                for code, url in cur.fetchall():
                    codes[url] = code
                missing = [u for u in missing if u not in codes]
    finally:
        conn.close()
    return {u: f"{base}/l/{codes[u]}" for u in urls if u in codes}


def list_sales_orders(status=None, limit=200):
    """Held / superseded / released sales orders for the Sales-orders tab (newest first)."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status:
                cur.execute("SELECT * FROM pending_sales_orders WHERE status = %s "
                            "ORDER BY id DESC LIMIT %s", (status, limit))
            else:
                cur.execute("SELECT * FROM pending_sales_orders ORDER BY id DESC LIMIT %s", (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def sales_orders_held_count():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pending_sales_orders WHERE status = 'held'")
            return cur.fetchone()[0]
    finally:
        conn.close()


def release_sales_order_now(order_id, username):
    """Shorten a held order's window to now, so the next classify run records it."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_sales_orders SET release_after = now() "
                        "WHERE id = %s AND status = 'held'", (order_id,))
            if cur.rowcount:
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, "
                            "new_value, changed_by) VALUES ('pending_sales_orders',%s,'release_after',"
                            "NULL,'now',%s)", (order_id, username))
    finally:
        conn.close()


def ignore_sales_order(order_id, username):
    """Reviewer drops a held order (won't be recorded) — marks it superseded/ignored."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_sales_orders SET status = 'superseded', "
                        "matched_invoice = '(manually ignored)' WHERE id = %s AND status = 'held'",
                        (order_id,))
            if cur.rowcount:
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, "
                            "new_value, changed_by) VALUES ('pending_sales_orders',%s,'status',"
                            "'held','superseded (ignored)',%s)", (order_id, username))
    finally:
        conn.close()


def get_link(code):
    """{'target_url', 'kind'} for a short code, or None. Used by the /l/<code> redirect."""
    if not code:
        return None
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT target_url, kind FROM links WHERE code = %s", (code,))
            return cur.fetchone()
    finally:
        conn.close()


# Reference-list sources a select column can draw its options from. Add future lists
# here (e.g. vendors) and reference them via a column's "options_from".
REFERENCE_OPTION_SOURCES = {"stores": store_names,
                            "vendor_names": standard_vendor_names,
                            "account_names": standard_account_names,
                            "corr_actions": lambda: ["ignore", "forward", "follow_up"],
                            "active_usernames": lambda: [u["username"] for u in list_users() if u.get("active")],
                            "corr_topics": lambda: [k for k, _ in database.CORR_TOPICS],
                            # Setup › Store rules "Applies to" (database.STORE_RULE_ONLY / _EXCEPT).
                            "store_rule_kinds": lambda: [database.STORE_RULE_ONLY,
                                                         database.STORE_RULE_EXCEPT]}


def exclude_vendor(vendor_name, username):
    """One-click 'ignore this vendor': flag this vendor exclude_from_qb=TRUE on vendor_name_map (the
    unified vendor list) so its invoices/credit memos never enter QuickBooks (read by
    classify_emails.is_excluded_vendor on the next run). IDEMPOTENT: if the name is already covered by
    the exclusion list (is_excluded_vendor — one of the excluded spellings, matched as a whole name since
    2026-10-14), it's a quiet no-op. Otherwise it sets the flag on the exact
    standard-name row, or creates a minimal vendor_name_map row for an exclude-only vendor. Returns
    'added', 'exists', or None (blank name). Logged to edit_log, same as a reference-page edit."""
    name = (vendor_name or "").strip()
    if not name:
        return None
    try:
        import classify_emails as ce
        if ce.is_excluded_vendor(name):
            return "exists"
    except Exception:
        pass  # fall through — worst case we set/create the flag explicitly below
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, exclude_from_qb FROM vendor_name_map "
                        "WHERE lower(standard_vendor_name) = lower(%s)", (name,))
            row = cur.fetchone()
            if row:
                rid, already = row
                if already:
                    return "exists"
                cur.execute("UPDATE vendor_name_map SET exclude_from_qb = TRUE, edited_by = %s, "
                            "edited_at = now() WHERE id = %s", (username, rid))
                _ref_log(cur, "vendor_name_map", rid, "exclude_from_qb", "False", "True", username)
                return "added"
            cur.execute("INSERT INTO vendor_name_map (standard_vendor_name, exclude_from_qb, created_by) "
                        "VALUES (%s, TRUE, %s) RETURNING id", (name, username))
            rid = cur.fetchone()[0]
            _ref_log(cur, "vendor_name_map", rid, "exclude_from_qb", None, "True", username)
            return "added"
    finally:
        conn.close()


def add_reference(table, values, username):
    m = _REF[table]
    data = _ref_clean(table, values)
    req = m.get("required") or m["unique"]        # the field that must be non-blank
    if not data.get(req):
        raise ValueError(f"{req.replace('_', ' ')} is required")
    cols = [c for c in m["cols"] if c in data]
    placeholders = ", ".join(["%s"] * len(cols))
    conn = connect()
    try:
        with conn.cursor() as cur:
            if m.get("unique"):                    # dedup on the unique key
                cur.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}, created_by) "
                    f"VALUES ({placeholders}, %s) "
                    f"ON CONFLICT ({m['unique']}) DO NOTHING RETURNING id",
                    [data[c] for c in cols] + [username])
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"'{data[m['unique']]}' already exists")
            else:                                  # no unique key (e.g. class_rules) — plain insert
                cur.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}, created_by) "
                    f"VALUES ({placeholders}, %s) RETURNING id",
                    [data[c] for c in cols] + [username])
                row = cur.fetchone()
            _ref_log(cur, table, row[0], req, None, data.get(req), username)
    finally:
        conn.close()


def edit_reference(table, record_id, values, username):
    m = _REF[table]
    data = _ref_clean(table, values)
    req = m.get("required") or m["unique"]
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (record_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("record not found")
            changed = False
            for c, new in data.items():
                old = row.get(c)
                if (old or None) == new:
                    continue
                if c == req and not new:
                    raise ValueError(f"{c.replace('_', ' ')} is required")
                if m.get("unique") and c == m["unique"]:
                    cur.execute(f"SELECT 1 FROM {table} WHERE {m['unique']} = %s AND id <> %s",
                                (new, record_id))
                    if cur.fetchone():
                        raise ValueError(f"'{new}' already exists")
                cur.execute(f"UPDATE {table} SET {c} = %s WHERE id = %s", (new, record_id))
                _ref_log(cur, table, record_id, c, old, new, username)
                changed = True
            if changed:
                cur.execute(f"UPDATE {table} SET edited_by = %s, edited_at = now() WHERE id = %s",
                            (username, record_id))
                # A store spelling (or the standard name itself) changed -> push the new QB name
                # onto records not yet entered in QB, so the next export uses it.
                if table in _QB_NAME_TARGETS:
                    std_name = data.get(m["unique"]) or row.get(m["unique"])
                    refresh_qb_names(cur, table, username, standard_names=[std_name])
    finally:
        conn.close()


def delete_reference(table, record_id, username):
    m = _REF[table]
    ident = m.get("unique") or m.get("required")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {ident} FROM {table} WHERE id = %s", (record_id,))
            row = cur.fetchone()
            if not row:
                return
            cur.execute(f"DELETE FROM {table} WHERE id = %s", (record_id,))
            _ref_log(cur, table, record_id, ident, row[0], None, username)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Cards discovery — every card last-4 ever seen on ANY record (invoice, credit
# memo, or payment), so it can be classified to a store (feeds card_store_map,
# read by the pipeline each run for all three record types).
# --------------------------------------------------------------------------- #

# A backfill last-4 counts ONLY when it's anchored to an explicit card-ending CUE
# ("ending in 1234", "acct ending 1234", …) or a card MASK ("****1234", "xxxx-1234",
# "x1234"), AND the 4 digits are a bounded group — not preceded by a digit and not
# followed by a digit OR letter. This rejects coincidental 4-digit runs inside
# auth/reference codes, check/order/invoice/tracking numbers (e.g. "Auth 05923G" no
# longer yields "0592"). Two capture groups (cue branch | mask branch); take whichever
# matched. Intentionally strict: a few cue-less bare-4-digit historicals are traded
# away for ZERO false positives — the clean going-forward card_last4 field covers new
# records reliably.
_CARD_LAST4_RE = re.compile(
    r'(?:ending(?:\s+(?:in|with))?|ends\s+in|card\s+ending|acc(?:oun)?t\s+ending)'
    r'\s*#?\s*[:\-]?\s*(?<!\d)(\d{4})(?![0-9A-Za-z])'
    r'|(?:[x\*]{2,}|[x\*])[-\s]?(?<!\d)(\d{4})(?![0-9A-Za-z])',
    re.I,
)


def _card_from_reference(text):
    """A genuine card last-4 parsed from a free-text field (payment reference_number
    or a record's notes), cue-/mask-anchored only (see _CARD_LAST4_RE). Returns the
    LAST such match (the ending is usually stated near the end), or '' when none is
    anchored — so unrelated 4-digit runs are never surfaced as a card."""
    if not text:
        return ""
    last = ""
    for m in _CARD_LAST4_RE.finditer(text):
        last = m.group(1) or m.group(2)
    return last or ""


def list_seen_cards():
    """One row per distinct card last-4 seen on ANY record (invoice, credit memo,
    or payment): last4, count, last_seen, an example vendor, and the currently
    mapped store (None = unmapped). Sources per record: (a) the record's structured
    card_last4 (going-forward, clean); (b) a STRICT cue-/mask-anchored backfill parsed
    from payments.reference_number and the notes field on all three tables (only a last-4
    that follows a card-ending cue or mask counts — see _card_from_reference — so auth
    codes and check/invoice/order numbers are never mistaken for cards).
    Unmapped last-4s sort first (the worklist), then by count desc."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT card_last4, reference_number, notes, vendor_matched, "
                        "payment_date, date_added FROM payments")
            payrows = cur.fetchall()
            cur.execute("SELECT card_last4, notes, vendor_matched, "
                        "invoice_date, date_added FROM invoices")
            invrows = cur.fetchall()
            cur.execute("SELECT card_last4, notes, vendor_matched, "
                        "credit_memo_date, date_added FROM credit_memos")
            cmrows = cur.fetchall()
            cur.execute("SELECT card_last4, store_matched FROM card_store_map")
            mapped = {database.norm_last4(r["card_last4"]): r["store_matched"]
                      for r in cur.fetchall() if database.norm_last4(r["card_last4"])}
    finally:
        conn.close()

    agg = {}

    def _add(last4, seen, vendor):
        if not last4:
            return
        a = agg.setdefault(last4, {"last4": last4, "count": 0, "last_seen": "", "vendor": ""})
        a["count"] += 1
        if seen and seen > a["last_seen"]:
            a["last_seen"] = seen
        if not a["vendor"] and vendor:
            a["vendor"] = vendor

    for r in payrows:
        last4 = (database.norm_last4(r["card_last4"])
                 or _card_from_reference(r["reference_number"])
                 or _card_from_reference(r["notes"]))
        seen = (r["payment_date"] or "") or (str(r["date_added"]) if r["date_added"] else "")
        _add(last4, seen, r["vendor_matched"])
    for r in invrows:
        last4 = database.norm_last4(r["card_last4"]) or _card_from_reference(r["notes"])
        seen = (r["invoice_date"] or "") or (str(r["date_added"]) if r["date_added"] else "")
        _add(last4, seen, r["vendor_matched"])
    for r in cmrows:
        last4 = database.norm_last4(r["card_last4"]) or _card_from_reference(r["notes"])
        seen = (r["credit_memo_date"] or "") or (str(r["date_added"]) if r["date_added"] else "")
        _add(last4, seen, r["vendor_matched"])

    out = []
    for last4, a in agg.items():
        a["store"] = mapped.get(last4)
        out.append(a)
    out.sort(key=lambda x: (x["store"] is not None, -x["count"], x["last4"]))
    return out


def assign_card_store(last4, store, username):
    """Upsert a card_last4 → store mapping from the Cards tab (insert or reassign),
    logged to edit_log. Tolerates an existing row (unlike add_reference). Mirrors
    the reference-editor audit; the next classify run reads the new mapping."""
    last4 = database.norm_last4(last4)
    store = (store or "").strip()
    if not last4:
        raise ValueError("card last 4 is required")
    if not store:
        raise ValueError("store is required")
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, store_matched FROM card_store_map WHERE card_last4 = %s", (last4,))
            row = cur.fetchone()
            if row:
                if (row["store_matched"] or "") != store:
                    cur.execute("UPDATE card_store_map SET store_matched = %s, "
                                "edited_by = %s, edited_at = now() WHERE id = %s",
                                (store, username, row["id"]))
                    _ref_log(cur, "card_store_map", row["id"], "store_matched",
                             row["store_matched"], store, username)
            else:
                cur.execute("INSERT INTO card_store_map (card_last4, store_matched, created_by) "
                            "VALUES (%s,%s,%s) RETURNING id", (last4, store, username))
                new_id = cur.fetchone()["id"]
                _ref_log(cur, "card_store_map", new_id, "card_last4", None, last4, username)
    finally:
        conn.close()


def apply_card_map_to_records(username, last4=None):
    """When a card is mapped to a store (Cards tab or Setup › Card → store, 2026-10-13), give that store
    to records waiting for QuickBooks entry that carry the card and have NO store yet (blank or
    'unknown'): invoices, credit memos and payments, not entered in QB. The card is the record's
    card_last4, or the same strict card-ending cue the Cards tab reads from the payment reference or the
    notes. A record that already has a store is left alone, and review items aren't touched (they carry
    no card; they pick the mapping up when they're re-read). The QuickBooks vendor / account names that
    depend on the store are re-derived. Every change is logged. last4=None applies every mapped card.
    Returns [(table, record_id, last4, store)]."""
    conn = connect()
    conn.autocommit = False
    changed = []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT card_last4, store_matched FROM card_store_map")
            mapping = {database.norm_last4(r["card_last4"]): (r["store_matched"] or "").strip()
                       for r in cur.fetchall() if database.norm_last4(r["card_last4"])}
            if last4 is not None:
                key = database.norm_last4(last4)
                mapping = {key: mapping[key]} if key in mapping else {}
            mapping = {k: v for k, v in mapping.items() if v}
            no_store = ("entered_in_qb IS NOT TRUE AND (coalesce(btrim(store_matched), '') = '' "
                        "OR lower(btrim(store_matched)) = 'unknown')")
            std_vendors, std_accounts = set(), set()
            for table, text_cols in (("invoices", ("notes",)), ("credit_memos", ("notes",)),
                                     ("payments", ("reference_number", "notes"))):
                if not mapping:
                    break
                extra = ", standard_vendor, standard_account" if table != "payments" else ""
                cur.execute(f"SELECT id, card_last4, store_matched, {', '.join(text_cols)}{extra} "
                            f"FROM {table} WHERE {no_store}")
                for r in cur.fetchall():
                    card = database.norm_last4(r["card_last4"]) or next(
                        (c for c in (_card_from_reference(r.get(col)) for col in text_cols) if c), "")
                    store = mapping.get(card)
                    if not store:
                        continue
                    cur.execute(f"UPDATE {table} SET store_matched = %s, edited_at = now() "
                                f"WHERE id = %s AND {no_store}", (store, r["id"]))
                    if cur.rowcount != 1:
                        continue
                    cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                                "changed_by) VALUES (%s,%s,'store_matched',%s,%s,%s)",
                                (table, r["id"], r["store_matched"], store,
                                 f"{username} (card {card} mapped to {store})"))
                    changed.append((table, r["id"], card, store))
                    if table != "payments":
                        std_vendors.add(r.get("standard_vendor"))
                        std_accounts.add(r.get("standard_account"))
            # The QuickBooks names follow the store (per-store spellings in Setup).
            if std_vendors - {None, ""}:
                refresh_qb_names(cur, "vendor_name_map", username, standard_names=sorted(std_vendors - {None, ""}))
            if std_accounts - {None, ""}:
                refresh_qb_names(cur, "account_name_map", username, standard_names=sorted(std_accounts - {None, ""}))
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Invoice history viewer — READ-ONLY (protects duplicate-detection integrity).
# --------------------------------------------------------------------------- #

_HISTORY_SORT = {"vendor": "h.vendor_matched", "invoice_number": "h.invoice_number",
                 "store": "h.store_matched", "invoice_date": "h.invoice_date",
                 "amount": "h.invoice_amount", "date_added": "h.date_added"}


def list_invoice_history(vendor=None, invoice_number=None, store=None, date_from=None,
                         date_to=None, amount_min=None, amount_max=None,
                         sort="date_added", direction="desc", page=1, page_size=50,
                         excluded="include", amount=None):
    clauses, params = [], []
    if vendor:
        clauses.append("h.vendor_matched ILIKE %s"); params.append(f"%{vendor}%")
    if invoice_number:
        clauses.append("h.invoice_number ILIKE %s"); params.append(f"%{invoice_number}%")
    if store:
        clauses.append("h.store_matched = %s"); params.append(store)
    if date_from:
        clauses.append("h.invoice_date >= %s"); params.append(date_from)
    if date_to:
        clauses.append("h.invoice_date <= %s"); params.append(date_to)
    if amount not in (None, ""):
        clauses.append("ROUND(h.invoice_amount::numeric, 2) = ROUND(%s::numeric, 2)")
        params.append(amount)
    else:
        if amount_min not in (None, ""):
            clauses.append("h.invoice_amount >= %s"); params.append(amount_min)
        if amount_max not in (None, ""):
            clauses.append("h.invoice_amount <= %s"); params.append(amount_max)
    excl = _excluded_vendor_sql(excluded, "h.vendor_matched")
    if excl:
        clauses.append(excl)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    order = _HISTORY_SORT.get(sort, "h.date_added")
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    page = max(1, int(page)); page_size = max(1, min(int(page_size), 200))
    offset = (page - 1) * page_size

    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT count(*) AS n FROM invoice_history h {where}", params)
            total = cur.fetchone()["n"]
            cur.execute(f"""
                SELECT h.vendor_matched, h.store_matched, h.invoice_number, h.invoice_date,
                       h.invoice_amount, h.source_file, h.date_added,
                       i.file_path, COALESCE(i.gmail_link, e.gmail_link) AS gmail_link
                  FROM invoice_history h
                  LEFT JOIN LATERAL (
                       SELECT file_path, gmail_link FROM invoices
                        WHERE email_filename = h.source_file
                          AND invoice_number = h.invoice_number LIMIT 1) i ON TRUE
                  LEFT JOIN emails e ON e.filename = h.source_file
                  {where}
                  ORDER BY {order} {direction} NULLS LAST
                  LIMIT %s OFFSET %s
            """, params + [page_size, offset])
            return cur.fetchall(), total
    finally:
        conn.close()



# --------------------------------------------------------------------------- #
# Correspondence triage (2026-10-15). Emails the classifier calls 'correspondence' produce no
# record, so before this nothing ever surfaced them. Status lives on the emails row:
# open (NULL) -> follow_up / handled / ignored. "Always ignore" = a correspondence_rules row.
# --------------------------------------------------------------------------- #

CORR_STATUSES = [("open", "Open"), ("follow_up", "Follow up"), ("handled", "Handled"),
                 ("ignored", "Dismissed"), ("all", "All")]     # 'ignored' is the stored value; "Dismiss" app-wide (10/26)


def list_correspondence(status="open", search=None, limit=50, offset=0, kind=None, assigned=None,
                        sender=None, topic=None):
    """(rows, total, counts_by_status). Newest first."""
    # An email sent back for processing (status 'fetched') waits for the next run -- not shown here.
    # Pure correspondence AND any other email (invoice, statement...) a person wrote a request in (2026-10-16).
    clauses, params = ["(e.email_type = 'correspondence' OR e.needs_response IS TRUE)", "coalesce(e.status, '') <> 'fetched'"], []
    if status and status != "all":
        clauses.append("coalesce(e.corr_status, 'open') = %s"); params.append(status)
    if assigned == "-":              # "Unassigned" in the assigned-to filter (2026-10-20)
        clauses.append("coalesce(e.corr_assigned_to, '') = ''")
    elif assigned:
        clauses.append("e.corr_assigned_to = %s"); params.append(assigned)
    if sender:                       # an address or '@domain' (the page's "Similar" links)
        clauses.append("lower(coalesce(e.from_address, '')) LIKE %s"); params.append(f"%{sender.lower()}%")
    if topic:
        clauses.append("e.topic = %s"); params.append(topic)
    if kind == "reply":
        clauses.append("e.needs_response IS TRUE")
    elif kind == "corr":
        clauses.append("e.email_type = 'correspondence'")
    if search:
        # Every word must appear somewhere (any field, any order): "crowncare refua" finds an email
        # from Crowncare that mentions Refuah, even though the two words aren't side by side.
        fields = ("e.subject", "e.from_address", "e.info_vendor", "e.info_store", "e.info_note",
                  "e.corr_note", "e.response_request", "e.recipients", "e.filename")
        for word in search.split():
            clauses.append("(" + " OR ".join(f"{f} ILIKE %s" for f in fields) + ")")
            params += [f"%{word}%"] * len(fields)
    where = " AND ".join(clauses)
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT count(*) AS n FROM emails e WHERE {where}", params)
            total = cur.fetchone()["n"]
            cur.execute(f"""
                SELECT e.filename, e.subject, e.from_address, e.received_at, e.date_added,
                       e.gmail_link, e.info_vendor, e.info_store, e.info_note, e.recipients,
                       e.email_type, e.needs_response, e.response_request, e.response_requested_by, e.topic,
                       (SELECT count(*) FROM invoices i WHERE i.email_filename = e.filename)
                       + (SELECT count(*) FROM credit_memos c WHERE c.email_filename = e.filename)
                       + (SELECT count(*) FROM payments p WHERE p.email_filename = e.filename) AS n_records,
                       coalesce(e.corr_status, 'open') AS corr_status, e.corr_note, e.corr_by, e.corr_at,
                       e.corr_assigned_to,
                       e.corr_forwarded_to, e.corr_forwarded_at
                  FROM emails e WHERE {where}
                 ORDER BY coalesce(e.received_at, e.date_added::timestamptz) DESC NULLS LAST, e.filename
                 LIMIT %s OFFSET %s""", params + [limit, offset])
            rows = cur.fetchall()
            cur.execute("SELECT coalesce(corr_status, 'open') AS s, count(*) AS n FROM emails "
                        "WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                        "AND coalesce(status, '') <> 'fetched' GROUP BY 1")
            counts = {r["s"]: r["n"] for r in cur.fetchall()}
            counts["all"] = sum(counts.values())
            return rows, total, counts
    finally:
        conn.close()


_UNSET = object()


def correspondence_assignee_counts():
    """{username or '-' (unassigned): n} over the Follow up tab, for the assigned-to filter."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(nullif(corr_assigned_to, ''), '-'), count(*) FROM emails "
                        " WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                        "   AND coalesce(status, '') <> 'fetched' AND corr_status = 'follow_up' "
                        " GROUP BY 1")
            return dict(cur.fetchall())
    finally:
        conn.close()


def set_correspondence_status(filename, status, note, username, assigned_to=_UNSET):
    """Set one correspondence email's triage status (open/follow_up/handled/ignored) + note.
    Logged to edit_log. Re-opening clears who/when. assigned_to (Follow up only, 2026-10-19): a username,
    or '' / None to unassign; left out = keep whatever it was."""
    if status not in ("open", "follow_up", "handled", "ignored"):
        raise ValueError("bad status")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(corr_status,'open'), corr_note FROM emails WHERE filename = %s",
                        (filename,))
            row = cur.fetchone()
            if not row:
                return
            note = (note or "").strip() or (row[1] if status != "open" else row[1])
            if status == "open":
                cur.execute("UPDATE emails SET corr_status = NULL, corr_by = NULL, corr_at = NULL, "
                            "corr_note = %s WHERE filename = %s", (note, filename))
            else:
                cur.execute("UPDATE emails SET corr_status = %s, corr_note = %s, corr_by = %s, "
                            "corr_at = now() WHERE filename = %s", (status, note, username, filename))
            if status == "follow_up" and assigned_to is not _UNSET:
                cur.execute("UPDATE emails SET corr_assigned_to = %s WHERE filename = %s",
                            ((assigned_to or "").strip() or None, filename))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('emails', NULL, %s, %s, %s, %s)",
                        (f"corr_status ({filename})"[:200], row[0],
                         status + (f" -> {assigned_to}" if status == "follow_up" and assigned_to not in (_UNSET, None, "")
                                   else ""), username))
    finally:
        conn.close()


def add_correspondence_rule(sender, subject_contains, note, username, action="ignore", forward_to=None,
                            recipients_contains=None, outside_recipient_only=False, topic=None,
                            assign_to=None):
    """Create a correspondence rule. sender and/or subject_contains (both = both must match).
    action 'ignore': applied right away to every OPEN correspondence email; returns how many it ignored.
    action 'forward': forward_to required; only emails arriving from now on are forwarded (by the
    classify run) -- returns 0."""
    sender = (sender or "").strip() or None
    subject_contains = (subject_contains or "").strip() or None
    recipients_contains = (recipients_contains or "").strip() or None
    action = action if action in ("ignore", "forward", "follow_up") else "ignore"
    assign_to = ((assign_to or "").strip() or None) if action == "follow_up" else None
    forward_to = (forward_to or "").strip() or None
    topic = (topic or "").strip() or None
    if topic and topic not in database.CORR_TOPIC_KEYS:
        raise ValueError("unknown request type")
    if not sender and not subject_contains and not recipients_contains and not topic:
        raise ValueError("a rule needs a request type, sender, subject and/or To/CC text")
    if action == "forward" and not forward_to:
        raise ValueError("a forward rule needs someone to forward to")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO correspondence_rules (topic, sender, subject_contains, recipients_contains, "
                        "outside_recipient_only, action, assign_to, forward_to, note, created_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (topic, sender, subject_contains, recipients_contains, bool(outside_recipient_only), action,
                         assign_to,
                         forward_to if action == "forward" else None,
                         (note or "").strip() or None, username))
            rid = cur.fetchone()[0]
            _ref_log(cur, "correspondence_rules", rid, "action", None, action, username)
            if action in ("ignore", "follow_up"):
                return database.apply_correspondence_rules(username, cur=cur)
            return 0
    finally:
        conn.close()


def forward_contact_list():
    """[{name, email, label}] for the Forward pick-lists, by name."""
    try:
        rows = list_reference("forward_contacts")
    except Exception:
        return []
    return [{"name": r.get("name") or "", "email": r["email"],
             "label": (f"{r['name']} <{r['email']}>" if r.get("name") else r["email"])}
            for r in rows if r.get("email")]



def requeue_correspondence_as_documents(filename, learning, username):
    """'Not correspondence -- process the documents' (2026-10-15). Puts the email back in line for the
    classifier exactly like requeue_by_vendor.py does: moves its stored copy back into test_emails/ and
    sets emails.status='fetched', so the next fetch/classify run reads it again. If `learning` is given
    it's added to Learnings first, so that run (and every later one) sees it. The email drops off the
    Correspondence page meanwhile; if the run still calls it correspondence it comes back as Open, with
    the note saying it was sent back. Returns where the file was moved from."""
    import shutil
    from pathlib import Path
    base = Path(__file__).parent
    inbox = base / "test_emails"
    src = None
    for d in ("processed_emails", "review_pending"):
        cand = base / d / filename
        if cand.is_file():
            src = cand
            break
    if src is None and (inbox / filename).is_file():
        src = inbox / filename                 # already in the inbox (e.g. pressed twice)
    if src is None:
        raise ValueError("the stored copy of this email wasn't found, so it can't be reprocessed "
                         "-- open it in Gmail and forward it to the invoices inbox instead")
    if learning and learning.strip():
        add_learning_web(learning, username)
    inbox.mkdir(exist_ok=True)
    if src.parent != inbox:
        shutil.move(str(src), str(inbox / filename))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE emails SET status = 'fetched', corr_status = NULL, corr_by = NULL, corr_at = NULL, "
                        "corr_note = %s, updated_at = now() WHERE filename = %s",
                        (f"Sent back to be processed as documents by {username} "
                         f"({datetime.date.today().isoformat()})", filename))
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('emails', NULL, %s, 'correspondence', 'requeued for processing', %s)",
                        (f"requeue ({filename})"[:200], username))
    finally:
        conn.close()
    return src.parent.name



def list_correspondence_follow_ups():
    """Follow-up correspondence emails (for My work): filename, subject, from, request/summary, assignee, when."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT filename, subject, from_address, coalesce(response_request, info_note) AS what,
                                  corr_note, corr_assigned_to, corr_at
                             FROM emails
                            WHERE (email_type = 'correspondence' OR needs_response IS TRUE)
                              AND corr_status = 'follow_up' AND coalesce(status, '') <> 'fetched'
                            ORDER BY corr_at""")
            return cur.fetchall()
    finally:
        conn.close()



def correspondence_similarity():
    """[(from_address, topic, status)] for every email the Correspondence page covers (any status) -- the
    page counts, per card, how many share its sender / domain / request type (2026-10-19)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT from_address, topic, coalesce(corr_status, 'open') FROM emails "
                        "WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                        "AND coalesce(status, '') <> 'fetched'")
            return cur.fetchall()
    finally:
        conn.close()
