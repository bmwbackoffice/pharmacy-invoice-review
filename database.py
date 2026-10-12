"""
PostgreSQL storage for the pharmacy invoice pipeline.

Connects via DATABASE_URL and creates the tables if they don't exist. The
public load_*/save_* helpers deliberately speak the SAME dict shapes (keyed by
the current Excel column names) that classify_emails.py already builds, so the
processing logic there is unchanged — only where it reads/writes swaps.

Tables (schema mirrors the Excel columns documented in PROJECT_STATUS.md):
  emails, invoices, payments, credit_memos, review_queue, learnings,
  invoice_history, runs (per classify run), and the editable reference tables
  stores / vendor_name_map (incl. the exclusion flags) / sender_store_map.

Note on TLS: the Techloq ca_bundle applies to HTTPS (Anthropic + Google). This
module speaks the PostgreSQL wire protocol, not HTTPS, so it does NOT use the
bundle — put any DB TLS settings (sslmode, sslrootcert) in DATABASE_URL itself.
"""

import json
import os
import re

import psycopg2
import psycopg2.extras


def _url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set — cannot use PostgreSQL storage.")
    return url


def connect():
    return psycopg2.connect(_url())


def is_configured():
    return bool(os.environ.get("DATABASE_URL"))


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

DDL = """
CREATE TABLE IF NOT EXISTS emails (
    filename           TEXT PRIMARY KEY,
    source             TEXT,            -- 'gmail' | 'manual'
    gmail_message_id   TEXT,
    gmail_link         TEXT,
    subject            TEXT,
    from_address       TEXT,            -- sender captured at fetch time (self-scan store signal)
    received_at        TIMESTAMPTZ,     -- Gmail internalDate: AUTHORITATIVE server receive time
    email_type         TEXT,            -- (the 'Date:' header is unreliable — scanner clock skew)
    status             TEXT,            -- 'processed' | 'error' | 'review'
    error              TEXT,
    run_id             TEXT,            -- the classify run that last processed this email
    date_added         DATE,
    updated_at         TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TIMESTAMPTZ DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    n_emails     INTEGER,          -- source emails processed this run
    n_results    INTEGER,          -- result rows written this run
    n_review     INTEGER           -- items flagged to the review queue this run
);

CREATE TABLE IF NOT EXISTS invoices (
    id                 SERIAL PRIMARY KEY,
    email_filename     TEXT REFERENCES emails(filename) ON DELETE CASCADE,
    source_email       TEXT,
    date_added         DATE,
    file_path          TEXT,
    gmail_link         TEXT,
    email_type         TEXT,
    confidence         TEXT,
    needs_review       BOOLEAN,
    review_reason      TEXT,
    route_to           TEXT,
    routing_reason     TEXT,
    vendor_raw         TEXT,
    vendor_matched     TEXT,
    vendor_confidence  TEXT,
    skip_qb_import     BOOLEAN,
    store_raw          TEXT,
    store_matched      TEXT,
    store_confidence   TEXT,
    invoice_number     TEXT,
    invoice_date       TEXT,
    amount             NUMERIC,
    ship_charge        NUMERIC,
    sales_tax          NUMERIC,
    duplicate_suspect  BOOLEAN,
    duplicate_reason   TEXT,
    duplicate_ignored  BOOLEAN DEFAULT FALSE,   -- REAL duplicate: kept OUT of QB + review
    duplicate_of       TEXT,                    -- source-email id of the record it duplicates
    notes              TEXT,
    entered_in_qb      BOOLEAN DEFAULT FALSE,
    card_last4         TEXT,             -- last 4 of a card ending shown on the doc (resolves store)
    class              TEXT              -- QuickBooks class (from the doc, or a class_rule)
);

CREATE TABLE IF NOT EXISTS credit_memos (
    id                 SERIAL PRIMARY KEY,
    email_filename     TEXT REFERENCES emails(filename) ON DELETE CASCADE,
    date_added         DATE,
    file_path          TEXT,
    gmail_link         TEXT,
    email_type         TEXT,
    needs_review       BOOLEAN,
    review_reason      TEXT,
    vendor_raw         TEXT,
    vendor_matched     TEXT,
    vendor_confidence  TEXT,
    skip_qb_import     BOOLEAN,
    store_raw          TEXT,
    store_matched      TEXT,
    store_confidence   TEXT,
    credit_memo_number TEXT,
    credit_memo_date   TEXT,
    amount             NUMERIC,
    notes              TEXT,
    entered_in_qb      BOOLEAN DEFAULT FALSE,
    card_last4         TEXT,             -- last 4 of a card ending shown on the memo (resolves store)
    class              TEXT              -- QuickBooks class (from the doc, or a class_rule)
);

CREATE TABLE IF NOT EXISTS payments (
    id                 SERIAL PRIMARY KEY,
    email_filename     TEXT REFERENCES emails(filename) ON DELETE CASCADE,
    date_added         DATE,
    file_path          TEXT,
    gmail_link         TEXT,
    email_type         TEXT,
    needs_review       BOOLEAN,
    review_reason      TEXT,
    vendor_raw         TEXT,
    vendor_matched     TEXT,
    skip_qb_import     BOOLEAN,
    store_matched      TEXT,
    reference_number   TEXT,
    invoice_number     TEXT,             -- which invoice this payment was applied
                                         -- against, when the document states it
                                         -- (disambiguates a shared payment/batch ID
                                         -- applied to multiple different invoices)
    payment_date       TEXT,
    amount             NUMERIC,
    duplicate_suspect  BOOLEAN,
    duplicate_reason   TEXT,
    duplicate_ignored  BOOLEAN DEFAULT FALSE,   -- REAL duplicate payment: auto-ignored
    duplicate_of       TEXT,
    notes              TEXT,
    card_last4         TEXT,             -- last 4 of the paying card (resolves store)
    class              TEXT              -- QuickBooks class (from the doc, or a class_rule)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id                 SERIAL PRIMARY KEY,
    filename           TEXT,
    source_email       TEXT,
    date_flagged       DATE,
    flagged_at         TIMESTAMPTZ DEFAULT now(),   -- real timestamp (date_flagged is date-only)
    run_id             TEXT,                        -- the run that flagged this item
    email_type         TEXT,
    vendor_matched     TEXT,
    store_matched      TEXT,
    invoice_number     TEXT,
    amount             NUMERIC,
    question           TEXT,
    file_path          TEXT,
    gmail_link         TEXT,
    answer             TEXT,
    notes_to_learn     TEXT,
    review_key         TEXT,             -- STABLE identity (source email + vendor + number),
                                         -- survives a reprocess that changes the display filename
    duplicate_group_id TEXT              -- if this flagged record matches a recorded one,
                                         -- the shared group id (informational; set by enrich pass)
);

CREATE TABLE IF NOT EXISTS learnings (
    id                 SERIAL PRIMARY KEY,
    learned_rule       TEXT UNIQUE,
    date_added         DATE,
    from_email         TEXT
);

CREATE TABLE IF NOT EXISTS invoice_history (
    id                 SERIAL PRIMARY KEY,
    vendor_matched     TEXT,
    store_matched      TEXT,
    invoice_number     TEXT,
    invoice_date       TEXT,
    invoice_amount     NUMERIC,
    source_file        TEXT,             -- the record's own filename (for display/link)
    source_email_id    TEXT,             -- STABLE source-email identity (self-match key)
    date_added         DATE
);

CREATE TABLE IF NOT EXISTS payment_history (
    id                 SERIAL PRIMARY KEY,
    vendor_matched     TEXT,
    store_matched      TEXT,
    reference_number   TEXT,             -- check/txn/card ref (dedup key with vendor)
    invoice_number     TEXT,             -- invoice this payment settled (disambiguates a shared ref)
    payment_date       TEXT,
    amount             NUMERIC,
    source_file        TEXT,
    source_email_id    TEXT,
    date_added         DATE
);

-- Sales orders / order acknowledgements held for a maturity window: NOT recorded
-- immediately — they wait until a formal invoice for the same vendor + order number
-- supersedes them, or the hold elapses and they record themselves. snapshot holds the
-- full record needed to record it later. SHARED (CLI writes + reconciles, web reads).
CREATE TABLE IF NOT EXISTS pending_sales_orders (
    id               SERIAL PRIMARY KEY,
    vendor_matched   TEXT,
    store_matched    TEXT,
    number           TEXT,            -- the order / (later) invoice number
    amount           NUMERIC,
    order_date       TEXT,
    source_email     TEXT,
    source_file      TEXT,
    file_path        TEXT,
    gmail_link       TEXT,
    snapshot         JSONB,           -- the full record/result to record it on release
    held_at          TIMESTAMPTZ DEFAULT now(),
    release_after    TIMESTAMPTZ,     -- held_at + SALES_ORDER_HOLD_DAYS
    status           TEXT DEFAULT 'held',   -- 'held' | 'released' | 'superseded'
    matched_invoice  TEXT             -- the invoice number that superseded it (if any)
);
CREATE INDEX IF NOT EXISTS ix_pending_sales_orders_status ON pending_sales_orders (status);

-- PDFs/images a reviewer uploads to a review item (e.g. hand-extracted from a zip the
-- pipeline couldn't open). Keyed by the review item's split filename (stable, like
-- review_notes). The web app inserts these; the CLI reads UNCONSUMED rows and appends
-- them to the reprocess of that item, then stamps consumed_at. SHARED (web writes,
-- CLI reads + consumes) — hence in the base DDL, not the web-only schema.
CREATE TABLE IF NOT EXISTS manual_attachments (
    id              SERIAL PRIMARY KEY,
    review_filename TEXT NOT NULL,       -- STABLE review_key value (source email+vendor+number);
                                         -- column name is historical (predates review_key)
    file_path       TEXT NOT NULL,       -- where the uploaded PDF/image was saved on disk
    original_name   TEXT,
    uploaded_by     TEXT,
    uploaded_at     TIMESTAMPTZ DEFAULT now(),
    consumed_at     TIMESTAMPTZ          -- set once a reprocess has used it
);
CREATE INDEX IF NOT EXISTS ix_manual_attachments_key ON manual_attachments (review_filename);

-- Statement reconciliation state (persistent, so lines re-check across runs). When a
-- vendor statement/aging is processed, the invoices it LISTS are ingested here (one row
-- per listed invoice), then each is cross-checked against what we actually recorded.
-- Statements record NO financial record themselves — this is reconciliation metadata.
-- SHARED (CLI ingests + reconciles; web surfaces exceptions + dismisses). status:
-- 'pre_cutoff' (dated before go-live+grace — never flagged) | 'matched' | 'missing' |
-- 'amount_mismatch' | 'in_flight' (in review/held — counts as present) | 'dismissed'.
CREATE TABLE IF NOT EXISTS statement_lines (
    id                SERIAL PRIMARY KEY,
    statement_email   TEXT,             -- stable source-email identity of the statement
    statement_file    TEXT,             -- the statement's filename (dedup key + preview)
    vendor            TEXT,
    invoice_number    TEXT,
    invoice_date      DATE,
    amount            NUMERIC,
    status            TEXT DEFAULT 'missing',
    recorded_amount   NUMERIC,          -- the amount we actually recorded, when matched
    first_seen_run    TEXT,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now(),
    dismissed_by      TEXT,
    dismissed_at      TIMESTAMPTZ,
    assigned_to       TEXT              -- who should work this statement's exceptions (Review queue)
);
-- Dedupe a statement's lines by file + normalized invoice number (re-ingest updates in place).
CREATE UNIQUE INDEX IF NOT EXISTS ux_statement_lines_key
    ON statement_lines (statement_file, (upper(btrim(invoice_number))));
CREATE INDEX IF NOT EXISTS ix_statement_lines_status ON statement_lines (status);
CREATE INDEX IF NOT EXISTS ix_statement_lines_lookup
    ON statement_lines ((upper(btrim(vendor))), (upper(btrim(invoice_number))));

-- Editable reference tables (curated in the web UI, read by the pipeline each
-- run so edits take effect next run with no code change). Audit columns mirror
-- the Learnings pattern; the web helpers also write to edit_log. The CLI only
-- READS these, so ids stay stable.
CREATE TABLE IF NOT EXISTS stores (
    id                 SERIAL PRIMARY KEY,
    store_name         TEXT UNIQUE NOT NULL,
    aliases            TEXT,            -- aka/DBA/legal-name clause (shown in parens)
    address            TEXT,
    notes              TEXT,            -- extra disambiguation guidance for this store
    sort_order         INTEGER DEFAULT 0,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    edited_by          TEXT,
    edited_at          TIMESTAMPTZ
);

-- (The flat excluded_vendors / statement_recon_excluded_vendors lists used to be created here.
-- Their settings moved to vendor_name_map's exclude_from_qb / exclude_from_statement_recon flags on
-- 2026-09-24 and nothing read them after that; on 2026-10-08 they were renamed to
-- archived_excluded_vendors / archived_statement_recon_excluded_vendors (data kept — see MIGRATIONS)
-- and are no longer created.)

-- Vendor / account name STANDARDIZATION: one canonical name per vendor/account plus each
-- entity's own spelling of it, so bills are coded to consistent QuickBooks vendor/account
-- names regardless of how the source document spelled the vendor. 7 store columns
-- (5 pharmacies + BMW + Lakewood Rx). Curated in the Setup UI; the classifier reads it each run.
CREATE TABLE IF NOT EXISTS vendor_name_map (
    id                   SERIAL PRIMARY KEY,
    standard_vendor_name TEXT UNIQUE NOT NULL,
    aliases              TEXT,   -- other spellings/variants, one per line, not tied to any store
    name_3m              TEXT,
    name_madison         TEXT,
    name_maple           TEXT,
    name_refuah          TEXT,
    name_westgate        TEXT,
    name_bmw             TEXT,
    name_lakewood_rx     TEXT,
    created_by           TEXT,
    created_at           TIMESTAMPTZ DEFAULT now(),
    edited_by            TEXT,
    edited_at            TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS account_name_map (
    id                     SERIAL PRIMARY KEY,
    standard_account_name  TEXT UNIQUE NOT NULL,
    name_3m                TEXT,
    name_madison           TEXT,
    name_maple             TEXT,
    name_refuah            TEXT,
    name_westgate          TEXT,
    name_bmw               TEXT,
    name_lakewood_rx       TEXT,
    created_by             TEXT,
    created_at             TIMESTAMPTZ DEFAULT now(),
    edited_by              TEXT,
    edited_at              TIMESTAMPTZ
);

-- Which standard account each standard vendor's bills get coded to.
CREATE TABLE IF NOT EXISTS vendor_account_assignment (
    id                     SERIAL PRIMARY KEY,
    standard_vendor_name   TEXT UNIQUE NOT NULL,
    standard_account_name  TEXT NOT NULL,
    created_by             TEXT,
    created_at             TIMESTAMPTZ DEFAULT now(),
    edited_by              TEXT,
    edited_at              TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sender_store_map (
    id                 SERIAL PRIMARY KEY,
    sender_email       TEXT UNIQUE NOT NULL,
    store_matched      TEXT,
    note               TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    edited_by          TEXT,
    edited_at          TIMESTAMPTZ
);

-- The MIRROR of sender_store_map keyed on a RECIPIENT (To/Cc) address: a third-party
-- vendor emails US and the giveaway is who it's addressed to (e.g. a biller emailing a
-- specific person/mailbox at a specific store). Editable in the web UI; the CLI reads it
-- each run to decisively set the store from a To/Cc match. Distinct from sender_store_map,
-- which is for OUR OWN self-scan mailboxes (the From address).
CREATE TABLE IF NOT EXISTS contact_store_map (
    id                 SERIAL PRIMARY KEY,
    contact_email      TEXT UNIQUE NOT NULL,
    store_matched      TEXT,
    note               TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    edited_by          TEXT,
    edited_at          TIMESTAMPTZ
);

-- A paying card's last 4 digits usually identify the store. Editable in the web
-- UI (Cards tab / reference editor); the CLI reads it each run to auto-assign the
-- store on a card payment and NOT flag it merely because the store was uncertain.
CREATE TABLE IF NOT EXISTS card_store_map (
    id                 SERIAL PRIMARY KEY,
    card_last4         TEXT UNIQUE NOT NULL,
    store_matched      TEXT,
    note               TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    edited_by          TEXT,
    edited_at          TIMESTAMPTZ
);

-- Default QuickBooks "class" by store and/or vendor. Editable in the web UI; read by
-- the pipeline each run to FILL IN a record's class when the email/document didn't state
-- one (an explicit class always wins). store_matched/vendor_matched NULL = "any".
CREATE TABLE IF NOT EXISTS class_rules (
    id                 SERIAL PRIMARY KEY,
    store_matched      TEXT,            -- NULL/blank = any store
    vendor_matched     TEXT,            -- NULL/blank = any vendor
    class              TEXT NOT NULL,   -- the class to apply
    note               TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    edited_by          TEXT,
    edited_at          TIMESTAMPTZ
);

-- Vendor-scoped store rules (Setup › Store rules), enforced in code by
-- classify_emails.apply_scoped_learning_gate. Standing learnings reach the model on EVERY email, so
-- a rule meant for one vendor ("Carepax = BMW on Sageteq bills") can leak onto another vendor's
-- invoice; each row here sends such a record to review instead. applies_to:
--   'only these vendors'        -> <reference> means <store> only for `vendors`: a record resolved
--                                  to <store> from a <reference> mention, from any OTHER vendor, is flagged.
--   'every vendor except these' -> <reference> means <store> for every vendor but `vendors`: a record
--                                  mentioning <reference> but resolved to a DIFFERENT store is flagged.
-- reference is matched in the record's store_raw (billed-to / ship-to text) ignoring case and spacing;
-- vendors: one per line, matched as part of the vendor name.
CREATE TABLE IF NOT EXISTS store_scope_rules (
    id                 SERIAL PRIMARY KEY,
    reference          TEXT NOT NULL,
    store_matched      TEXT,            -- a rule without a store is ignored
    applies_to         TEXT DEFAULT 'only these vendors',
    vendors            TEXT,
    note               TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    edited_by          TEXT,
    edited_at          TIMESTAMPTZ
);

-- Sitewide maintenance banner: warn active users before a disruptive op (code restart, or a
-- bulk reprocess/recovery that overwrites existing live records). Only the LATEST still-active
-- row matters (active_until NULL = until explicitly cleared); no history view needed.
CREATE TABLE IF NOT EXISTS system_notices (
    id                 SERIAL PRIMARY KEY,
    message            TEXT NOT NULL,
    created_at         TIMESTAMPTZ DEFAULT now(),
    active_until       TIMESTAMPTZ,      -- NULL = shown until explicitly cleared
    created_by         TEXT
);

-- "Entered in QuickBooks" outlives a reprocess. save_results replaces an email's records by
-- deleting and re-inserting them, and the inserts don't carry entered_in_qb. Before the delete,
-- every entered row is copied here; after the insert, an open mark is matched to the re-recorded
-- record (same type + vendor + number + amount) and its entered status is put back. A mark with no
-- match yet (the record went to review, or was read differently) stays open and re-attaches
-- whenever that record is recorded again, so it can't come back as "not entered" and be keyed twice.
CREATE TABLE IF NOT EXISTS qb_entered_marks (
    id                 SERIAL PRIMARY KEY,
    record_type        TEXT NOT NULL,    -- invoice | credit_memo | payment
    vendor_key         TEXT,             -- norm_vendor_dedupe(vendor_matched)
    number_key         TEXT,             -- upper(btrim(number)), '' when none
    amount_key         NUMERIC,          -- round(amount, 2)
    date_key           TEXT,             -- the record's own date; only used when there's no number
    vendor_matched     TEXT,
    number             TEXT,
    amount             NUMERIC,
    entered_qb_by      TEXT,
    entered_qb_at      TIMESTAMPTZ,
    old_record_id      INTEGER,          -- the row the reprocess deleted
    email_filename     TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    restored_record_id INTEGER,          -- the re-recorded row that got the status back
    restored_at        TIMESTAMPTZ,      -- NULL = still open
    restore_note       TEXT
);
CREATE INDEX IF NOT EXISTS qb_entered_marks_open ON qb_entered_marks (record_type, vendor_key)
    WHERE restored_at IS NULL;

-- QuickBooks reconciliation import (qb_match.py): a user uploads a QB register export before a QB
-- data-entry session; the app checks each app record against it to avoid double entry, and resolves
-- stale 'missing' statement flags that QB shows aren't actually missing. One batch per upload.
CREATE TABLE IF NOT EXISTS qb_import_batches (
    id                   SERIAL PRIMARY KEY,
    filename             TEXT,
    uploaded_by          TEXT,
    uploaded_at          TIMESTAMPTZ DEFAULT now(),
    n_rows               INTEGER DEFAULT 0,
    n_auto_matched       INTEGER DEFAULT 0,
    n_doubtful           INTEGER DEFAULT 0,
    n_statement_resolved INTEGER DEFAULT 0,
    n_no_match           INTEGER DEFAULT 0
);
-- Tier-2 (doubtful) matches needing a human: QB-side fields frozen next to the candidate record id.
CREATE TABLE IF NOT EXISTS qb_match_candidates (
    id                SERIAL PRIMARY KEY,
    batch_id          INTEGER REFERENCES qb_import_batches(id) ON DELETE CASCADE,
    record_type       TEXT,     -- 'invoice' | 'credit_memo'
    record_id         INTEGER,
    qb_type           TEXT,
    qb_date           DATE,
    qb_num            TEXT,
    qb_name           TEXT,
    qb_amount         NUMERIC,
    qb_store          TEXT,
    confidence_reason TEXT,     -- which tier-2 rule matched, human-readable
    status            TEXT DEFAULT 'pending',  -- pending | confirmed | rejected
    decided_by        TEXT,
    decided_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qb_match_candidates_pending ON qb_match_candidates (status, batch_id);

-- QB export rows that matched NOTHING at upload time (no candidate at all). Frozen here so a
-- later-arriving email can be matched to them on arrival (resolve_unmatched_for_new_records) or
-- by the recheck button, reusing the same Tier-1 vendor+number+amount+store logic.
CREATE TABLE IF NOT EXISTS qb_unmatched_rows (
    id                   SERIAL PRIMARY KEY,
    batch_id             INTEGER REFERENCES qb_import_batches(id) ON DELETE CASCADE,
    qb_type              TEXT,     -- 'Bill' | 'Credit'
    qb_date              DATE,
    qb_num               TEXT,
    qb_name              TEXT,
    qb_std_vendor        TEXT,     -- resolved standard vendor AT PARSE TIME (frozen, like
                                   -- qb_match_candidates freezes its qb-side fields)
    qb_amount            NUMERIC,
    qb_store             TEXT,
    status               TEXT DEFAULT 'pending',  -- pending | resolved
    resolved_record_type TEXT,
    resolved_record_id   INTEGER,
    resolved_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qb_unmatched_rows_pending
    ON qb_unmatched_rows (status, qb_store);
"""

# Migrations for databases created before a column/table existed. Safe to run
# every startup (all IF NOT EXISTS). Kept separate from DDL so ordering is clear.
# Existing rows get NULL run_id/flagged_at (pre-tracking) — nothing breaks.
MIGRATIONS = """
ALTER TABLE emails        ADD COLUMN IF NOT EXISTS from_address TEXT;
ALTER TABLE emails        ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE emails        ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;
ALTER TABLE review_queue  ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE review_queue  ADD COLUMN IF NOT EXISTS flagged_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE review_queue  ADD COLUMN IF NOT EXISTS review_key TEXT;
ALTER TABLE invoices      ADD COLUMN IF NOT EXISTS duplicate_ignored BOOLEAN DEFAULT FALSE;
ALTER TABLE invoices      ADD COLUMN IF NOT EXISTS duplicate_of TEXT;
ALTER TABLE invoice_history ADD COLUMN IF NOT EXISTS source_email_id TEXT;
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS duplicate_suspect BOOLEAN;
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS duplicate_reason TEXT;
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS duplicate_ignored BOOLEAN DEFAULT FALSE;
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS duplicate_of TEXT;
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS card_last4 TEXT;
ALTER TABLE invoices      ADD COLUMN IF NOT EXISTS card_last4 TEXT;
ALTER TABLE credit_memos  ADD COLUMN IF NOT EXISTS card_last4 TEXT;
ALTER TABLE runs          ADD COLUMN IF NOT EXISTS learnings_hash TEXT;
ALTER TABLE invoices      ADD COLUMN IF NOT EXISTS class TEXT;
ALTER TABLE credit_memos  ADD COLUMN IF NOT EXISTS class TEXT;
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS class TEXT;
-- Shared duplicate GROUP id: a short human-typeable tag stamped on EVERY record in a matched
-- duplicate cluster (the original AND every later match), independent of duplicate_suspect/
-- duplicate_ignored — a pure "these are related" tag to filter/click through. Written by the
-- CLI (apply_duplicate_checks + history round-trip), so it lives in the base DDL.
ALTER TABLE invoices        ADD COLUMN IF NOT EXISTS duplicate_group_id TEXT;
ALTER TABLE payments        ADD COLUMN IF NOT EXISTS duplicate_group_id TEXT;
ALTER TABLE invoice_history ADD COLUMN IF NOT EXISTS duplicate_group_id TEXT;
ALTER TABLE payment_history ADD COLUMN IF NOT EXISTS duplicate_group_id TEXT;
ALTER TABLE review_queue    ADD COLUMN IF NOT EXISTS duplicate_group_id TEXT;
-- Vendor/account standardization results, stamped per-record by apply_vendor_standardization():
-- the canonical vendor/account, and the store-specific spelling used for QuickBooks entry.
-- Payments intentionally excluded (no expense account in QB; not on /qb).
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS standard_vendor TEXT;
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS standard_account TEXT;
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS qb_vendor TEXT;
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS qb_account TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS standard_vendor TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS standard_account TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS qb_vendor TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS qb_account TEXT;
ALTER TABLE vendor_name_map ADD COLUMN IF NOT EXISTS aliases TEXT;
-- Unified exclusion flags (merged from the former excluded_vendors /
-- statement_recon_excluded_vendors flat tables). exclude_from_qb => skip_qb_import;
-- exclude_from_statement_recon => silence statement-recon "missing" noise only.
ALTER TABLE vendor_name_map ADD COLUMN IF NOT EXISTS exclude_from_qb BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE vendor_name_map ADD COLUMN IF NOT EXISTS exclude_from_statement_recon BOOLEAN NOT NULL DEFAULT FALSE;
-- Vendors whose bills only list line items (no printed total): when ticked, an amount the model
-- added up from the line items is accepted instead of sent to review. Off for every vendor until
-- someone ticks it in Setup › Vendor names (2026-10-08). A missing/zero amount is still flagged.
ALTER TABLE vendor_name_map ADD COLUMN IF NOT EXISTS trust_line_item_totals BOOLEAN NOT NULL DEFAULT FALSE;
-- Which invoice a payment settles, when the remittance states it. Disambiguates a shared
-- payment/batch reference_number applied to several different invoices in one document.
ALTER TABLE payments        ADD COLUMN IF NOT EXISTS invoice_number TEXT;
ALTER TABLE payment_history ADD COLUMN IF NOT EXISTS invoice_number TEXT;
-- Manual per-record "Ignore" (exclude-from-QB) audit stamps. Distinguishes a human's manual
-- ignore (ignored_by set) from an automatic excluded-vendor skip (skip_qb_import TRUE, ignored_by
-- NULL) so the UI offers Restore only for the manual case. Mirrors entered_qb_by/_at.
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS ignored_by TEXT;
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS ignored_at TIMESTAMPTZ;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS ignored_by TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS ignored_at TIMESTAMPTZ;
ALTER TABLE payments     ADD COLUMN IF NOT EXISTS ignored_by TEXT;
ALTER TABLE payments     ADD COLUMN IF NOT EXISTS ignored_at TIMESTAMPTZ;
-- Where the amount came from, as the model reported it: 'stated' (a printed Total / Amount Due /
-- Balance) or 'derived' (summed from line items — the amount check sends those to review). NULL when
-- the model didn't say, and on every record saved before 2026-10-08.
ALTER TABLE invoices     ADD COLUMN IF NOT EXISTS total_source TEXT;
ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS total_source TEXT;
ALTER TABLE payments     ADD COLUMN IF NOT EXISTS total_source TEXT;
-- Who should work a statement's open exceptions (grouped as one Review-queue row). statement_lines
-- isn't wiped per run (unlike review_queue), so the assignment persists with no carry-over logic.
ALTER TABLE statement_lines ADD COLUMN IF NOT EXISTS assigned_to TEXT;
-- Archive the two retired flat exclusion lists (2026-10-08): rename, keeping their rows. Idempotent and
-- never fails — skipped when already archived, and if an old copy of this code re-created an empty
-- table after the rename, both are simply left alone.
DO $archive$
BEGIN
  IF to_regclass('public.excluded_vendors') IS NOT NULL
     AND to_regclass('public.archived_excluded_vendors') IS NULL THEN
    ALTER TABLE public.excluded_vendors RENAME TO archived_excluded_vendors;
  END IF;
  IF to_regclass('public.statement_recon_excluded_vendors') IS NOT NULL
     AND to_regclass('public.archived_statement_recon_excluded_vendors') IS NULL THEN
    ALTER TABLE public.statement_recon_excluded_vendors RENAME TO archived_statement_recon_excluded_vendors;
  END IF;
END
$archive$;
"""


# --------------------------------------------------------------------------- #
# Seed data for the reference tables (also the Excel-mode fallback + the single
# source the classifier rebuilds the prompt from). Seeded on first run ONLY if
# a table is empty, so hand-curation in the UI is never overwritten.
# --------------------------------------------------------------------------- #

DEFAULT_STORES = [
    {"store_name": "Maple Pharmacy",
     "aliases": 'aka "Maple Monsey", "Maple Vitamins"; legal name Maple Rx NY LLC',
     "address": "59 Route 59 Ste 143, Monsey, NY 10952",
     "notes": ('This is the Maple Pharmacy store/operating address; the email domain '
               'maplerx.com and "Maple Vitamins" also belong to Maple Pharmacy. Do not '
               "confuse 59 Route 59 Ste 143 (Maple's store) with 382 Route 59 Ste 276 "
               "(the shared BMW back-office address described below).")},
    {"store_name": "Madison Pharmacy",
     "aliases": "legal name Madison Rx LLC",
     "address": "400 Madison Ave, Lakewood, NJ 08701",
     "notes": ""},
    {"store_name": "Refuah Pharmacy",
     "aliases": "legal name Refuah Rx LLC",
     "address": "911 E County Line Rd, Lakewood, NJ 08701",
     "notes": ""},
    {"store_name": "Westgate Pharmacy",
     "aliases": "legal name Maple Rx LLC",
     "address": "112 Hillside Blvd, Lakewood, NJ 08701",
     "notes": ""},
    {"store_name": "3M Pharmacy",
     "aliases": 'aka "3M Remedies", "Maple NJ"; legal name Maple Rx NJ LLC, formerly 3M Rx LLC',
     "address": "1091 River Ave, Lakewood, NJ 08701",
     "notes": ('This entity is sometimes also DBA\'d as "Maple Pharmacy" — if an invoice '
               'just says "Maple Pharmacy" with NO address, check for the address first '
               "(382 Route 59 = Maple Pharmacy/NY; 1091 River Ave = 3M Pharmacy/NJ); if "
               "there's truly no address to disambiguate, set store_confidence to low and "
               "needs_human_review to true rather than guessing.")},
    {"store_name": "BMW",
     "aliases": "legal name BMW Technologies LLC",
     "address": "382 Route 59 Ste 276, Airmont, NY 10952",
     "notes": ("This is the SAME office address as Maple Pharmacy. It's the group's shared "
               "back-office/billing address, and it is NORMAL and EXPECTED for it to appear "
               "as the BILL-TO address on invoices for ANY of the six stores, regardless of "
               "which store the invoice is actually for. Seeing 382 Route 59 in Bill-To is "
               "not a mismatch and should NOT by itself cast doubt on the store match or "
               "lower store_confidence — it does not mean the invoice belongs to Maple "
               "Pharmacy or BMW. Determine store_matched from the store name actually stated "
               "on the invoice first; if that's ambiguous or missing, use the ship-to address "
               "(which is more likely to reflect the real operating location) rather than the "
               "382 Route 59 bill-to address.")},
    {"store_name": "Lakewood Rx",
     "aliases": "",
     "address": "",
     "notes": ("The entity Sageteq lists as the real billed party just below the Bill To box; "
               "see the Sageteq learning. Address/aliases can be filled in later via the "
               "Stores tab.")},
]

# Standing Learnings applied on every init (see _apply_standing_rules). Injected
# into the model each run and editable in the Learnings tab.
_SAGETEQ_LEARNING = (
    "Sageteq invoices ONLY (vendor Sageteq, 1885 Swarthmore Ave, Lakewood NJ 08701): Sageteq "
    "always addresses the 'Bill To' box to Madison Pharmacy, so the Bill To box must NOT be used "
    "to determine the store. Instead determine store_matched from the real billed entity name "
    "printed just BELOW the Bill To box (often highlighted). Apply: (a) if no entity is listed "
    "below the box, store_matched = Madison Pharmacy; (b) if that entity is 'Carepax', "
    "store_matched = BMW; (c) if that entity is 'Lakewood Rx', store_matched = Lakewood Rx; "
    "(d) if it clearly names one of the other known pharmacies, use that pharmacy; (e) if an "
    "entity is listed that does not clearly match any of these, set needs_human_review = true and "
    "explain the unrecognized Sageteq billed entity in review_reason. This rule applies ONLY to "
    "invoices from Sageteq."
)

DEFAULT_EXCLUDED_VENDORS = [
    "Smith Drug Company", "McKesson", "Parmed", "James RX", "JAMS RX", "Masters",
    "SmartSource", "Smartsource", "SmartSource (ABC)", "Anda", "Anda New",
    "BluPax", "BluPax Pharma", "NDC Distributors", "NDC Distributors LLC",
    "Prescription Supply", "Auburn", "NuMed", "Top RX", "AmerisourceBergen",
]

# Vendors whose STATEMENT lines are never flagged missing/mismatch (statement-recon noise
# only). Separate from DEFAULT_EXCLUDED_VENDORS — does NOT affect QuickBooks entry.
DEFAULT_STATEMENT_RECON_EXCLUDED_VENDORS = [
    "Jersey Central Power and Light",
]

# Self-scan mailboxes → the store they belong to. When an email arrives from one
# of these, that store is a STRONG (decisive) signal to the classifier.
DEFAULT_SENDER_STORE_MAP = [
    ("scanner@maplerx.com", "Maple Pharmacy"),
    ("refuahscanner@lakewoodrx.com", "Refuah Pharmacy"),
    ("madisonscanner@lakewoodrx.com", "Madison Pharmacy"),
    ("westgatescanner@lakewoodrx.com", "Westgate Pharmacy"),
    ("3m-scanner@lakewoodrx.com", "3M Pharmacy"),
]

# Recipient (To/Cc) address → the store it identifies, for third-party senders (billers/
# vendors emailing US). Mirror of DEFAULT_SENDER_STORE_MAP, keyed on the recipient.
DEFAULT_CONTACT_STORE_MAP = [
    ("yossi@thevitamindesk.com", "Maple Pharmacy"),
    ("vitamins@maplerx.com", "Maple Pharmacy"),
]

# Card last-4 → store. Seeded empty: cards are discovered from real payments and
# curated in the Cards tab (there's no safe default set of card numbers).
DEFAULT_CARD_STORE_MAP = []

# QuickBooks class defaults by store/vendor. Seeded EMPTY (site-specific); curated in
# the web UI. Each: {"store_matched": str|None, "vendor_matched": str|None, "class": str}.
DEFAULT_CLASS_RULES = []

# Vendor-scoped store rules (store_scope_rules — see the DDL). The first is the Carepax→BMW guard
# that used to be hard-coded; the other two mirror the Accuscript and Braun learnings (2026-10-08).
STORE_RULE_ONLY = "only these vendors"
STORE_RULE_EXCEPT = "every vendor except these"
DEFAULT_STORE_SCOPE_RULES = [
    {"reference": "Carepax", "store_matched": "BMW", "applies_to": STORE_RULE_ONLY,
     "vendors": "Sageteq\nMicrosoft",
     "note": "Sageteq Bill-To learning + Microsoft 365 learning. 'Carepax' is not itself a store."},
    {"reference": "Accuscript", "store_matched": "Lakewood Rx", "applies_to": STORE_RULE_ONLY,
     "vendors": "East Coast Building Maintenance",
     "note": "Accuscript LLC = Lakewood Rx on East Coast Building Maintenance invoices only (learning)."},
    {"reference": "112 Hillside", "store_matched": "Westgate Pharmacy", "applies_to": STORE_RULE_EXCEPT,
     "vendors": "Braun",
     "note": "112 Hillside Blvd is Westgate's address. Braun bills every location there (learning)."},
]


def init_db():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(MIGRATIONS)
            _seed_reference_tables(cur)
            _apply_standing_rules(cur)
        conn.commit()
    finally:
        conn.close()


# SQL mirror of classify_emails._norm_vendor_dedupe() — lowercase + strip trailing legal-entity
# suffixes (pllc/llc/llp/lllp/inc/incorporated/corp/corporation/co/company/pc/ltd/cpa/cpas), from
# the END only, punctuation-tolerant, possibly repeated ("Co., Inc."). Lets SQL compare
# norm_vendor_dedupe(a) = norm_vendor_dedupe(b) instead of lower(a) = lower(b), so a "same vendor,
# different legal suffix" pair matches consistently everywhere — the SAME way check_duplicate() does
# in Python. Validated byte-identical to the Python normalizer over all live vendor_matched values
# (0/282 disagreements, 2026-09-24) before being wired into any query. IMMUTABLE so it can back an
# expression index if a hot path ever needs one.
_NORM_VENDOR_FN_DDL = r"""
CREATE OR REPLACE FUNCTION norm_vendor_dedupe(v text) RETURNS text AS $BODY$
DECLARE
  name text := lower(btrim(coalesce(v, '')));
  m text[];
  tok text;
BEGIN
  LOOP
    IF name = '' THEN EXIT; END IF;
    m := regexp_match(name, '[[:space:],]+([a-z.]+)$');
    IF m IS NULL THEN EXIT; END IF;
    tok := replace(m[1], '.', '');
    IF tok NOT IN ('pllc','llc','llp','lllp','inc','incorporated','corp',
                   'corporation','co','company','pc','ltd','cpa','cpas') THEN
      EXIT;
    END IF;
    name := regexp_replace(name, '[[:space:],]+[a-z.]+$', '');
    name := regexp_replace(name, '[ ,.]+$', '');
  END LOOP;
  RETURN name;
END;
$BODY$ LANGUAGE plpgsql IMMUTABLE;
"""

# Every "spelling" of every EXCLUDED vendor, unnested from vendor_name_map — the standard name,
# each per-store spelling, and each newline-separated alias — carrying that row's two exclusion
# flags. This is the single source the exclusion reads use (load_excluded_vendors, the SQL
# _excluded_vendor_sql filter, statement-recon), so a vendor is excluded by ANY of its spellings
# (Joel's decision 2026-09-24: match standard name + aliases + per-store spellings, not just the
# standard name). Blank spellings are dropped; only rows with at least one flag set are emitted.
_EXCLUSION_VIEW_DDL = r"""
CREATE OR REPLACE VIEW vendor_exclusion_spellings AS
SELECT btrim(s.name) AS name, vm.exclude_from_qb, vm.exclude_from_statement_recon
  FROM vendor_name_map vm
  CROSS JOIN LATERAL (
        SELECT vm.standard_vendor_name AS name
  UNION ALL SELECT vm.name_3m
  UNION ALL SELECT vm.name_madison
  UNION ALL SELECT vm.name_maple
  UNION ALL SELECT vm.name_refuah
  UNION ALL SELECT vm.name_westgate
  UNION ALL SELECT vm.name_bmw
  UNION ALL SELECT vm.name_lakewood_rx
  UNION ALL SELECT x FROM regexp_split_to_table(coalesce(vm.aliases, ''), E'\n') AS x
  ) s(name)
 WHERE (vm.exclude_from_qb OR vm.exclude_from_statement_recon)
   AND s.name IS NOT NULL AND btrim(s.name) <> '';
"""


def _apply_standing_rules(cur):
    """Idempotent additions applied on EVERY init (unlike _seed_reference_tables,
    which only fills empty tables) so existing curated DBs pick them up too. Each is
    ON CONFLICT DO NOTHING, so re-running never clobbers hand edits or duplicates.
    Add new standing rules here rather than editing the seed data (which won't reach
    already-populated tables)."""
    # SQL vendor normalizer used by the duplicate-matching queries (see constant above).
    cur.execute(_NORM_VENDOR_FN_DDL)
    # Unified vendor-exclusion spellings view (reads the vendor_name_map flags).
    cur.execute(_EXCLUSION_VIEW_DDL)
    # A 7th selectable store. Fresh installs get the richer DEFAULT_STORES row; an
    # existing DB (stores already populated, so the seed skipped) gets this bare row
    # here — %%STORE_LINES%% then includes it every run. DO NOTHING keeps the seed row.
    cur.execute(
        "INSERT INTO stores (store_name, created_by) VALUES ('Lakewood Rx', 'web:rule') "
        "ON CONFLICT (store_name) DO NOTHING"
    )
    # The Sageteq Bill-To disambiguation rule, injected into the model every run and
    # editable in the Learnings tab (learned_rule is UNIQUE).
    cur.execute(
        "INSERT INTO learnings (learned_rule, date_added, from_email) "
        "VALUES (%s, CURRENT_DATE, 'web:rule') ON CONFLICT (learned_rule) DO NOTHING",
        (_SAGETEQ_LEARNING,),
    )


def _seed_reference_tables(cur):
    """Populate the reference tables on first run ONLY when empty, so the
    pipeline works before any hand-curation and curation is never clobbered."""
    cur.execute("SELECT count(*) FROM stores")
    if cur.fetchone()[0] == 0:
        for i, s in enumerate(DEFAULT_STORES):
            cur.execute(
                "INSERT INTO stores (store_name, aliases, address, notes, sort_order, created_by) "
                "VALUES (%s,%s,%s,%s,%s,'seed') ON CONFLICT (store_name) DO NOTHING",
                (s["store_name"], s["aliases"], s["address"], s["notes"], i),
            )
    # Exclusions are vendor_name_map boolean flags (the old flat excluded_vendors /
    # statement_recon_excluded_vendors tables were archived 2026-10-08). Seed the defaults as flagged vnm rows
    # on a fresh DB (guarded so an already-curated/migrated DB is never touched).
    cur.execute("SELECT count(*) FROM vendor_name_map WHERE exclude_from_qb IS TRUE")
    if cur.fetchone()[0] == 0:
        for v in DEFAULT_EXCLUDED_VENDORS:
            cur.execute(
                "INSERT INTO vendor_name_map (standard_vendor_name, exclude_from_qb, created_by) "
                "VALUES (%s, TRUE, 'seed') "
                "ON CONFLICT (standard_vendor_name) DO UPDATE SET exclude_from_qb = TRUE",
                (v,),
            )
    cur.execute("SELECT count(*) FROM vendor_name_map WHERE exclude_from_statement_recon IS TRUE")
    if cur.fetchone()[0] == 0:
        for v in DEFAULT_STATEMENT_RECON_EXCLUDED_VENDORS:
            cur.execute(
                "INSERT INTO vendor_name_map (standard_vendor_name, exclude_from_statement_recon, created_by) "
                "VALUES (%s, TRUE, 'seed') "
                "ON CONFLICT (standard_vendor_name) DO UPDATE SET exclude_from_statement_recon = TRUE",
                (v,),
            )
    cur.execute("SELECT count(*) FROM sender_store_map")
    if cur.fetchone()[0] == 0:
        for email, store in DEFAULT_SENDER_STORE_MAP:
            cur.execute(
                "INSERT INTO sender_store_map (sender_email, store_matched, created_by) "
                "VALUES (%s,%s,'seed') ON CONFLICT (sender_email) DO NOTHING",
                (email, store),
            )
    cur.execute("SELECT count(*) FROM contact_store_map")
    if cur.fetchone()[0] == 0:
        for email, store in DEFAULT_CONTACT_STORE_MAP:
            cur.execute(
                "INSERT INTO contact_store_map (contact_email, store_matched, created_by) "
                "VALUES (%s,%s,'seed') ON CONFLICT (contact_email) DO NOTHING",
                (email, store),
            )
    cur.execute("SELECT count(*) FROM card_store_map")
    if cur.fetchone()[0] == 0:
        for last4, store in DEFAULT_CARD_STORE_MAP:
            cur.execute(
                "INSERT INTO card_store_map (card_last4, store_matched, created_by) "
                "VALUES (%s,%s,'seed') ON CONFLICT (card_last4) DO NOTHING",
                (last4, store),
            )
    cur.execute("SELECT count(*) FROM class_rules")
    if cur.fetchone()[0] == 0:
        for cr in DEFAULT_CLASS_RULES:
            cur.execute(
                "INSERT INTO class_rules (store_matched, vendor_matched, class, created_by) "
                "VALUES (%s,%s,%s,'seed')",
                (cr.get("store_matched"), cr.get("vendor_matched"), cr.get("class")),
            )
    # Store rules: seeded once. An emptied table stays empty when its rows were deleted in Setup
    # (edit_log has them; edit_log is a web table, so check it exists first).
    cur.execute("SELECT count(*) FROM store_scope_rules")
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT to_regclass('edit_log') IS NOT NULL")
        curated = False
        if cur.fetchone()[0]:
            cur.execute("SELECT 1 FROM edit_log WHERE table_name = 'store_scope_rules' LIMIT 1")
            curated = cur.fetchone() is not None
        if not curated:
            for r in DEFAULT_STORE_SCOPE_RULES:
                cur.execute(
                    "INSERT INTO store_scope_rules (reference, store_matched, applies_to, vendors, note, "
                    "created_by) VALUES (%s,%s,%s,%s,%s,'seed')",
                    (r["reference"], r["store_matched"], r["applies_to"], r["vendors"], r["note"]),
                )


# --------------------------------------------------------------------------- #
# Reference-table readers (used by the classifier each run)
# --------------------------------------------------------------------------- #

def _norm(value):
    """Normalize a name for matching — same as classify_emails._norm (strip + lower).
    Kept local so database.py has no import cycle back into the classifier."""
    return (value or "").strip().lower()


def load_excluded_vendors():
    """Vendor names/spellings currently on the QuickBooks-exclusion list. Now sourced from
    vendor_name_map rows flagged exclude_from_qb, expanded to ALL their spellings (standard name +
    aliases + per-store) via the vendor_exclusion_spellings view — so exclusion matches any spelling
    (Joel's 2026-09-24 decision), not just the standard name."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT name FROM vendor_exclusion_spellings "
                        "WHERE exclude_from_qb IS TRUE ORDER BY name")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def load_store_scope_rules():
    """The Setup › Store rules rows (store_scope_rules), as dicts with reference / store_matched /
    applies_to / vendors (a list) / note, in id order."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT reference, store_matched, applies_to, vendors, note "
                        "FROM store_scope_rules ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(r, vendors=[v.strip() for v in (r.get("vendors") or "").splitlines() if v.strip()])
            for r in rows if (r.get("reference") or "").strip() and (r.get("store_matched") or "").strip()]


def load_statement_recon_excluded_vendors():
    """Vendor names/spellings whose statement lines should never be flagged missing/mismatch —
    for vendors (like utilities) whose 'statement' IS the bill, with no separate invoice to
    reconcile against. Does NOT affect QuickBooks entry, only statement-reconciliation noise. Now
    sourced from vendor_name_map rows flagged exclude_from_statement_recon (all spellings)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT name FROM vendor_exclusion_spellings "
                        "WHERE exclude_from_statement_recon IS TRUE ORDER BY name")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def load_stores():
    """The pharmacy entities (name/aliases/address/notes) in display order."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT store_name, aliases, address, notes "
                        "FROM stores ORDER BY sort_order, id")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_sender_store_map():
    """{normalized sender_email: store_matched} for self-scan mailboxes."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT sender_email, store_matched FROM sender_store_map")
            return {(e or "").strip().lower(): s for e, s in cur.fetchall() if e}
    finally:
        conn.close()


def load_contact_store_map():
    """{normalized contact_email: store_matched} for recipient (To/Cc) addresses that
    identify a store on email FROM third-party vendors."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT contact_email, store_matched FROM contact_store_map")
            return {(e or "").strip().lower(): s for e, s in cur.fetchall() if e}
    finally:
        conn.close()


def norm_last4(v):
    """Normalize a card last-4: strip non-digits, keep the trailing 4 digits."""
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[-4:] if digits else ""


def load_card_store_map():
    """{normalized card_last4: store_matched} for cards whose store is known."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT card_last4, store_matched FROM card_store_map")
            return {norm_last4(c): s for c, s in cur.fetchall() if norm_last4(c)}
    finally:
        conn.close()


def load_class_rules():
    """The QuickBooks class-default rules: [{store_matched, vendor_matched, class}, …]
    (blank store/vendor = 'any'). Read each run; the pipeline fills a record's class from
    the best match only when the document didn't state one."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT store_matched, vendor_matched, class FROM class_rules "
                        "WHERE class IS NOT NULL AND class <> '' ORDER BY id")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_vendor_standardization():
    """Everything the classifier needs to standardize a vendor + resolve its store-specific
    QB names. `lookup` maps a bare .strip().lower() spelling (standard name AND every store
    spelling) -> standard vendor name; classify_emails re-keys it with its own real
    normalization once (avoids a circular import into the classifier's helpers). Returns
    {lookup, vendor_names, account_names, vendor_account}."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM vendor_name_map")
            vn = cur.fetchall()
            cur.execute("SELECT * FROM account_name_map")
            an = cur.fetchall()
            cur.execute("SELECT standard_vendor_name, standard_account_name FROM vendor_account_assignment")
            va = cur.fetchall()
    finally:
        conn.close()
    store_cols = {"3m": "name_3m", "madison": "name_madison", "maple": "name_maple",
                  "refuah": "name_refuah", "westgate": "name_westgate",
                  "bmw": "name_bmw", "lakewood_rx": "name_lakewood_rx"}

    def _bare(v):
        return (v or "").strip().lower()

    lookup, vendor_names = {}, {}
    for r in vn:
        std = r["standard_vendor_name"]
        if _bare(std):
            lookup[_bare(std)] = std
        # Store-independent variants: ONE ALIAS PER LINE (the same way the Setup page edits them),
        # matched like the standard name and store columns above. Split on NEWLINES ONLY — never on
        # commas: a comma is legitimate inside a single vendor name (e.g. "EnlivenHealth (FDS, Inc.)",
        # "Applied Medical Technology, Inc."), and splitting on it shattered such an alias into broken
        # fragments that could never match a real vendor again. (Audited 2026-09-24: every comma in
        # the live aliases data is an intra-name legal-suffix comma, none a legacy CSV of joined names.)
        for alias in (r.get("aliases") or "").splitlines():
            alias = alias.strip()
            if alias:
                lookup[_bare(alias)] = std
        names = {}
        for key, col in store_cols.items():
            v = (r.get(col) or "").strip()
            if v:
                lookup[_bare(v)] = std
                names[key] = v
        vendor_names[std] = names
    account_names = {}
    for r in an:
        std = r["standard_account_name"]
        names = {}
        for key, col in store_cols.items():
            v = (r.get(col) or "").strip()
            if v:
                names[key] = v
        account_names[std] = names
    vendor_account = {r["standard_vendor_name"]: r["standard_account_name"] for r in va}
    # Vendors ticked "Trust line-item totals" in Setup: their summed-from-line-items amounts
    # aren't sent to review (classify_emails.apply_amount_review_gate).
    trusted_totals = {r["standard_vendor_name"] for r in vn if r.get("trust_line_item_totals")}
    return {"lookup": lookup, "vendor_names": vendor_names,
            "account_names": account_names, "vendor_account": vendor_account,
            "trusted_totals": trusted_totals}


# --------------------------------------------------------------------------- #
# Value coercion helpers (Excel dicts <-> DB types)
# --------------------------------------------------------------------------- #

def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yn(v):
    """'Yes'/'No' (or bool) -> bool/None."""
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return None
    return str(v).strip().lower() in ("yes", "true", "1")


def _date(v):
    if v in (None, ""):
        return None
    return str(v)[:10]  # DATE column accepts 'YYYY-MM-DD'


def _yn_str(v):
    if v is None:
        return ""
    return "Yes" if v else "No"


# --------------------------------------------------------------------------- #
# Emails + records (results)
# --------------------------------------------------------------------------- #

def _upsert_email(cur, row, run_id=None):
    fn = row.get("Filename")
    if not fn:
        return
    status = "error" if row.get("Error") else "processed"
    cur.execute(
        """
        INSERT INTO emails (filename, source, gmail_link, subject, email_type,
                            status, error, run_id, date_added, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (filename) DO UPDATE SET
            gmail_link = COALESCE(EXCLUDED.gmail_link, emails.gmail_link),
            email_type = EXCLUDED.email_type,
            status     = EXCLUDED.status,
            error      = EXCLUDED.error,
            run_id     = COALESCE(EXCLUDED.run_id, emails.run_id),
            updated_at = now()
        """,
        (fn, row.get("Source") or "manual", row.get("Gmail Link") or None,
         row.get("Subject"), row.get("Email Type"), status,
         row.get("Error") or None, run_id, _date(row.get("Date Added"))),
    )


def clear_stale_error_status(filenames, run_id=None):
    """Safety net for the status-update gap: once an email has been SUCCESSFULLY handled in a run
    (classified without error — whether its records were recorded, routed to review, or
    informational), make sure its emails row isn't left stuck on a stale status='error' from an
    earlier failed run. _upsert_email only updates status for rows that reach save_results, so an
    email handled through any other path kept 'error' forever (this is what let the EuroPharma bill
    sit untracked for 2 weeks). GUARDED on status='error' so it only ever CLEARS a stuck error —
    never disturbs a normal 'fetched'/'review'/'processed' row. Returns how many it cleared."""
    fns = [f for f in (filenames or []) if f]
    if not fns:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE emails SET status = 'processed', error = NULL, "
                "run_id = COALESCE(%s, run_id), updated_at = now() "
                "WHERE filename = ANY(%s) AND status = 'error'",
                (run_id, fns))
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def stamp_handled_emails(entries, run_id=None):
    """Guarantee an emails-row status write for emails HANDLED this run that produced NO recorded
    row — i.e. ones routed entirely to review (all records flagged, or a record-less email the model
    flagged) or 'unreadable'. save_results' _upsert_email only runs for recorded rows, so without
    this such an email's emails row stays frozen at its fetch-time status forever (the 237-email
    'fetched'/run_id-NULL backlog). `entries` is [(filename, status, email_type)]. Returns the count."""
    rows = [(fn, st, et) for (fn, st, et) in (entries or []) if fn]
    if not rows:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            for fn, status, etype in rows:
                cur.execute(
                    "INSERT INTO emails (filename, status, email_type, run_id, updated_at) "
                    "VALUES (%s, %s, %s, %s, now()) "
                    "ON CONFLICT (filename) DO UPDATE SET status = EXCLUDED.status, "
                    "email_type = COALESCE(EXCLUDED.email_type, emails.email_type), "
                    "run_id = COALESCE(EXCLUDED.run_id, emails.run_id), updated_at = now()",
                    (fn, status, etype, run_id))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def stamp_moved_from_queue(entries, run_id=None):
    """BY-CONSTRUCTION backstop at the single point where source emails leave test_emails/.

    Any source email that leaves the inbox (pending_moves) has, by definition, been handled this
    run — so its emails row MUST have advanced off the fetch-time status. The known-good writes
    (_upsert_email for recorded rows, clear_stale_error_status, stamp_handled_emails for
    review-routed/unreadable) already do this, so in a healthy run THIS CORRECTS 0 ROWS. It exists
    so a future code path that moves a file out of test_emails/ without stamping its status (the
    invoice_history-orphan / frozen-'fetched' bug class) cannot silently reintroduce the gap: the
    row is advanced here regardless, and a non-zero return is logged by the caller as a red flag.

    GUARDED on status still being 'fetched'/NULL/'' so it never clobbers a status another write
    already set correctly ('processed'/'review'/'error'). `entries` is [(filename, implied_status)]
    where implied_status is derived from the move destination. Returns the count it CORRECTED."""
    rows = [(fn, st) for (fn, st) in (entries or []) if fn]
    if not rows:
        return 0
    conn = connect()
    try:
        n = 0
        with conn.cursor() as cur:
            for fn, status in rows:
                cur.execute(
                    "UPDATE emails SET status = %s, run_id = COALESCE(%s, run_id), updated_at = now() "
                    "WHERE filename = %s AND (status = 'fetched' OR status IS NULL OR status = '')",
                    (status, run_id, fn))
                n += cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def create_run(run_id):
    """Open a run row at the start of a classify run (idempotent)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO runs (run_id) VALUES (%s) ON CONFLICT (run_id) DO NOTHING",
                        (run_id,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Sitewide maintenance notice (banner). Canonical here (schema + CLI notice.py live
# at this layer); web_db re-exports thin wrappers for the web app. See notice.py.
# --------------------------------------------------------------------------- #

def set_notice(message, minutes, username):
    """Show a sitewide banner. `minutes` None => active until explicitly cleared; otherwise it
    auto-expires that many minutes from now. Each call is a new row; get_active_notice reads the
    latest still-active one, so a new notice supersedes any prior."""
    message = (message or "").strip()
    if not message:
        raise ValueError("notice message is empty")
    conn = connect()
    try:
        with conn.cursor() as cur:
            if minutes is None:
                cur.execute(
                    "INSERT INTO system_notices (message, active_until, created_by) "
                    "VALUES (%s, NULL, %s)", (message, username))
            else:
                cur.execute(
                    "INSERT INTO system_notices (message, active_until, created_by) "
                    "VALUES (%s, now() + make_interval(mins => %s), %s)",
                    (message, int(minutes), username))
        conn.commit()
    finally:
        conn.close()


def clear_notice(username):
    """End every currently-active notice immediately (active_until = now()). Idempotent."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE system_notices SET active_until = now() "
                "WHERE active_until IS NULL OR active_until > now()")
        conn.commit()
    finally:
        conn.close()


def get_active_notice():
    """The current banner as {message, active_until} or None — the latest row still active
    (active_until NULL or in the future). Used for both first render and the poll endpoint."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT message, active_until FROM system_notices "
                "WHERE active_until IS NULL OR active_until > now() "
                "ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        if not row:
            return None
        return {"message": row[0],
                "active_until": row[1].isoformat() if row[1] is not None else None}
    finally:
        conn.close()


def finish_run(run_id, n_emails, n_results, n_review, learnings_hash=None):
    """Stamp completion time + counts once the run's writes have succeeded. Also
    records the learnings-text hash in effect this run, so the NEXT run can tell
    whether the learnings changed and re-check the open review queue (gated)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET finished_at = now(), n_emails = %s, n_results = %s, "
                        "n_review = %s, learnings_hash = %s WHERE run_id = %s",
                        (n_emails, n_results, n_review, learnings_hash, run_id))
        conn.commit()
    finally:
        conn.close()


def get_last_learnings_hash(current_run_id):
    """The learnings_hash recorded by the most recent PRIOR run (any earlier run with
    a stored hash), or None if there is no prior baseline yet. Used to decide whether
    the learnings changed since last run."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT learnings_hash FROM runs WHERE run_id <> %s "
                        "AND learnings_hash IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                        (current_run_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _insert_invoice(cur, row):
    cur.execute(
        """
        INSERT INTO invoices (email_filename, date_added, file_path, gmail_link,
            email_type, confidence, needs_review, review_reason, route_to, routing_reason,
            vendor_raw, vendor_matched, vendor_confidence, skip_qb_import,
            store_raw, store_matched, store_confidence, invoice_number, invoice_date,
            amount, ship_charge, sales_tax, duplicate_suspect, duplicate_reason,
            duplicate_ignored, duplicate_of, duplicate_group_id, notes, card_last4, class,
            standard_vendor, standard_account, qb_vendor, qb_account, total_source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (row.get("Filename"), _date(row.get("Date Added")), row.get("File Path"),
         row.get("Gmail Link"), row.get("Email Type"), row.get("Confidence"),
         _yn(row.get("Needs Review")), row.get("Review Reason"), row.get("Route To"),
         row.get("Routing Reason"), row.get("Vendor Raw"), row.get("Vendor Matched"),
         row.get("Vendor Confidence"), _yn(row.get("Skip QB Import")), row.get("Store Raw"),
         row.get("Store Matched"), row.get("Store Confidence"), row.get("Invoice #"),
         row.get("Invoice Date"), _num(row.get("Amount")), _num(row.get("Ship Charge")),
         _num(row.get("Sales Tax")), _yn(row.get("Duplicate Suspect")),
         row.get("Duplicate Reason"), _yn(row.get("Duplicate Ignored")),
         row.get("Duplicate Of"), row.get("Duplicate Group"), row.get("Notes"),
         row.get("Card Last4"), row.get("Class"),
         row.get("Standard Vendor"), row.get("Standard Account"),
         row.get("QB Vendor"), row.get("QB Account"), row.get("Total Source") or None),
    )


def _insert_credit_memo(cur, row):
    cur.execute(
        """
        INSERT INTO credit_memos (email_filename, date_added, file_path, gmail_link,
            email_type, needs_review, review_reason, vendor_raw, vendor_matched,
            vendor_confidence, skip_qb_import, store_raw, store_matched, store_confidence,
            credit_memo_number, credit_memo_date, amount, notes, card_last4, class,
            standard_vendor, standard_account, qb_vendor, qb_account, total_source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (row.get("Filename"), _date(row.get("Date Added")), row.get("File Path"),
         row.get("Gmail Link"), row.get("Email Type"), _yn(row.get("Needs Review")),
         row.get("Review Reason"), row.get("Vendor Raw"), row.get("Vendor Matched"),
         row.get("Vendor Confidence"), _yn(row.get("Skip QB Import")), row.get("Store Raw"),
         row.get("Store Matched"), row.get("Store Confidence"), row.get("Invoice #"),
         row.get("Invoice Date"), _num(row.get("Amount")), row.get("Notes"),
         row.get("Card Last4"), row.get("Class"),
         row.get("Standard Vendor"), row.get("Standard Account"),
         row.get("QB Vendor"), row.get("QB Account"), row.get("Total Source") or None),
    )


def _insert_payment(cur, row):
    cur.execute(
        """
        INSERT INTO payments (email_filename, date_added, file_path, gmail_link,
            email_type, needs_review, review_reason, vendor_raw, vendor_matched,
            skip_qb_import, store_matched, reference_number, invoice_number, payment_date, amount,
            duplicate_suspect, duplicate_reason, duplicate_ignored, duplicate_of,
            duplicate_group_id, notes, card_last4, class, total_source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (row.get("Filename"), _date(row.get("Date Added")), row.get("File Path"),
         row.get("Gmail Link"), row.get("Email Type"), _yn(row.get("Needs Review")),
         row.get("Review Reason"), row.get("Vendor Raw"), row.get("Vendor Matched"),
         _yn(row.get("Skip QB Import")), row.get("Store Matched"), row.get("Invoice #"),
         row.get("Applied Invoice #"), row.get("Invoice Date"), _num(row.get("Amount")),
         _yn(row.get("Duplicate Suspect")), row.get("Duplicate Reason"),
         _yn(row.get("Duplicate Ignored")), row.get("Duplicate Of"),
         row.get("Duplicate Group"), row.get("Notes"),
         row.get("Card Last4"), row.get("Class"), row.get("Total Source") or None),
    )


# --------------------------------------------------------------------------- #
# Keep "entered in QuickBooks" across a reprocess (qb_entered_marks — see the DDL).
# save_results deletes an email's records and re-inserts them, and the inserts don't carry
# entered_in_qb, so a reprocessed record that was already keyed into QB used to come back as
# "not entered" on /qb — a double-entry risk. Now the entered rows are marked before the delete
# and the marks are given back to the re-recorded records after the insert.
# --------------------------------------------------------------------------- #
_ENTERED_TABLES = (   # table, record_type, number column, date column, duplicate-ignored expression
    ("invoices", "invoice", "invoice_number", "invoice_date", "duplicate_ignored"),
    ("credit_memos", "credit_memo", "credit_memo_number", "credit_memo_date", "FALSE"),
    ("payments", "payment", "reference_number", "payment_date", "duplicate_ignored"),
)
_ENTERED_ACTOR = "reprocess:keep-entered"


def _entered_keys_sql(numcol, datecol):
    """SQL select-list of a record's match keys: normalized vendor, number, amount to the cent, date."""
    return (f"norm_vendor_dedupe(vendor_matched), upper(btrim(coalesce({numcol}, ''))), "
            f"round(amount::numeric, 2), coalesce({datecol}::text, '')")


def _money_text(v):
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return "(no amount)"


# Scoped replace (save_results' `replace_records`). save_results deletes EVERY record filed under a
# touched email filename and re-inserts what the run produced for it. Two callers re-record ONE record
# whose filename other records share: a released sales order (filed under its original email) and a
# superseded re-record (its stale filename can be a whole-email recording). Deleting the whole filename
# there took the email's other records with it, so those callers name just the record to replace:
# {"filename", "record_type", "vendor", "number", "amount" (optional)}.
_SCOPED_TABLES = {"invoice": ("invoices", "invoice_number"),
                  "credit_memo": ("credit_memos", "credit_memo_number"),
                  "payment": ("payments", "reference_number")}


def _scoped_where(spec):
    """(table, record_type, WHERE sql, params) for the records a replace_records spec names: the same
    email_filename, vendor (legal-suffix-insensitive, like the duplicate check) and number, plus the
    same amount to the cent when the spec gives one."""
    rtype = spec.get("record_type") or "invoice"
    table, numcol = _SCOPED_TABLES[rtype]
    sql = ("email_filename = %s AND norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
           f"AND lower(btrim(coalesce({numcol}, ''))) = lower(btrim(coalesce(%s, '')))")
    params = [spec.get("filename"), spec.get("vendor") or "", spec.get("number") or ""]
    amount = _num(spec.get("amount"))
    if amount is not None:
        sql += " AND amount IS NOT NULL AND abs(amount - %s) < 0.005"
        params.append(amount)
    return table, rtype, sql, params


def _mark_entered_rows(cur, filenames, replace_records=()):
    """Before save_results deletes records: copy each one that's entered in QuickBooks into
    qb_entered_marks — every record under `filenames`, plus each record a replace_records spec names
    (see _scoped_where). Returns [(mark_id, label)] for the run log."""
    targets = []
    if filenames:
        for table, rtype, numcol, datecol, _dup in _ENTERED_TABLES:
            targets.append((table, rtype, numcol, datecol, "email_filename = ANY(%s)", [list(filenames)]))
    for spec in replace_records or ():
        table, rtype, where, params = _scoped_where(spec)
        numcol, datecol = next((t[2], t[3]) for t in _ENTERED_TABLES if t[0] == table)
        targets.append((table, rtype, numcol, datecol, where, params))
    out = []
    for table, rtype, numcol, datecol, where, params in targets:
        cur.execute(
            f"INSERT INTO qb_entered_marks (record_type, vendor_key, number_key, amount_key, date_key, "
            f"    vendor_matched, number, amount, entered_qb_by, entered_qb_at, old_record_id, email_filename) "
            f"SELECT %s, {_entered_keys_sql(numcol, datecol)}, vendor_matched, {numcol}, amount, "
            f"       entered_qb_by, entered_qb_at, id, email_filename "
            f"  FROM {table} WHERE {where} AND entered_in_qb IS TRUE "
            f"RETURNING id, vendor_matched, number, amount",
            [rtype] + params)
        out += [(i, f"{v} #{n or '?'} {_money_text(a)}") for i, v, n, a in cur.fetchall()]
    return out


def _restore_entered_marks(cur, filenames):
    """After save_results inserts `filenames`' records: give each OPEN qb_entered_mark back to its
    re-recorded record — from this run's delete, or from an earlier run whose record went to review
    and is only now being recorded. A match is the same record type + vendor + number + amount (plus
    the date when there's no number). When only the amount differs it's still the same document and
    it IS in QuickBooks, so the status comes back too, with a note to check the QB entry. A live copy
    wins over an ignored duplicate. Only ever sets entered TRUE. Returns (restored labels,
    amount-changed descriptions, consumed mark ids)."""
    restored, changed, consumed = [], [], set()
    cur.execute("SELECT 1 FROM qb_entered_marks WHERE restored_at IS NULL LIMIT 1")
    if cur.fetchone() is None:
        return restored, changed, consumed
    exact = (lambda m, r: m["vkey"] == r["vkey"] and m["nkey"] == r["nkey"] and m["akey"] == r["akey"]
             and (m["nkey"] != "" or m["dkey"] == r["dkey"]))
    same_doc = (lambda m, r: m["vkey"] == r["vkey"] and m["nkey"] != "" and m["nkey"] == r["nkey"])
    for table, rtype, numcol, datecol, dupcol in _ENTERED_TABLES:
        cur.execute(
            f"SELECT id, {_entered_keys_sql(numcol, datecol)}, {dupcol}, amount, vendor_matched, {numcol} "
            f"  FROM {table} WHERE email_filename = ANY(%s) AND entered_in_qb IS NOT TRUE",
            (list(filenames),))
        new = [dict(zip(("id", "vkey", "nkey", "akey", "dkey", "ignored", "amount", "vendor", "number"), r))
               for r in cur.fetchall()]
        if not new:
            continue
        cur.execute(
            "SELECT id, vendor_key, number_key, amount_key, date_key, entered_qb_by, entered_qb_at, "
            "       amount, old_record_id FROM qb_entered_marks "
            " WHERE restored_at IS NULL AND record_type = %s AND vendor_key = ANY(%s) ORDER BY id",
            (rtype, list({r["vkey"] for r in new if r["vkey"]})))
        marks = [dict(zip(("id", "vkey", "nkey", "akey", "dkey", "by", "at", "amount", "old_id"), r))
                 for r in cur.fetchall()]
        new.sort(key=lambda r: (bool(r["ignored"]), r["id"]))     # a live copy before an ignored one
        used = set()
        for is_exact, match in ((True, exact), (False, same_doc)):
            for m in marks:
                if m["id"] in consumed:
                    continue
                r = next((r for r in new if r["id"] not in used and match(m, r)), None)
                if r is None:
                    continue
                used.add(r["id"])
                consumed.add(m["id"])
                label = f"{r['vendor']} #{r['number'] or '?'}"
                note = None
                if not is_exact:
                    note = (f"[Entered in QuickBooks as {_money_text(m['amount'])} before a reprocess read "
                            f"{_money_text(r['amount'])} — check the QuickBooks entry.]")
                    changed.append(f"{label} was entered in QuickBooks as {_money_text(m['amount'])} "
                                   f"but now reads {_money_text(r['amount'])}")
                cur.execute(
                    f"UPDATE {table} SET entered_in_qb = TRUE, entered_qb_by = %s, entered_qb_at = %s"
                    + (", notes = CASE WHEN coalesce(notes, '') = '' THEN %s ELSE notes || ' ' || %s END"
                       if note else "") + " WHERE id = %s",
                    (m["by"], m["at"]) + ((note, note) if note else ()) + (r["id"],))
                cur.execute("UPDATE qb_entered_marks SET restored_record_id = %s, restored_at = now(), "
                            "restore_note = %s WHERE id = %s", (r["id"], note, m["id"]))
                cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, "
                            "changed_by) VALUES (%s, %s, 'entered_in_qb', 'False', %s, %s)",
                            (table, r["id"], f"True (carried over from record #{m['old_id']})",
                             _ENTERED_ACTOR))
                restored.append(label)
    return restored, changed, consumed


def _entered_step(cur, fn):
    """Run one entered-status step inside a savepoint. It's a safety net layered on save_results, so a
    problem here must never stop the records themselves from being saved — it's reported and skipped."""
    cur.execute("SAVEPOINT keep_entered")
    try:
        out = fn()
        cur.execute("RELEASE SAVEPOINT keep_entered")
        return out
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT keep_entered")
        print(f"  QB-ENTERED: could not carry 'entered in QuickBooks' over ({e}); saved without it.")
        return None


# A reprocess must not delete-and-replace a record a human just acted on (entered it in QB,
# ignored a duplicate, or flagged it) out from under them. The banner (notice.py) is a courtesy;
# this is the hard backstop. ~10 min covers "someone is mid-task right now".
REPROCESS_PROTECT_MINUTES = 10


def _recently_human_touched(cur, touched, minutes):
    """Of `touched` email filenames, which have a record a HUMAN acted on within `minutes`.
    Signals used — each has a username companion and NO system writer, so they mean a person,
    not the pipeline: entered_qb_at (QB toggle), duplicate_ignored_at (duplicate decision), and a
    flagged_reviews row (always human-submitted). edited_at is DELIBERATELY excluded: the pipeline
    itself writes it (apply_vendor_exclusion / needs_review backfill), so it isn't a reliable
    'a human touched this' signal. Returns a set of filenames to leave untouched this run."""
    if not touched:
        return set()
    protected = set()
    # Per-record audit timestamps (columns vary by table; credit_memos has no duplicate_ignored_at).
    for sql in (
        "SELECT DISTINCT email_filename FROM invoices WHERE email_filename = ANY(%s) "
        "  AND (entered_qb_at > now() - make_interval(mins => %s) "
        "    OR duplicate_ignored_at > now() - make_interval(mins => %s))",
        "SELECT DISTINCT email_filename FROM payments WHERE email_filename = ANY(%s) "
        "  AND (entered_qb_at > now() - make_interval(mins => %s) "
        "    OR duplicate_ignored_at > now() - make_interval(mins => %s))",
        "SELECT DISTINCT email_filename FROM credit_memos WHERE email_filename = ANY(%s) "
        "  AND entered_qb_at > now() - make_interval(mins => %s)",
    ):
        n_times = sql.count("make_interval")
        cur.execute(sql, (touched, *([minutes] * n_times)))
        protected.update(r[0] for r in cur.fetchall())
    # A flag submitted on any of this email's records (flagged_reviews is keyed by record id).
    cur.execute(
        "SELECT DISTINCT r.email_filename FROM flagged_reviews f "
        "JOIN ("
        "  SELECT id, email_filename, 'invoice' t FROM invoices "
        "  UNION ALL SELECT id, email_filename, 'payment' FROM payments "
        "  UNION ALL SELECT id, email_filename, 'credit_memo' FROM credit_memos) r "
        "  ON r.id = f.record_id AND r.t = f.record_type "
        "WHERE r.email_filename = ANY(%s) AND f.created_at > now() - make_interval(mins => %s)",
        (touched, minutes))
    protected.update(r[0] for r in cur.fetchall())
    return protected


def recently_human_touched(filenames):
    """Which of `filenames` have a record a human acted on in the last REPROCESS_PROTECT_MINUTES — the
    save_results backstop, for a caller that wants to hold off BEFORE doing its work (see
    classify_emails.reconcile_sales_orders)."""
    names = sorted({f for f in (filenames or []) if f})
    if not names:
        return set()
    conn = connect()
    try:
        with conn.cursor() as cur:
            return _recently_human_touched(cur, names, REPROCESS_PROTECT_MINUTES)
    finally:
        conn.close()


def save_results(new_rows, touched_filenames, run_id=None, replace_records=None):
    """Persist this run's result rows. Records for any re-run (touched) filename
    are deleted first, so reprocessing replaces rather than duplicates. run_id is
    stamped on every email processed this run.

    SCOPED REPLACE: `replace_records` names single records to delete instead of whole filenames (see
    _scoped_where) — for a caller re-recording one record whose filename other records share.

    BACKSTOP: a touched email whose records a human acted on in the last
    REPROCESS_PROTECT_MINUTES is SKIPPED this run (not deleted, its new rows not inserted) so a
    reprocess can't silently overwrite someone's in-progress work. replace_records filenames get the
    same check. Returns the list of skipped (protected) filenames so the caller can surface them for
    a manual look.

    KEEPS "ENTERED IN QUICKBOOKS": records being replaced that were already entered are marked first
    (qb_entered_marks) and the status is given back to the re-recorded records after the insert —
    or later, if a record went to review this time (see _restore_entered_marks)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            touched = list(touched_filenames or [])
            replace = [s for s in (replace_records or []) if s.get("filename")]
            protected = _recently_human_touched(
                cur, sorted(set(touched) | {s["filename"] for s in replace}), REPROCESS_PROTECT_MINUTES)
            touched = [t for t in touched if t not in protected]
            replace = [s for s in replace if s["filename"] not in protected]
            new_marks = []
            if touched or replace:
                new_marks = _entered_step(cur, lambda: _mark_entered_rows(cur, touched, replace)) or []
            if touched:
                cur.execute("DELETE FROM invoices     WHERE email_filename = ANY(%s)", (touched,))
                cur.execute("DELETE FROM credit_memos WHERE email_filename = ANY(%s)", (touched,))
                cur.execute("DELETE FROM payments     WHERE email_filename = ANY(%s)", (touched,))
            for spec in replace:
                table, _rtype, where, params = _scoped_where(spec)
                cur.execute(f"DELETE FROM {table} WHERE {where}", params)
            recorded = set()
            for row in new_rows:
                # Skip protected emails entirely — leave their existing records + status as the
                # human left them. (Their source file still moves/stays per the caller's pending
                # moves; only the DB delete-and-replace is withheld.)
                if row.get("Filename") in protected:
                    continue
                _upsert_email(cur, row, run_id)
                rtype = row.get("Record Type")
                if rtype == "invoice":
                    _insert_invoice(cur, row)
                elif rtype == "credit_memo":
                    _insert_credit_memo(cur, row)
                elif rtype == "payment":
                    _insert_payment(cur, row)
                # informational / error rows: the emails upsert above is enough.
                if rtype in ("invoice", "credit_memo", "payment") and row.get("Filename"):
                    recorded.add(row.get("Filename"))
            carried = (_entered_step(cur, lambda: _restore_entered_marks(cur, sorted(recorded)))
                       if recorded else None) or ([], [], set())
        conn.commit()
        if protected:
            print(f"  REPROCESS-PROTECT: skipped {len(protected)} email(s) with human activity in the "
                  f"last {REPROCESS_PROTECT_MINUTES} min (left as-is, not overwritten): "
                  + ", ".join(sorted(protected)))
        restored, changed, consumed = carried
        if restored:
            print(f"  QB-ENTERED: kept 'entered in QuickBooks' on {len(restored)} re-recorded record(s): "
                  + ", ".join(restored))
        for c in changed:
            print(f"  QB-ENTERED WARNING: {c} — check the QuickBooks entry.")
        still_open = [label for mark_id, label in new_marks if mark_id not in consumed]
        if still_open:
            print(f"  QB-ENTERED WARNING: {len(still_open)} record(s) already entered in QuickBooks were not "
                  f"re-recorded by this reprocess (sent to review, or read differently); their entered "
                  f"status re-attaches when they're recorded again: " + "; ".join(still_open))
        # protected: emails left untouched (reprocess-protect). recorded: filenames that got a
        # fresh invoice/credit_memo/payment row this run — the QB-unmatched recheck pass looks at
        # exactly these (see qb_match.resolve_unmatched_for_new_records).
        return sorted(protected), sorted(recorded)
    finally:
        conn.close()


def _invoice_row(r):
    return {
        "Filename": r["email_filename"], "Date Added": str(r["date_added"] or ""),
        "File Path": r["file_path"], "Gmail Link": r["gmail_link"], "Record Type": "invoice",
        "Email Type": r["email_type"], "Confidence": r["confidence"],
        "Needs Review": _yn_str(r["needs_review"]), "Review Reason": r["review_reason"],
        "Route To": r["route_to"], "Routing Reason": r["routing_reason"],
        "Vendor Raw": r["vendor_raw"], "Vendor Matched": r["vendor_matched"],
        "Vendor Confidence": r["vendor_confidence"], "Skip QB Import": _yn_str(r["skip_qb_import"]),
        "Store Raw": r["store_raw"], "Store Matched": r["store_matched"],
        "Store Confidence": r["store_confidence"], "Invoice #": r["invoice_number"],
        "Invoice Date": r["invoice_date"],
        "Amount": float(r["amount"]) if r["amount"] is not None else "",
        "Ship Charge": float(r["ship_charge"]) if r["ship_charge"] is not None else "",
        "Sales Tax": float(r["sales_tax"]) if r["sales_tax"] is not None else "",
        "Duplicate Suspect": _yn_str(r["duplicate_suspect"]), "Duplicate Reason": r["duplicate_reason"],
        "Duplicate Ignored": _yn_str(r.get("duplicate_ignored")), "Duplicate Of": r.get("duplicate_of"),
        "Notes": r["notes"], "Card Last4": r.get("card_last4"), "Class": r.get("class"),
        "Total Source": r.get("total_source") or "",
    }


def _credit_row(r):
    return {
        "Filename": r["email_filename"], "Date Added": str(r["date_added"] or ""),
        "File Path": r["file_path"], "Gmail Link": r["gmail_link"], "Record Type": "credit_memo",
        "Email Type": r["email_type"], "Needs Review": _yn_str(r["needs_review"]),
        "Review Reason": r["review_reason"], "Vendor Raw": r["vendor_raw"],
        "Vendor Matched": r["vendor_matched"], "Vendor Confidence": r["vendor_confidence"],
        "Skip QB Import": _yn_str(r["skip_qb_import"]), "Store Raw": r["store_raw"],
        "Store Matched": r["store_matched"], "Store Confidence": r["store_confidence"],
        "Invoice #": r["credit_memo_number"], "Invoice Date": r["credit_memo_date"],
        "Amount": float(r["amount"]) if r["amount"] is not None else "", "Notes": r["notes"],
        "Card Last4": r.get("card_last4"), "Class": r.get("class"),
        "Total Source": r.get("total_source") or "",
    }


def _payment_row(r):
    return {
        "Filename": r["email_filename"], "Date Added": str(r["date_added"] or ""),
        "File Path": r["file_path"], "Gmail Link": r["gmail_link"], "Record Type": "payment",
        "Email Type": r["email_type"], "Needs Review": _yn_str(r["needs_review"]),
        "Review Reason": r["review_reason"], "Vendor Raw": r["vendor_raw"],
        "Vendor Matched": r["vendor_matched"], "Skip QB Import": _yn_str(r["skip_qb_import"]),
        "Store Matched": r["store_matched"], "Invoice #": r["reference_number"],
        "Invoice Date": r["payment_date"],
        "Amount": float(r["amount"]) if r["amount"] is not None else "",
        "Duplicate Suspect": _yn_str(r.get("duplicate_suspect")), "Duplicate Reason": r.get("duplicate_reason"),
        "Duplicate Ignored": _yn_str(r.get("duplicate_ignored")), "Duplicate Of": r.get("duplicate_of"),
        "Notes": r["notes"], "Card Last4": r.get("card_last4"), "Class": r.get("class"),
        "Total Source": r.get("total_source") or "",
    }


def load_all_result_rows():
    """Reconstruct the flat results rows (RESULTS-style dicts) from the invoices,
    credit_memos and payments tables — used for a full Excel export in DB mode."""
    conn = connect()
    rows = []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM invoices ORDER BY id")
            rows += [_invoice_row(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM credit_memos ORDER BY id")
            rows += [_credit_row(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM payments ORDER BY id")
            rows += [_payment_row(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def load_processed_filenames():
    """Filenames the CLASSIFIER already finished successfully — for skip logic.
    Only status='processed' counts. Rows that gmail_fetcher created (status
    'fetched') or that errored are NOT skipped, so they still get classified /
    retried."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM emails WHERE status = 'processed'")
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def email_message_ids():
    """{filename: gmail_message_id} for every email that has one. Lets the skip logic match a
    message that was already handled under a DIFFERENT filename (e.g. a manual import whose
    gmail_message_id we backfilled, vs the same message later re-pulled by the scanner as
    'Maple Scanner - <id>.eml') — filename alone can't catch that."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, gmail_message_id FROM emails "
                        "WHERE gmail_message_id IS NOT NULL AND gmail_message_id <> ''")
            return {fn: mid for fn, mid in cur.fetchall()}
    finally:
        conn.close()


def backfill_gmail_message_ids():
    """One-time (idempotent) repair: fill emails.gmail_message_id where it's NULL but a matching
    invoices/credit_memos/payments row's gmail_link encodes the Gmail message id (…#all/<id>).
    Manual-import emails were created with gmail_message_id NULL, which made the scanner re-pull
    the SAME message as a brand-new record. Returns how many email rows it filled."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(r"""
                WITH ids AS (
                    SELECT email_filename, substring(gmail_link from '#[^/]+/([0-9a-fA-F]+)') AS mid
                    FROM (
                        SELECT email_filename, gmail_link FROM invoices
                        UNION ALL SELECT email_filename, gmail_link FROM credit_memos
                        UNION ALL SELECT email_filename, gmail_link FROM payments
                    ) r
                    WHERE gmail_link ~ '#[^/]+/[0-9a-fA-F]+'
                ),
                one AS (  -- one id per email (they all share the same message)
                    SELECT email_filename, min(mid) AS mid FROM ids WHERE mid IS NOT NULL
                    GROUP BY email_filename
                )
                UPDATE emails e SET gmail_message_id = one.mid
                FROM one
                WHERE e.filename = one.email_filename
                  AND (e.gmail_message_id IS NULL OR e.gmail_message_id = '')
            """)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Review queue / learnings / history  (full-replace semantics like the sheets)
# --------------------------------------------------------------------------- #

def _json_str(o):
    return str(o)


def insert_sales_order(row):
    """Hold a sales order. Dedupe: skip when a still-'held' row already exists for the
    same normalized vendor + number (so a reprocess/re-fetch doesn't stack duplicates)."""
    num = "".join((row.get("number") or "").split()).lower()
    conn = connect()
    try:
        with conn.cursor() as cur:
            # Vendor compared via norm_vendor_dedupe (legal-suffix-insensitive), consistent with
            # check_duplicate — so a re-fetch under a different suffix spelling doesn't stack a
            # duplicate held order.
            cur.execute("SELECT 1 FROM pending_sales_orders WHERE status = 'held' "
                        "AND norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
                        "AND lower(regexp_replace(coalesce(number,''), '\\s', '', 'g')) = %s",
                        (row.get("vendor_matched") or "", num))
            if cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO pending_sales_orders (vendor_matched, store_matched, number, amount, "
                "order_date, source_email, source_file, file_path, gmail_link, snapshot, release_after) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (row.get("vendor_matched"), row.get("store_matched"), row.get("number"),
                 _num(row.get("amount")), row.get("order_date"), row.get("source_email"),
                 row.get("source_file"), row.get("file_path"), row.get("gmail_link"),
                 psycopg2.extras.Json(row.get("snapshot") or {},
                                      dumps=lambda o: json.dumps(o, default=_json_str)),
                 row.get("release_after")))
        conn.commit()
        return True
    finally:
        conn.close()


def load_held_sales_orders():
    """All still-'held' sales orders (for the reconcile pass), newest first."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pending_sales_orders WHERE status = 'held' ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def find_recorded_invoice_ref(vendor, number):
    """The invoice_number of a RECORDED invoice (invoices table = never a sales order)
    matching vendor + normalized number, or None. Amount is irrelevant (invoice wins).
    Tries an exact match first, then falls back to the recorded invoice number simply
    starting with the sales-order number (e.g. order '1618282' -> invoice '1618282-IN'
    — a numbering pattern confirmed used by Bluebonnet Nutrition and plausible for
    other vendors that append a suffix like -IN to the order number)."""
    vend = (vendor or "").strip().lower()
    num = "".join((number or "").split()).lower()
    if not vend or not num:
        return None
    conn = connect()
    try:
        with conn.cursor() as cur:
            # Vendor via norm_vendor_dedupe (legal-suffix-insensitive), consistent with check_duplicate.
            cur.execute("SELECT invoice_number FROM invoices "
                        "WHERE norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
                        "AND lower(regexp_replace(coalesce(invoice_number,''), '\\s', '', 'g')) = %s "
                        "LIMIT 1", (vendor or "", num))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("SELECT invoice_number FROM invoices "
                        "WHERE norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
                        "AND lower(regexp_replace(coalesce(invoice_number,''), '\\s', '', 'g')) LIKE %s "
                        "LIMIT 1", (vendor or "", num + "%"))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def find_released_sales_order(vendor, number):
    """The newest RELEASED held order for vendor + normalized number (same matching as
    insert_sales_order), as {id, held_at, release_after}, or None. Lets a re-read of an order whose
    hold already ran out record as an invoice instead of being held again."""
    num = "".join((number or "").split()).lower()
    if not (vendor or "").strip() or not num:
        return None
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, held_at, release_after FROM pending_sales_orders "
                        "WHERE status = 'released' "
                        "AND norm_vendor_dedupe(vendor_matched) = norm_vendor_dedupe(%s) "
                        "AND lower(regexp_replace(coalesce(number,''), '\\s', '', 'g')) = %s "
                        "ORDER BY id DESC LIMIT 1", (vendor, num))
            return cur.fetchone()
    finally:
        conn.close()


def set_sales_order_status(order_id, status, matched_invoice=None):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_sales_orders SET status = %s, matched_invoice = %s "
                        "WHERE id = %s", (status, matched_invoice, order_id))
        conn.commit()
    finally:
        conn.close()


def append_review_rows(rows, run_id=None):
    """INSERT extra review-queue rows WITHOUT the full-replace save_review_queue does —
    used to add a released sales order that needs review, after the run's queue is written."""
    if not rows:
        return
    for r in rows:
        r.setdefault("Run ID", run_id)
    conn = connect()
    try:
        with conn.cursor() as cur:
            for r in rows:
                _insert_review_row(cur, r)
        conn.commit()
    finally:
        conn.close()


def delete_review_row(filename):
    """DELETE one review_queue row by filename, immediately — the out-of-band-delete
    counterpart to append_review_rows. Called the moment a review item is confirmed resolved
    during a run, so an interruption before the end-of-run save_review_queue() full-sync can't
    strand the already-recorded item as a stale 'ghost' row (its record is already committed)."""
    if not filename:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM review_queue WHERE filename = %s", (filename,))
        conn.commit()
    finally:
        conn.close()


def load_manual_attachments(review_filename):
    """File paths of UNCONSUMED manually-uploaded PDFs/images for a review item, so a
    reprocess can append them to that record's attachments. Empty when none."""
    if not review_filename:
        return []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT file_path FROM manual_attachments "
                        "WHERE review_filename = %s AND consumed_at IS NULL ORDER BY id",
                        (review_filename,))
            return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def mark_manual_attachments_consumed(review_filename):
    """Stamp consumed_at on a review item's unconsumed manual attachments once a
    reprocess has actually used them (so they don't re-trigger reprocessing forever)."""
    if not review_filename:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE manual_attachments SET consumed_at = now() "
                        "WHERE review_filename = %s AND consumed_at IS NULL", (review_filename,))
        conn.commit()
    finally:
        conn.close()


def _review_row_dict(r, review_stamps=False):
    """A review_queue DB row (RealDictCursor) in the dict shape the classifier uses.
    review_stamps adds reviewed_by/at, which only save_review_queue's live merge needs."""
    d = {
        "Id": r["id"],
        "Filename": r["filename"], "Source Email": r["source_email"],
        "Date Flagged": str(r["date_flagged"] or ""), "Email Type": r["email_type"],
        # Carried through a full-replace so carryover rows keep their
        # original flag time / run instead of being reset to now().
        "Flagged At": r["flagged_at"].isoformat() if r.get("flagged_at") else "",
        "Run ID": r.get("run_id") or "",
        "Vendor Matched": r["vendor_matched"], "Store Matched": r["store_matched"],
        "Invoice #": r["invoice_number"],
        "Amount": float(r["amount"]) if r["amount"] is not None else "",
        "Question (AI review reason)": r["question"], "File Path": r["file_path"],
        "Gmail Link": r["gmail_link"],
        "Answer": r["answer"] or "", "Notes to Learn": r["notes_to_learn"] or "",
        "Review Key": r.get("review_key") or "",
        "Duplicate Group": r.get("duplicate_group_id") or "",
        "Assigned To": r.get("assigned_to") or "",
    }
    if review_stamps:
        d["Reviewed By"] = r.get("reviewed_by")
        d["Reviewed At"] = r["reviewed_at"].isoformat() if r.get("reviewed_at") else None
    return d


# What the queue looked like when this process last loaded it (load_review_queue), so the
# end-of-run save_review_queue can tell which rows the web app changed DURING the run.
# None -> no load in this process (migrate_from_excel etc.): save is a plain full replace.
_QUEUE_AS_LOADED = None

# The web-editable fields a run never changes on a carryover row. If the live value differs
# from the value at load time, a person changed it during the run and it wins.
_WEB_FIELDS = ("Answer", "Notes to Learn", "Assigned To")


def load_review_queue():
    global _QUEUE_AS_LOADED
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT now() AS t")
            loaded_at = cur.fetchone()["t"]
            cur.execute("SELECT * FROM review_queue ORDER BY id")
            out = [_review_row_dict(r) for r in cur.fetchall()]
            _QUEUE_AS_LOADED = {"at": loaded_at,
                                "rows": {r["Id"]: {f: r[f] for f in _WEB_FIELDS} for r in out}}
            return out
    finally:
        conn.close()


def _insert_review_row(cur, row, review_id=None):
    # flagged_at: keep an existing value (carryover) or default to now() for freshly-
    # flagged rows. COALESCE lets one INSERT do both. review_id: keep the row's existing id
    # (save_review_queue); None -> a new id from the sequence. reviewed_by/at: set only by
    # save_review_queue's live merge; NULL otherwise, as before.
    cur.execute(
        """
        INSERT INTO review_queue (id, filename, source_email, date_flagged, flagged_at,
            run_id, email_type, vendor_matched, store_matched, invoice_number, amount,
            question, file_path, gmail_link, answer, notes_to_learn, review_key,
            duplicate_group_id, assigned_to, reviewed_by, reviewed_at)
        VALUES (COALESCE(%s, nextval(pg_get_serial_sequence('review_queue', 'id'))),
            %s,%s,%s,COALESCE(%s::timestamptz, now()),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s::timestamptz)
        """,
        (review_id, row.get("Filename"), row.get("Source Email"), _date(row.get("Date Flagged")),
         row.get("Flagged At") or None, row.get("Run ID") or None,
         row.get("Email Type"), row.get("Vendor Matched"), row.get("Store Matched"),
         row.get("Invoice #"), _num(row.get("Amount")),
         row.get("Question (AI review reason)"), row.get("File Path"),
         row.get("Gmail Link"), row.get("Answer") or None, row.get("Notes to Learn") or None,
         row.get("Review Key") or None, row.get("Duplicate Group") or None,
         row.get("Assigned To") or None,
         row.get("Reviewed By") or None, row.get("Reviewed At") or None),
    )


def _merge_live_review_changes(cur, rows, loaded):
    """Fold web-app changes made DURING a classify run into the run's queue before the
    full replace, so they aren't overwritten by the run's start-of-run copy:
      - answer / Notes to Learn / assignee changed on a row -> the live value wins;
      - a row ignored during the run (gone from the queue, with an ignored_reviews snapshot
        taken after the load) -> dropped, instead of being re-inserted beside its snapshot;
      - a row restored or otherwise added during the run -> kept;
      - reviewed_by/at -> kept whenever the saved answer is the live one (they were never
        written back before, so a kept answered row lost who answered it).
    Caller holds a lock on review_queue. Returns (rows, counts)."""
    cur.execute("SELECT * FROM review_queue ORDER BY id")
    live = {r["id"]: _review_row_dict(r, review_stamps=True) for r in cur.fetchall()}
    at_load = loaded["rows"]
    ignored_since = set()
    if at_load.keys() - live.keys():
        cur.execute("SELECT to_regclass('ignored_reviews') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            cur.execute("SELECT DISTINCT (snapshot->>'id')::int AS id FROM ignored_reviews "
                        "WHERE ignored_at >= %s AND snapshot ? 'id'", (loaded["at"],))
            ignored_since = {r["id"] for r in cur.fetchall()} & (at_load.keys() - live.keys())
    counts = {"changed": 0, "ignored": 0, "added": 0}
    out, out_ids = [], set()
    for row in rows:
        rid = _review_row_id(row)
        if rid in ignored_since:
            counts["ignored"] += 1
            continue
        row = dict(row)
        cur_live, before = live.get(rid), at_load.get(rid)
        if cur_live is not None and before is not None:
            changed = False
            for f in _WEB_FIELDS:
                if (cur_live.get(f) or "") != (before.get(f) or "") \
                        and (row.get(f) or "") != (cur_live.get(f) or ""):
                    row[f] = cur_live.get(f) or ""
                    changed = True
            counts["changed"] += changed
        if cur_live is not None and (row.get("Answer") or "").strip() \
                and (row.get("Answer") or "") == (cur_live.get("Answer") or ""):
            row["Reviewed By"] = cur_live.get("Reviewed By")
            row["Reviewed At"] = cur_live.get("Reviewed At")
        out.append(row)
        if rid is not None:
            out_ids.add(rid)
    for rid, cur_live in live.items():
        if rid not in at_load and rid not in out_ids:
            out.append(cur_live)
            counts["added"] += 1
    return out, counts


def _review_row_id(row):
    """A queue row's existing id ("Id", from load_review_queue), or None for a new row."""
    try:
        return int(row.get("Id")) if str(row.get("Id") or "").strip() else None
    except (TypeError, ValueError):
        return None


def save_review_queue(rows):
    """Full-replace the review queue with `rows`. A row that already had an id (loaded from the
    queue, or a re-flagged item given its old row's id by the classifier) KEEPS it, so /review/<id>
    links, --retry-id values and an open page stay pointed at the same item across runs; new rows
    get the next id. (Ids used to be reassigned on every run — before 2026-10-08 — so an answer
    submitted from a page opened before a run could land on a different item.) If two rows claim
    the same id, the first keeps it and the other gets a new one.

    When this process loaded the queue first (a classify run), web-app changes made since that
    load are merged in under a table lock instead of being overwritten (_merge_live_review_changes;
    before 2026-10-12 an answer, assignment, ignore or restore made during a run was lost)."""
    global _QUEUE_AS_LOADED
    loaded, _QUEUE_AS_LOADED = _QUEUE_AS_LOADED, None
    conn = connect()
    try:
        if loaded is not None:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Blocks web writes to the queue until this commit, so nothing lands between
                # the re-read and the rewrite. Reads (page loads) aren't blocked.
                cur.execute("LOCK TABLE review_queue IN SHARE ROW EXCLUSIVE MODE")
                rows, counts = _merge_live_review_changes(cur, rows, loaded)
            if any(counts.values()):
                print(f"Review queue: kept web changes made during this run - "
                      f"{counts['changed']} item(s) answered/assigned, "
                      f"{counts['ignored']} ignored, {counts['added']} restored/added.")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM review_queue")
            used = set()
            for row in rows:
                rid = _review_row_id(row)
                if rid is not None and rid in used:
                    rid = None
                if rid is not None:
                    used.add(rid)
                _insert_review_row(cur, row, rid)
            if used:
                # Kept ids all came from the sequence, so it's already past them; this only guards
                # against a hand-edited id ever colliding with a future new row.
                cur.execute("SELECT pg_get_serial_sequence('review_queue', 'id')")
                seq = cur.fetchone()[0]
                cur.execute(f"SELECT last_value FROM {seq}")
                if max(used) > cur.fetchone()[0]:
                    cur.execute("SELECT setval(%s, %s)", (seq, max(used)))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Statement reconciliation (statement_lines): ingest the invoices a statement
# LISTS, then cross-check each against what we actually recorded. See §5ab.
# --------------------------------------------------------------------------- #

def statement_grace_days():
    """Grace window (days) added to the go-live before a listed invoice is eligible
    to be flagged. STATEMENT_RECON_GRACE_DAYS, default 14."""
    try:
        return int(os.environ.get("STATEMENT_RECON_GRACE_DAYS", "14"))
    except (TypeError, ValueError):
        return 14


def ingest_statement_lines(statement_email, statement_file, lines, run_id=None):
    """UPSERT the invoices a statement lists into statement_lines (dedupe by
    statement_file + normalized invoice_number). Status starts 'missing' and is
    (re)computed by reconcile_statement_lines; a re-ingest never disturbs a
    'dismissed' line's dismissal (reconcile skips dismissed). Returns rows ingested."""
    n = 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            for ln in lines or []:
                num = (ln.get("invoice_number") or "").strip()
                if not num:
                    continue                     # a listed line with no invoice number is unusable
                cur.execute(
                    """
                    INSERT INTO statement_lines
                        (statement_email, statement_file, vendor, invoice_number,
                         invoice_date, amount, status, first_seen_run, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,'missing',%s,now(),now())
                    ON CONFLICT (statement_file, (upper(btrim(invoice_number)))) DO UPDATE SET
                        vendor          = EXCLUDED.vendor,
                        invoice_date    = EXCLUDED.invoice_date,
                        amount          = EXCLUDED.amount,
                        statement_email = EXCLUDED.statement_email,
                        updated_at      = now()
                    """,
                    (statement_email, statement_file, ln.get("vendor"), num,
                     _date(ln.get("invoice_date")), _num(ln.get("amount")), run_id),
                )
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def count_flagged_statement_lines():
    """Current count of open statement exceptions (missing + amount_mismatch). Used to measure
    how many a fresh reconcile newly cleared (flagged-before minus flagged-after)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM statement_lines "
                        "WHERE status IN ('missing','amount_mismatch')")
            return cur.fetchone()[0]
    finally:
        conn.close()


def reconcile_statement_lines(run_id=None, grace_days=None):
    """Re-check every non-dismissed statement_line against recorded invoices. Match key is
    vendor + normalized invoice_number; amount (within a cent) decides matched vs mismatch.
    pre_cutoff basis (never flagged): the STATEMENT's arrival (received_at, fallback date_added), NOT
    the invoice's transaction date — so a late-arriving invoice (old printed date, emailed after
    go-live) on a post-go-live statement IS flagged as missing when we don't have it. A second guard
    ALSO marks pre_cutoff any invoice dated >60 days before go-live, so vendors carrying years-old
    balances on a current statement don't generate noise (only genuinely recent or just-late invoices
    surface). A line present only in the review queue / held sales orders is 'in_flight' (counts as
    present). Recomputed from scratch each call, so a flagged line auto-resolves to 'matched' once the
    invoice arrives.

    Go-live cutoff (not hardcoded): the earliest classify run start (runs.started_at) — that is
    definitionally when email processing went live — falling back to MIN(received_at) only if no
    runs are recorded. NOT raw MIN(received_at): received_at is each email's true arrival time, so
    the go-live fetch pulls year-old inbox mail and MIN(received_at) sits ~17 months before go-live
    (same finding as the purge tool). STATEMENT_GOLIVE_DATE (YYYY-MM-DD) overrides the derivation.
    Returns a status→count dict for the lines touched."""
    if grace_days is None:
        grace_days = statement_grace_days()
    override = (os.environ.get("STATEMENT_GOLIVE_DATE") or "").strip() or None
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH golive AS (
                    SELECT COALESCE(
                             %(golive)s::date,
                             (SELECT MIN(started_at)::date FROM runs),
                             (SELECT MIN(received_at)::date FROM emails WHERE gmail_message_id IS NOT NULL)
                           ) AS d0
                ),
                cutoff AS (
                    SELECT (d0 + make_interval(days => %(grace)s))::date AS d FROM golive
                )
                UPDATE statement_lines sl SET
                    status = CASE
                      -- pre_cutoff (never flagged) when the STATEMENT arrived before go-live, OR the
                      -- invoice's transaction date is CLEARLY ancient (>60d before go-live) — the
                      -- latter guards against vendors carrying years-old balances on a current
                      -- statement, while still flagging a genuinely late-arriving invoice (dated
                      -- within ~2 months of go-live but emailed after it).
                      WHEN cutoff.d IS NULL OR se.stmt_date IS NULL
                           OR se.stmt_date < cutoff.d
                           OR sl.invoice_date < (golive.d0 - interval '60 days')  THEN 'pre_cutoff'
                      WHEN m.rec_amount IS NOT NULL
                           AND abs(m.rec_amount - sl.amount) <= 0.01   THEN 'matched'
                      WHEN m.rec_amount IS NOT NULL                    THEN 'amount_mismatch'
                      WHEN f.present                                   THEN 'in_flight'
                      ELSE 'missing'
                    END,
                    recorded_amount = m.rec_amount,
                    updated_at = now()
                FROM statement_lines s2
                CROSS JOIN cutoff
                CROSS JOIN golive
                LEFT JOIN LATERAL (
                    SELECT i.amount AS rec_amount
                    FROM invoices i
                    WHERE norm_vendor_dedupe(i.vendor_matched) = norm_vendor_dedupe(s2.vendor)
                      AND upper(btrim(i.invoice_number)) = upper(btrim(s2.invoice_number))
                      AND i.amount IS NOT NULL
                    ORDER BY i.id DESC LIMIT 1
                ) m ON TRUE
                LEFT JOIN LATERAL (
                    SELECT TRUE AS present WHERE
                      EXISTS (SELECT 1 FROM review_queue rq
                              WHERE norm_vendor_dedupe(rq.vendor_matched) = norm_vendor_dedupe(s2.vendor)
                                AND upper(btrim(rq.invoice_number)) = upper(btrim(s2.invoice_number)))
                      OR EXISTS (SELECT 1 FROM pending_sales_orders p
                              WHERE p.status = 'held'
                                AND norm_vendor_dedupe(p.vendor_matched) = norm_vendor_dedupe(s2.vendor)
                                AND upper(btrim(p.number)) = upper(btrim(s2.invoice_number)))
                ) f ON TRUE
                LEFT JOIN LATERAL (
                    -- When the STATEMENT email arrived — the basis for the go-live cutoff (not the
                    -- invoice's transaction date). A statement that arrived after go-live lists mail
                    -- we should have captured, so its unmatched lines are real 'missing', even for an
                    -- invoice with an old printed date (a late-arriving invoice). received_at is the
                    -- true Gmail arrival; date_added is the fallback for a manual/no-receipt statement.
                    SELECT COALESCE(e.received_at::date, e.date_added) AS stmt_date
                    FROM emails e WHERE e.filename = s2.statement_file
                    LIMIT 1
                ) se ON TRUE
                WHERE sl.id = s2.id AND sl.status <> 'dismissed'
                RETURNING sl.status
                """,
                {"grace": grace_days, "golive": override},
            )
            counts = {}
            for (st,) in cur.fetchall():
                counts[st] = counts.get(st, 0) + 1

            # Intercept open exceptions (missing/amount_mismatch) whose vendor is on the
            # QuickBooks exclusion list and mark them 'excluded_vendor' instead — the SAME
            # rule as classify_emails.is_excluded_vendor() (bidirectional normalized substring
            # against the Exclude from QB flags on Setup › Vendor names). Those vendors are never entered in
            # QB through this pipeline, so a missing/mismatch flag is noise. Since
            # list_open_statement_exceptions filters status IN ('missing','amount_mismatch'),
            # an 'excluded_vendor' line simply drops out of the Review queue — no change needed
            # there. Recomputed every run, so already-ingested excluded lines self-heal next run.
            # Both exclusion kinds silence recon "missing" noise: a QB-excluded vendor never enters
            # QB through this pipeline, and a statement-recon-excluded vendor (utility whose
            # statement IS the bill) has no separate invoice to match. The unified
            # vendor_exclusion_spellings view yields every spelling of any vendor_name_map row with
            # EITHER flag set (so aliases/store spellings are honored, per Joel's 2026-09-24 decision).
            cur.execute("SELECT DISTINCT name FROM vendor_exclusion_spellings")
            excluded = [n for n in (_norm(r[0]) for r in cur.fetchall()) if n]
            if excluded:
                cur.execute("SELECT id, vendor, status FROM statement_lines "
                            "WHERE status IN ('missing','amount_mismatch')")
                override_ids = []
                for lid, vendor, st in cur.fetchall():
                    nm = _norm(vendor)
                    if nm and any(ex in nm or nm in ex for ex in excluded):
                        override_ids.append(lid)
                        counts[st] = counts.get(st, 0) - 1
                if override_ids:
                    cur.execute("UPDATE statement_lines SET status='excluded_vendor', "
                                "updated_at=now() WHERE id = ANY(%s)", (override_ids,))
                    counts["excluded_vendor"] = counts.get("excluded_vendor", 0) + len(override_ids)

            # --- Second pass: alias-aware rescue -----------------------------------------
            # The bulk SQL above matches vendor by LITERAL text equality, so a recorded invoice
            # under a slightly different spelling gets a false 'missing'/'amount_mismatch'. Re-check
            # only the still-open exceptions (a small set) using classify_emails' OWN vendor
            # standardization (vendor_name_map aliases/store spellings + fuzzy) — the same source of
            # truth as QB entry. A line matches here iff BOTH its own vendor AND a candidate
            # invoice's vendor_matched (sharing the invoice_number) resolve to the same standard
            # vendor. This only WIDENS matching; raw-text matches from pass 1 are untouched. No-op
            # when the tables aren't seeded (same guard apply_vendor_standardization uses).
            counts["alias_resolved"] = 0
            try:
                import classify_emails as ce
                # Load the raw tables via THIS module's loader (NOT ce.load_vendor_standardization(),
                # which routes through _database().init_db() and would deadlock on our own open
                # reconcile transaction), then re-key the lookup with the classifier's real
                # normalization — exactly what ce.load_vendor_standardization() does internally, so
                # matching stays identical to QB entry, just without the init_db() side trip.
                std = load_vendor_standardization()
                std["lookup"] = {ce._norm_vendor_std_key(k): v for k, v in std["lookup"].items()}
            except Exception as e:
                std = None
                print(f"(statement alias-rescue skipped — could not load vendor standardization: {e})")
            if std:
                cur.execute("SELECT id, vendor, invoice_number, amount, status FROM statement_lines "
                            "WHERE status IN ('missing','amount_mismatch')")
                open_lines = cur.fetchall()
                for lid, vendor, inv_no, amount, st in open_lines:
                    stdv = ce.resolve_standard_vendor(vendor, std)
                    if not stdv:
                        continue
                    # Candidate recorded invoices sharing this invoice number (any vendor spelling).
                    cur.execute(
                        "SELECT vendor_matched, amount FROM invoices "
                        "WHERE upper(btrim(invoice_number)) = upper(btrim(%s)) AND amount IS NOT NULL "
                        "ORDER BY id DESC", (inv_no,))
                    cands = [ra for (rv, ra) in cur.fetchall()
                             if ce.resolve_standard_vendor(rv, std) == stdv and ra is not None]
                    if not cands:
                        continue
                    amt_match = next((ra for ra in cands
                                      if amount is not None and abs(float(ra) - float(amount)) <= 0.01), None)
                    if amt_match is not None:
                        new_status, chosen = "matched", amt_match
                    elif st == "missing":
                        new_status, chosen = "amount_mismatch", cands[0]
                    else:
                        continue  # already amount_mismatch and still no amount match -> no change
                    cur.execute("UPDATE statement_lines SET status=%s, recorded_amount=%s, "
                                "updated_at=now() WHERE id=%s", (new_status, chosen, lid))
                    counts[st] = counts.get(st, 0) - 1
                    counts[new_status] = counts.get(new_status, 0) + 1
                    if new_status == "matched":
                        counts["alias_resolved"] += 1
        conn.commit()
    finally:
        conn.close()
    counts["flagged"] = counts.get("missing", 0) + counts.get("amount_mismatch", 0)
    return counts


def load_learnings():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT learned_rule, date_added, from_email FROM learnings ORDER BY id")
            return [{"Learned Rule": a, "Date Added": str(b or ""), "From Email": c or ""}
                    for a, b, c in cur.fetchall()]
    finally:
        conn.close()


def save_learnings(rows):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM learnings")
            for row in rows:
                rule = (row.get("Learned Rule") or "").strip()
                if not rule:
                    continue
                cur.execute(
                    "INSERT INTO learnings (learned_rule, date_added, from_email) VALUES (%s,%s,%s) "
                    "ON CONFLICT (learned_rule) DO NOTHING",
                    (rule, _date(row.get("Date Added")), row.get("From Email")),
                )
        conn.commit()
    finally:
        conn.close()


def load_history():
    conn = connect()
    try:
        with conn.cursor() as cur:
            # from_address (the source email's sender) is joined in so check_duplicate can prefer
            # a store-scanned copy over the vendor's own send on an exact duplicate.
            cur.execute("SELECT h.vendor_matched, h.store_matched, h.invoice_number, h.invoice_date, "
                        "h.invoice_amount, h.source_file, h.source_email_id, h.date_added, "
                        "h.duplicate_group_id, e.from_address "
                        "FROM invoice_history h LEFT JOIN emails e ON e.filename = h.source_file "
                        "ORDER BY h.id")
            out = []
            for a, b, c, d, e, f, sid, g, gid, fa in cur.fetchall():
                out.append({
                    "vendor_matched": a, "store_matched": b, "invoice_number": c,
                    "invoice_date": d, "invoice_amount": float(e) if e is not None else None,
                    "source_file": f, "source_email_id": sid, "date_added": str(g or ""),
                    "duplicate_group_id": gid, "from_address": fa,
                })
            out.extend(_kept_copies_missing_from_ledger(cur, "invoice"))
            return out
    finally:
        conn.close()


# Live copies that settled a duplicate set but never made it into the ledger. A copy the pipeline
# flagged stays OUT of the ledger while a person decides (and an auto-ignored copy is never added);
# when a person then keeps it on Possible duplicates (or restores it), nothing added it — so later
# copies were compared only with the copies that had been IGNORED. Incento #12598 (2026-10-11): the
# kept scan #3381 wasn't in the ledger, a new scan matched only the ignored vendor copy, "superseded"
# it again and stayed clean — two live copies. Rule: live, not flagged, in a duplicate group, not
# skip-QB, not a waiting payment reminder, and no ledger row for this record's own file. The rows are
# added to every load (so a keep made while a run is in progress isn't lost to save_history's
# rewrite) and persist with that run's save_history.
_KEPT_COPY_SQL = {
    "invoice": (
        "SELECT i.vendor_matched, i.store_matched, i.invoice_number, i.invoice_date, i.amount, "
        "       i.email_filename, i.date_added, i.duplicate_group_id, e.from_address "
        "  FROM invoices i LEFT JOIN emails e ON e.filename = i.email_filename "
        " WHERE i.duplicate_ignored IS NOT TRUE AND i.duplicate_suspect IS NOT TRUE "
        "   AND coalesce(i.duplicate_group_id, '') <> '' AND coalesce(i.invoice_number, '') <> '' "
        "   AND i.skip_qb_import IS NOT TRUE AND i.legacy_migrated IS NOT TRUE "
        "   AND NOT (coalesce(i.notes, '') LIKE '%%Payment reminder for invoice #%%' "
        "            AND coalesce(i.notes, '') LIKE '%%NO matching invoice is on file%%') "
        "   AND NOT EXISTS (SELECT 1 FROM invoice_history h WHERE h.source_file = i.email_filename "
        "                     AND lower(h.invoice_number) = lower(i.invoice_number) "
        "                     AND norm_vendor_dedupe(h.vendor_matched) = norm_vendor_dedupe(i.vendor_matched)) "
        " ORDER BY i.id"),
    "payment": (
        "SELECT p.vendor_matched, p.store_matched, p.reference_number, p.payment_date, p.amount, "
        "       p.email_filename, p.date_added, p.duplicate_group_id, p.invoice_number "
        "  FROM payments p "
        " WHERE p.duplicate_ignored IS NOT TRUE AND p.duplicate_suspect IS NOT TRUE "
        "   AND coalesce(p.duplicate_group_id, '') <> '' "
        "   AND p.skip_qb_import IS NOT TRUE AND p.legacy_migrated IS NOT TRUE "
        "   AND NOT EXISTS (SELECT 1 FROM payment_history h WHERE h.source_file = p.email_filename "
        "                     AND lower(coalesce(h.reference_number, '')) = lower(coalesce(p.reference_number, '')) "
        "                     AND norm_vendor_dedupe(h.vendor_matched) = norm_vendor_dedupe(p.vendor_matched)) "
        " ORDER BY p.id"),
}


def _kept_copies_missing_from_ledger(cur, kind):
    """Ledger-shaped rows (invoice_history / payment_history) for the live copies described above.
    source_email_id comes from another ledger row for the same file (a reprocessed split file keeps
    its original email), else the file itself — what the pipeline would have recorded."""
    ledger = "invoice_history" if kind == "invoice" else "payment_history"
    cur.execute(_KEPT_COPY_SQL[kind])
    rows = cur.fetchall()
    if not rows:
        return []
    cur.execute(f"SELECT source_file, source_email_id FROM {ledger} "
                f"WHERE source_file = ANY(%s) AND coalesce(source_email_id, '') <> ''",
                ([r[5] for r in rows],))
    origin = {f: sid for f, sid in cur.fetchall()}
    out = []
    for a, b, c, d, e, f, g, gid, extra in rows:
        row = {"vendor_matched": a, "store_matched": b, "source_file": f,
               "source_email_id": origin.get(f) or f, "date_added": str(g or ""),
               "duplicate_group_id": gid}
        if kind == "invoice":
            row.update({"invoice_number": c, "invoice_date": d,
                        "invoice_amount": float(e) if e is not None else None, "from_address": extra})
        else:
            row.update({"reference_number": c, "payment_date": d,
                        "amount": float(e) if e is not None else None, "invoice_number": extra})
        out.append(row)
    print(f"  LEDGER: added {len(out)} live {kind} cop{'y' if len(out) == 1 else 'ies'} from settled duplicate "
          f"sets that weren't in {ledger} yet.")
    return out


def email_from_addresses():
    """{email filename: from_address} for every email — used to resolve a record's sender-store
    at dedup time (check_duplicate's store-vs-vendor preference)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, from_address FROM emails")
            return {fn: fa for fn, fa in cur.fetchall()}
    finally:
        conn.close()


def load_credit_memo_keys():
    """Every recorded credit memo with a number, for the classifier's credit memo duplicate gate
    (credit memos have no ledger). Read-only."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, vendor_matched, credit_memo_number, amount, email_filename, "
                        "entered_in_qb FROM credit_memos "
                        "WHERE credit_memo_number IS NOT NULL AND btrim(credit_memo_number) <> '' "
                        "ORDER BY id")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def find_live_invoice(source_file, vendor_matched, invoice_number):
    """Look up the live invoices-table row for a prior invoice_history match, by source file
    + vendor + invoice number. Returns {id, entered_in_qb, duplicate_group_id} or None.

    NOTE: a history entry's `source_file` is the recording run's result filename, which the
    invoices table stores as `email_filename` (there is no `source_file` column on invoices) —
    so the match is on email_filename. Newest row wins if somehow more than one matches.

    Vendor is compared with the SAME legal-suffix normalization check_duplicate() uses
    (_norm_vendor_dedupe), so an auto-supersede lookup still finds its prior when the two copies
    were extracted under different suffixes ("Lantev Distributing Corp" vs "…Company"). Without
    this, the store-preferred/store-conflict caller silently failed to supersede the earlier copy
    and mis-flagged the new one as a suspect instead. email_filename + invoice_number already scope
    the match tightly, so relaxing only the vendor suffix can't introduce a wrong match.

    The vendor also matches on its STANDARD name (classify_emails._dedupe_vendor_key — the same key
    check_duplicate has used since 2026-10-08). Without that, a prior check_duplicate matched by
    standard name ('MaxiHealth' vs 'Maxi Health') wasn't found here, so the store-preferred supersede
    flagged the store scan instead of ignoring the vendor's copy (MaxiHealth #803670, 2026-10-12)."""
    import classify_emails as ce   # function-level import avoids a circular import at module load
    target = ce._norm_vendor_dedupe(vendor_matched)
    std_target = ce._dedupe_vendor_key(vendor_matched)
    conn = connect()
    try:
        with conn.cursor() as cur:
            # duplicate_ignored too (2026-10-12): an already-ignored prior means a person kept a
            # DIFFERENT copy, so the caller must not "supersede" it again and keep the new copy clean.
            cur.execute(
                "SELECT id, entered_in_qb, duplicate_group_id, vendor_matched, duplicate_ignored FROM invoices "
                "WHERE email_filename = %s AND lower(invoice_number) = lower(%s) ORDER BY id DESC",
                (source_file, invoice_number or ""))
            for r in cur.fetchall():
                if ce._norm_vendor_dedupe(r[3]) == target or ce._dedupe_vendor_key(r[3]) == std_target:
                    return {"id": r[0], "entered_in_qb": bool(r[1]), "duplicate_group_id": r[2],
                            "duplicate_ignored": bool(r[4])}
            return None
    finally:
        conn.close()


def find_live_payment(source_file, vendor_matched, reference_number):
    """Payment counterpart of find_live_invoice — matches the live payments row on
    email_filename + reference_number + vendor (vendor via the SAME _norm_vendor_dedupe
    legal-suffix normalization check_duplicate uses, so a suffix-spelling difference doesn't
    defeat an auto-supersede lookup; email_filename + reference_number keep the match tight).
    Also matches on the STANDARD vendor name, like find_live_invoice."""
    import classify_emails as ce   # function-level import avoids a circular import at module load
    target = ce._norm_vendor_dedupe(vendor_matched)
    std_target = ce._dedupe_vendor_key(vendor_matched)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, entered_in_qb, duplicate_group_id, vendor_matched, duplicate_ignored FROM payments "
                "WHERE email_filename = %s AND lower(reference_number) = lower(%s) ORDER BY id DESC",
                (source_file, reference_number or ""))
            for r in cur.fetchall():
                if ce._norm_vendor_dedupe(r[3]) == target or ce._dedupe_vendor_key(r[3]) == std_target:
                    return {"id": r[0], "entered_in_qb": bool(r[1]), "duplicate_group_id": r[2],
                            "duplicate_ignored": bool(r[4])}
            return None
    finally:
        conn.close()


def set_group_id(record_type, record_id, group_id):
    """Retroactively stamp a duplicate_group_id onto an already-recorded prior record, so the
    ORIGINAL — not just the later copy — carries the group tag. Invoices/payments only."""
    table = {"invoice": "invoices", "payment": "payments"}.get(record_type)
    if not table:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET duplicate_group_id = %s WHERE id = %s",
                        (group_id, record_id))
        conn.commit()
    finally:
        conn.close()


def list_open_reminder_records():
    """Live (not ignored) invoices recorded as a payment REMINDER whose invoice wasn't on file yet —
    the 'NO matching invoice is on file' note classify_emails.apply_duplicate_checks writes.
    [{id, email_filename, vendor_matched, invoice_number, entered_in_qb, duplicate_group_id}]."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, email_filename, vendor_matched, invoice_number, entered_in_qb, duplicate_group_id "
                "  FROM invoices WHERE duplicate_ignored IS NOT TRUE "
                "   AND notes LIKE '%Payment reminder for invoice #%' "
                "   AND notes LIKE '%NO matching invoice is on file%' ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def supersede_duplicate_invoice(invoice_id, duplicate_of, reason, group_id=None):
    """Retroactively mark an already-recorded invoice as a duplicate, ignored — mirrors the
    exact update web_db.py does for duplicate_ignored/duplicate_of, plus stamps
    duplicate_suspect and duplicate_reason for the audit trail, and the shared duplicate_group_id
    so a superseded (store-conflict) record also carries the group tag. Used when a LATER copy of
    the same invoice reclassifies it to a different store and should supersede this earlier one."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE invoices SET duplicate_suspect = TRUE, duplicate_ignored = TRUE, "
                "duplicate_reason = %s, duplicate_of = %s, "
                "duplicate_group_id = COALESCE(%s, duplicate_group_id) WHERE id = %s",
                (reason, duplicate_of, group_id, invoice_id))
        conn.commit()
    finally:
        conn.close()


def flag_store_conflict_prior(invoice_id, reason, group_id=None, actor="store-conflict check"):
    """Mark an already-recorded invoice a POSSIBLE duplicate (suspect, NOT ignored) because a later
    copy of the same invoice + amount names a different store -- so it drops off QB entry and shows
    with the later copy on Possible duplicates until a person keeps the right one. Never touches a
    copy already entered in QuickBooks or already ignored. Logged to edit_log. True if it changed."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE invoices SET duplicate_suspect = TRUE, duplicate_reason = %s, "
                "duplicate_group_id = COALESCE(%s, duplicate_group_id) "
                "WHERE id = %s AND entered_in_qb IS NOT TRUE AND duplicate_ignored IS NOT TRUE "
                "  AND duplicate_suspect IS NOT TRUE",
                (reason, group_id, invoice_id))
            changed = cur.rowcount > 0
            if changed:
                cur.execute(
                    "INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                    "VALUES ('invoices', %s, 'duplicate_suspect', 'false', 'true', %s)",
                    (invoice_id, actor))
        conn.commit()
        return changed
    finally:
        conn.close()


def save_history(rows):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM invoice_history")
            for row in rows:
                cur.execute(
                    "INSERT INTO invoice_history (vendor_matched, store_matched, invoice_number, "
                    "invoice_date, invoice_amount, source_file, source_email_id, date_added, "
                    "duplicate_group_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (row.get("vendor_matched"), row.get("store_matched"), row.get("invoice_number"),
                     row.get("invoice_date"), _num(row.get("invoice_amount")),
                     row.get("source_file"), row.get("source_email_id"), _date(row.get("date_added")),
                     row.get("duplicate_group_id")),
                )
        conn.commit()
    finally:
        conn.close()


def load_payment_history():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT vendor_matched, store_matched, reference_number, payment_date, "
                        "amount, source_file, source_email_id, date_added, duplicate_group_id, "
                        "invoice_number "
                        "FROM payment_history ORDER BY id")
            out = []
            for a, b, c, d, e, f, sid, g, gid, inv in cur.fetchall():
                out.append({
                    "vendor_matched": a, "store_matched": b, "reference_number": c,
                    "payment_date": d, "amount": float(e) if e is not None else None,
                    "source_file": f, "source_email_id": sid, "date_added": str(g or ""),
                    "duplicate_group_id": gid, "invoice_number": inv,
                })
            out.extend(_kept_copies_missing_from_ledger(cur, "payment"))   # see load_history
            return out
    finally:
        conn.close()


def save_payment_history(rows):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM payment_history")
            for row in rows:
                cur.execute(
                    "INSERT INTO payment_history (vendor_matched, store_matched, reference_number, "
                    "payment_date, amount, source_file, source_email_id, date_added, "
                    "duplicate_group_id, invoice_number) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (row.get("vendor_matched"), row.get("store_matched"), row.get("reference_number"),
                     row.get("payment_date"), _num(row.get("amount")),
                     row.get("source_file"), row.get("source_email_id"), _date(row.get("date_added")),
                     row.get("duplicate_group_id"), row.get("invoice_number")),
                )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Emails table (gmail_fetcher upserts source-email metadata here)
# --------------------------------------------------------------------------- #

def upsert_email_source(filename, gmail_message_id=None, gmail_link=None,
                        subject=None, source="gmail", from_address=None, received_at=None):
    """Record a fetched email's source metadata. Status is set to 'fetched'
    (NOT 'processed') so the classifier still picks it up; an existing status
    (e.g. 'processed' from a prior run) is preserved. from_address is captured
    at fetch time and used as a self-scan store signal during classification;
    received_at is Gmail's authoritative server receive time (internalDate)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO emails (filename, source, gmail_message_id, gmail_link, subject,
                                    from_address, received_at, status, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'fetched', now())
                ON CONFLICT (filename) DO UPDATE SET
                    source           = EXCLUDED.source,
                    gmail_message_id = EXCLUDED.gmail_message_id,
                    gmail_link       = EXCLUDED.gmail_link,
                    subject          = COALESCE(EXCLUDED.subject, emails.subject),
                    from_address     = COALESCE(EXCLUDED.from_address, emails.from_address),
                    received_at      = COALESCE(EXCLUDED.received_at, emails.received_at),
                    status           = COALESCE(emails.status, 'fetched'),
                    updated_at       = now()
                """,
                (filename, source, gmail_message_id, gmail_link, subject, from_address, received_at),
            )
        conn.commit()
    finally:
        conn.close()


def get_received_dates():
    """{filename: received_at ISO string} for emails that have one — the
    authoritative server receive time, carried to the classifier's date checks."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, received_at FROM emails WHERE received_at IS NOT NULL")
            return {fn: dt.isoformat() for fn, dt in cur.fetchall() if dt}
    finally:
        conn.close()


def get_gmail_links():
    """{filename: gmail_link} for emails that have one — used to carry links
    into invoice/payment records at processing time."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, gmail_link FROM emails WHERE gmail_link IS NOT NULL")
            return {fn: link for fn, link in cur.fetchall()}
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized (tables created if missing).")
