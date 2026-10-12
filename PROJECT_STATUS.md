# PROJECT_STATUS.md — Pharmacy Invoice Email Classifier

> **Purpose of this file:** a single, always-current snapshot of the project so a
> fresh Claude chat (or a new person) can understand the whole system by reading
> only this file — no need to re-read the codebase. It is updated after every
> significant change. Last updated: **2026-10-12**.

---

## 1. What this project does

An automated local pipeline that processes a multi-location pharmacy group's
incoming vendor invoice emails end-to-end:

1. **Fetch** ([`gmail_fetcher.py`](gmail_fetcher.py)) — pulls invoice emails from
   Gmail into `test_emails/` as `.eml` files (or drop files there manually).
2. **Classify** ([`classify_emails.py`](classify_emails.py)) — sends each email
   (text **plus** PDF/image attachments) to the Claude API, which classifies it and
   extracts structured invoice / payment / credit-memo data.
3. **Store** — writes to **PostgreSQL** ([`database.py`](database.py)) when
   `DATABASE_URL` is set, or **Excel** otherwise (fully backward compatible).
4. **Files** — saves the actual source document per billable record (attachment,
   split to just that record's pages, or an email-to-PDF render); optionally
   uploads them to **Google Drive** ([`drive_uploader.py`](drive_uploader.py)) and
   stores the share link.
5. **Human-in-the-loop review** — records the AI isn't sure about go to a review
   queue instead of results; you answer them, and the next run applies your answers
   and permanently "learns" your notes.

`run_all.py` chains fetch → classify in one command. Every record carries a
**Gmail deep-link** back to its source email.

The user (js@maplerx.com) runs the scripts themselves — **Claude does not have the
Anthropic API key, Google credentials, or DATABASE_URL** and cannot run them.
Claude edits the code and stages files; the user runs it.

---

## 2. The pharmacy entities (6 + back office, + Lakewood Rx)

| Store | Aliases / DBAs | Address |
|---|---|---|
| Maple Pharmacy | Maple Monsey, Maple Vitamins, maplerx.com | 59 Route 59 Ste 143, Monsey NY 10952 |
| Madison Pharmacy | — | 400 Madison Ave, Lakewood NJ 08701 |
| Refuah Pharmacy | — | 911 E County Line Rd, Lakewood NJ 08701 |
| Westgate Pharmacy | — | 112 Hillside Blvd, Lakewood NJ 08701 |
| 3M Pharmacy | 3M Remedies, Maple NJ | 1091 River Ave, Lakewood NJ 08701 |
| BMW | BMW Technologies LLC | 382 Route 59 Ste 276, Airmont/Monsey NY 10952 |
| Lakewood Rx | — | (blank — fill in via Stores tab if needed) |

- **BMW address (382 Route 59 Ste 276)** is the shared back-office/billing address;
  it can appear as Bill-To for ANY store and must NOT by itself change the store match.
- **Do not confuse** 59 Route 59 Ste 143 (Maple store) with 382 Route 59 Ste 276 (BMW office).
- Store identity is resolved by **address match**, not just the name on the invoice.
- **Lakewood Rx** (7th store, added 2026-08-19) exists mainly for the **Sageteq** billed-entity rule
  (see §5m) — Sageteq lists it just below the Bill-To box. It is now a valid `store_matched` value.
  **Do not confuse** this selectable store with the flag-don't-map related entity **Lakewood Rx Inc /
  Rx Fleet Inc @ 10 Prag Blvd #203, Monroe NY** on the next line — different entity, still flagged.
- Excluded/related entity (flag, don't map): Lakewood Rx Inc / Rx Fleet Inc, 10 Prag Blvd #203, Monroe NY.

**Vendors excluded from QuickBooks import** (`skip_qb_import: true`): Smith Drug Company,
McKesson, Parmed, James RX / JAMS RX, Masters, SmartSource(/Smartsource/ABC), Anda /
Anda New, BluPax / BluPax Pharma, NDC Distributors(/LLC), Prescription Supply, Auburn,
NuMed, Top RX, AmerisourceBergen. **Exclusion applies to invoices & credit memos only —
payments are always entered in QB** regardless of vendor.

---

## 3. Current architecture (data flow)

```
test_emails/          <- inbox: new .txt/.eml/.msg files to process
review_pending/       <- emails the AI flagged for review (open these to answer)
        |
        v
  classify_emails.py  (python classify_emails.py)
        |
   Phase 1: reprocess review_queue.xlsx rows that you ANSWERED
            (source read from review_pending/, your answer + learnings injected)
   Phase 2: process new emails in test_emails/
        |
        +--> resolved (not flagged) ---> results.xlsx (+ invoice_history.xlsx)
        |                            ---> qb_ready.xlsx (non-skip invoices + credit memos)
        |                            ---> source docs saved to invoice_files/ or payment_files/
        |                            ---> email archived to processed_emails/
        |
        +--> flagged / unreadable ----> review_queue.xlsx (email moved to review_pending/)
```

### Run phases (`main()`)
- **Phase 1 — resolve answers:** loads `review_queue.xlsx`. For each row with an
  `Answer` filled in: appends its `Notes to Learn` to `learnings.xlsx`, reprocesses
  the email from `review_pending/` with the answer + all learnings injected, and on
  success sends it to results and moves the email to `processed_emails/`. Rows without
  answers stay. Reprocess errors keep the row for retry.
- **Phase 2 — new emails:** processes `test_emails/` (skips filenames already in
  `results.xlsx`). Non-flagged → results + files + `processed_emails/`. Flagged /
  unreadable → `review_queue.xlsx` + email moved to `review_pending/`. Read errors
  stay in `test_emails/` for auto-retry.

### Key behaviors
- **Per-record review (important):** review is flagged per invoice/credit-memo/payment,
  NOT per email. When a combined scan holds several invoices, the clean ones are processed
  into results/qb_ready and only the questionable one(s) go to the review queue.
- **Each flagged record becomes its OWN standalone split file** in `review_pending/`
  (just that record's page(s), named Store-Vendor-Invoice#-Amount-Date). The review-queue
  row's `Filename` is that split file; `Source Email` names the batch it came from.
  Answering it reprocesses ONLY that one file (read as a single-record "email") — the
  clean records in the same batch are never re-read and never falsely marked duplicate.
  The original batch email is archived to `processed_emails/` once its clean records land.
- **qb_ready column `Possible Duplicate`** (renamed from `Needs Review`): "Yes" means the
  vendor+invoice# already exists in `invoice_history.xlsx` — verify before entering.
- **Skip-already-done:** filenames already in `results.xlsx` (without an Error) are
  skipped; errored rows are retried. Successfully processed emails leave `test_emails/`.
- **Duplicate detection** is done in Python (vendor + invoice #, in `invoice_history.xlsx`),
  never by the model guessing from wording. Flagged items are NOT added to history until resolved.
- **Empty/parse-fail → retry** up to 3×; if still failing → routed to review queue as
  "unreadable" (not left erroring forever).
- **Text-less message guard:** an image-only / scanned-PDF email with no body (and no reviewer
  answer to append) would leave the user message's first text block empty, which the API rejects
  (400 "text content blocks must be non-empty"). `classify_email()` now substitutes a minimal
  instruction ("classify from the attached document(s) below") whenever `text` is blank, so the
  scan is classified from its attachment instead of 400-ing. Only fires when the text is genuinely
  empty (normal emails are unchanged); a truly unreadable scan with no vendor match still, correctly,
  stays flagged for a human.
- **Attachments read natively by Claude** (PDF/image), no local OCR. Attachment type is
  detected by **file extension** when the MIME type is generic (`application/octet-stream`).
- **Zip attachments auto-expand** (§5s): a `.zip` is opened in memory and replaced by its contained
  PDFs/images before classification, so zipped invoices classify automatically. A locked/corrupt/
  oversized/nested zip is left as-is → the record flags as before (and the manual-upload path §5r
  handles it).
- **Over-large images auto-downscaled before sending:** the API rejects an image whose longest edge
  exceeds 8000 px (400 `invalid_request_error`) and internally downscales anything past ~1568 px
  anyway, so `build_attachment_blocks` runs each image through `_downscale_image_for_api` (Pillow),
  capping the long edge at `MAX_IMAGE_EDGE = 1568` (aspect preserved). No legibility loss vs. what the
  API would use; within-limit images pass through untouched; Pillow-missing/corrupt/any error → sent
  as-is (safe fallback). Fixes emails with a big `image001.jpg`-style attachment that previously
  400-ed. See the 2026-08-19 note below.
- **Deterministic 400s no longer retry forever:** a `400 invalid_request_error` is a permanent
  client-side problem (unlike a connection blip), so `classify_with_retry` returns a PLAIN error for
  it (not `infra_error`) and the email routes to the review queue as "unreadable" for a human, instead
  of being left in `test_emails/` to retry every run. (401 still fails the run fast; genuine
  connection/network errors are still `infra_error` → left for retry.)
- **TLS:** this machine is behind a Techloq inspection proxy; `ca_bundle.pem` (certifi +
  Techloq root) is used for HTTPS if present, else default certs.

---

## 3b. Storage modes (PostgreSQL vs Excel — backward compatible)

Chosen automatically by `classify_emails.py`:
- **No `DATABASE_URL`** → Excel only, exactly as the original tool. (`--no-db` forces this.)
- **`DATABASE_URL` set** → writes to PostgreSQL (`database.py`). Pass **`--excel`** to ALSO
  write the Excel workbooks. Each saved invoice/payment file is **uploaded to Google Drive by
  default** (since 2026-10-08) and its share link stored as the file path; **`--no-drive`** (or
  `UPLOAD_TO_DRIVE=0`) skips it for a run. `--drive` / `UPLOAD_TO_DRIVE=1` are still accepted (and are
  what turns Drive on in Excel-only mode).

`database.py` load/save helpers deliberately return/accept the SAME dict shapes (keyed by
the Excel column names), so the processing logic is identical in both modes. DB tables:
`emails, invoices, payments, credit_memos, review_queue, learnings, invoice_history`.
`qb_ready` is not a table — it's derived (non-skip invoices + credit memos); `Entered in QB`
is a boolean column on `invoices`/`credit_memos`. `migrate_from_excel.py` loads existing
Excel output into the DB (preserving history + the Entered-in-QB flags).

## 4. Output files (Excel mode — all real `.xlsx`, bold frozen header, autofilter)

| File | Contents |
|---|---|
| `output/results.xlsx` | One row per invoice / payment / credit_memo / informational email. Accumulates across runs. Has a `Gmail Link` column. |
| `output/qb_ready.xlsx` | QB import sheet: non-skip **invoices** + **credit memos** (credit = negative amount). Manual `Entered in QB` + `Date Added` preserved across regenerations (keyed on Filename + Invoice #). `Possible Duplicate` column (was `Needs Review`). |
| `output/invoice_history.xlsx` | Ledger of every invoice ever recorded (dup-check source). |
| `output/review_queue.xlsx` | Flagged items awaiting review. `Filename`=the record's split file, `Source Email`=original, `Gmail Link`, + your `Answer` and `Notes to Learn`. |
| `output/learnings.xlsx` | Durable rules distilled from your review notes; injected into the model on every run. Hand-editable. |
| `output/gmail_links.json` | Sidecar written by `gmail_fetcher.py`: filename → gmail deep-link, read by the classifier to stamp `Gmail Link` on records. |

**File Path column** (results + qb_ready): points to the saved source document.
Saved for non-skip invoices, non-skip credit memos, and **payments** (proof of payment).
NOT saved for skip-QB records, statements/ship-notices, or flagged items.

### Folders
| Folder | Role |
|---|---|
| `test_emails/` | Inbox of pending emails. |
| `review_pending/` | Flagged emails, kept here so you can open them while answering the queue. |
| `processed_emails/` | Archive of every resolved source email. |
| `invoice_files/` | Per-record source doc for **invoices & credit memos**. Named `Store - Vendor - Invoice # - Amount - Date`. Multi-record PDFs are split to just that record's pages. |
| `payment_files/` | Same, for **payments**. |
| `pending_full_reprocess/` | (staging) the 44 non-review emails set aside for the eventual full reprocess. |
| `output_prev_run/`, `invoice_files_prev_run/`, `review_emails_temp/` | Backups from earlier runs (safe to delete once satisfied). |

---

## 5. The system prompt's key rules (in `SYSTEM_PROMPT_TEMPLATE` + reference tables)
- The prompt's **store list** and **excluded-vendor list** are no longer hardcoded — they're
  spliced in at run time from the `stores` / `excluded_vendors` reference tables (see §5a) via
  `build_system_prompt()` (`%%STORE_LINES%%` / `%%EXCLUDED_VENDORS%%` sentinels). Everything else
  is the static template.
- Resolve store by address, incl. the Maple vs 3M "Maple Pharmacy" DBA overlap.
- 382 Route 59 in Bill-To is normal (back office), don't let it change the store match.
- Address match beats name (e.g. "Lakewood Pharmacy" @ 911 E County Line = Refuah).
- Shipment notices / statements → informational, `route_to: none`, don't flag unless something's wrong.
- **Revised/amended/re-issued invoices → flag for human review** (avoid double entry).
- **Recount line items before flagging a sum ≠ total mismatch** (2026-09) — the model must re-add every
  line item one at a time and confirm the sum is actually wrong before citing a line-item/total
  discrepancy as a review reason, so an arithmetic slip in its own explanation doesn't false-flag a
  correct invoice (reproduced: All Natural #14380 $78.50, where the model dropped a $12 item and
  double-counted an $11 item while justifying a nonexistent $1 gap). Mitigation, not a guarantee —
  worth tracking whether it actually cuts the false-positive rate.
- **Verify the amount against the document's printed total, digit for digit** (2026-09-08) — a separate
  prompt clause: before writing `invoice_amount`/`credit_amount`/`payment_amount`, re-read the exact
  digits printed next to "Total"/"Amount Due"/"Balance" and confirm they match, letting a nearby number
  (price/qty/item code/PO#) not bleed into the total; a single line's Qty×Price that equals the printed
  total is a confirmation signal, not a substitute. Targets a DIFFERENT failure than the recount rule
  above — a pure transcription error with nothing to add (reproduced: Marine Park #232738 printed
  "Total: $92.00" with one line 4×$23.00=$92.00, recorded as **$592.00** — an invented leading digit).
  Mitigation only; an LLM can still misread occasionally.
- **Invoice + payment note in one email → two records** (an invoice AND a payment).
- **Paid order/receipt (e.g. Shaklee) → still an invoice** (the bill) + a payment; never "just a payment."
- **Payments always `skip_qb_import: false`**, even for excluded vendors.
- **Multi-store cost-split invoice → `skip_qb_import: true` on EVERY per-store record** (added 2026-09).
  When ONE invoice (single invoice #) splits a shared cost across multiple store sections, each with its
  own subtotal (e.g. **Sageteq's** monthly "IT Service Plan": Bill-To Lakewood Rx, sections for Lakewood
  Rx / 3M / Carepax / Madison / Maple Medex / Refuah / Westgate), Joel enters it as ONE combined bill on
  the Lakewood Rx entity, so none of the per-store records go in QB. The model still emits one record per
  store section (audit trail) but marks them all `skip_qb_import` with an explanatory `notes`. Keyed on
  invoice **SHAPE**, not vendor — a normal single-store Sageteq invoice (#29347–29353) is unaffected.
  ⚠️ **This is a structural rule baked into the prompt text** (near the excluded-vendors paragraph), NOT
  a Setup-page toggle like `excluded_vendors` — changing/removing it later means editing the prompt in
  `classify_emails.py`, not the UI.
- Real invoices (incl. skip-QB) → `route_to: accounts_payable`; `none` only for informational.
- `source_document` + `source_pages` per record → drives which file/pages get saved.
- `page_rotations` per record → the model reports the clockwise degrees (0/90/180/270) each
  attributed page needs to display upright; the saved/split PDF is rotated accordingly (see §5d).

---

## 5a. Editable reference tables (config lists, curated in the web UI)

Three lists that used to be hardcoded now live in PostgreSQL and are **read by the pipeline on
every run**, so edits take effect on the next `classify_emails.py` run with no code change.
Created + **seeded on first run only if empty** (by `database.init_db`), so nothing breaks
before curation and curation is never clobbered. In Excel-only mode (no `DATABASE_URL`) the
classifier falls back to the same seed defaults (`DEFAULT_*` in `database.py`).

| Table | What it drives | UI page |
|---|---|---|
| `stores` | The 6 pharmacy entities (name, aliases/DBAs, address, disambiguation notes). The classifier rebuilds the prompt's store bullets from this. | **Stores** |
| ~~`excluded_vendors`~~ | **Retired.** Replaced 2026-09-24 by the Exclude from QB / Exclude from stmt recon checkboxes on `vendor_name_map` (Setup › Vendor names), which the prompt's exclusion line, `is_excluded_vendor()` and statement recon read via the `vendor_exclusion_spellings` view. The old flat tables were **archived 2026-10-08** as `archived_excluded_vendors` (27 rows) / `archived_statement_recon_excluded_vendors` (1 row) — kept, never read, no longer created. | **Vendor names** |
| `sender_store_map` | Self-scan mailboxes → store. An email **From** one of these gets a **decisive** store signal injected into the model (self-scan); unmapped third-party senders still resolve by address-on-invoice. | **Senders** |
| `card_store_map` | Card last-4 → store. Any invoice, credit memo, or payment whose card matches gets the mapped store (high confidence) and isn't flagged merely for an uncertain store (see §5k). Seeded EMPTY (no safe default card set); curated on the **Cards** tab. | **Cards** / **Card → store** |
| `class_rules` | Default QuickBooks **class** by store and/or vendor — fills a record's class when the document didn't state one (an explicit class wins). Precedence both-fields > vendor-only > store-only. Seeded EMPTY; curated in the UI. See §5u. | **Class rules** |
| `store_scope_rules` | Vendor-scoped store guards, enforced in code by `apply_scoped_learning_gate` (2026-10-08): "only these vendors" (Carepax→BMW: Sageteq/Microsoft; Accuscript→Lakewood Rx: East Coast Building Maintenance) and "every vendor except these" (112 Hillside = Westgate, except Braun). Seeded ONCE (not re-seeded after Setup deletes). No UNIQUE key (required `reference`). See "FIX SWEEP …", item 8. | **Store rules** |
| `vendor_name_map.trust_line_item_totals` | Checkbox on **Vendor names**: a ticked vendor's derived (summed-from-line-items) totals are accepted with a note instead of sent to review. Default off for all. | **Vendor names** |

- **From-address capture:** `gmail_fetcher.py` records each email's sender (`emails.from_address`,
  and the `gmail_links.json` sidecar). The classifier also parses `From:` from the email text, so
  the self-scan signal works for manually-dropped files too (`_sender_store_hint`).
- **Editing/audit:** same list/add/edit/delete pattern as the Learnings page. Every change is
  written to `edit_log` **and** stamped on-row (`created_by/at`, `edited_by/at`) — ids are stable
  because the CLI only reads these tables. **Delete is admin-only + confirmed.** Add/edit enforce
  the UNIQUE key (`store_name` / `vendor_name` / `sender_email` / `card_last4`) with a friendly error.
  `class_rules` has no UNIQUE key (a required `class` instead) — the CRUD supports that (see §5u).
- **Reference-valued fields use a dropdown, not free text** (prevents typos/mismatches). A column can
  declare `{"type":"select","options_from":"<source>"}`; the store columns of **Card→store** and
  **Sender→store** use `options_from:"stores"` — the option list is `web_db.store_names()` (the canonical
  `store_name`s in display order). `_reference_options(page)` builds `{source: [values]}` from
  `web_db.REFERENCE_OPTION_SOURCES` (add future lists there); `reference.html`'s `ref_select` macro
  renders the `<select>` in BOTH the add-row and inline-edit forms with a blank option + the current
  value preselected, and — importantly — **keeps a legacy/typo'd value that isn't in the list** as a
  selected "(not in list)" option so it stays visible/editable. The **Cards** assign-store control uses
  the same `store_names()` list. Non-select columns render as text/textarea as before. Backend
  (`add/edit_reference`) is unchanged — the select posts the same field name.
- Seed defaults + readers: `database.py` (`DEFAULT_STORES`, `DEFAULT_EXCLUDED_VENDORS`,
  `DEFAULT_SENDER_STORE_MAP`, `DEFAULT_CARD_STORE_MAP`; `load_stores/load_excluded_vendors/
  load_sender_store_map/load_card_store_map`). Web CRUD: `web_db.py` (`list/add/edit/delete_reference`).
  Routes: `review_app.py` (`/reference/{table}` family, driven by `REFERENCE_PAGES`; template
  `templates/reference.html`).

---

## 5b. Run & email traceability

Every classify run gets a **run_id** (`YYYYMMDDThhmmss`, generated in
`classify_emails.main()`), recorded in a `runs` table (`started_at`, `finished_at`,
`n_emails`, `n_results`, `n_review`, `learnings_hash` — the last drives the gated queue re-check,
see §5e). The run_id is stamped on:
- **`emails.run_id`** — every email processed in that run (records inherit it via
  their `email_filename` join; `all_records` view + `list_qb_ready` expose it).
- **`review_queue.run_id`** — items flagged in that run. Carryover rows keep their
  original run_id across the queue's full-replace.

**Real review timestamp:** `review_queue.flagged_at TIMESTAMPTZ` (the old
`date_flagged` is date-only). It's preserved across the queue's DELETE+re-INSERT via
`COALESCE(%s, now())` — carryover rows keep their first flag time; only genuinely new
rows get `now()`. Shown (date + time) + run_id on the Review queue list and item pages.

**UI run filter:** a "Run" dropdown (newest first, showing time + `N rec / N rev`
counts, from `web_db.list_runs()`) on **Browse**, **QB entry**, and **Review queue**,
combinable with the existing filters. Filtering by run joins `emails` on `run_id`.

**Per-email grouping:** Browse and QB show a **Source email** column
(`email_filename`) that links to `/browse?email=<filename>` so every record from one
email (invoice + credit memo + payment) groups together. Review pages link the source
email the same way.

All additive — existing rows have `run_id`/`flagged_at` = NULL (pre-tracking) and still
render. Excel-only mode is unaffected (run tracking is DB-only).

---

## 5c. Authoritative received date (scanner-clock fix)

**Problem:** the classifier saw the email's `Date:` header, and some store scanners
(Refuah, Maple) have mis-set internal clocks that put that header years off (e.g.
2024/2025 on a mail that truly arrived now). That falsely tripped an
"invoice date is later than the email date" review flag.

**Fix (uses the TRUE server receive time, never the `Date:` header):**
- `gmail_fetcher.py` captures Gmail's **`internalDate`** (authoritative server receive
  time) → `emails.received_at TIMESTAMPTZ` + `received_at` in `gmail_links.json`.
- `classify_emails.py` loads it (`load_received_dates()` → `RECEIVED_AT`) and injects an
  `[EMAIL RECEIVED — authoritative … : <ts>]` line into the message; for a manually
  dropped `.eml` with no captured date it falls back to the **top `Received:` header**
  (`_received_from_eml`), never the `Date:` header. The system prompt now tells the model
  to use that timestamp for date sanity checks and to ignore the `Date:` header (scanner
  skew), while still allowing a flag for an invoice date implausibly in the FUTURE vs the
  true received time.
- **Backfill:** `backfill_received_at.py` (dry-run default, `--commit`) fills
  `received_at` for old rows from the sidecar or the stored `.eml`'s `Received` header.
- **Rectify already-flagged items:** `requeue_email_date_flags.py` (dry-run default,
  `--commit`) backs up `review_queue`, finds the date-discrepancy rows, and splits them:
  - **Pure-date items** (a row whose ONLY reason was the date, on an email where every
    flagged record is like that) → **requeued**: source `.eml` back to `test_emails/`,
    `emails.status='fetched'`, its rows deleted, stale split files removed. Re-running
    `classify_emails.py` resolves them and they vanish.
  - **Mixed items** (a row that ALSO has a genuine reason — e.g. the Maxi Health "page 1
    of 3, pages missing" scan) → **kept**; only the date SENTENCE is stripped out of the
    row's `question` in place (`_strip_date_sentences`), so the review line now shows just
    the real issue. Never requeued or deleted; each edit logged to `edit_log`.

  So the date discrepancy disappears everywhere, pure false positives clear themselves on
  the next run, and genuine review lines stay put (minus the date note) — no churn, no
  duplicate re-flagging. The dry-run prints each requeue and every before→after strip.

---

## 5d. Auto-rotate saved PDF pages to upright

Scans often arrive upside-down or sideways. Since the model already reads every page to
classify, it also reports **`page_rotations`** per record — an object mapping the 1-based
`source_document` page number (same numbering as `source_pages`) to the CLOCKWISE degrees
(0/90/180/270) needed to make that page upright. For a single **image** attachment (a
photo/scan, so `source_pages` is null) the model reports the rotation under key `"1"`. On
save the saved file is rotated so it displays right-side up for reviewers — no local OCR.

- **PDFs** — `_extract_pdf_pages` applies `page.rotate(deg)` per page (via pypdf).
- **Images (PNG/JPG/GIF/WebP)** — `_rotate_image_bytes` (via **Pillow**) first runs
  `ImageOps.exif_transpose()` to normalize phone-photo EXIF orientation, then applies the
  model's clockwise rotation on top (`Image.rotate(-deg, expand=True)` — PIL rotates CCW, so
  the degrees are negated). Covers photos of invoices (e.g. WhatsApp `IMG-*.jpg`) that the
  PDF path skipped.
- Applies to **all** saved source docs — `invoice_files/`, `payment_files/`, and the
  `review_pending/` split files — because they all go through `_write_record_document`.
  PDFs work in both the split path (multi-record) and whole-document path (single-record
  still rotated).
- **Display-only:** rotation never changes extraction or page-to-record attribution. It
  rotates the /Rotate flag (additive to any existing rotation, which is correct — the model
  reports relative to what it sees). Mirrored/flipped scans are a reflection, not a rotation
  → out of scope.
- **Display-only + safe fallback:** rotation never changes extraction or attribution.
  Missing/invalid/0 `page_rotations` → saved as-is (`_parse_rotations` drops anything not in
  {90,180,270}). Both helpers return None = keep the original bytes when there's nothing to
  do, and `_rotate_image_bytes` also returns None if **Pillow isn't installed** or on any
  error — the save never fails.
- No DB/schema change — the rotation is baked into the saved file at write time.

### Blank-page drop + page reorder in split files (2026-09-22)

Two additive per-record fields alongside `source_pages`/`page_rotations`, reusing the SAME
multimodal page read the model already does (no second pass, no new dependency):
- **`blank_pages`** — list of 1-based `source_document` page numbers (within `source_pages`) that
  are ENTIRELY blank; `_extract_pdf_pages` drops them from the saved file. Prompt is strict:
  a faint letterhead/footer is NOT blank; omit when unsure. Never drops EVERY page (all-blank
  claim → kept, safe fallback).
- **`page_order`** — an exact permutation of the record's `source_pages` in correct reading order,
  set ONLY when the model can read a printed "Page X of Y" showing scan order is wrong; else null.
  `_extract_pdf_pages` uses it via `_apply_page_order` **only if it validates as an exact
  permutation of the selected pages**, else falls back to ascending — reordering on a guess never
  happens.
- Order of ops in `_extract_pdf_pages`: select pages → apply `page_order` → drop `blank_pages` →
  rotate → rebuild only if the final sequence differs from the untouched original or a page needs
  rotating (`return None` = keep original bytes, unchanged).
- **Chunking**: `_offset_page_list()` shifts both new fields from chunk-local to original PDF
  numbering in `_classify_oversized_scan`'s merge, alongside the existing
  `_offset_page_spec`/`_offset_rotations` — or blank/reorder would target wrong pages on big scans.
- **Prompt**: both fields added to all three record schemas + a conservative instruction paragraph
  by the `source_pages`/`page_rotations` explanation; MOST documents need neither.
- **Safe-fallback contract (same as source_pages/page_rotations)**: a wrong/absent value on either
  field never loses data — worst case the page stays exactly as today. Verified: **270 old-vs-new
  comparisons** (15 real PDFs × 6 specs × 3 rotations) with the fields absent → **byte-identical**;
  unit tests cover drop/reorder/subset/all-blank/bad-permutation/offset. Joel's Boiron scan
  (8 pages, pages 3 & 5 blank) → 6 pages via the mechanism; the real model-driven values come from
  a reprocess run (needs the API key).

---

## 5e. Excluded-vendor review handling

Vendor exclusion (`skip_qb_import`) applies to **invoices & credit memos only**; **payments are
always entered in QB** regardless of vendor. Since an excluded invoice/credit memo is never
entered in QuickBooks, sending it to human review is wasted effort — so it's suppressed from the
queue but still recorded.

- `routes_to_review(rtype, rec)` = the model flagged it **and** it isn't an excluded-vendor
  invoice/credit memo. This predicate (not raw `record_flagged`) now gates every review path:
  `result_has_review`, `build_review_rows`, `save_review_files`, and the "skip to review"
  decision in `build_result_rows`.
- An excluded invoice/credit memo the model flagged is therefore **not queued and gets no split
  file**, but IS emitted to results (so it shows in Browse/History). The recorded row is marked
  `Needs Review = No` with a note `[review skipped: excluded vendor — not entered in QuickBooks…]`
  (`review_suppressed_by_exclusion` + `_EXCLUDED_SKIP_NOTE`).
- **Payments are never touched by this** — `routes_to_review` only consults `is_excluded_vendor()`
  for `invoice`/`credit_memo`, so a flagged payment still routes to review and a normal payment is
  entered in QB (`skip_qb_import` stays false). `is_excluded_vendor()` is applied to payments
  nowhere (also guarded in `web_db.update_record_field`).
- **Deterministic `skip_qb_import` enforcement (2026-09).** The model is told the excluded list in
  the prompt but occasionally still sets `skip_qb_import=false` for an excluded vendor (found live: 36
  McKesson invoices sitting QB-ready). `apply_vendor_exclusion(rec)` — run in the invoice + credit-memo
  loops right after `apply_class_rules`, before `routes_to_review` — forces `skip_qb_import=True`
  whenever `is_excluded_vendor(vendor_matched)`, making the outcome certain instead of probabilistic.
  Only ever flips False→True (no regression). Invoices/credit memos only. Applies on (re)processing —
  it does NOT retroactively update already-recorded rows (those need a reprocess or a one-time
  `UPDATE … SET skip_qb_import=TRUE WHERE <excluded vendor>`).
  - **Retroactive backfill run 2026-09-14.** The one-time UPDATE above was run once against the live
    DB to fix rows recorded before the enforcement landed: **78 invoices + 4 credit_memos** flipped
    to `skip_qb_import=TRUE` (ParMed/Parmed 38, McKesson/McKesson Corporation 36, Smith Drug Company 4).
    None were `entered_in_qb`, so nothing already booked in QB was touched. Bidirectional
    normalized-substring match against `excluded_vendors`, same rule as `is_excluded_vendor()`. This is
    a completed one-off; the code enforcement handles all future runs, so it should not need repeating.
- **Duplicate logic is unchanged:** `apply_duplicate_checks` still keys on raw `record_flagged`,
  so this change touches only review routing, never dedup.
- **Review-item breadcrumb:** the review detail page shows a compact, read-only "Other records
  from this email" list — sibling records already recorded (not in the queue): vendor + # +
  amount + status (`excluded — in Browse` for a skipped excluded record, else `recorded`), each
  linking to `/browse?email=<source_email>`. Backed by `web_db.list_email_siblings(source_email)`
  (records in `all_records` sharing the review item's `source_email`; flagged records aren't in
  results so they never appear).
- **Retroactive clearing of old queued items (self-healing):** `routes_to_review()` only blocks
  NEW excluded-vendor invoices/credit memos from entering the queue — items queued by an OLDER run
  (before the vendor was added to the exclusion list) used to ride the unanswered carryover forever
  because carryover never re-checked exclusion. Fixed in `classify_emails.main()` Phase 1
  (additive): right after `queue_carryover` is computed, rows whose `Vendor Matched` is now
  `is_excluded_vendor()` are split into `exclusion_clear`, removed from carryover, and reprocessed
  through the SAME existing answered-item loop (`resolve_list = answered + exclusion_clear`) with no
  reviewer answer (`Answer` is ""). An excluded invoice/credit memo then reprocesses to
  `routes_to_review()=False` → recorded to Browse (review-skipped), dropped from the queue. An
  excluded-vendor **payment** reprocesses back as still-flagged (payments are never excluded) and is
  regenerated as a fresh review row, so it correctly stays queued — nothing is silently dropped.
  A reprocess error or missing split file keeps the row in the queue for retry (same as answered
  items). The run summary prints `N excluded-vendor item(s) cleared`. Works in both DB and Excel
  modes (only `load/save_review_queue` + `is_excluded_vendor`; no schema change). The existing
  backlog clears itself on the next classify run, and any future vendor added to the exclusion list
  self-heals its already-queued items the same way.

- **Gated whole-queue re-check against updated learnings (Phase 1b):** a queued item can only resolve
  DIFFERENTLY once a rule changed, so instead of re-checking the open queue every run (wasteful API
  calls), `classify_emails.main()` re-checks it ONLY on runs where the learnings changed since the
  previous run. **Detection:** the learnings text is hashed (sha256) *after* this run's review answers
  are folded in, and compared to the hash the previous run stored — so it fires for BOTH a rule edited
  in the Learnings tab between runs AND a rule added via a review answer this run. The hash lives on
  `runs.learnings_hash` (additive migration; written by `finish_run`, read by
  `database.get_last_learnings_hash`); no baseline (first run with the feature) → no re-check.
  **What it does:** reprocesses each remaining UNANSWERED, non-excluded carryover item once with the
  current `learnings_text` and `reviewer_answer=""`, **adopt-only-if-resolved** — a reprocess that now
  resolves is recorded to results and the row leaves the queue; one that stays flagged (or errors) is
  DISCARDED and the ORIGINAL row is kept untouched (its `flagged_at`/run lineage preserved). Targets
  are captured *before* the answered/exclusion resolve loop appends its failures back, so only genuine
  unanswered carryover is re-checked. Prints, only when it runs: `Re-checking N queued item(s) against
  updated learnings — resolved M, kept N-M.` **DB-only** (needs the `runs` table to compare
  run-to-run); in Excel-fallback mode the queue carries over exactly as before. For the *existing*
  backlog after adding a rule (no learnings change vs. the just-established baseline), use the manual
  `requeue_all_reviews.py` — this gate covers only changes going forward.

---

## 5f. Review lifecycle + Gmail-link fixes

- **Answered items stay in the queue** (visible via a toggle). `list_open_reviews` still holds EVERY
  row until a classify run resolves it, but the Review-queue LIST now **hides answered / pending-
  reprocess rows by default** and shows them via a **"Show answered (N)" toggle** (`show_answered=1` →
  `include_answered=True`; the default WHERE adds `AND (answer IS NULL OR answer = '')`). The item
  pages still show the **"Answered — pending reprocess"** status (with `reviewed_by/at`).
- **Review-queue list filters + ordering (read-only, additive).** `list_open_reviews` takes
  `store` / `vendor` / `search` filters (exact store/vendor match; `search` is a case-insensitive
  substring over vendor/store/invoice #/flag reason — shared `_review_filter_clause`, fully
  parameterized) plus the existing Run filter and column-sort whitelist. The list page has a filter
  bar (Run / Store / Vendor selects that auto-submit + a Search box; a hidden `sort`/`direction`
  preserves the active column sort across filter changes; a `clear` link). Dropdown options come from
  `review_filter_options(run_id)` (DISTINCT non-empty store/vendor currently in the queue) and the
  toggle label count from `answered_count(run_id, store, vendor, search)`. After the DB query the
  route **stable-sorts noted items (the 📝 note badge) to the BOTTOM** (`sort(key= 1 if note_count
  else 0)` — stable, so the DB order is preserved within each group). Pending-answer items still sort
  first by default. All read-only; the CLI pipeline is untouched.
- **Links survive review + rerun.** The Phase-1 reprocess reads the per-record SPLIT file, whose
  name isn't in `GMAIL_LINKS` (that's keyed by the original email filename) — so the reprocessed
  record used to lose its Gmail link. Fix: in `main()`'s answered loop, carry the review_queue
  row's stored `Gmail Link` onto the split filename (`GMAIL_LINKS[filename] = row["Gmail Link"]`)
  before reprocessing, so `build_result_rows` and the DB save stamp it. `file_path`/Drive link is
  regenerated as before by `assign_invoice_files`/`upload_record_files`.
- **Gmail deep-link opens the right mailbox.** `review_app.fix_gmail_link()` repairs a stale bare
  `/u/N/#…` link at display time by inserting `?authuser=<AP_MAILBOX>` (default
  `maplegroupap@lakewoodrx.com`, env `GMAIL_MAILBOX`) — idempotent, applied on the Review queue +
  item pages. Persist the fix in the DB by running `python backfill_gmail_links.py --commit`
  (already rewrites `review_queue.gmail_link` among the other tables).
- **Show the email's REAL Gmail receive time on the review screens (display-only, 2026-09).** The
  Review queue / item page / Ignored list showed `flagged_at` (when `classify_emails.py` happened to
  process the item) as the date — unrelated to when the email landed in Gmail, so searching Gmail by it
  found nothing. Now they show **`emails.received_at`** (Gmail's own `internalDate`, captured at fetch
  and never overwritten). `web_db.list_open_reviews` / `get_review` / `list_ignored_reviews` LEFT JOIN
  `emails` on `source_email` and expose `email_received_at`; `list_open_reviews` wraps the join in a
  subquery re-aliased `review_queue` so its existing unqualified WHERE/ORDER fragments stay valid (and
  `emails.run_id` can't collide with `review_queue.run_id`). Each screen now shows **ONE** email
  timestamp = the Gmail receive time (Review queue date column, the item page's **Received (Gmail)** row,
  and a **Received** column on Ignored). The old `flagged_at` (pipeline processing time) is **no longer
  shown** — it contradicted the Gmail time and broke searching Gmail by it. (Ignored keeps its distinct
  **Ignored**-at action time — when it was dismissed — which isn't an email date.) Purely additive to the
  web layer — no change to `classify_emails.py`, the CLI, or any recorded data. Emails whose `received_at` was never captured (≈329 older ones) show "—" until a one-time
  `python backfill_received_at.py --commit` (fills only NULL rows, from the `gmail_links.json` sidecar or
  the `.eml`'s own `Received:` header — never the unreliable `Date:` header). Verified live (read-only):
  all three queries execute, no `run_id` ambiguity under a sort filter, `email_received_at` populated on
  3/4 open + all ignored rows.
- **Ghost review rows on an interrupted run (fixed 2026-09).** `save_review_queue()` full-replaces
  `review_queue` **once**, near the end of `main()` — but individual records commit much earlier (the
  per-email + resolve loops). If a run was interrupted between an item resolving (its record committed
  to `invoices`/`credit_memos`/`payments`) and that final sync, the item's **old `review_queue` row was
  never removed** — a stale "ghost" that looks like an open duplicate of something already recorded
  (reproduced: review #2252 Bagel Nosh #280611, already recorded as invoice #1206). Fix: new
  `database.delete_review_row(filename)` (out-of-band DELETE-by-filename; the counterpart to
  `append_review_rows`), called the **moment** an item is confirmed resolved (`not
  result_has_review`) in BOTH resolve paths — the answered/retry/manual `resolve_list` loop and the
  updated-learnings re-check loop — so the queue reflects resolution incrementally. The end-of-run
  `save_review_queue(queue_out)` stays as the clean-run full-sync safety net (the incremental delete is
  idempotent with it). Verified: `delete_review_row` DELETEs by filename + commits, and is a no-op for
  an empty/None filename. **One-time backlog cleanup is NOT auto-run** — read-only `find_ghost_reviews.py`
  lists open review items whose (vendor+number+amount) is already recorded as a non-ignored
  invoice/credit_memo/payment, printing each with its match + a ready `DELETE … WHERE id IN (…)`, so Joel
  confirms each is a genuine ghost (not a real open dup) before deleting. The script deletes nothing.
- **Web changes made DURING a classify run are no longer overwritten (fixed 2026-10-12).** A run loads
  the queue at the start of phase 1 and full-replaces it near the end, so anything done in the web app
  in between was lost: an answer reset to blank, an assignment undone, an ignored item re-inserted (left
  in both the queue and `ignored_reviews`), a restored item deleted. Separately, `reviewed_by` /
  `reviewed_at` were never written back, so a kept answered row (errored reprocess) lost who answered.
  Fix, all in `database.py`: `load_review_queue()` remembers the loaded answer / Notes to Learn /
  assignee per id plus the DB time (`_QUEUE_AS_LOADED`); `save_review_queue()` then takes
  `LOCK TABLE review_queue IN SHARE ROW EXCLUSIVE MODE` (web writes wait for the commit; page reads
  don't), re-reads the live rows and merges via `_merge_live_review_changes()`: a field changed since
  the load → live value wins; a row gone from the queue with an `ignored_reviews` snapshot taken after
  the load → dropped; a row added since the load (restore) → kept; `reviewed_by/at` kept whenever the
  saved answer is the live one. A re-flagged row whose answer the run consumed stays blank (the live
  answer equals the loaded one, so nothing is resurrected). `_insert_review_row` now writes
  `reviewed_by/at` (NULL for callers that don't set them, e.g. `append_review_rows`). With no load in
  the process (`migrate_from_excel.py`) save is the old plain full replace. Run log, only when
  something merged: `Review queue: kept web changes made during this run - N item(s)
  answered/assigned, M ignored, K restored/added.` Verified with a self-reverting temp-table test
  (answer, assign, ignore, restore, errored-answer stamps, consumed-answer re-flag, no-load path;
  live counts unchanged). No `classify_emails.py` change.
- **Credit memos get a duplicate check (2026-10-12).** Credit memos have no ledger and no duplicate
  columns, so a credit sent twice reached QB entry twice. New `apply_credit_memo_duplicate_gate(c,
  filename)` in [classify_emails.py](classify_emails.py), called in `process_source_email` right after
  `apply_review_gates` (before files are filed, so the flagged copy gets its own review document): a
  credit memo whose vendor (standard-name `_dedupe_vendor_key`, like DU-01) + `_norm` number matches one
  already recorded from a DIFFERENT email, or seen clean earlier in this run, goes to review. Two reasons:
  same amount → "looks like a second copy … Ignore this item if it is, or answer \"enter it in
  QuickBooks\""; different amount → "possibly a revised credit memo". Names the earlier record id and
  whether it's entered in QB. Never auto-ignores. Skips skip-QB and excluded-vendor credits, no vendor /
  no number, an email reprocessed with a reviewer's answer (`_reviewer_answered`, so it can't loop), and
  the same email (`filename` or its `_record_source_id`). Pool: new read-only
  `database.load_credit_memo_keys()`, loaded once per run into `_CREDIT_MEMO_POOL`; clean in-run credits
  are appended. Live data (2026-10-12): 116 numbered credit memos, 8 same-number sets across emails; 7 are
  excluded vendors (McKesson ×5, SmartSource, Madison Pharmacy — never entered), the 8th is the known
  Prime Packaging #AR092424 pair (#104 entered, #112 not). Verified read-only: same/different amount flag
  with the right sentence (once, on repeat), same email / reviewer-answered / skip-QB don't, second copy
  in one run flags.
- **Retry/reprocess recovers From/To/Cc header context (fixed 2026-09).** A flagged item's reprocess
  source is its **split PDF/PNG** in `review_pending/`, which has no email headers of its own —
  `read_email_content` returns `text=""` for it (by design; the file IS the attachment). So on every
  retry, `_sender_store_hint` / `_contact_store_hint` (which parse `From/To/Cc` out of `text`) found
  nothing and returned "" — the store signal silently vanished on the 2nd+ pass (reproduced: #2779
  Herbal Health USA never re-resolving to Maple Pharmacy via `contact_store_map`). Fix: new best-effort
  `_read_headers_only(path)` (the header block of the original `.eml`/`.msg`, no body/attachments), plus
  a new `header_source_path` param on `process_source_email` that fills `text` from it **only when `text`
  is otherwise empty** (never touches the normal new-email path). Both reprocess loops pass
  `PROCESSED_DIR / r["Source Email"]` as that header source. Verified against the real #2779 `.eml`: the
  reprocess `text` now carries From/To/Cc and the contact hint fires (→ Maple Pharmacy); without the
  fallback it stays empty (the old bug).
- **Notes/attachments survive a reprocess rename — stable `review_key` (fixed 2026-09).** `review_notes`
  and `manual_attachments` were keyed by `review_queue.filename` to survive the queue's DELETE+re-INSERT,
  but the FILENAME itself isn't stable across a reprocess of the SAME record — it's regenerated from the
  (possibly changed) extracted store/vendor/date, and a store correction or a naming collision (suffix
  bumped to `__2`/`__4`) silently orphaned anything keyed to the old name (reproduced: RT-023426 from R&T
  Distributors, reprocessed 4× under different names). This is the **third symptom** of the same
  "identity doesn't survive reprocess" root cause (after ghost rows + lost headers). Fix: a stable
  `_stable_review_key(source_email, vendor, number)` = `source_email::norm_vendor::norm_number` —
  computed from fields that DON'T change between reprocesses (crucially **excludes store**, the thing
  that actually changes), so a still-flagged reprocess re-flags under the SAME key even when its filename
  changes. New nullable `review_queue.review_key` column (base DDL + ALTER), set by `build_review_rows`,
  written by `_insert_review_row`, read back by `load_review_queue`. All note/attachment callers now key
  on `review_key` (with a `review_key or filename` fallback for legacy rows): `review_app`'s list/detail/
  note/attach routes and the CLI resolve loop's `load_manual_attachments`/`mark_..._consumed`. The upload
  *folder* still uses the display filename (only the storage KEY changed). One-time backfill:
  `migrate_review_keys.py --commit` fills `review_key` on existing open rows (dry-run default; does NOT
  recover already-orphaned notes — those are gone, re-add by hand once). Old superseded `review_pending`
  files are already deleted on reprocess (the `pending_deletes` path). Verified (self-reverting test): the
  key is store-independent + vendor-normalized + number/email-sensitive; `build_review_rows` yields the
  SAME `review_key` across a store-correction rename (`unknown → Westgate`, different filename), so notes
  stay attached; record-less emails get a key too. ⚠ Schema (`review_key` column) applies on app restart
  / next `init_db()`; run the backfill after. (⚠ 2026-10-07: that held for a first-run row, but a re-flagged
  SPLIT item was rebuilt with the split PDF as its source email and so got a NEW key — fixed; see "FIX:
  re-flagged review items keep their identity".)

---

## 5g. Duplicate handling (four outcomes)

`check_duplicate(invoice, history, source_id)` compares by **vendor + invoice #** against
`invoice_history`, keyed by a **source-email identity** so a record never duplicates itself, and
returns one of four outcomes (SELF / REAL duplicate / POSSIBLE / STORE_CONFLICT):

- **SELF** — the only match is a row from the SAME source email → **not** a duplicate. Fixes a
  reprocessed/review item matching its own prior history row. Identity: `invoice_history` now
  carries `source_email_id` (a stable id) alongside `source_file` (the record's own filename);
  reprocessed split files keep the ORIGINAL email's identity via the `ORIGINAL_EMAIL` carry in
  `main()`'s Phase-1 loop (same mechanism as the Gmail-link carry).
- **REAL duplicate** — a DIFFERENT source email with vendor + invoice # + **amount all matching** →
  **auto-ignored**: `duplicate_ignored=True` + `duplicate_of` (the id of the record it duplicates).
  It's still recorded (shows in Browse marked **"Duplicate — ignored"** with a ↗ link to the
  original) but kept **OUT of QB entry** (`list_qb_ready` excludes `duplicate_ignored`) and OUT of
  review. A real duplicate is NOT added to history (so it can't later make the original look like a
  duplicate).
- **POSSIBLE** — a different source matches vendor + invoice # but the **amount differs** →
  `duplicate_suspect=True` + reason, flagged as before (shown with the possible-duplicate pill).
- **STORE_CONFLICT** (added 2026-09) — a different source matches vendor + invoice # + **amount**,
  but was recorded to a **DIFFERENT store**. This is a store *reclassification*, not an ordinary
  duplicate (Joel forwards a copy with an explicit reclassify-to-X instruction). Joel's convention:
  the **LATER** copy is the keeper; the **EARLIER**, already-recorded one is **auto-ignored**.
  `apply_duplicate_checks` (invoices only — credit memos don't run through `check_duplicate`) calls
  `database.find_live_invoice(dup_of, vendor, invoice#)` → if the earlier live row exists and is
  **not `entered_in_qb`**, `database.supersede_duplicate_invoice()` retroactively stamps it
  `duplicate_suspect/duplicate_ignored=TRUE, duplicate_of, duplicate_reason` (same UPDATE shape
  `web_db` uses) and the later copy survives **clean** (all dup flags cleared) + is added to history
  so a THIRD copy still dedupes against the survivor. **Safety:** if the earlier copy is **already
  entered in QuickBooks** (or the DB is unavailable / the prior row can't be found), nothing is
  overridden — the later copy is instead flagged **POSSIBLE** for a human ("earlier copy is already
  in QuickBooks — verify manually"), and is NOT appended to history. Accepted trade-off (confirmed
  with Joel): if the earlier one were ever actually the correct store, it's silently superseded —
  mitigated by never auto-superseding once something is in QB. The earlier copy's saved
  `invoice_files/` PDF is **orphaned, not auto-deleted** (logged on the console).
  - **CHANGED 2026-10-12: a store conflict no longer auto-supersedes unless a reviewer's answer produced
    the later copy.** None of the 3 real supersedes were a forwarded reclassify instruction: Garden of
    Life #52080970 (vendor email → Refuah; a scan from REFUAH's own scanner read 3M and won, 3005 live
    under 3M — likely wrong), #52099183 (later ended with all 3 copies ignored, none entered), Lantev
    #9GH5739 (a copy with NO store superseded Maple; both copies now ignored, none entered). Now, when the
    earlier copy is live and not entered and `classification["_reviewer_answered"]` is false, both copies
    become POSSIBLE duplicates: new `database.flag_store_conflict_prior(id, reason, gid)` sets the earlier
    one suspect (not ignored; skips entered/ignored/already-suspect; edit_log by "store-conflict check"),
    the new copy is suspect with a "Two copies … name different stores (X and Y) — open Possible
    duplicates and Keep this one on the copy with the right store" reason, shared group, not added to the
    ledger. Both drop off QB entry and show as one set on /duplicates. With a reviewer's answer the old
    supersede still runs (reason now ends "a reviewer's answer"). Entered-in-QB / prior-not-found
    fallbacks unchanged. Same-run pair (prior not in the DB yet): only the new copy is flagged, as before.
    `check_duplicate`'s store_conflict reason now says "this copy has it under Y" (not "reclassifies").
    Verified with a self-reverting single-connection test (no answer → both suspect, set on /duplicates;
    answer → superseded; entered prior untouched; live edit_log/suspect counts unchanged).
  - **OPEN (found 2026-10-12, data NOT changed — Joel to decide):** invoice groups where EVERY copy is
    duplicate-ignored, none entered, none skip-QB, with no person behind any ignore — the bill never
    shows for entry: Garden of Life #52099183 ($438.40, 3M; ids 1083/1404/1834 — note a matching payment
    #125 exists, ignored by joel), Lantev #9GH5739 ($413.30, Maple; 2156/3347), Lake Distribution #1051
    ($419.85, Madison; 356/357/358/590/946, all auto-ignored as duplicates of each other). Plus Garden of
    Life #52080970: the live copy 3005 is under 3M but came from refuahscanner@ and the vendor email said
    Refuah. Other all-ignored groups (Schwartz #235938, Emerson #E29402599, Herbal Health #2798 reminder,
    7 payments) have a person's ignore in them and look deliberate. Fix with Restore on the new
    `/no-live-copy` page (NOT /duplicates — it only shows sets that still have a live copy).
  - **BUILT 2026-10-12: "Bills with no live copy" monitor.** `web_db.no_live_copy_groups()` groups
    invoices + payments by `_duplicate_vendor_keyer()` + `ce._norm(number)` (same key as /duplicates) and
    keeps groups where every copy is `duplicate_ignored`, none `entered_in_qb`, none `skip_qb_import`.
    `by_person` = some copy's `duplicate_ignored_by` is a person (`_ignored_by_person`: non-blank and no
    script marker — claude / cleanup / backfill / recheck / auto / diagnosis; so joel, js, Amy are people;
    NULL = the pipeline). New web table `no_live_copy_confirmed` (record_type, vendor_key, number_key PK,
    confirmed_by/at) — "Confirmed — leave ignored" on the page; undo deletes; both logged to edit_log
    (table no_live_copy_confirmed, record_id 0). `no_live_copy_count()` (not by_person, not confirmed)
    feeds a new dashboard tile "Bills with no live copy" → `GET /no-live-copy` (sections: needs a look /
    a person ignored a copy / confirmed; each copy links to its record page and has a Restore button
    posting to the existing `/records/{type}/{id}/duplicate` action=restore). `POST /no-live-copy/confirm`.
    Template `templates/no_live_copy.html`. Live 2026-10-12: 13 groups, **5 need a look** (Lantev
    #9GH5739, Emerson #E29402599 [ignored by manual-import-cleanup, a script], Herbal Health #2798 [an
    auto-ignored payment reminder with no live invoice], Garden of Life #52099183, Lake Distribution
    #1051), 8 include a person's ignore. Verified: TestClient page + dashboard render (fake user
    -424242), confirm drops the count by 1 and shows "confirmed by", undo restores it, 2 edit_log rows,
    all rolled back (live counts unchanged). The web app needs a restart to show it (WEB_DDL adds the
    table on startup; already created on the live DB by the test's init_web_schema()).
  - `find_live_invoice` matches the live `invoices` row on **`email_filename`** (= a history
    entry's `source_file`); the `invoices` table has no `source_file` column, so the task's spec
    column name was corrected to `email_filename`.

**Vendor-suffix tolerance in dedupe (Part 1, 2026-09):** `check_duplicate` / `check_duplicate_payment`
now compare vendor names with a dedupe-only normalizer `_norm_vendor_dedupe()` that strips common
trailing legal-entity suffixes (PLLC / LLC / L.L.C. / Inc / Incorporated / Corp / Corporation / Co /
PC / Ltd / LLP, punctuation-tolerant, END only) so **"Schwartz & Company CPAs"** and **"…CPAs PLLC"**
match — the gap that let invoice #236187 record twice. The general `_norm()` / `is_excluded_vendor()`
are deliberately left unchanged.
- **`cpa`/`cpas` added to the strip set (2026-09-09).** The stripper stops at the first non-suffix word,
  so `"Schwartz & Company CPAs PLLC"` stripped only `PLLC` → `"schwartz & company cpas"` (stuck on the
  unknown word `cpas`), while `"Schwartz & Company"` stripped straight to `"schwartz &"` — different
  strings, so `check_duplicate` never matched the pair and #235938 recorded twice. Adding `cpa`/`cpas`
  lets it strip past the professional designation to reach `company` (the loop already chains several,
  e.g. `Co., Inc.`), so both now normalize to `"schwartz &"`. Scoped to accounting-firm naming (the case
  that occurred); extend the same way for a future `esq`/`p.a.` if one shows up.

Schema: `invoices.duplicate_ignored` + `invoices.duplicate_of`, `invoice_history.source_email_id`
(all additive migrations). The UI edit re-eval (`web_db.update_record_field`) uses the same
three-way result. With self-match excluded, in-review records could later be added to history
without false self-flags (enables the future review-merge work).

**Payments** get the same framework (`check_duplicate_payment` + a `payment_history` ledger with
`source_email_id`). Match key: **vendor + reference_number (+ amount)**:
- SELF — same source email id + same reference (or, when there's no reference, same vendor +
  amount + payment_date) → not a duplicate.
- REAL — DIFFERENT source with vendor + reference_number + amount ALL matching → auto-ignored
  (`payments.duplicate_ignored`). **Only when a reference_number is present** — a payment is
  never auto-ignored on amount alone.
- POSSIBLE — reference matches but amount differs, OR no reference but vendor + amount +
  payment_date match → flagged (`duplicate_suspect`), not auto-ignored.
Auto-ignored payments are **hidden from the default Payments view** (they're never entered) but
reachable with the **"show auto-ignored duplicates"** toggle (`?ignored=1`); when shown they're
marked **"Duplicate — ignored"** with a ↗ link to the original. Possible ones show a "possible
duplicate" pill. The Payments tab has a **Possible-Duplicate filter** (all / only flagged / only
not), the show-ignored toggle, and includes the flag in its CSV/Copy export. Schema:
`payments.duplicate_suspect|reason|ignored|of` + the `payment_history` table (additive migrations).
The payment **Edit page re-evaluates the flag live** (parity with invoices) when vendor /
reference / amount is edited — `web_db.update_record_field` runs `check_duplicate_payment` against
`payment_history` immediately. (This also fixed a latent bug in the invoice re-eval — both history
reads use a `RealDictCursor` and now index by column name, not position.)

### Re-record (churned-filename) duplicate fix (2026-08-20)

**Bug:** reprocessing a record could ADD a duplicate recorded row instead of REPLACING the original,
because the reprocess saved the split/source doc under a churned `__2` filename → the record's
`email_filename` changed → `save_results`' delete-by-`email_filename` couldn't replace the prior row,
and the SELF rule kept both. Confirmed on invoice id 701 + 704 (same email/vendor/#/amount) and the
paired payment — **double-counting money.** Fixed at three levels (all additive; normal same-filename
reprocess unchanged):

- **ROOT** (`_supersede_reprocessed_records`, run in `main()` before `apply_duplicate_checks`): for each
  REPROCESSED record, find a PRIOR ledger entry with the SAME source-email identity + IDENTICAL content
  but a DIFFERENT `source_file` (the churned earlier recording), drop that ledger entry, and add its OLD
  `email_filename` to `touched_filenames` so `save_results` DELETEs the stale row in place → the
  reprocess records cleanly as **exactly one** row. A same-filename reprocess finds no different-file
  prior → untouched.
- **SAFETY NET** (`check_duplicate` / `check_duplicate_payment`, new `source_file` arg): a SELF match
  whose prior is a DIFFERENT recorded file with IDENTICAL content is a **redundant re-record** →
  returns `duplicate` (auto-ignored, "redundant re-record of the same document") instead of recording a
  second counted row. Guarantees no double-count even if the root fix misses an edge. Backward
  compatible — the web edit-re-eval callers pass no `source_file`, so it stays inert there.
- **ONE-OFF CLEANUP** (`classify_emails.py --dedupe-records`, opt-in, exits without a classify run):
  scans `invoices`/`credit_memos`/`payments`, groups NON-ignored rows by content-key (vendor + number +
  amount), and where ≥2 share the same **base filename** (`email_filename` or `file_path`, ignoring a
  `__N` suffix — i.e. the same document) keeps the lowest id and DELETEs the rest, printing each.
  Different-source true duplicates are already `duplicate_ignored`, so they're skipped. Clears the
  existing backlog (drops invoice 704 + the duplicate Emerson payment, leaving 701 + one payment).

### Manual duplicate management (web, 2026-09) — invoices + payments

Until now nothing let a reviewer flip `duplicate_ignored` by hand (only the classify-time check and
the web edit-log re-eval set it). Three additive pieces (no CLI change; `duplicate_ignored`'s existing
`IS NOT TRUE` filters do the excluding):
- **Manual ignore/restore/clear action.** New `web_db.set_duplicate_ignored(record_type, id, ignored,
  user, duplicate_of=None)` (ignore = mark confirmed duplicate → out of QB; restore = clear it) and
  `clear_duplicate_flag(record_type, id, user)` ("not actually a duplicate" → drops `duplicate_suspect`
  only). Both audited to `edit_log`; invoices/payments only (credit memos aren't dup-checked). New
  audit columns `duplicate_ignored_by`/`_at` on `invoices`+`payments` (mirror `entered_qb_by/at`).
  One route `POST /records/{record_type}/{record_id}/duplicate` with `action` = `ignore` / `restore` /
  `clear` (+ optional `duplicate_of`). Buttons on **record_detail.html** and inline on **browse.html**
  rows (`ignore this one` / `undo`, `.linklike` text-buttons).
- **Records duplicate filter.** `list_all_records` gains `dup` (`yes`/`no`, mirrors `/qb`) and `ignored`
  (default **hides** ignored rows; `ignored=1` shows them, mirrors `/qb` + `/payments`). `browse()`
  threads both through and into `params` (so sort links preserve them via `sort_th`). Controls added to
  the Records filter form.
- **Dedicated "Possible Duplicates" page** (`/duplicates`, nav under Records). `list_duplicate_candidates()`
  returns every still-flagged (`duplicate_suspect` & not `duplicate_ignored`) invoice/payment paired with
  its best-guess counterpart — `duplicate_of` if set, else the earliest OTHER record with the same
  `number` + case-insensitive vendor. `templates/duplicates.html` shows one side-by-side card per group
  with **Ignore this one** (passes the counterpart's `email_filename` as `duplicate_of`) and **Not a
  duplicate — dismiss flag**; counterpart-less groups offer only dismiss. Empty state when clean.
- **Verified** (self-reverting fake-DB test): set-ignored marks ignored+suspect with COALESCE'd
  `duplicate_of` and an audit row; restore/clear behave and audit correctly; credit_memo is a no-op;
  `list_all_records` emits the right `dup`/`ignored` WHERE clauses (ignored hidden by default);
  `list_duplicate_candidates` pairs via `duplicate_of` then vendor+number, and yields `counterpart=None`
  when nothing matches. All modules import; all templates compile.

### Duplicate GROUP ID + /qb ignored toggle (2026-09)

Two additions so related records are visible/filterable as one cluster, and /qb matches /browse's
ignored control:
- **`/qb` "show ignored duplicates" toggle** — `list_qb_ready` previously excluded
  `duplicate_ignored` unconditionally; now `ignored='1'` reveals them (parity with /browse + /payments).
  Threaded through `qb_list` + `qb_export` (CSV matches screen) + the qb.html checkbox.
- **`duplicate_group_id`** — a new short (8-hex, e.g. `A1B2C3D4`), human-typeable TEXT tag stamped on
  **EVERY** record in a matched cluster (the original AND every later match), independent of
  `duplicate_suspect`/`duplicate_ignored` — a pure "these are related" tag. New nullable column on
  `invoices`, `payments`, `invoice_history`, `payment_history` (base DDL — the CLI writes it; credit
  memos stay out, not part of dedup). Threaded through the history load/save round-trip (so it persists
  run-to-run and a THIRD later match reuses the same id automatically — `prior` is the SAME dict
  save_history rewrites) and the `_insert_invoice`/`_insert_payment` inserts (`row["Duplicate Group"]`).
  - `check_duplicate` / `check_duplicate_payment` now return a **4th value**: the matched `prior` dict
    (all callers updated — 2 in classify, 2 in web_db's live re-eval).
  - `classify_emails._group_id_for(prior, record_type)`: reuse `prior`'s id if set, else mint one and
    stamp it on BOTH the in-memory prior AND (via new `database.find_live_{invoice,payment}` +
    `set_group_id`) the prior's LIVE row — so the ORIGINAL, not just the copy, shows the tag. Wired into
    every branch of `apply_duplicate_checks` (plain duplicate/possible, both store-conflict paths —
    `supersede_duplicate_invoice` gained a `group_id` param); mirrored for payments. Also wired into the
    manual `web_db.set_duplicate_ignored` (a hand-linked pair via `duplicate_of` gets/mints a shared id).
  - **Surface**: `all_records` view + `list_all_records` / `list_qb_ready` / `list_payments` gained a
    `group=` filter (and select the column); `/browse`, `/qb`, `/payments` routes thread `group`. Each row
    renders a clickable **"Group XXXXXXXX"** pill → `?group=…` re-filters that page to the whole cluster
    (original included, regardless of flags), with a "Filtered to duplicate group … — clear" banner.
- **Verified** (self-reverting fake-DB test): `check_duplicate` returns the prior dict (None on no match);
  `_group_id_for` mints an 8-char id, stamps the in-memory + live original, reuses on repeat, None-safe;
  a real duplicate makes original + copy share one id with the original's live row stamped once; a 3rd
  copy reuses the existing id with no re-stamp; `list_qb_ready` hides ignored by default / reveals on
  `ignored=1` / filters + selects `group`. All modules import; all templates compile.
- ⚠ **Schema applies on app restart** — `duplicate_group_id` is added by `init_db()` (columns) then the
  `all_records` view is recreated by `init_web_schema()` (correct order). The live web app / next CLI run
  must (re)start to pick up the new column before these queries run.

---

## 5h. Merged Records tab (Browse + History)

Browse and Invoice-history are now **one tab** ("Records"); the standalone History nav tab is
gone (`/history` 302-redirects to `/browse?source=history`). Because the two are genuinely
different row-sets — recorded records vs the dedup ledger — they share the tab via a **source
toggle**, each keeping its own columns/filters:

- **Records** (`source=records`, default) — `list_all_records` over the `all_records` view, now
  with a 4th UNION arm exposing `review_queue` rows as `record_type='review'` / status **"In
  review"** (record_id = the review id → links to `/review/<id>`). Filters: store, vendor, record
  type (incl. `review`), status, entered-in-QB, date range, run, search — plus the shared
  excluded filter. Safe to surface in-review rows now that `check_duplicate` excludes self-matches.
- **History ledger** (`source=history`) — `list_invoice_history`, READ-ONLY, with paging; filters:
  vendor, invoice #, store, date range, amount min/max — plus the excluded filter.
- **Excluded-vendors filter** (both sources): `include` / `exclude` / `only`, **defaulting to
  `exclude`**. Implemented in SQL by `_excluded_vendor_sql` as an `EXISTS`/`NOT EXISTS` against
  `excluded_vendors` using normalized substring match **either direction** — the same rule as the
  pipeline's `is_excluded_vendor`.
- **Sorting on all columns** via the `sort_th` clickable headers (which also reset paging).

---

## 5i. Chat Q&A (read-only, grounded)

A **Chat** tab (`/chat`) lets a signed-in user ask free-form questions about the invoice data and
get answers grounded in real records — Claude never sees raw SQL and can't change anything.

- `chat_qa.py` defines READ-ONLY query **tools** (fixed, parameterized SELECTs, no model-authored
  SQL): `search_records`, `get_record`, `explain_duplicate`, `list_review_items`, `totals`. Claude
  calls them in an agentic loop (`ask()`), then answers only from the results; the loop is capped
  (`MAX_TOOL_ITERS`) and every tool runs via `web_db.connect()` (autocommit reads).
- **"Why is X a possible duplicate?"** is deterministic: `explain_duplicate` returns the record's
  stored `duplicate_reason` (e.g. "Invoice #7650879366 from McKesson was already recorded from …")
  plus the matched record(s) sharing the same vendor + invoice/reference number — no guessing.
- Reuses the **Anthropic client + `ca_bundle.pem`** (Techloq TLS) and `ANTHROPIC_API_KEY` (same key
  the pipeline uses — must be in the web process's env); `MODEL` from `classify_emails`. Secrets
  (API key, `DATABASE_URL`, users) are never queried or returned.
- Routes: `GET /chat` (page) + `POST /chat/ask` (JSON `{question, history}` → `{answer, tools_used}`);
  both login-required, and `/chat/ask` degrades to a friendly message (never a 500) if the key is
  missing or the API errors. UI: `templates/chat.html` (message list + input, vanilla-JS fetch).
- **Non-blocking + fail-fast (don't hang the request):** `chat_qa.ask()` is fully synchronous
  (blocking Anthropic + DB calls), so `/chat/ask` runs it via `starlette.concurrency.run_in_threadpool`
  instead of calling it directly in the async handler (which would tie up the event loop and leave
  the request hanging). The chat Anthropic client sets `timeout=60` + `max_retries=1` so a stalled
  call fails fast. The frontend renders non-200 responses as visible text and uses an
  `AbortController` (90 s) so it can never hang silently — errors/timeouts always show a message.
  The chat JS conversation array is named `convo` (NOT `history`) and the send button id is
  `sendbtn` — naming them `history`/`send` collided with `window.history` / the element-id global
  and threw before the request fired (symptom: message bubble appeared, no `POST /chat/ask`, no reply).

---

## 5j. Global table layout (base.html)

Global CSS tweaks in `templates/base.html` apply to every page's tables (Review queue, Records,
QB entry, Payments, …): the content column is widened to `main { max-width: min(96vw, 1800px) }`
(was 1200px — data tables now use most of the viewport, side padding kept); rows are more compact
at `th, td { padding: 4px 10px }` (was `8px 10px`); and data rows get **zebra striping**
(`tbody tr:nth-child(even) { background: var(--zebra) }`, row-level so per-`td` hover still paints
over it).

**Link labels:** links no longer show long paths/filenames as their text. The source-email column
links read **"records"** (→ `/browse?email=…`, full filename kept in a `title` tooltip), file links
read **"File link"**, and Gmail deep-links read **"Email link"** — across Review queue, Records, QB
entry, and Payments. The **Invoice #** column on the Review queue is tightened
(`white-space:nowrap; width:1%`) so the "Why flagged" text gets the room.

**Currency:** amounts render as `$1,091.33` (negatives `-$50.00`) via the `money()` macro
(`_macros.html`) on Review queue, Records + History ledger, Payments, and the review-item page.
**QB entry is the deliberate exception** — it shows raw numbers (and the CSV/Copy `data-*` values
stay raw too) so figures paste cleanly into QuickBooks without `$`/commas.

**Grouped dropdown nav (2026-08):** the flat link row in `base.html` is now **six grouped dropdowns**.
Each group's PRIMARY item is a real top-level link (one click); secondary items live in a dropdown
under a caret. Routes are unchanged — this only regroups existing links.
- Groups (defined as a `groups` list in `base.html`, primary → children): **Review queue** (`/review`)
  → Tracked / Ignored / Sales orders; **QB entry** (`/qb`) → Payments; **Chat** (`/chat`) → Learnings;
  **Records** (`/browse`) → Emails; **Setup** (dropdown-only, no own page) → Stores / Vendors / Senders
  / Cards (`/cards`) / Class rules; **Users** (`/admin/users`, **admin-only**, no dropdown). Brand/home
  link and the right-side user + logout are unchanged.
- **Open behavior:** CSS-driven — opens on hover **and** on keyboard focus (`.navgroup:focus-within`);
  a tiny `toggleNav()` pins it open on click/tap (`.navgroup.open`), and a document-level outside-click
  handler closes any pinned menu. No libraries. The primary label still navigates (it's an `<a>`); the
  caret is a separate `<button>` so clicking it opens the menu instead of navigating. Setup's label is
  itself the toggle button (no primary page).
- **Active state:** the item for the current page is highlighted, and its **parent group is underlined**
  (`.navgroup-head.active`) so the section is always obvious. Computed from `request.url.path` via the
  `is_here()` macro (matches the primary or any child; `/review` also matches `/`). The primary link
  also gets a `.current` pill when you're exactly on its page.
- Responsive: the bar wraps on a narrow window (LAN desktop use); no hamburger.
- Verified (live DOM test): menu hidden by default → opens on caret-click and on focus → closes on
  outside click; Review group underlined + Tracked child active on `/tracked`; QB primary `.current` on
  `/qb`; Setup group underlined on `/reference/class_rules`; Users renders only for `role=='admin'`.

---

## 5k. Resolve a record's store by a card's last-4 (invoices, credit memos & payments)

Records — payments especially — are often flagged **only** because the store is unknown, and a
card's last 4 digits usually identify which pharmacy the document belongs to (a card ending shown
anywhere, e.g. an invoice memo "paid with card ending 1234", identifies the store the same way).
So a card last-4 → store map auto-assigns the store and suppresses that lone reason to flag,
uniformly across **invoices, credit memos, and payments**. Mirrors the `sender_store_map`
reference-table pattern end-to-end. **Additive; the CLI pipeline is unchanged when the map is empty
(the Excel-mode default).**

- **Extraction:** the `invoices[]`, `credit_memos[]`, and `payments[]` JSON schema blocks each carry
  `card_last4` ("last 4 digits of any card ending shown on the doc/memo, digits only, or null"); the
  prompt tells the model that on ANY record, if a card ending is shown/mentioned it captures the last
  4. Persisted on `invoices.card_last4` / `credit_memos.card_last4` / `payments.card_last4` (base DDL
  + `ADD COLUMN IF NOT EXISTS` migrations) and carried through `_invoice_row`/`_credit_row`/
  `_payment_row` (Card Last4) so exports/reprocessing keep it.
- **The map (`card_store_map`):** `id, card_last4 UNIQUE, store_matched, note` + audit columns —
  the SAME shape as `sender_store_map`. Read every run by `load_card_store_map()` → `{normalized
  last4: store}` (`database.norm_last4` = digits-only, keep trailing 4). Seed default is EMPTY
  (`DEFAULT_CARD_STORE_MAP = []` — there's no safe default card set). One card = one store.
- **Prompt:** `build_system_prompt` splices a `%%CARD_STORE_MAP%%` sentinel (lines like
  `1234 -> Madison Pharmacy`, or `(none configured)`), telling the model that for any invoice, credit
  memo, or payment whose card matches, set that record's `store_matched` to the mapped store with HIGH
  confidence and **not** flag it merely for an uncertain store.
- **Deterministic override (safety net):** a single helper `apply_card_store_map(rec)` — if the
  record's normalized `card_last4` is a key in `CARD_STORE_MAP`, set `rec["store_matched"]` to the
  mapped store (authoritative, overrides the model). Called for invoice, credit_memo, AND payment
  records at the top of each recording loop in `build_result_rows`, **before** the review-routing
  check, so a corrected store also reaches any review row / split-file name. It does **not** change
  flag logic — the prompt handles "don't flag" holistically. A record whose card isn't mapped is
  untouched, so a genuinely unresolvable one still flags for a human as before.
- **Cards discovery tab (`/cards`):** `web_db.list_seen_cards()` returns one row per distinct last-4
  seen across **all three tables** — count, last seen, example vendor, current store (LEFT JOIN
  `card_store_map`) — from **(a)** each record's `card_last4` (going-forward) and **(b)** a BACKFILL
  parsed from `payments.reference_number` **and** the `notes` field on invoices/credit_memos/payments.
  The backfill is **STRICT / cue-anchored** (`_card_from_reference` + `_CARD_LAST4_RE`): a last-4
  counts ONLY when it immediately follows an explicit card-ending **cue** ("ending in / ending with /
  ends in / card ending / acct|account ending", optional `#`/`:`/`-`) or a card **mask** (`****1234`,
  `xxxx-1234`, `x1234`), AND the 4 digits are a **bounded group** (not preceded by a digit, not
  followed by a digit or letter). This rejects coincidental 4-digit runs inside auth/reference codes,
  check/order/invoice/tracking numbers — e.g. the proven case `"…/ Auth 05923G"` no longer yields the
  phantom `0592`, while `"ending in 9704"` is surfaced correctly. Deliberately trades a few cue-less
  bare-4-digit historicals for ZERO false positives; the clean `card_last4` field covers new records.
  **Unmapped last-4s sort first** (the worklist). Each unmapped row has an inline store `<select>` → `POST /cards/assign`
  → `web_db.assign_card_store()` (upsert + `edit_log`); mapped rows link to the `card_store_map`
  reference editor. Nav "Cards" tab points here; the editor lives at `/reference/card_store_map`
  (**Card → store**).
- **Both storage modes:** DB mode reads/writes the tables; Excel-fallback mode uses
  `DEFAULT_CARD_STORE_MAP` (empty) so nothing changes until curated. No CLI behavior change beyond
  the additive store resolution above.

---

## 5m. Lakewood Rx store + the Sageteq Bill-To rule (2026-08-19)

Two additive pieces so **Sageteq** invoices resolve to the right store. Additive; the CLI is unaffected.

- **7th store "Lakewood Rx":** added to the hardcoded `store_matched` enum in the JSON schema (the
  top-level, `invoices[]`, and `credit_memos[]` lines — `payments.store_matched` stays a free string)
  and to `DEFAULT_STORES` (fresh installs / Excel fallback). For already-populated DBs (where the seed
  is skipped), a new `database._apply_standing_rules(cur)` — called from `init_db` on **every** init —
  runs `INSERT INTO stores (store_name, created_by) VALUES ('Lakewood Rx','web:rule') ON CONFLICT DO
  NOTHING`, so `%%STORE_LINES%%` includes it every run. Everything downstream is data-driven from the
  `stores` table (prompt bullets) / `distinct_values` (UI filters) / `list_reference('stores')` (Cards
  + reference dropdowns) — **nothing hardcodes exactly six stores**, so the 7th flows through cleanly.
  A store filter only lists a value once a record carries it (normal data-driven behavior).
- **Sageteq learning:** `_apply_standing_rules` also idempotently inserts `_SAGETEQ_LEARNING` into
  `learnings` (`learned_rule` UNIQUE, `date_added = CURRENT_DATE`, `from_email = 'web:rule'`,
  `ON CONFLICT DO NOTHING`) — so it's injected into the model every run and is editable/removable in
  the **Learnings** tab like any other rule. The rule: Sageteq always addresses the Bill-To box to
  Madison Pharmacy, so ignore the Bill-To box and read the real billed entity printed **just below**
  it — none → Madison Pharmacy; `Carepax` → BMW; `Lakewood Rx` → Lakewood Rx; another known pharmacy →
  that pharmacy; an unrecognized entity → `needs_human_review = true`. Applies ONLY to Sageteq invoices.
- **`_apply_standing_rules` is the pattern for future one-off standing additions** (unlike
  `_seed_reference_tables`, which only fills EMPTY tables and so never reaches a curated DB). Each
  statement is `ON CONFLICT DO NOTHING`, so re-running never clobbers hand edits or duplicates — a
  user can still edit/delete the seeded store or learning afterward and it won't be re-forced with
  different values (only re-inserted if entirely absent).
- **Note — two different "Lakewood Rx" entities:** the new selectable store **Lakewood Rx** (Sageteq
  billed party) is distinct from the flag-don't-map related entity **Lakewood Rx Inc / Rx Fleet Inc @
  10 Prag Blvd, Monroe NY** in the prompt's related-entities line (still flagged outside the Sageteq
  context). See §2.

---

## 5n. AI-assisted answer + learning drafting on the review item

On the Review item page, a reviewer can turn a rough note into BOTH a record-specific **Answer** and a
general **Learning** rule via a short back-and-forth with Claude, using the record's context, then
approve them together. **Additive; generation is READ-ONLY** — nothing is saved until the reviewer
approves through the existing paths. Classifier + CLI untouched.

- **Backend (`chat_qa.draft_learning(rough_text, context, history=None, learnings=None)`):** reuses
  the Chat tab's `_client()` (same `ANTHROPIC_API_KEY` + `ca_bundle` Techloq TLS) and `ce.MODEL`,
  `max_tokens=700`, **no tools**. Returns a **structured pair `{"answer", "learning"}`**:
  - `answer` — the concrete decision for THIS specific record, written as the reviewer's authoritative
    answer (MAY reference the record's specifics, e.g. "This invoice is for 3M — 112 Hillside is
    Braun's default billing address…"). Goes in the **Answer** box; resolves the record on reprocess.
  - `learning` — ONE self-contained GENERAL rule (names vendor/condition, never "this/that record").
    Goes in **Notes to Learn**.
  The system prompt asks for exactly these two as JSON `{"answer": "...", "learning": "..."}`; the
  reply is parsed (`_parse_draft_json`, tolerating ```json fences) and **on a parse miss retried ONCE**
  (the bad output is appended as the assistant turn + a corrective user turn, so user/assistant
  alternation stays valid). The record `context` (vendor/store/invoice #/amount/flag reason/source
  email/email type, from `get_review`) is folded into the FIRST user turn so it survives multi-turn
  refinement; the current learnings list is passed for style/dedup. Each refine turn returns an
  updated pair.
- **Routes (`review_app.py`, login-required, JSON):** `POST /review/{id}/draft-learning`
  ({rough, history?} → `{answer, learning}`) loads `get_review` for context (404 if missing) and
  **saves nothing** — runs `draft_learning` via `run_in_threadpool`, degrading to a friendly `{error}`
  never a 500 (incl. an empty pair → "Couldn't draft — try rephrasing"). `POST /review/{id}/save-learning`
  ({rule}) approves a draft by calling `web_db.add_learning_web(rule, user)` immediately — decoupled
  from answering the record (a general rule can be added without resolving the item).
- **Frontend (`review_detail.html`, vanilla-JS `fetch`, no reload):** an "AI assist" panel by Notes to
  Learn — a rough-note input + **Draft**, a preview showing **both** the proposed Answer and the
  proposed Learning, a **Refine** input (sends the running history — assistant turns stored as the
  JSON pair — and replaces both), and two approve actions: **Use in my answer** fills **BOTH** fields
  (Answer textarea ← answer, Notes to Learn ← learning; the reviewer can edit or clear either before
  submitting) and **Save as learning now** (POSTs only the learning to save-learning). Nothing persists
  until the Answer form is submitted or "Save as learning now" is clicked.

---

## 5o. One-click Ignore / dismiss on review items (web-only, undoable)

A reviewer can dismiss a review item in one click — it leaves the queue **instantly, with no reprocess
and no API call** — and it's snapshotted so it can be viewed and restored. Fully web-side; **CLI +
classifier untouched**.

- **Table `ignored_reviews`** (in `web_db.WEB_DDL`, created by `init_web_schema` on next web start —
  web-only, so the CLI's `init_db` never creates it, per the additive guarantee): `id`, `snapshot`
  JSONB (the full `review_queue` row, for exact restore), denormalized display columns
  (source_email / vendor / store / invoice # / filename / question / run_id), `ignore_reason`,
  `ignored_by`, `ignored_at`. *(Spec suggested database.py; placed in WEB_DDL to keep the web-only
  table out of the CLI's schema, matching `review_notes`.)*
- **`web_db` (all one transaction, autocommit off, logged to `edit_log`):**
  `ignore_review(review_id, username, reason)` — reads the row (`SELECT *`), INSERTs the snapshot +
  denormalized fields + reason/user into `ignored_reviews`, DELETEs it from `review_queue`, logs
  `field='ignored'` (filename + reason). `restore_ignored_review(ignored_id, username)` — re-INSERTs
  the snapshot into `review_queue` **minus the old `id`** (fresh SERIAL — queue ids aren't stable),
  DELETEs the ignored row, logs `field='restored'`. Plus `list_ignored_reviews()` (newest-first;
  flagged date/amount/gmail-link pulled from the JSON snapshot) and `ignored_count()`.
- **Routes (login-required):** `POST /review/{id}/ignore` (optional `reason` form field) → redirect
  `/review`; `GET /ignored` → `ignored.html`; `POST /ignored/{id}/restore` → redirect `/ignored`.
- **UI:** the Review-queue list has a per-row **Ignore** button (the frequent path — dismiss straight
  from the list, confirm dialog) and a **"View ignored (N)"** header link; the Review-item page has an
  **"Ignore item"** control in its own form (optional reason) below the answer form, so it doesn't need
  the Answer field. New **Ignored** nav tab + `ignored.html` table (flagged/ignored dates, vendor,
  store, invoice #, why-flagged, ignored_by, reason) with a **Restore** button per row.
- **The split `review_pending/` file is intentionally LEFT in place** on ignore, so the ignored item's
  preview and restore keep working — the queue is DB-driven, so a queue row is never rebuilt from that
  file. No pipeline impact: answered-item resolution and a normal classify run are unaffected.

---

## 5p. Opt-in outcome tracking for answered review items ("Tracked" tab)

Lets a reviewer confirm HOW their answer resolved before writing a general rule. **Cheap — NO extra API
calls:** the outcome is DERIVED live from the normal reprocess results already in the DB. Mostly
web-side; CLI untouched.

- **Table `tracked_reviews`** (in `web_db.WEB_DDL`, web-only — same reasoning as `ignored_reviews`):
  `filename`, `source_email`, denormalized vendor/store/invoice, `original_question`, `answer_given`,
  `tracked_by`, `tracked_at`, `status` ('open' | 'done').
- **`web_db`:** `track_review(review_id, answer, username)` snapshots the answered item's display
  fields + the answer (skips if an OPEN row for that `filename` already exists — no dupes); called *in
  addition to* `submit_review_answer`. `list_tracked(status='open')` returns open rows, each with a
  live **outcome** matched by the split `filename` the pipeline records the reprocessed record under
  (`email_filename` = that split filename): still in `review_queue` **with** an answer → `pending`
  (awaiting next run); in `review_queue` with the **answer cleared** → `still_flagged` (+ the current
  question — a re-flag comes back as a fresh, answer-empty row); a recorded **non-review** record
  exists in `all_records` → `resolved` (+ amount / store / entered / skip); neither → `no_record`
  (dropped/ignored). Two batched lookups (queue + records by `filename = ANY`), derivation in Python.
  `tracked_open_count()` and `mark_tracked_done(id, username)` (status→'done' + `edit_log`).
- **Routes:** the answer form gains a **`track_outcome`** checkbox → `review_answer` calls
  `track_review` after `submit_review_answer` when ticked. `GET /tracked` → `tracked.html`;
  `POST /tracked/{id}/done`.
- **UI:** a **Track outcome** checkbox by Submit on the review item; a new **Tracked (N)** nav tab
  (count injected into every render by the `render()` helper — cheap COUNT, wrapped so a DB hiccup
  never breaks a page); `tracked.html` shows vendor / invoice # / your answer / original flag reason /
  **Outcome** (Resolved → `$amount @ store` [+ entered / skip-QB / payment], Still flagged → new
  reason, Pending → awaiting next run, No record → dropped/ignored), with per-row **Mark done** + links
  to the source-email records and to add a rule.
- **Derived-live:** because the outcome is computed from existing tables on each page load, it updates
  on its own as classify runs happen — no CLI change, no extra API cost. Edge: if an answer changes the
  store, the re-flagged split file is renamed, so exact-`filename` matching can read that as `no_record`
  rather than `still_flagged` (acceptable — matches how the pipeline links records by filename).

---

## 5q. Per-user QB-entry column view + CSV export layout

Each user chooses which QB-entry columns show and their order (persisted per user), and the CSV export
follows that view by default. Additive, web-only, CLI untouched. **A user with no prefs sees today's
exact layout and gets today's exact CSV** (the canonical order).

- **Available columns / default order** = `QB_EXPORT_COLUMNS` (master list): Vendor, Store,
  Invoice #, Date, Amount, Sales Tax, Shipping, Class, Possible Duplicate, File Link, Gmail Link,
  **Memo**. Per-user prefs pick a subset + order; `_sanitize_qb_columns` drops unknowns/dupes and
  **falls back to the full canonical order** on empty/invalid.
- **Memo column (§5t combined):** one paste-ready field holding BOTH short links, labeled —
  `_qb_memo(short_file, short_gmail)` → `"Doc: <file-short> | Email: <email-short>"` (a missing half is
  dropped; neither → ''). Built from the SAME batched `short_links_for` values already stamped on each
  row (`r["memo"]`), so no extra queries; the CSV/export path rebuilds it in `_qb_cell_value` via the
  export `shorten`. Labels live in the small `QB_MEMO_LABELS` constant (trivial to reword). Renders in
  the table, the `data-memo` attribute (so **Copy visible rows** emits it as one TSV cell), and the CSV
  — a toggleable/reorderable Columns-panel column. The separate **File Link** / **Gmail Link** columns
  remain (Memo is additive); users can show Memo alone for a single paste-ready cell.
- **Storage:** `users.preferences` JSONB (`ALTER … ADD COLUMN … DEFAULT '{}'` in `WEB_DDL`) +
  `web_db.get_user_prefs(user_id)` / `set_user_pref(user_id, key, value)` (merges one key via
  `preferences || %s::jsonb`). Keys: `qb_view_columns` (ordered visible list), `qb_export_follow_view`
  (bool, default **true**), `qb_export_columns` (used only when follow is false).
- **QB page:** the table renders the **control columns** (select / Entered / source-email / edit —
  always shown, with the credit pill on the edit cell) plus the user's `qb_view_columns` in order, via
  the `qb_head`/`qb_cell` macros (sortable where `QB_SORT_KEY` has a key). The **Columns ⚙** panel has a
  checkbox-per-column + ↑/↓ reorder (checked boxes POST in DOM order to `POST /qb/columns` →
  `set_user_pref`), a **Reset to standard order**, and the export options. **"Copy visible rows"** now
  builds its TSV from `qb_view_columns` (each row keeps ALL `data-*` values incl. `data-memo`, so any
  subset/order copies cleanly).
- **CSV export (`/qb/export.csv`):** by default follows the view (`qb_export_follow_view` → the CSV
  matches the screen); unticking "CSV export matches my column view" reveals a separate
  `qb_export_columns` selector. A **Standard QB order** button (`?layout=standard`) always ignores prefs
  and emits the full canonical order — the safe, import-ready fallback (kept prominent since the CSV
  feeds a QuickBooks import expecting a fixed layout). Export cells come from the shared
  `_qb_cell_value(col, row, absfn, shorten=None)` (`shorten` compacts the two link columns — see §5t).

---

## 5u. QuickBooks "class" + editable default class rules

Records carry a QuickBooks **class**, and editable rules fill it in by store/vendor when the document
didn't state one. Additive. (Note: the base "class" field didn't previously exist in the pipeline —
this adds the minimal `class` plumbing along with the rules.)

- **Extraction + persistence:** the invoices/credit_memos/payments JSON schema blocks gain a `class`
  field (the model sets it only if the document EXPLICITLY states one, else null). Persisted on a new
  `class` column on all three record tables (base DDL + `ADD COLUMN IF NOT EXISTS` migrations), carried
  through `_insert_*`/`_*_row`, and added to `RESULTS_FIELDNAMES` (Excel column).
- **QB entry + CSV:** `Class` is a canonical QB column — added to `QB_EXPORT_COLUMNS` (after Shipping),
  `QB_DATA_ATTR` (`class`), and `_qb_cell_value`; `list_qb_ready` selects `class` from both arms. It
  renders on the QB-entry table, is included in the CSV export and Copy-rows, and is a toggleable/
  reorderable column in the Columns panel (non-sortable header). Existing per-user column prefs that
  predate it simply don't list it until the user adds it (additive).
- **`class_rules` table** (base DDL, curated in the web UI, read each run): `store_matched`
  (NULL=any), `vendor_matched` (NULL=any), `class` NOT NULL, `note`, audit cols. `DEFAULT_CLASS_RULES =
  []` + `load_class_rules()` (DB or defaults) + a seed block.
- **Apply (deterministic, post-classification):** `apply_class_rules(rec)` — if the record already has
  a class (from the doc), keep it; otherwise pick the best-matching rule and set `rec["class"]`. A rule
  matches when (rule.store blank OR == record store) AND (rule.vendor blank OR == record vendor), with
  ≥1 of store/vendor specified; **precedence both-fields > vendor-only > store-only**. Called in
  `build_result_rows` right after `apply_card_store_map` (so the store is resolved first) for invoice,
  credit_memo, and payment. No-op when `CLASS_RULES` is empty (Excel/uncurated) — the CLI is unchanged
  until rules are added.
- **UI:** registered in the generic reference system (`_REF` + `REFERENCE_PAGES` → **"Class rules"**
  nav tab) with a **Store dropdown** (blank = any, reuses `store_names`), a **Vendor** text field
  (blank = any), a required **Class**, and a note. Because `class_rules` has **no single UNIQUE key**
  (many rules may share a store/vendor), the reference CRUD was generalized to support `unique: None` +
  a `required` field: `add_reference` does a plain INSERT (no `ON CONFLICT`) enforcing the required
  field; `edit_reference` skips the uniqueness probe; `delete_reference` logs by the required field.
  Existing unique-keyed tables are unchanged.

---

## 5t. Internal short links for QB memos (private URL shortener)

The three record links (File/Drive, Email/Gmail, Records) are long; QuickBooks memo fields are short.
An internal shortener maps each to a compact `/l/<code>` URL. **Private — nothing goes to a third
party** (the app just 302s to the stored target), and **scaling-ready** for Phase 2C. Web-side; CLI
untouched.

- **`links` table** (`WEB_DDL`, web-only): `code TEXT UNIQUE`, `target_url TEXT UNIQUE`, `kind`
  ('file'|'email'|'records'), `created_at`. **Deduped by `target_url`** — the same URL always reuses
  its code (stable). Codes are 7-char base62 via `secrets` (unique; a collision/race re-selects).
- **`web_db.short_link_for(target_url, kind, base_url=None)`** → `f"{base}/l/{code}"`, upserting the
  code (`_link_code`). **Host precedence:** env **`LINK_BASE_URL`** (set at Phase 2C to the cloud
  domain — a one-line change; codes stay stable) wins, else the caller's `base_url` (the current LAN
  host). Empty target → `""`. `web_db.get_link(code)` resolves a code for the redirect.
- **Route `GET /l/{code}`** 302s to the target: **'file'** (public Drive / the app's read-only static
  mounts) redirects **without login**; **'email'/'records'** are **login-required** (so remote access
  stays gated by the app's auth once Phase 2C lands). Unknown code → `/review`.
- **Used where links go INTO QuickBooks:** the QB-entry page precomputes `short_file` / `short_gmail`
  / `short_records` per row (in `qb_list`, base = the request's LAN URL) — the table's File/Gmail/
  Records anchors, the Copy-rows `data-*` attributes, and the CSV export (`_qb_cell_value`'s `shorten`
  hook, File→'file' / Gmail→'email') all use the short links, so pasted memos stay compact. Raw links
  remain elsewhere (e.g. the review-item detail) unchanged. Note: links already pasted into QB with
  the LAN host keep working only while that host is reachable (expected for the local phase).
- **Batched resolution (perf):** the QB page + CSV would otherwise call `short_link_for` ~3× PER ROW,
  each opening a NEW `connect()` — ~3N serial connections that made the page crawl. Both hot paths now
  build a list of `(url, kind)` across ALL rows and call `web_db.short_links_for(pairs, base)` ONCE:
  one connection — a single `SELECT … WHERE target_url = ANY(%s)` for the already-coded URLs plus one
  `execute_values` batch INSERT (`ON CONFLICT (target_url) DO NOTHING`, bounded code-collision retry)
  for any new ones — returning `{target_url: short_url}` that the routes map per row. In the steady
  state (URLs already coded) it's a single SELECT with zero inserts. A `links_target_url_key` unique
  index backs the dedupe lookup (redundant with the column's UNIQUE, added explicitly). The one-off
  `short_link_for` stays for single callers (review detail); output is identical either way.

---

## 5r. Reviewer-uploaded PDFs → reprocess a review item

A reviewer can upload PDF(s)/image(s) to a review item — e.g. hand-extracted from a zip the pipeline
couldn't open — and the next classify run reprocesses that record USING them. **Additive & opt-in:** the
extra-attachments path defaults off, so normal processing is byte-identical when nothing is uploaded.

- **Table `manual_attachments`** (in the **base** `database.py` DDL — genuinely SHARED: the web app
  writes, the CLI reads + stamps `consumed_at`, unlike the web-only ignored/tracked tables): keyed by
  the review item's split `filename` (stable, like `review_notes`); `file_path`, `original_name`,
  `uploaded_by/at`, `consumed_at`.
- **CLI (`classify_emails.py`):** `process_source_email(..., extra_attachments=None)` reads each given
  file path into an attachment dict `{filename, mimetype, data}` and APPENDS it to the email's own
  attachments before `classify_with_retry` (the unreadable original stays skipped; the appended PDFs
  give the model the real document). In the Phase-1 resolve loop: (a) items with UNCONSUMED manual
  attachments join the reprocess set **even with no answer** (the upload is the trigger; pulled out of
  the carryover before the whole-queue re-check so they aren't double-processed); (b) every reprocessed
  item loads its manual attachments and passes them as `extra_attachments`; (c) on a non-error result
  the files are stamped `consumed_at` (so they don't re-trigger forever) — a genuine read failure leaves
  them unconsumed for another try. DB-only + USE_DB-guarded, so Excel mode and the no-upload case are
  unchanged. `database.load_manual_attachments(filename)` (unconsumed paths) +
  `mark_manual_attachments_consumed(filename)`.
- **Uploads are the source of truth** (so an item whose original split file is gone/unreadable still
  reprocesses from the uploads instead of staying "pending" forever): the resolve loop loads the item's
  manual attachments FIRST and only bails when there's **nothing** to work with (`not src.exists() and
  not extra`); if the source is missing but uploads exist it proceeds, using the queue filename purely
  as the record name. `process_source_email` mirrors this — if `read_email_content` raises AND
  `extra_attachments` is non-empty it classifies from the uploads with a minimal placeholder text
  (skipping the received-date injection, since there's no source header) instead of returning a
  `read_error`; with no uploads it returns the `read_error` exactly as before. On success the item is
  recorded and drops out of the queue (attachments consumed, pending pill clears); duplicate detection
  handles it normally if that invoice was already recorded elsewhere.
- **Web:** `POST /review/{id}/attach` (multipart, multi-file) validates extension **and** content-type
  (PDF + png/jpg/jpeg/gif/webp), sanitizes the name (`_safe_upload_name`, traversal-safe), saves to
  `review_pending/manual/<item-stem>/<name>` (never clobbering), and records a `manual_attachments` row
  per file (`web_db.add_manual_attachment`). The review item page has an **"Add PDFs (from the zip) &
  reprocess"** panel (multi-file input + a list of already-attached files with preview links + a
  pending/used ✓ status). Previews use `_manual_preview_url` (keeps the sub-path under the mounted
  `review_pending/`, unlike the basename-only `file_preview_url`). `web_db.list_manual_attachments`
  lists them (incl. consumed).
- **Timing:** the upload only STAGES the files + flags the item; the NEXT classify run does the work
  (same model + adopt-only-if-resolved flow as answered items).
- **Visible "action taken" status (so an uploaded item never looks untouched):** a successful upload
  ALSO auto-adds an internal note via the existing `add_review_note` ("Uploaded N file(s) for reprocess:
  …", authored by the uploader) — audit trail + the 📝 note badge appears. An item with UNCONSUMED
  manual attachments shows a distinct **"Files uploaded — pending reprocess"** pill (styled like
  "Answered — pending reprocess") instead of "Needs review", on BOTH the queue list and the item page.
  Driven by `web_db.pending_manual_filenames()` (review filenames with an unconsumed attachment) →
  `it.has_pending_upload` in the review-list route (same pattern as `note_count`); the detail page
  derives it from the item's manual list. These items stay **VISIBLE in the default queue** (not
  hidden) so the reviewer sees the upload took effect; the pill clears automatically once a run consumes
  the files (`consumed_at` set) and resolves the item — or the item stays pending if it's still
  unreadable (files left unconsumed).

---

## 5s. Auto-expand zip attachments (+ one-off backlog retry)

Emails whose invoices arrive **zipped** now classify automatically — the pipeline expands the zip in
memory and feeds the contained PDFs/images to the model, instead of the zip being unreadable and the
record flagged. **Additive:** a zip that can't be safely expanded falls back to EXACTLY today's
behavior (the record flags, and the reviewer-upload path §5r handles it).

- **`expand_zip_attachments(attachments)`** (`classify_emails.py`): replaces each ZIP attachment
  (`application/zip` or a `.zip` name) with its contained SUPPORTED files
  (`SUPPORTED_DOCUMENT_TYPES`/`IMAGE`/`INLINABLE_TEXT`, detected via the existing
  `_effective_media_type`), each relabeled **`"<zip stem> :: <inner name>"`** so the model's
  `source_document` citation stays traceable (matched back by `_match_attachment` on save). Same
  `{filename, mimetype, data}` shape, so ALL downstream (`build_attachment_blocks` **and**
  `assign_invoice_files`/`_write_record_document`) just work. Non-zip attachments pass through
  unchanged; order preserved. Wired into `process_source_email` right after `read_email_content`, so
  the model + the file-saving both see the real documents. Prints e.g. `unzipped invoices.zip -> 3
  file(s)` / `skipped zip invoices.zip (password-protected …)`.
- **Safety guards — skip the whole zip (leave as-is) when:** it's password-protected/corrupt
  (`zipfile` raises on open/read), a member is itself a `.zip` (**no recursion** — zip-bomb guard),
  or a cap is exceeded — `ZIP_MAX_ENTRIES=100`, `ZIP_MAX_INNER_BYTES=25 MB`, `ZIP_MAX_TOTAL_BYTES=100
  MB`, all checked via `ZipInfo.file_size` (**uncompressed, BEFORE extracting**). A zip that yields no
  supported files is also left as-is. Composes with §5r — no changes needed there.
- **One-off backlog retry (opt-in flags; default runs unchanged):** `--retry-unreadable` pulls the
  already-queued unreadable-attachment/zip items (matched by `_is_unreadable_review_item` — the
  `unreadable` email-type or a reason containing zip / "could not be read/opened" / "unreadable" /
  "reading failed" / attachment+read) into the Phase-1 resolve set so the new auto-unzip clears the
  backlog; `--retry-open-all` retries every remaining open item once. Both go through the SAME resolve
  machinery: a now-readable zip yields its PDFs and resolves (recorded + dropped); one that's still
  unreadable errors and is **kept in the queue unchanged** (available for manual upload). Prints
  `Retried N unreadable-attachment item(s): resolved M, still stuck K (need manual upload).` Gated
  behind the explicit flag (a deliberate one-off — run once after auto-unzip deploys); **without a
  flag the resolve loop is exactly as before** (no attempt-counter, no re-hammering). Flags parsed via
  the existing `_ARGS` set convention (like `--excel`/`--drive`), not argparse.
- **`--retry-id=<N>`: reprocess ONE specific open review item (2026-09).** A third, narrowly-scoped
  retry flag — reprocesses only the open review item whose **displayed id** is N, through the SAME
  resolve path, without touching any other open item (unlike `--retry-open-all`/`--retry-unreadable`
  which act on the whole matching set — wasteful when Joel just wants to check "did this prompt/rule
  change fix THIS one record"). `load_review_queue` now returns the row's DB id as `"Id"` (additive);
  `RETRY_ID` selects `queue_carryover` rows where `str(Id)==str(RETRY_ID)`. If none match it prints a
  clear miss message (already resolved, or the id changed — **ids are only stable until the next run
  rewrites the queue**, so run `--retry-id=<N>` before triggering any other run, and re-check the item's
  CURRENT id on the Review page first). Status lines read `#<N>` for this mode. Verified: the flag parses
  from `--retry-id=2287`; the label logic yields open / unreadable-attachment / `#2287` for the three
  modes.

---

## 5v. Sales-order hold + invoice supersession

A **sales order / order acknowledgement** is NOT recorded immediately — it waits a maturity window
(~2 days); if a formal invoice for the same vendor + order number arrives it supersedes the order
(invoice wins), else the order records itself when the window elapses. Additive; normal invoices/
payments/credit-memos are unaffected.

- **Detect:** the invoices JSON schema gains `is_sales_order` (true for a doc titled Sales Order /
  Order Acknowledgement / Order Confirmation, showing an "Order Total" not an amount due, or "we've
  received your order").
- **Hold (`pending_sales_orders`, base DDL — SHARED: CLI writes/reconciles, web reads):** an
  `is_sales_order` invoice is **skipped** by `build_result_rows`, `apply_duplicate_checks`,
  `_supersede_reprocessed_records`, and `routes_to_review` (never recorded, dup-checked, or reviewed
  while held) — but LEFT in the classification so a pure-order email doesn't emit a junk informational
  row. After the normal saves, `main()` snapshots each held order into `pending_sales_orders`
  (`insert_sales_order`, deduped on normalized vendor+number among still-'held' rows), with
  `release_after = now + SALES_ORDER_HOLD_DAYS` (env/const, default 2).
- **Reconcile (`reconcile_sales_orders(run_id)`, end of `main()`, AFTER this run's records are saved
  so it sees invoices recorded this run):** for each 'held' row — **SUPERSEDE** (`status='superseded'`,
  `matched_invoice`) if `find_recorded_invoice_ref(vendor, number)` finds a recorded invoice (invoices
  table is never a sales order); **RELEASE** if the hold elapsed and none matched — rebuild a normal
  single-invoice `result` from the snapshot (`_result_from_sales_order_snapshot`, `is_sales_order`→
  false) and run it through the normal save path (`apply_duplicate_checks` → `build_result_rows` →
  `save_results`), queueing it via **append-only** `append_review_rows` if it now needs review;
  else keep 'held'. Match key everywhere = vendor + **normalized number** (spaces stripped, lowercased);
  amount is irrelevant (invoice wins on partial-shipment/adjustment). Runs every run (even with no new
  mail), so matured orders release on schedule.
- **Order#→invoice# suffix matching (2026-09).** `find_recorded_invoice_ref` tries an exact
  vendor+number match first, then falls back to **the recorded invoice number STARTING WITH the
  sales-order number** (`LIKE '<order#>%'`) — so a held order `1618282` matches its recorded invoice
  `1618282-IN`. Confirmed the `-IN` suffix pattern across 8 Bluebonnet Nutrition invoices; generic for
  any vendor that suffixes the order number. Verified live: all 5 sampled Bluebonnet orders
  (1618282/1613348/1615219/1616269/1545243) now resolve to their `-IN` invoice; exact path + no-match
  unaffected.
- **`is_sales_order` anti-override clause (2026-09).** The `is_sales_order` schema description now
  states that showing full itemized pricing does NOT make a document an invoice (order confirmations
  routinely show full pricing), and that a doc titled 'Sales Order' or showing 'Order Number'/'Order
  Total' is a sales order even with every line/price shown. Fixes a case (Bluebonnet #1618282, Refuah
  $6,760.71) where the model invented a nonexistent "process as invoice per policy" rule and routed a
  6-page Sales Order to human review instead of holding it.
  - **Strengthened with a worked example (2026-09-07).** The abstract wording didn't stick 100% — the
    same bug recurred on a run AFTER the fix was live (Releaf Healing #2315, a handwritten slip headed
    'Sales Order #'; also Rockland Cosmetics #2316). Added a concrete example to `is_sales_order` (a
    handwritten quantity/item/amount slip headed 'Sales Order #' IS a sales order even if pricing looks
    complete / handwritten / informal; "no such policy exists — set is_sales_order true and let it be
    held, don't record it yourself"), AND tightened the nearby **PAID order-confirmation** rule so it
    explicitly does NOT apply to an unpaid sales order (that case is governed by `is_sales_order`) —
    the suspected source of the model blending the two rules. Prompt-only; no schema/pipeline change.
- **Diagnostic: `_write_record_document` logs WHY a record has no source file.** When it returns `""`
  (record shows "No source file recorded" in review), it now prints which side failed — the model's
  `source_document` matched no attachment (names listed) AND the email-body render fell through — so the
  next occurrence is diagnosable. (Investigated #2296: the source `.eml`'s `1618282.PDF` attachment is
  present, matchable, and renderable now, so its empty `file_path` was a non-reproducible transient /
  model mis-attribution at flag time; the log line will pin it if it recurs.)
- **Web — "Sales orders" tab** (`/sales-orders`, `web_db.list_sales_orders`): **Held** (vendor / order
  # / amount / store / held-at / records-on + source links) with per-row **Release now**
  (`release_sales_order_now` → shortens the window so the next run records it) and **Ignore**
  (`ignore_sales_order` → `status='superseded'`, `matched_invoice='(manually ignored)'`); **Superseded**
  (shows the replacing invoice); **Recently released** (links to the recorded invoice). Read-only
  otherwise; both actions logged to `edit_log`.

---

## 5w. Per-record "how it was processed" detail view (2026-08)

A read-only detail page exposes data that already existed. Route **`GET /records/{record_type}/{record_id}`**
→ `record_detail.html`, reached from a **Details** link on every Records row.

- **`all_records` view** gained `sender` (`emails.from_address`) + `received_at` on every arm; the
  `review` arm now LEFT JOINs `emails` on `rq.source_email` so those populate for in-review rows too.
- **`web_db.get_record`** is enriched (still returns all raw `r.*` columns the edit page needs, just
  adds keys): LEFT JOINs `emails` (sender/received_at/run_id/email_status/gmail_message_id) and `runs`
  (`run_started_at`/`run_finished_at`), and now also handles `record_type='review'`.
- **`outcome(r)` macro** (`_macros.html`, used by list + detail) derives, in order: review → *In review*;
  `entered_in_qb` → *Entered in QB*; `duplicate_ignored` → *Duplicate — auto-ignored (kept out of QB)*;
  `skip_qb_import` → *Excluded vendor — recorded, not entered in QB*; `duplicate_suspect` → *Possible
  duplicate — recorded*; else *Recorded — pending QB entry*.
- **Detail page** has three sections: Record (type/vendor/store/number/amount/date/class); Source &
  timing (sender, received, processed/recorded, run — linked to `/browse?run=` with started/finished,
  email status, source-email link, File/Email/records links); Outcome (the derived line + specifics —
  review reason, duplicate reason + `duplicate_of`, excluded note, entered flag, record notes + any
  internal `review_notes` on that file — plus a short Received → Processed-in-run → outcome timeline).
- **Records list** (`browse.html`) added **Sender**, **Received**, and an **Outcome** column (replacing
  the raw email-status column) and a **Details** link per row. (Columns default on; making
  Sender/Received/Class optional via the §5q per-user column mechanism is a deferred nice-to-have.)
- Web-only + additive; the CLI pipeline (`classify_emails.py`, `database.load_review_queue`/`save_review_queue`)
  is untouched.

---

## 5x. US date/time display filters (web-only)

All dates shown in the web UI render as **M/D/YY** (e.g. `8/27/26`) and timestamps as
**M/D/YY H:MM** (24-hour), via two centralized Jinja filters registered on
`templates.env.filters` in `review_app.py` — `us_date` and `us_datetime`. Both are built by
hand (NOT `strftime('%-m')`, which is not portable and fails on Windows, the host):
`us_date` → `f"{v.month}/{v.day}/{v.year % 100:02d}"`; `us_datetime` adds `" {H}:{MM:02d}"`
for datetimes with a time (midnight/date-only render as date). `None`/`""` → `""`; a string is
parsed (`fromisoformat`, then `%Y-%m-%d` / `%m/%d/%Y` / … fallbacks) and reformatted, or returned
unchanged if unparseable. Every template pipes date fields through `| us_date` (record_date,
invoice_date, payment_date, date_flagged, date_added, …) and timestamps through `| us_datetime`
(flagged_at, received_at, reviewed_at, created_at/edited_at, started_at/finished_at, held_at,
release_after, tracked_at, ignored_at, uploaded_at, …); no template uses `strftime` anymore.
**Display only** — stored DB values, the classifier's date fields, and the QB copy/CSV `data-*`
payloads (raw for QuickBooks paste) are untouched; `run_id` (an id like `20260827T130149`) is
deliberately left as-is.

---

## 5y. One-off vendor rename (`rename_vendor.py`)

Standalone maintenance script (uses `DATABASE_URL`; does NOT touch the classifier or normal runs)
to collapse a vendor recorded under **many spellings to ONE canonical** name across the recorded
tables (`invoices`, `credit_memos`, `payments`, `review_queue`) AND the dedup ledgers
(`invoice_history`, `payment_history` — so future duplicate checks match the canonical name too).
Writes one `edit_log` row per updated record (`changed_by='rename_vendor'`, `old_value`=the matched
variant). Idempotent (re-run finds 0).

    python rename_vendor.py --like "prestige"                  # discovery (repeatable): distinct values + per-table counts
    python rename_vendor.py --like "a&s" --like "a & s"
    python rename_vendor.py --to "A&S Sales" \
        --from "Prestige Sales Inc." --from "A & S Sales" \
        --from "A&S Sales (Prestige Sales Inc.)" --from "Prestige Sales Inc. (A&S Sales)" --dry-run
    python rename_vendor.py --to "A&S Sales" --from "..." --from "..."     # apply
    python rename_vendor.py "Prestige Sales Inc." "A&S Sales"              # single-pair shorthand still works

Match is `btrim(vendor_matched) = <variant>` (exact, case-sensitive; `--like` is how you find the exact
spellings — run it for each stem and add any variant it surfaces to `--from`). Each `--from` is mapped
to the single `--to` CANONICAL; a `--from` that already equals the canonical is skipped. `--dry-run`
prints per-variant, per-table counts with no writes. It only normalizes the NAME on existing rows — it
does **not** merge or re-dedupe records. Stopgap until the vendor-match DB owns alias→canonical
normalization (new + retroactive).

---

## 5z. Emails ledger tab (every ingested email + disposition)

A read-only **Emails** tab (`GET /emails` → `emails.html`, nav tab) lists EVERY row in the `emails`
table with a derived **disposition** — what the system did with it — so nothing is invisible.

- `web_db.list_emails(filters)` — one row per email in a single query: a CTE aggregates per-email
  counts of invoices/credit_memos/payments (with a skip_qb_import count), plus review_queue /
  ignored_reviews / held `pending_sales_orders` presence (all keyed by `email_filename` =
  `source_email`; aggregated once per table, not per-row). A CASE derives `disposition_key` in this
  precedence: `error` → **Error / unreadable**; has records → **Recorded** (+ breakdown like
  "1 invoice, 1 payment"; adds "(excluded — in Browse)" when every record is skip_qb); in
  review_queue → **In review**; in ignored_reviews → **Ignored**; held sales order → **Held (sales
  order)**; else an informational label from `email_type` (Shipment notice / Statement / Order ack /
  Correspondence / No action). The human label + breakdown are computed in `_email_disposition()`.
  Returns `received_at, from_address, subject, email_type, status, run_id, gmail_link`, the
  disposition, and the produced records (`record_type` + id, fetched for the visible page only) for
  linking to each record's detail page (`/records/...`).
- **Filters** (all in SQL, so paging is correct): disposition category, email_type, run, received-at
  date range, and a subject/sender search. Paginated (page size 200, prev/next); shows the total
  ("N emails"). Dates via the `us_date`/`us_datetime` filters.
- Web-only, read-only, additive — no classifier/pipeline change; the `emails` table + produced
  records are the data source.
- **Real-emails-only filter (2026-08-27):** the ledger now counts real emails only —
  `WHERE (e.gmail_message_id IS NOT NULL OR e.filename ILIKE '%%.eml')` in the CTE (the `%%` is
  escaped for psycopg2 param interpolation). This excludes **stub `emails` rows** that the pipeline
  writes per split/reprocessed record (one `_upsert_email` keyed by the record's filename, which for a
  split/reprocessed record is a split `.pdf` like `… - $705.63 - 8.17.2026__2.pdf`, not an email).
  Bucket counts on the live DB: (a) `gmail_message_id NOT NULL` = **1061**; (b) manual `.eml` drops =
  **48**; (c) split/record stubs = **61**. Header reconciles to **1109** (= a + b), matching the Gmail
  "fetched" label (± manual `.eml` drops); the 61 stubs are hidden.
- ⚠️ **Future cleanup (do NOT change the pipeline now):** those stub rows are an artifact of storing
  split/reprocessed records under their split filename. A later pass could avoid creating them (or roll
  a split record up to its original source email in the ledger). Some also vanish once the duplicate-
  record `__N` fix lands (the `__2.pdf` names above). Flagged only — not refactored here.

---

## 5aa. Purge/archive non-email records (`purge_non_email_records.py`)

Standalone one-off (uses `DATABASE_URL`; no classifier/pipeline change) to remove or archive
records that never came from a real email. **Two discriminators:**

- **RECOMMENDED — go-live date cutoff (`--before DATE`):** targets records recorded before the
  Gmail-flow go-live that ALSO lack any email/run metadata (belt-and-suspenders):
  `date_added < DATE AND emails.gmail_message_id IS NULL AND emails.run_id IS NULL`, across
  invoices/credit_memos/payments + invoice_history/payment_history (via `source_file`) + resulting
  orphan stub emails (which must ALSO be `date_added < DATE`, so recent manual/unprocessed emails are
  never touched).
- **Legacy fingerprint (no `--before`):** the old "no Gmail receipt" match
  (`received_at IS NULL AND gmail_message_id IS NULL`, or no emails row). Kept for back-compat but
  **NOT recommended** — see the caveat below.

```
python purge_non_email_records.py --before 2026-08-06 --dry-run   # DRY-RUN: per-table counts + sample, NO writes
python purge_non_email_records.py --before 2026-08-06 --delete     # delete rows + ledger rows + orphaned stubs
python purge_non_email_records.py --before 2026-08-06 --archive     # flag legacy_migrated + hide from email views
python purge_non_email_records.py                                   # (no --before) legacy fingerprint DRY-RUN
```

- `--archive` adds `legacy_migrated BOOLEAN DEFAULT FALSE` (WEB_DDL, on invoices/credit_memos/payments/
  emails), sets it TRUE on the matches, and `list_all_records` + `list_emails` (and the ledger count)
  filter `legacy_migrated IS NOT TRUE`. Default FALSE = **no behavior change until archiving is used**;
  reversible (set FALSE). `all_records` view carries the column.
- **Go-live verification (2026-08-27, read-only on the live DB), cutoff = Thu Aug 6 2026 1:52 PM:**
  - `MIN(received_at)` across the 1061 Gmail emails = **2025-03-02** — this is each email's *actual arrival*
    time (Gmail internalDate); the go-live fetch pulled older inbox emails too. Go-live (first fetch) maps
    to **`date_added`**, not `received_at` — so the cutoff lives on `date_added`.
  - **Clean cut:** 0 email-flow/reprocessed records dated `< 2026-08-06` (earliest `date_added` = Aug 7);
    records with `date_added < 2026-08-06` = **0**; old-set == fingerprint-set (both 0).
  - ⇒ **`--before 2026-08-06` targets 0 rows here** — there are no pre-go-live/Excel records in this DB.
- ⚠️ **Why the legacy fingerprint is dangerous here (do not use it to `--delete`):** it matches
  **57 invoices / 1 credit_memo / 16 payments / 44+11 ledger / 63 emails** that are all legitimate
  `source='manual'` **post-go-live** records — manually-dropped `.msg`/`.eml` files AND reprocessed review
  split-file records (51 of the 57 invoices even carry a `run_id`). They never get Gmail receipt metadata,
  so the fingerprint over-matches. The **date cutoff correctly excludes them** (they're dated Aug 2026,
  and carry a `run_id`). Verified: `--before` dry-run = 0; `_purge`/`_archive` before-mode = 0 across all
  tables incl. stub emails (rolled-back live-DB test); legacy mode still selects the 57/1/16 set.

---

## 5ab. Statement reconciliation (`statement_lines`)

When a vendor **statement** is processed, the invoices it LISTS are extracted and cross-checked
against what we recorded; **missing invoices** and **amount mismatches** surface in the Review
queue. Additive; statements still record no financial record of their own. SHARED (CLI ingests +
reconciles; web surfaces + dismisses).

- **CLASSIFY** — the model schema (`classify_emails.py` prompt) gains `statement_invoices:
  [{vendor, invoice_number, invoice_date, amount}]`, populated ONLY for `email_type='statement'`
  (every invoice the statement references), else omitted/`[]`. It's reconciliation metadata — it
  does not create records.
- **DB** — new base-schema table `statement_lines` (`database.py` DDL): `id, statement_email,
  statement_file, vendor, invoice_number, invoice_date DATE, amount, status, recorded_amount,
  first_seen_run, created_at, updated_at, dismissed_by, dismissed_at`. `status ∈ {pre_cutoff,
  matched, missing, amount_mismatch, in_flight, dismissed, excluded_vendor}`. Dedup: unique index on
  `(statement_file, upper(btrim(invoice_number)))`; lookup index on
  `(upper(btrim(vendor)), upper(btrim(invoice_number)))`.
  - `ingest_statement_lines(statement_email, statement_file, lines, run_id)` — UPSERT one row per
    listed invoice (re-ingest updates in place; never disturbs a `dismissed` line).
  - `reconcile_statement_lines(run_id, grace_days)` — one `UPDATE … FROM … LEFT JOIN LATERAL`
    recomputing status for every non-dismissed line: match by vendor + normalized invoice_number;
    amount within a cent ⇒ `matched` (stores `recorded_amount`), else `amount_mismatch`; else
    present in `review_queue`/held `pending_sales_orders` ⇒ `in_flight`; else `missing`; anything
    dated before the go-live cutoff ⇒ `pre_cutoff`. Recomputed from scratch each run, so a flagged
    line **auto-resolves to `matched`** once the invoice arrives.
  - **Excluded-vendor override** (added) — after that UPDATE, any line still `missing`/`amount_mismatch`
    whose vendor is on the **`excluded_vendors`** list is flipped to **`excluded_vendor`**, using the
    SAME rule as `classify_emails.is_excluded_vendor()` (bidirectional normalized substring; a local
    `database._norm` mirrors `classify_emails._norm` to avoid an import cycle). Those vendors (McKesson,
    Parmed, …) are never entered in QB through this pipeline, so a missing/mismatch flag was pure noise.
    Since the surface query filters `status IN ('missing','amount_mismatch')`, an `excluded_vendor` line
    just drops out of the queue — **no web change needed**, and because reconcile recomputes every run,
    already-ingested excluded lines **self-heal on the next run** (no backfill).
  - **Statement-recon-only exclusion list** (added 2026-09) — a SEPARATE table
    `statement_recon_excluded_vendors` (+ `DEFAULT_STATEMENT_RECON_EXCLUDED_VENDORS = ['Jersey Central
    Power and Light']`, `load_statement_recon_excluded_vendors()`) for vendors whose STATEMENT *is* the
    bill (utilities — no separate invoice to reconcile against, so every month's lines sit `missing`
    forever). `reconcile_statement_lines` unions this list into the same `excluded` override, so their
    lines flip to `excluded_vendor` and drop from Review. **Unlike `excluded_vendors`, this does NOT set
    `skip_qb_import`** — JCP&L's actual bills are still entered in QB. Editable via its own Setup page
    **"Statement recon — excluded vendors"** (generic `_REF` editor: `review_app.REFERENCE_PAGES` +
    `web_db._REF` + a Setup-nav link). New table created/seeded on next `init_db()`; verified JCP&L is
    currently the only vendor with an open (`missing`) statement line, so this clears exactly that noise.
- **Go-live cutoff** = go-live + `STATEMENT_RECON_GRACE_DAYS` (default 14). Go-live is derived from
  **`MIN(runs.started_at)`** (the first classify run — definitionally when processing went live;
  = 2026-08-13 in this DB), falling back to `MIN(received_at)` only if no runs exist.
  ⚠️ **NOT raw `MIN(received_at)`**: as with the purge tool, `received_at` is each email's true
  arrival time, so the go-live fetch pulls year-old inbox mail and `MIN(received_at)` = 2025-03-02
  (~17 months too early), which would make the cutoff a no-op and false-flag every pre-go-live
  invoice as `missing`. The spec's "MIN(received_at) … the go-live ~2026-08-06" was self-inconsistent
  with the data; the run-start derivation honors the **intent**. `STATEMENT_GOLIVE_DATE=YYYY-MM-DD`
  overrides the derivation (mirrors the purge tool's `--before`).
- **SURFACE** — open exceptions (`missing`/`amount_mismatch`, not dismissed, not `excluded_vendor`) are
  **UNIONed into the Review queue at read time** (`web_db.list_open_statement_exceptions` → prepended in
  `review_app.review_list`), never stored in `review_queue` (which is full-replaced each run). Shaped
  as review rows with `kind='statement'`; `review_list.html` renders a distinct amber row (📄
  Statement pill). Shown only when no store filter is active (statement lines carry no store); respects
  vendor/search/run filters.
- **Grouped one row per statement** (changed) — `list_open_statement_exceptions` now fetches the same
  lines but **groups them by `statement_file`**, returning **ONE** queue row per statement (was one per
  line — a 5-missing-invoice statement made 5 rows). The grouped row (`_statement_group_row`) carries
  `line_ids` (all its lines), a `line_count`, `status_label` reflecting the mix (`"3 missing, 1
  mismatch"`, or `"Missing invoice"`/`"Amount mismatch"` when uniform; pill colored red/amber/violet for
  missing/mismatch/mixed), vendor (or `"(multiple vendors)"`), and a **"Why flagged"** that lists every
  invoice with its number, amount, and date — missing grouped first, then mismatches with
  statement-vs-recorded — so the reviewer can act without opening anything (the "clear list of invoices
  and amounts" Joel asked for). Invoice#/Amount cells show a count / "—".
  - **"Why flagged" is a one-per-line list** (formatting): a header line `Statement <file> — <vendor> —`,
    then a labeled block `Missing invoice(s):` / `Amount mismatch:` with **one invoice per line** in
    `number - $amount - date` form (missing) / `number - statement $x vs recorded $y - date` (mismatch),
    a blank line between the two blocks. The `question` string carries real `\n`s; the cell renders them
    via `white-space: pre-line` in `review_list.html` (plain text — no HTML built in Python, nothing
    marked `| safe`).
- **Group dismiss** (added) — the row's **Resolve / dismiss** submits all its `line_ids` (comma-joined
  hidden field) to new `POST /statement-line/dismiss-group` → `web_db.dismiss_statement_lines(ids,
  user)` (one `UPDATE … WHERE id = ANY(%s)` to `dismissed`, one `edit_log` row per line, skips
  already-dismissed). The single-line `POST /statement-line/{id}/dismiss` + `dismiss_statement_line`
  are **kept** as a building block.
- **Verified** (self-reverting live-DB test): a statement of 5 lines → `matched` / `amount_mismatch`
  / `in_flight` / `missing` / `pre_cutoff` respectively; only the 2 flagged surface to the web;
  recording the missing invoice auto-flips it to `matched`; dismissing a line keeps it out across
  reconciles; `STATEMENT_GOLIVE_DATE` override moves the cutoff. CLI pipeline untouched.
- **Verified** (self-reverting fake-DB test, `test_stmtgroup.py`): the excluded-vendor override marks
  exactly the McKesson/Parmed lines `excluded_vendor` (matching parity with `is_excluded_vendor`
  confirmed) and drops the open-flagged count accordingly, leaving matched/in-flight/pre_cutoff alone;
  a 3-missing+1-mismatch statement groups into ONE row with all 4 `line_ids`, `status='mixed'`, and a
  Why-flagged listing all four invoices; multiple statements → one row each; `dismiss_statement_lines`
  updates all ids in one round-trip with an audit row per line (empty id list → no DB hit). Template
  parses; the grouped dismiss form + `line_ids | join(',')` render; all web/CLI modules import.

## 5ac. HTML-email fallback PDF rendering (system Edge via Playwright)
- **Problem** — when a record has **no matched attachment**, `_write_record_document` rendered a
  PDF from the email's stripped **plaintext** (`render_text_to_pdf`, via LibreOffice) — an ugly
  monospace header dump. Now it renders the email's **real HTML body** through headless system Edge,
  so the saved document looks like the actual email (styled receipt box etc.), like it does in Gmail.
- **Additive & non-breaking** — the plaintext `text` used for **classification** is untouched. The
  HTML render is used **only** for the no-attachment fallback document, and `render_text_to_pdf`
  remains the safety net: `ok = render_html_to_pdf(...) if html_body else False; if not ok: ok =
  render_text_to_pdf(text, tmp)`. If Playwright/Edge isn't set up (or a render times out/fails), it
  falls back silently — the CLI can't break.
- **Data threading** (`classify_emails.py`): `read_email_content` now returns a **3-tuple**
  `(text, attachments, html_body)`. `_read_eml` captures the raw (unstripped) HTML part via
  `msg.get_body(preferencelist=("html",))`; `_read_msg` captures `m.htmlBody` unconditionally.
  Non-email sources (`.txt`, standalone doc) → `html_body=None`. Each attachment dict gains a
  `content_id` (eml: `part.get("Content-ID")`; msg: `att.cid`/`att.contentId`, stripped of `<>`).
  `process_source_email` builds `cid_images` (`{cid: (bytes, mimetype)}`) once per email and threads
  `html_body`/`cid_images` through `assign_invoice_files` → `save_review_files` →
  `_write_record_document` (all new params default `None`, so nothing else changes).
- **Inline images** — `cid:XXXX` refs in the HTML are swapped to `data:<mime>;base64,…` URIs from
  `cid_images` (`_inline_cid_images`) so embedded logos render with no network fetch; remote
  `http(s)` images are fetched live by Edge at render time (through the OS cert store / Techloq
  proxy, same as normal browsing). *If image fetches ever fail on the proxy, that's the place to
  look.*
- **Singleton browser** — `_get_edge_browser()` lazily launches one headless Edge
  (`chromium.launch(channel="msedge")`) reused for the whole batch and closed at `atexit`. This path
  runs **only from the CLI batch** (`classify_emails.py`/`run_all.py`) — the always-on FastAPI review
  UI never calls it — so a plain process-exit close is safe. Launch/import failure is remembered
  (`_EDGE_UNAVAILABLE`) so it's attempted at most once per run.
- **Setup** — `pip install playwright` then `playwright install msedge` (registers the channel against
  the already-installed system Edge; no full-browser download). `playwright` added to
  `requirements.txt`. Until installed, every no-attachment record just uses the old plaintext render.
- **Verified** (self-reverting `test_htmlrender.py`, no Playwright needed): `read_email_content`
  returns a 3-tuple with raw HTML for the real CARLO MINUTO receipt `.eml`; `.txt` → `html_body=None`;
  `cid` inlining swaps known cids to data URIs and leaves unknown/remote refs alone; fallback order is
  HTML-first, text safety-net on HTML failure, text-only when no HTML; `render_html_to_pdf` returns
  `False` (never raises) when Playwright is absent. All main modules import.

## 5ad. Oversized-scan handling: raised output budget + page chunking
- **Problem** — "Scan to E-mail Server Job" emails carry ONE big scanned PDF (a stack of separate
  paper invoices scanned at the office MFP, e.g. 12 / 20 pages). Asking the model to enumerate every
  invoice across all pages into one JSON response exhausted the 8192-token output cap and returned
  **zero** usable text (`Empty response from model (stop_reason=max_tokens)`). `classify_with_retry`
  retried 3× with identical input, failed identically, and dumped the whole scan in the review queue
  as unreadable. Fixed in two complementary ways.
- **Part 1 — raised `MAX_TOKENS` 8192 → 20000** (`classify_emails.py:113`). One-line ceiling bump,
  fixes the common case. `claude-sonnet-5`'s real output ceiling is 128K, but this pipeline calls the
  API **non-streaming**, and the anthropic SDK (`_calculate_nonstreaming_timeout`) **hard-raises**
  "Streaming is required…" once `max_tokens` would risk the 10-min non-streaming timeout — i.e. above
  ~21,333 (`128000·600/3600`). 20000 is the max SAFE non-streaming value (~2.4× the old cap); going
  higher would force a streaming rewrite. No beta header needed (`output-128k-*` was a Claude-3.7
  thing; 128K is native on Sonnet 5). Verified the SDK guard's exact threshold in
  `anthropic` 0.116.0 before choosing the number.
- **Part 2 — auto-split a lone oversized scan** (safety net for scans too dense even for 20000).
  Scoped narrowly: `_lone_oversized_pdf(attachments)` engages ONLY when the email's SINGLE attachment
  is a PDF (`_effective_media_type`) with **more than `CHUNK_THRESHOLD_PAGES` (8)** NON-BLANK pages;
  multiple attachments, non-PDF, or short PDFs all fall through to the unchanged single call.
  - **Blank-back (duplex) guard (2026-09).** Before chunking, `_count_nonblank_pdf_pages` (PyMuPDF —
    optional; falls back to the raw page count if not installed) renders each page at 40 dpi and counts
    those that are NOT essentially blank (≥`0.90` of pixels near-white in the 235-255 band). A duplex
    scan of a single invoice comes through as `content, blank-back, content, blank-back…` — double its
    real length — so a 5-page invoice scanned duplex is 10 physical pages and used to trip the >8
    chunker, which split ONE invoice across two independently-classified chunks that each thought pages
    were missing (reproduced: review #2299/#2301 — chunk 1 recorded a partial Zahler invoice, chunk 2
    flagged "only page 1/5 and 2/5 visible"). Now only real content pages count, so that scan (10 raw →
    5 real) takes the single call. Thresholds calibrated on that exact scan (blank backs measured
    ~0.98-0.99 near-white, content ≤0.73 — wide clean gap; the briefing's stricter 250-255 @ 0.995
    detected 0 blanks and was corrected). Erring toward "blank" is the SAFE direction (a lower count only
    makes chunking less likely; over-eager chunking is the bug). Verified live: #2299 → no chunk; genuine
    12- and 10-real-page multi-invoice scans still chunk. `pymupdf` added to requirements. When it engages,
  `_classify_oversized_scan` splits the PDF into sequential `CHUNK_SIZE_PAGES (6)`-page chunks
  (`_split_pdf_into_chunks`), classifies each chunk on its own (same email `text` + a note "part k of
  n … classify only these pages"), and **merges** the per-chunk classifications:
  - `invoices`/`credit_memos`/`payments`/`statement_invoices` concatenated IN ORDER; first chunk's
    top-level fields (`email_type`, `route_to`, …) kept as the base; `needs_human_review` OR'd.
  - **CRITICAL page re-offset** — before appending, each record's `source_pages` and `page_rotations`
    keys are shifted from chunk-local to ORIGINAL-PDF numbering (`_offset_page_spec`,
    `_offset_rotations`; offset = chunk start − 1). Downstream `_write_record_document` →
    `_extract_pdf_pages` re-opens the **original full PDF** and splits by `source_pages` verbatim, so
    these MUST be global page numbers or every saved file would contain the wrong pages. A chunk
    record with no/blank `source_pages` (model treated it as spanning the whole chunk) is bounded to
    that chunk's global range, never the whole scan.
  - **No silent loss** — if any chunk's `classify_with_retry` still fails after 3 attempts, the WHOLE
    merged result fails with a descriptive error (`page {a}-{b} of scan failed to classify: …`), so it
    routes exactly like today's failure; a network-blip chunk propagates `infra_error` (left for
    retry, not dumped to review).
  - `attachments` handed to the downstream save path stays the **original single full-PDF** (chunks
    are only for the model calls), so each record's file is split from the real original bytes.
- **Where** — the split/single decision sits in `process_source_email`, after `read_email_content` +
  `expand_zip_attachments` + manual-upload append, right before the classify call; the merged result
  feeds the SAME dedup / `assign_invoice_files` / `save_review_files` pipeline as any single-call
  result. The non-chunking path (the vast majority of emails) is untouched.
- **Verified** (self-reverting `test_chunkscan.py`, no API key — `classify_with_retry` monkeypatched):
  `MAX_TOKENS` is >8192 and under the SDK's non-streaming guard; offset helpers shift `source_pages`
  and rotation-dict keys (positional lists/None left alone); the gate chunks only a lone >8-page PDF;
  the real 12-page scan splits to `[(1,6),(7,12)]`; the merge yields 12 invoices with GLOBAL
  `source_pages` in order and preserved top-level fields; **every merged record's split PDF matches
  its correct original page by embedded-image fingerprint** (these are image-only scans, so pages are
  compared by content, not text); a failed chunk fails the whole result, and an infra-error chunk
  propagates `infra_error`. All main modules import.

## 5ae. "Resolved but produced nothing" console log line
- **Problem** — in `process_source_email`, a successful classification prints `saved N invoice
  file(s)` / `saved N flagged record(s)` only when those lists are non-empty. When BOTH came back
  empty the log said **nothing** — so a legitimate zero-output email (informational, or an
  all-excluded/all-held batch) looked IDENTICAL to a record silently vanishing. Hit live on a 20-page
  McKesson scan (McKesson is QB-excluded, so `routes_to_review` + `_record_needs_file` both correctly
  returned False): zero files/flags was correct, but indistinguishable from a bug without manually
  pulling the PDF apart.
- **Fix** — purely additive `else` log branch (no routing logic touched, so it can't change what's
  saved/flagged/recorded — only what prints). When nothing was saved AND nothing queued: if there are
  no records, print `no invoice/credit-memo/payment records found (informational/non-invoice email)`;
  otherwise print `N record(s) found, none needed a file/review (…breakdown…) — recorded only`, tallying
  **excluded-vendor** (`is_excluded_vendor`, invoices/credit-memos) and **sales-order-held**
  (`is_sales_order`, invoices), plus an **`UNEXPECTED — investigate`** count for any record explained by
  neither — the anomaly signal that was missing.
- **Overlap fix (deviation from the literal spec)** — the task's `other = len(records) − excluded −
  held` assumed the buckets are mutually exclusive, but they are **not**: a held sales order from an
  excluded vendor is in BOTH, so the subtraction could go **negative** and false-fire `UNEXPECTED` on a
  legitimate outcome. `other` is instead computed **per-record** (`not _excluded and not _held`), so it
  is always a true non-negative count of genuinely-unexplained records.
- **Verified** (self-reverting `test_emptyoutcome.py`, no API key — drove the REAL
  `process_source_email` on the real McKesson `.eml` with the API call + file-writers stubbed):
  all-excluded batch → `13 record(s) found … (13 excluded-vendor) — recorded only`; zero records →
  the informational line; held-only → `1 sales-order-held`; excluded+held overlap → both tallies, NO
  false `UNEXPECTED` and no negative count; a non-excluded/non-held fall-through → `1 UNEXPECTED —
  investigate`; a normal saved invoice → existing `saved 1 invoice file(s)` intact with no extra line.
  All main modules import.

## 5af. Requeue already-clean records by vendor (`requeue_by_vendor.py`)

Counterpart to `requeue_all_reviews.py` (§ review-queue requeue) for records that **never got
flagged** — they classified cleanly, were recorded, and their source `.eml` was archived to
`processed_emails/` with `emails.status='processed'`. That status is exactly what
`load_processed_filenames()` checks to SKIP a file even when it's back in `test_emails/`, so moving the
`.eml` alone does nothing. Built to retroactively apply the **multi-store cost-split rule** (§5, added
2026-09) to already-recorded Sageteq records (#29365/#29389) so they come back `skip_qb_import` on
reprocess. DB mode only.

    python requeue_by_vendor.py "Sageteq"            # dry-run: list what would be requeued
    python requeue_by_vendor.py "Sageteq" --commit   # back up, move .eml, status='fetched'
    then:  python run_all.py --drive                 # re-classify under the current rules

- **Match** — every recorded record (`invoices`/`credit_memos`/`payments`) whose `vendor_matched`
  matches the arg by the SAME normalized-substring rule as `is_excluded_vendor()`/`database._norm`
  (either direction) — matches by **vendor, not invoice shape**, so it also lists Sageteq's genuine
  single-store invoices (#29347–29353); that's expected — the prompt rule re-sorts split-vs-single on
  reprocess, this tool only requeues.
- **Group + locate** — records are grouped by their source-email filename (`email_filename`; a split
  invoice's 7 records share ONE `.eml`), each located via the same `processed_emails/` → `review_pending/`
  → `test_emails/` search as `requeue_all_reviews._find_source_eml`. A source `.eml` not found on disk is
  **reported and left untouched**, never silently dropped. Dry-run prints, per email, each record's
  vendor/store/#/amount and the total count so the operator can eyeball the batch first.
- **Commit (mirrors `requeue_all_reviews`)** — (1) backs up every affected result row to timestamped
  `<table>_requeue_backup_<ts>` tables first (recovery path; empty ones dropped), (2) moves each `.eml`
  into `test_emails/` (skip if already there) and `UPDATE emails SET status='fetched' WHERE filename =
  ANY(%s)`. Deletes **nothing** — reprocessing replaces the DB rows via `save_results`' `ON CONFLICT
  (filename) DO UPDATE`, and the dup check self-matches on source-email identity, so history isn't
  double-counted. Any `invoice_files/` PDF the reprocessed record no longer needs (now `skip_qb_import`)
  is **ORPHANED on disk** — those exact existing paths are listed for manual cleanup (the tool never
  deletes files). Errors roll back the DB (moved files stay moved; rerun is safe).
- **Verified** (self-reverting fake-DB test): a Sageteq query matches all 4 Sageteq records (split #29365
  ×3 + single #29347), excludes McKesson, groups into 2 source emails (split shown as 3 records); a
  missing `.eml` is reported not dropped; existing file paths are listed as orphan-risk; `--commit` writes
  the invoices backup (empty credit_memos/payments backups dropped), moves both `.eml` into
  `test_emails/`, sets `status='fetched'` for exactly those two filenames, and prints the reprocess hint;
  missing-arg → usage, no-match → clean no-op. All output is cp1252-safe (Windows console).

## 5ag. Contact (To/Cc) → store map (2026-09)

The mirror of `sender_store_map`, keyed on a **recipient** address instead of the sender — for a
THIRD-PARTY vendor emailing US where who it's addressed **To/Cc** identifies the store (e.g. a biller
that emails `vitamins@maplerx.com` or a buyer's own `yossi@thevitamindesk.com` → Maple Pharmacy).
Additive; built exactly like `sender_store_map`/`card_store_map`:
- New `contact_store_map` table + `DEFAULT_CONTACT_STORE_MAP` (seeded only when empty) +
  `database.load_contact_store_map()`. Registered in the generic reference editor
  (`review_app.REFERENCE_PAGES` + `web_db._REF`) as **"Contact → store"**, nav under Setup as
  **Contacts** — so Joel adds more himself, no code change.
- `classify_emails.py`: module `CONTACT_STORE_MAP` (loaded in `main()` next to `SENDER_STORE_MAP`);
  new `_extract_recipients(text)` (all To+Cc addrs via `getaddresses`) and `_contact_store_hint(text)`
  — a decisive HIGH-confidence store instruction appended to `text` right after `_sender_store_hint`.
  Returns "" when no recipient matches OR when matched recipients point to **conflicting** stores
  (ambiguous → left to normal review). **Cc is now captured** in the header block of `_read_eml`
  (added to the `From/To/Subject/Date` list) and `_read_msg` (`getattr(m,"cc",None)`); this only adds
  a header line to `text`, nothing else changes.
- Verified: To/Cc match → hint; no-match/conflict/sender-only → empty; defaults + registrations present.

## 5ah. Fetch the invoice page for link-only emails (BillerGenie pilot, 2026-09)

Some vendors (Herbal Health USA via **BillerGenie**) send **link-only** invoice emails — no invoice
attachment, just a customer-portal link whose **public (no-login) page** shows invoice #, balance,
due date, and billing address. Now the pipeline fetches that PAGE and renders it to a PDF fed into the
normal classification path, instead of classifying from only the email body. **Scoped to a verified
allow-list**, not "follow any link":
- `LINK_INVOICE_DOMAINS = ["customer.billergenie.com"]` (a list on purpose — add a domain ONLY after
  confirming that vendor's portal is publicly viewable the same way; this fetches a real external site).
- Detection in `process_source_email`, after zip-expand + manual-uploads: when there is **no usable
  document** (`_has_usable_document` — an inline cid image like BillerGenie's `logo.png` does NOT
  count) AND the HTML body has a link whose host matches the allow-list (`_find_invoice_link`, exact
  or dot-subdomain match, rejects lookalikes like `…billergenie.com.evil.com`), it fetches that URL.
- `render_url_to_pdf(url, out)` reuses the session's Playwright/system-Edge singleton: `goto` →
  wait for `networkidle` (the portal is a client-side app that loads data after `load`) → **emulate
  `screen` media** (the portal hides content under print CSS — plain `page.pdf()` came out blank) →
  `page.pdf(...)`. Bails to `False` if the page is essentially blank (`< 40` chars body text) or on any
  error. The fetched PDF is appended as `invoice_portal_page.pdf`; **any failure leaves attachments
  empty → falls through to today's email-body render** (never a hard dependency on the site).
- **Verified against the 6 real Herbal Health/BillerGenie emails in `processed_emails/`** (#2740/#2770/
  #2773/#2779/#2653/#2666): each detected as link-only (logo excluded), fetched, and rendered to a
  ~68 KB PDF showing the correct invoice #, amount ($659.77 / $461.08 / $2,294.64 / $270.06 / $154.75 /
  $154.75), and due date. **Next candidate** Masterko ("View & pay your account online") is NOT added —
  verify its portal's public view by opening a real link first, then append its host to
  `LINK_INVOICE_DOMAINS`.

## 5ai. Vendor / account standardization (2026-09)

Codes each invoice/credit-memo's vendor to a canonical **standard vendor**, looks up that vendor's
**standard account**, and resolves BOTH to the **store-specific spelling** used in that entity's
QuickBooks — so exports carry consistent vendor + account names regardless of how the source doc
spelled the vendor. Additive, deterministic post-classification (same slot as `apply_class_rules`),
DB-only (no-op in Excel mode / until seeded). Payments intentionally excluded (no expense account in
QB, not on /qb).
- **Tables** (`database.py`): `vendor_name_map` / `account_name_map` (standard name + 7 store columns:
  `name_3m/madison/maple/refuah/westgate/bmw/lakewood_rx`) and `vendor_account_assignment` (standard
  vendor → standard account). New columns `standard_vendor/standard_account/qb_vendor/qb_account` on
  `invoices` + `credit_memos`, written by `_insert_invoice`/`_insert_credit_memo`.
- **Matching** (`classify_emails.apply_vendor_standardization`): normalize the record's vendor
  (`_norm_vendor_std_key` — reuses `_norm_vendor_dedupe`'s legal-suffix stripping, then `&`→`and`, drop
  punctuation, collapse space) → **exact** lookup first, then **fuzzy** (`rapidfuzz.token_sort_ratio`,
  stdlib `difflib` fallback; accept only ≥`VENDOR_FUZZY_THRESHOLD=88` AND beating the runner-up
  *different* standard by `VENDOR_FUZZY_MARGIN=4`, so two similar-but-different vendors never silently
  merge — an ambiguous case falls through to review). A fuzzy match is auto-accepted with an audit note
  in `notes`. No vendor match, OR a match with **no account assigned**, sets `needs_human_review` with a
  distinct review reason (still recorded, just no `qb_vendor`/`qb_account` until resolved). Store-specific
  spelling falls back to the standard name when that store's cell is blank. Loaded once/run into
  `VENDOR_STD` in `main()`; `database.load_vendor_standardization` builds bare `.strip().lower()` keys and
  `classify_emails` re-keys with the real normalizer (keeps fuzzy/normalization logic in one file, no
  import cycle).
- **Setup UI** (generic `_REF`/`REFERENCE_PAGES` editor, zero new UI code): **Vendor names**, **Account
  names**, **Vendor→account** (the last two selects sourced from `REFERENCE_OPTION_SOURCES`
  `vendor_names`/`account_names`); nav links under Setup; a client-side **Filter box** added to
  `reference.html` for the 600+-row tables.
- **QB entry**: `/qb` gains an **Account** column (after Vendor) and the **Vendor** column now shows
  `qb_vendor` (the store-specific spelling) falling back to `vendor_matched`; threaded through
  `list_qb_ready`, `QB_EXPORT_COLUMNS`/`QB_DATA_ATTR`/`_qb_cell_value`, and the qb.html macro (so CSV +
  Copy match the screen).
- **Import**: one-time `import_vendor_standardization.py` upserts the three CSVs (idempotent — re-run on
  updated sheets). `rapidfuzz` added to requirements. **Verified** (self-reverting fake-`VENDOR_STD`
  test): exact + store-specific resolution; store-without-spelling fallback; fuzzy typo match + audit
  note; no-mapping and no-account review flags; empty-`VENDOR_STD` no-op; the equidistant-ambiguity
  margin guard. All modules import; templates parse. ⚠ New tables/columns apply on next `init_db()`
  (restart / import script), and the CSVs must be dropped in the project root before the import.

### QB entry — full filter bar (2026-09-15)

`/qb` gained the filters already available on Records/Payments/Review, chosen for QB-entry relevance:
**Vendor** (`ILIKE` contains), a general **Search** box (vendor / invoice# / email), **Date range**
(`record_date` from/to), **Amount range** (min/max — note credit memos are negative), and a **Type**
dropdown (Invoice / Credit memo / All). Records' invoice_number-only and status filters were deliberately
skipped (status is email/review state, not QB-readiness).
- `web_db.list_qb_ready()` — new params added as **outer clauses on `q`** (the already-unioned/aliased
  result, so one clause covers both invoice + credit_memo without threading through both UNION arms).
  `outer_params` already flows into the final `execute`, so no other change.
- `review_app.py` — both `qb_list()` (page) and `qb_export()` (CSV) take the new params and pass them
  through, so the CSV download always matches the visible rows; `params` dict carries them onto the
  All / Not-entered toggles and sort links; `record_types=["invoice","credit_memo"]` feeds the dropdown.
- `templates/qb.html` — six fields added to the existing filter `<form>`; the typed/date/number fields
  (Vendor, Search, Date from/to, Amount min/max) do **not** auto-submit — they apply via an explicit
  **Apply** button with a **Reset** link to bare `/qb`, mirroring Records' (browse.html) pattern. The
  single-click dropdowns (Store, Run, dup, show-ignored, Type) keep auto-submit and carry the typed values
  along. The persistent-querystring `base` builder carries all of them onto the toggle + CSV links.
- **Verified**: all modules compile, qb.html parses, and every new filter path executes against the live
  DB (record_type→15, search→34, date/amount ranges, combined) with no SQL error.

### Retroactive transfer/excluded-vendor backlog cleanup (2026-09-15)

`cleanup_transfer_backlog.py` (one-time, run by hand) flips `skip_qb_import=TRUE` on every
already-recorded invoice/credit_memo whose vendor is on `excluded_vendors` but that predates the
deterministic `apply_vendor_exclusion` enforcement — now including **store-to-store inventory transfers**
since the store names (Madison/Refuah/Westgate/3M/Maple Pharmacy) are on Setup → Excluded vendors (a
transfer's `vendor_matched` is the counterparty store). Matching is identical to
`is_excluded_vendor()` (normalized substring either direction via `classify_emails._norm`); the script
loads the excluded list **once** and reuses **one** connection rather than calling `is_excluded_vendor()`
per row, because `database.load_excluded_vendors()` opens a fresh (slow, remote) connection every call —
877 rows × per-row connect would run for hours. **Run 2026-09-15**: 13 invoices + 13 credit_memos flipped
(store-named vendors + one stray McKesson); verified 0 excluded-vendor rows remain QB-ready, all records
still present for the audit trail. Idempotent; the code enforcement handles all future runs.

### "Multiple versions exist" notice on flagged/review records (2026-09-15)

`apply_duplicate_checks()` deliberately **skips** any record already flagged for review (`record_flagged`
→ `continue`), so a review-bound record was never cross-checked against invoice history — even though a
prior recorded copy might exist. New **read-only** pass `enrich_flagged_with_duplicate_info(results,
history)` (in `classify_emails.py`, right after `apply_duplicate_checks`; called just before the
`newly_flagged` → `build_review_rows` loop) runs `check_duplicate()` for flagged invoices too and, on a
`duplicate`/`possible`/`store_conflict` match, **prepends** a "Multiple versions of invoice #… from …
exist — see the Group link…" notice to the review reason and stamps the shared `duplicate_group_id` on the
record. It **never auto-records/resolves/supersedes** the flagged record — it only annotates + links, since
the item is already flagged for its own reason and may not be a real revision. (`_group_id_for` may stamp
the group id on the prior's live row so the Group link can show both copies — benign metadata.)
- **DB**: new `review_queue.duplicate_group_id TEXT` (in the CREATE + an idempotent `ADD COLUMN IF NOT
  EXISTS`, applied by `init_db()` — done live 2026-09-15). `_insert_review_row` and `load_review_queue`
  carry it (`"Duplicate Group"`), so it survives the full-replace `save_review_queue` on carryover rows.
- **Pipeline**: `"Duplicate Group"` added to `REVIEW_QUEUE_CONTEXT_FIELDS`; `build_review_rows` sets it in
  both the record and record-less branches.
- **Web**: `list_open_reviews` / `get_review` already `SELECT rq.*`, so the field flows through; a **Group
  <id>** pill linking to `/browse?group=<id>` was added to `review_list.html` (regular-review branch) and
  `review_detail.html` (under the flag reason), mirroring the existing qb.html/browse pattern.
- **Verified**: all modules compile, templates parse; a no-DB unit test confirms the notice+group id land
  on a match and that a non-match, an unflagged record, and a sales order are left untouched (no false
  positives, no crash on null classification); the new column is live and both review read paths expose it.
  ⚠ Behavior on the pipeline (the notice text on a real retry, e.g. `--retry-id 2367`) applies on the next
  `classify_emails.py` run — that run is Joel's (no API key here).

### XLSX/XLSM attachment support (2026-09-15)

`.xlsx`/`.xlsm` attachments now reach the model instead of flagging as unreadable — their cell values are
extracted to tab-separated text and inlined into the prompt, the same way CSV already is (openpyxl, already
a dependency; legacy binary `.xls` is **not** covered — still unreadable). All in `classify_emails.py`:
- New `SUPPORTED_SPREADSHEET_TYPES` set (the two OOXML spreadsheet MIME types) + `.xlsx`/`.xlsm` entries in
  `EXT_TO_MEDIA_TYPE` / `MEDIA_TYPE_TO_EXT`, so a spreadsheet tagged `application/octet-stream` still
  resolves by extension.
- `_extract_xlsx_text(data, max_rows_per_sheet=300, max_sheets=10)` — `load_workbook(data_only=True,
  read_only=True)`: formulas render as their **last cached value**, blank rows skipped, tab-separated,
  per-sheet/workbook truncation with an explicit "more … not shown" note; a non-workbook fails gracefully
  to `[could not open as a spreadsheet: …]` (no crash).
- New branch in `build_attachment_blocks` (inlined as a labelled text block, like CSV); recognized by
  `_effective_media_type`; and picked up **inside a zip** by `_expand_one_zip`'s member filter.
- **Verified**: module compiles; unit tests cover multi-sheet + cached-formula + blank-row extraction,
  the 300-row truncation cap, empty-workbook and garbage-bytes handling, block routing (labelled text,
  nothing skipped), and the octet-stream→extension fallback. PDF/image/CSV paths are untouched (the new
  branch only fires for the two spreadsheet media types).
  ⚠ Retesting on real emails (e.g. `--retry-id 2366`, or `--retry-unreadable` to clear the whole
  unreadable-attachment backlog once the wording fix is also live) is Joel's run — no API key here.

### Promotional email + unreadable attachment — prompt carve-out (2026-09-15)

`SYSTEM_PROMPT_TEMPLATE` (the "Shipment/delivery notifications…" paragraph): **removed** "or an attachment
that couldn't be read" from the inline review-trigger list, and **added** a promotional/marketing carve-out
— a new-product announcement, sale blast, or newsletter (recognized by its own wording / mailing-list
framing) routes to `"none"` with `needs_human_review` false **even if** it carries an unreadable attachment
(blank order form, catalog, price list, flyer), because a blank template isn't a real order and the email's
own wording says so. "An attachment that couldn't be read" is now reserved as a review trigger for a
document whose own text reads like a REAL transaction ("invoice attached," "please remit," "balance due").
Prompt-text only — compiles; classifier behavior applies on Joel's next `classify_emails.py` run (no API
key here). Test: `--retry-id 2366` should route to none/no-flag; spot-check other prior "attachment
couldn't be read" flags — only clearly-promotional ones should flip, real-transaction ones must not.
General-case fix; the XLSX support above was the one-off unblock for that specific attachment type.

### check_duplicate self-match gate — AMOUNT-based, same-email revisions (2026-09-15)

**Root cause of review #2396/#2412:** `check_duplicate()` treated ANY prior from the same `source_id` (same
original email) as "self" and skipped it — so a duplicate-then-revision pair bundled in ONE scan-to-email
job was never compared, getting no dup flag, no revision flag, no group link. The **self gate is now
AMOUNT-based, not filename-based**: a first attempt narrowed the skip to `prior_file == source_file`, but
review/recorded filenames are GENERATED from (store, vendor, invoice#, amount, date) and **collide by
design** — two different scans with the same vendor/number/amount get the identical filename, which
re-triggered the bug (the $458.38 recorded copy and the $458.38 review item shared a filename → still
skipped). Correct gate:
- same email + **same amount** → **self** (literal same document, or same page scanned twice); if the
  filenames differ it's additionally noted as `redundant` (→ auto-ignored "duplicate" via the tail), else
  pure `self`. Filename only distinguishes redundant-vs-self for the audit note; it never gates the skip.
- same email + **different amount** → **"possible"**, worded as a likely **REVISED/CORRECTED invoice** with
  a "(from the same email/scan)" suffix — the case previously swallowed as self.
- cross-email store_conflict / duplicate / possible paths unchanged.
No other function changed: `enrich_flagged_with_duplicate_info` / `apply_duplicate_checks` already handle
these statuses, and `_group_id_for` already reuses a prior's existing `duplicate_group_id`, so A+B+C
sharing one group falls out automatically once this gate stops blocking same-email matches.
- **Verified** (no-DB unit test): the KEY regression (same email + **same generated filename** + different
  amount → "possible"/revision); the #2412 three-record shape returns "possible" matching the $427.42
  original; same-email/same-amount/different-file → redundant duplicate; same-email/same-amount/same-file →
  pure self; different invoice numbers → none (no false cross-match); cross-email store_conflict / duplicate.
  Docstring updated. Applies on Joel's next run.

### _group_id_for retroactive stamp — self-heal on reuse (2026-09-15)

**Symptom:** Mehadrin #797482 group `D18513E8` had the $458.38 copies but NOT the true
$427.42 original (invoices id 1744, `duplicate_group_id=''`), so `/browse?group=D18513E8`
showed only 1 row. **Diagnosis (against the live DB):** NOT the briefing's hypothesized
`email_filename` vs `invoice_history.source_file` mismatch — those matched byte-for-byte for 1744/its
history row (76598, which *does* carry the gid). Real cause: `_group_id_for` stamped the prior's **live
row** only in the *mint* branch; `if gid: return gid` short-circuited on reuse. The original is tagged
only via that retroactive stamp (it had no prior when first recorded), so a single mint-time miss — here
find_live_invoice resolving to the *other* $427.42 recording (id 1745) or 1744 sitting under a
churned-filename at that moment — was never retried, because every later match saw the prior already had a
gid and returned early.
- **Fix**: `_group_id_for` now runs the live-row stamp on **both mint and reuse**, stamping only when the
  live row's `duplicate_group_id` is missing/`''`/different from the gid (guarded by find_live_invoice's
  returned value, so no redundant UPDATE per reuse). Self-heals any original that missed its one-shot stamp
  on the next match. `find_live_invoice` itself was left as-is (its exact `email_filename = source_file`
  match is correct for this case). Compiles; applies on Joel's next run.
- **Backfilled** the specific case: `UPDATE invoices SET duplicate_group_id='D18513E8' WHERE id=1744` (was
  `''`), committed 2026-09-15; verified `/browse?group=D18513E8` now returns the 2 expected non-ignored
  rows ($427.42 id 1744 + $458.38 id 1885). The other three group members (1745/1984/1993) stay
  `duplicate_ignored` → hidden by default (correct; visible via show-ignored). Display/linking only — no
  record was double-entered or mis-recorded either way.

### Vendor Names — store-independent aliases field (2026-09-15)

New `vendor_name_map.aliases` column: a free-text list (one variant per line — also comma-separated) of
alternate spellings of the same vendor that can appear from ANY store's documents (OCR quirks,
abbreviations, old letterheads), not tied to a store column. Matched **identically** to the standard name
and store columns — `database.load_vendor_standardization()` feeds each alias line into the same `lookup`
dict (`re.split(r"[\n,]+", …)`, blanks ignored), which `classify_emails` re-keys with its own normalizer,
so exact-normalized-first-then-fuzzy applies with no classifier change.
- `database.py`: `aliases TEXT` in the `CREATE TABLE vendor_name_map` + idempotent `ADD COLUMN IF NOT
  EXISTS` (applied via `init_db()`, done live 2026-09-15); `import re` added.
- `web_db.py`: `"aliases"` added to `_REF["vendor_name_map"]["cols"]` (generic add/edit/`_ref_clean`
  machinery handles it — textarea included, no other change).
- `review_app.py`: `REFERENCE_PAGES["vendor_name_map"]` gains an "Other spellings / variants" **textarea**
  column (reference.html already supports `type:"textarea"`) + updated help text.
- **Verified**: all three modules compile; column is live; self-reverting DB test confirms standard name +
  3 alias variants (newline- and comma-split) + store spelling all resolve to the standard name; store
  columns and `account_name_map` untouched. Applies on Joel's next run.

### Prompt: correct page order/orientation before recounting line items (2026-09-15)

`SYSTEM_PROMPT_TEMPLATE` — new paragraph inserted between the "recount every line item carefully"
paragraph and the "Separately, before writing invoice_amount…" paragraph. Tells the model that a scanned
multi-page doc may have pages out of physical order, upside-down, or sideways, and that this is **never**
by itself a reason to flag a line-item/total mismatch: first work out the true reading order/orientation
(printed page numbers, or content continuity — running item list, "continued" marker, repeated
header/invoice#), read every page upright and in true sequence, then recount/total using that corrected
order — only flag a mismatch if it still doesn't reconcile. Prompt-text only; compiles; classifier behavior
applies on Joel's next run. Test: `--retry-id <current id for Nutri-Supreme #68719>` should stop citing a
line-item/total mismatch (the 34 items reconcile to $12,509.40 once read in order). Its "no standardized
vendor mapping" flag is the separate aliases-field fix above.

### Assign review items to a user (2026-09-15)

Review-queue rows can be assigned to an active user — with a filter, list + detail controls, and (the
easy-to-miss part) survival across the queue's per-run delete+reinsert.
- **Schema**: `review_queue.assigned_to TEXT` via `WEB_DDL` `ADD COLUMN IF NOT EXISTS` (applied by
  `init_web_schema()`, done live 2026-09-15).
- **Carryover survival**: `database.load_review_queue()` returns `"Assigned To"`, and `_insert_review_row`
  writes `assigned_to` back on every reinsert — so an assignment persists through `save_review_queue`'s
  delete+reinsert (freshly-flagged rows default NULL = unassigned). Same mechanism as Notes / Duplicate
  Group. (Since 2026-10-07 a RE-flagged item's fresh row also keeps its assignee — `carry_review_assignments`.)
- **web_db.py**: `assign_review(review_id, assigned_to, username)` sets/clears (`''`→NULL) with an
  `edit_log` audit entry; `_review_filter_clause` gains an `assigned` param (a username, or the sentinel
  `"__unassigned__"` → `assigned_to IS NULL OR ''`), threaded through `list_open_reviews` + `answered_count`.
- **review_app.py**: `/review` takes `assigned=`, passes it through, and hands the template the active-user
  list (`review_detail` too); new `POST /review/{id}/assign` (redirects back via referer). Statement-recon
  exceptions (not in review_queue, no assignee) are hidden when an `assigned` filter is active, like the
  store filter.
- **Templates**: `review_list.html` — Assigned-to filter dropdown (anyone / unassigned / each user),
  clear-filters condition widened, and per-row 👤 pill + quick-reassign select; `review_detail.html` —
  editable Assigned-to row after Run.
- **Verified** (self-reverting DB test): assign writes the value + audit row; filter by user finds it and
  `__unassigned__` excludes it; clear → NULL; and it survives a `load_review_queue` → delete → reinsert
  round-trip. All modules compile; both templates parse; column is live. Web-only — the CLI is untouched.

### Records page: label the Date for in-review rows (2026-09-15)

The `all_records` view substitutes `rq.date_flagged::text` for `record_date` on review-queue rows (they
have no invoice date), so an in-review row's "Date" silently meant *when it was flagged* while every other
row's Date means *the document's own printed date* — which looked like a timing mismatch on Hold End Dist
WB-001987 (real invoice date 09/10/2026; the page just had no field to show it for a still-in-review row).
Fix is display-only in `templates/browse.html`: for `is_review` rows the Date cell now renders
`flagged <date>` in muted text with an explanatory tooltip; recorded rows are unchanged. No schema change,
same underlying value → column sort-by-Date still works. Template parses.

### Records page: Amount filter (2026-09-15)

`/browse?source=records` declared `amount_min`/`amount_max` (shared with the History tab) but never forwarded
them to `list_all_records`, and the Records filter form had no Amount fields — so they silently did nothing.
Wired through: `web_db.list_all_records` gains `amount_min`/`amount_max` params + `amount >= / <= %s` clauses
(mirrors the existing `date_from`/`date_to` block); `review_app.browse()` passes them through and into `params`;
`browse.html` Records form gets two number inputs beside Added-from/to. Verified: modules compile, template
parses, and the filter narrows live results (invoices ≥1000 → 422, ≤50 → 157, 100..500 → 719 of 1709).

### Duplicate-group backfill for pre-existing pairs (2026-09-15)

`_group_id_for`'s self-heal only fixes a pair when a NEW match re-triggers it, so pairs already in the DB
before the stamp succeeded stayed split. Ran the briefing's general backfill (`UPDATE {invoices,payments}
orig SET duplicate_group_id = later.duplicate_group_id FROM … WHERE later.duplicate_of = orig.email_filename
AND …`): **45 invoice originals + 1 payment** stamped, committed 2026-09-15.
- **8 residual invoice originals** belong to >1 conflicting group (churned-filename artifact — the same
  original is pointed at by later copies carrying different gids, so no single value satisfies all); left
  as-is (a manual call per the briefing).
- **The join only reaches "real duplicate" pairs.** `apply_duplicate_checks` sets `duplicate_of` only for
  `status=="duplicate"`, never for `"possible"` (revisions), so revision/possible pairs have a blank
  `duplicate_of` and are invisible to this backfill. That's why **F98B40C0** (Prodermix #9152, a $2393.78→
  $2333.78 revision) wasn't fixed by the general pass — its original (1694) was stamped directly instead.
  **42 more** invoice originals remain ungrouped but linkable by vendor+invoice# (the possible-pair gap) —
  handled by a second pass below.
- **Second pass — vendor+invoice# backfill of the possible-pair gap (2026-09-15).** Of those 42, stamped an
  original's `duplicate_group_id` **only when exactly one** distinct group id is reachable across its
  same-vendor+invoice# siblings: **30 stamped, 12 skipped as ambiguous** (multiple conflicting sibling gids —
  e.g. Crown Care #210689 across B0598EDC/B66E83F1/B816D853, McKesson #7656928941 across 8506F310/8C0EF6E5;
  left for a manual look like the 8 residual conflicts). Only `duplicate_group_id` touched — no change to
  `duplicate_of`/`duplicate_suspect`/`duplicate_ignored`/`entered_in_qb`. Spot-checked (Crown Care #216177 →
  087B6AC1, GCNY Marketing #22679 → 9CD60A7E, Maxi Health #797226 → 3E555A7B). Committed 2026-09-15.
- **D18513E8** (Mehadrin #797482): all 5 rows carry the gid; 4 are `duplicate_ignored` so the default group
  view shows 1 (correct) — show-ignored reveals all 5. The briefing's "2 rows" predated those being ignored.
- Verified `/browse?group=F98B40C0` → 2 rows; `/browse?group=D18513E8` → 1 default / 5 with show-ignored.

### Ignoring a duplicate clears the survivor's "possible duplicate" flag (2026-09-15)

Clicking "ignore this one" set `duplicate_ignored=TRUE` on that record only, never clearing
`duplicate_suspect` on the kept sibling — so the survivor kept showing the dup? pill / "Possible duplicate —
recorded" outcome forever after the decision was made (confirmed live: Prodermix F98B40C0, 1694 ignored but
1695 still `duplicate_suspect=TRUE`). Fix in `web_db.set_duplicate_ignored()` (ignored=True branch): the
UPDATE now `RETURNING duplicate_group_id`, and any **non-ignored** sibling in that same group has its
`duplicate_suspect` cleared (group tag left intact for the Group link). Docstring notes the side effect.
Only touches `duplicate_suspect`/`duplicate_ignored` bookkeeping — CLI pipeline untouched.
- **One-time cleanup** of records already stuck: `UPDATE {invoices,payments} keep SET duplicate_suspect=FALSE
  FROM … ign WHERE ign.duplicate_group_id=keep.duplicate_group_id AND ign.duplicate_ignored AND NOT
  keep.duplicate_ignored AND keep.duplicate_suspect` → **11 invoices, 0 payments**, committed 2026-09-15.
- **Verified**: F98B40C0 now renders 1694 as "Duplicate — ignored" and 1695 with no dup flag on /browse + /qb
  (default shows only 1695); self-reverting test confirms a fresh ignore clears the sibling's suspect flag and
  keeps its group id. web_db compiles.

### "Not received" markup evidence + Ignore/Undo on /qb and /payments (2026-09-16)

**Part 1 — markup evidence in the revision reason.** Invoice JSON schema gains `not_received_note`
(describe checkmark/strikeout/handwritten markings that items were not-received/short-shipped/back-ordered)
and `not_received_amount` (their dollar impact if determinable), both nullable. `apply_duplicate_checks`
carries them onto each invoice's `history` entry. `check_duplicate`'s "possible" branch, when either copy
carries a note, appends it to the reason and — if `not_received_amount` is set — checks it against the
amount delta (`abs(delta - note_amount) <= 0.02` → "matches the $X difference … consistent with the lower
amount being corrected"; else "does NOT cleanly match … double-check"). **Guidance only** — an amount
mismatch still returns `"possible"` and needs a human click; no auto-resolution. The note reaches the
reviewer via the already-persisted `duplicate_reason` (shown on the record Details page); note fields
themselves aren't persisted to `invoice_history` (prior-copy markup carries within-run; the current copy's
always applies). Verified (unit test): matching-delta, non-matching, note-without-amount, markup-on-prior,
and no-markup (unchanged generic reason).

**Part 2 — inline Ignore/Undo on /qb and /payments.** These pages showed only the dup? pill; now they
carry the same inline ignore/undo forms as /browse (`POST /records/{record_type}/{record_id}/duplicate`,
which already redirects back via `referer`). `list_qb_ready` selects `duplicate_reason, duplicate_of` in
both UNION arms (credit_memo arm → NULL, NULL) + the outer list; `list_payments` adds `duplicate_reason`.
`qb.html`'s `qb_cell` "Possible Duplicate" branch and `payments.html`'s dup cell render the pill + tooltip
(`duplicate_reason`) + ignore/undo forms, **gated on `record_type in ['invoice','payment']`** so a
credit_memo row (never duplicate-checked, and not accepted by the route) shows only the pill. Verified:
web_db compiles, both templates parse, the queries return the new columns (no SQL error on the credit_memo
NULLs), and a render test confirms invoice/payment get ignore+undo, credit_memo gets neither, tooltip
carries the reason. /browse + /duplicates unchanged; CLI pipeline untouched.

### Dedupe manual-upload attachments by content hash (2026-09-16)

Reprocessing a review item with reviewer-uploaded manual attachments appended EVERY uploaded file onto the
email's own attachments with no dedupe — so re-attaching images the email already had sent the same bytes to
the model twice. On "Refuah 630 credit memo — 1a087c537844cc74.eml" that was 4 native + the same 4 re-uploaded
= 8 images, blowing past `max_tokens` with an empty response on every retry (`classify_with_retry` resends the
identical request, so it failed forever). Fix in `process_source_email` (the `extra_attachments` merge, right
after zip-expansion): build a set of SHA-256 content hashes of the email's own attachments, and skip any
manual upload whose hash is already present (hash, not filename, so a renamed re-upload is still caught). Two
genuinely different manual uploads are NOT deduped against each other. The existing "found N attachment(s)"
log reads `len(attachments)` after the loop, so it now reports the deduped count. No DB cleanup needed — the 4
`manual_attachments` rows stay unconsumed, so the next retry re-reads them and the dedupe drops all 4.
- **Verified**: compiles; unit test (4 identical re-uploads + 1 new → 5 kept, only the new one added). Confirmed
  against the live item (review id 2567): the 4 native and 4 manual files are byte-identical (SHA-256 match),
  so the next retry collapses 8→4 images. Retry itself is Joel's run (no API key here). CLI run path otherwise
  unchanged — dedupe only affects the manual-upload merge.

### max_tokens failures: raise budget + fix the misleading review message (2026-09-16)

A 4-image itemized credit memo failed all 3 attempts with `stop_reason=max_tokens` (ran out of the 20K output
budget mid-response, not a missing/unreadable attachment) and landed in review saying "enter this one
manually" — which read like the attachments weren't found, so they got re-uploaded by hand (the separate
duplicate-attachment bug).
- **Part 1 — MAX_TOKENS 20000 → 21000.** claude-sonnet-5's true output ceiling is 128K, but this pipeline
  calls the API **non-streaming**, and the anthropic Python SDK hard-raises ("Streaming is required…") once
  `max_tokens` risks the 10-min non-streaming timeout — the practical cap is ~21,333 (128000 × 600/3600). So
  20000 was already near the ceiling; 21000 takes the last safe headroom (~5% more) without a streaming
  rework. **Reaching the real 128K would require switching this call to `client.messages.stream(...)
  .get_final_message()`** — a deliberate non-goal; flagged to Joel as the actual fix for genuinely huge docs.
  Cost/latency note: a higher cap doesn't force longer responses (you pay only for tokens generated), so the
  bump has no cost impact on normal emails; it only lets an unusually large itemized response finish.
- **Part 2 — accurate wording for the max_tokens case only.** `build_error_review_row` now, when
  `"max_tokens" in result["error"]`, uses a message explaining the response got too long to finish (large/
  detailed doc, NOT a missing/corrupt attachment), says **do not re-attach the same file**, and to retry
  as-is (`--retry-id`) or put details in Answer. Every OTHER failure (API errors, genuinely corrupt files)
  keeps the existing "enter this one manually" message. Verified (unit test): both branches + MAX_TOKENS
  value; compiles. Only the failure path changed — normal classification untouched.

### Fix /duplicates 500 — nav var name collision (2026-09-16)

The nav rename refactor put a `{% set groups = [...] %}` nav list in `base.html`. Because that set runs
during every page render and leaks into the surrounding context, it overwrote the `groups` context var
`duplicates_page()` passed to `duplicates.html` (the duplicate-pairs list) — so the page looped over nav
entries, hit `g.flagged` (Undefined), and Jinja threw `UndefinedError` → FastAPI 500. Only `/duplicates`
collided (its context key happened to match). Fix: renamed the duplicates context key `groups` → `dup_groups`
(`review_app.duplicates_page` + both refs in `duplicates.html`), AND renamed base.html's nav list `groups` →
`nav_groups` to kill the whole collision class for any future page. Verified: review_app compiles; a full
end-to-end render of duplicates.html (extends base.html) works for both a populated group and the empty case
with no UndefinedError; the "Excluded Vendors" nav label still renders; and no template references a bare
`groups` anymore. Rename only — no logic change.

### /duplicates: group by document, exclude excluded-vendor invoices (2026-09-16)

Rework of the Possible-Duplicates working queue:
- **Excluded-vendor INVOICES left out** of duplicate review (they never enter QB, so nothing to protect);
  **payments always included**, excluded-vendor or not. Applied in `list_duplicate_candidates` via the
  pipeline's bidirectional-substring rule (`_excluded_vendor_sql`) on the flagged copy; members share the
  vendor so the whole set is governed consistently.
- **Grouped by document, not per-flagged-row.** Old function returned one {flagged, counterpart} pair per
  flagged row, so the same invoice (e.g. McKesson #246118, 8 copies) appeared many times, scattered.
  New function groups every non-ignored copy sharing `(record_type, normalized vendor, number)` into one
  **set** (a cluster with ≥1 still-flagged copy) — grouping is by vendor+number, NOT `duplicate_group_id`,
  because 26 of 27 qualifying flagged rows have no group id stamped. Returns
  `[{key, record_type, records[], original_email}]`, newest-flagged set first; within a set the non-suspect
  original is first, then flagged copies oldest→newest.
- **Template redesign** (`duplicates.html`): each set renders as a **comparison table** (scoped `.duptable`,
  overriding base.html's sticky-th + zebra) — fields down the side (Store, Amount, Date, Recorded, Received,
  Source, Actions), **one column per copy** — so amounts/dates line up side by side for easy reading
  (replaced the earlier wide two-per-row cards where the right-aligned amount sat far from its label). Each
  copy's Actions cell carries **Ignore this one** (both sides — see below) and, on flagged copies only,
  **Not a dup**.
- **Verified**: web_db compiles; `list_duplicate_candidates` returns 23 sets (20 invoice + 3 payment, 27
  flagged copies, set sizes 2–4) in ~0.1s with **0 excluded McKesson sets**; full page render shows 27
  ignore/dismiss button pairs, no excluded vendor, and the empty state. Consistency note: before this change
  the /duplicates flagged set equalled `/browse?dup=yes` exactly (53 = 53); now /duplicates shows the 27
  non-excluded flagged copies. **Records `dup=yes` now applies the same exclusion** (per Joel): its branch
  in `list_all_records` adds `NOT (record_type='invoice' AND <excluded vendor>)`, so excluded-vendor
  invoices drop from the duplicate view while **payments and all other types are unaffected** (`dup=no` and
  the unfiltered view unchanged). Verified: `/browse?dup=yes` returns 27 (24 invoice + 3 payment), 0 excluded
  McKesson invoices, exactly matching the /duplicates flagged-copy count (27).

### Don't extract persistent account/customer numbers as payment reference_number (2026-09-16)

`check_duplicate_payment` keys on vendor + reference_number, so when a receipt has no real per-transaction
ID and the AI used a persistent Customer/Account # (e.g. BluPax "6077NY0059"), two unrelated payments matched
on that ref and got flagged as possible duplicates forever (amount difference only downgrades to "possible",
date isn't checked on that path). The existing `elif not ref` fallback (vendor + amount + payment_date) would
have handled these correctly had the ref been null.
- **Prompt fix (`SYSTEM_PROMPT_TEMPLATE`)**: the payments-array `reference_number` field (the one used for
  dup matching) now instructs the model to use ONLY a unique per-transaction identifier (check #, confirmation/
  auth code, per-payment txn/receipt ID) and to leave it null when the only number present is a persistent
  customer/account number. The top-level fallback `reference_number` got a shorter matching pointer. Compiles;
  applies on Joel's next run. Existing rows are unaffected until reprocessed (see audit below).
- **Audit (read-only, 2026-09-16, no changes made)**: query for (vendor, reference_number) groups with 2+
  recorded payments sharing a ref but DIFFERENT amounts (the persistent-account-number signature) found
  **5 groups / 16 payment rows**: Garden of Life `Card ending 8400` (6 pmts, $152.83–$5710.09), Micro Merchant
  Systems `ARC_52AFLAM16TUJ` (4), BluPax Pharma `6077NY0059` (2), Garden of Life `Card ending 5913` (2),
  Prodermix `104507` (2).
- **Cleanup run 2026-09-16** (authorized): on those 16 rows, nulled `reference_number` (**16 rows**) so old
  records stop acting as duplicate-flag magnets, and cleared `duplicate_suspect` + `duplicate_reason` on the
  **3** that were currently flagged & not ignored (ids 79, 133, 182). `duplicate_ignored` rows left untouched
  (id 78 stays ignored). Single transaction, verified: all 16 refs NULL, the 3 flags cleared, and
  `/duplicates` no longer shows any of these payments (payment sets there dropped from 3 to 0; total sets
  23 → 20). The vendor+amount+date fallback now governs these payments; the prompt fix prevents recurrence
  on future runs.

### /duplicates: "Ignore this one" on either side of a set (2026-09-16)

The ignore/dismiss buttons were gated on `{% if flagged %}`, so the "Likely original" card had no way to be
picked as the discard except via its Details page. Now **"Ignore this one" shows on every card** in a set;
**"Not a dup"** (clear the suspect flag) still only shows on flagged cards. The flagged card passes
`duplicate_of=original_email`; the original card passes it blank.
- **Backend fix required** (the briefing assumed none, on the premise every set record has a
  `duplicate_group_id` — but ~26 of 27 flagged rows here have none; sets are grouped by vendor+number).
  `set_duplicate_ignored`'s sibling-clear now matches surviving siblings by **shared group id OR shared
  vendor+number** (the actual duplicate identity, and the key /duplicates groups on) — so ignoring EITHER
  side flips the set even with no group id. Verified with a no-group_id pair, both directions: ignoring the
  original marks it ignored and clears the flagged sibling; ignoring the flagged copy leaves the original
  clean — the surviving copy becomes the unflagged kept record either way. web_db compiles; template parses.
- **"Keep this one" — resolve a whole set in one click (2026-09-16)**: `web_db.keep_and_ignore_others(kept_type,
  kept_id, others, username)` clears the kept record's own suspect flag (so a flagged column can be the keeper)
  and marks every `other` (list of (type,id)) as a confirmed duplicate via `set_duplicate_ignored`, linked back
  with `duplicate_of=<kept email>`. Route `POST /records/{type}/{id}/duplicate/keep` reads the `others` form
  field (`type:id` tokens). `duplicates.html` action cell now leads with a green **Keep this one** (only when the
  set has >1 copy), then **Ignore this one**, then **Not a dup** (flagged only). Verified (self-reverting 4-copy
  test): keeping any column — even a flagged one — leaves it clean and marks the other 3 `duplicate_ignored`
  with `duplicate_of`=kept email; render shows 31 Keep buttons across the multi-copy sets; modules compile.
- **Received row (2026-09-16)**: each `dup_card` now shows a **Received** line (`received_at | us_datetime`,
  date+time) after Recorded, keeping three distinct labels — Date (document's own date), Recorded (added to
  DB), Received (Gmail server receive time). Template-only (data already selected via `all_records` m.*).
  Verified rendering on both flagged and likely-original cards.

### Hide possible duplicates from /qb + /payments by default (2026-09-16)

Previously only CONFIRMED duplicates (`duplicate_ignored`) were hidden by default from QB entry; a POSSIBLE
duplicate (`duplicate_suspect=TRUE`, still unresolved on /duplicates) showed normally on /qb and /payments and
could be marked entered with nothing blocking it. New `dup` semantics on both `list_qb_ready` and
`list_payments`: **default (or `dup="no"`) now HIDES possible duplicates** (`duplicate_suspect IS NOT TRUE`);
`dup="yes"` = only flagged (unchanged); `dup="all"` = no suspect filter (the old default, now an explicit
opt-in). Dropdowns on qb.html / payments.html relabeled: **hide flagged (default)** / only flagged / **all
(include flagged)** (the `no` value still works server-side if linked directly). The `/qb` CSV export
(`qb_export`) passes `dup=dup or None`, so the export inherits the new default automatically (possible
duplicates excluded unless the filter is `all`/`yes`). Credit-memo rows (always `duplicate_suspect=FALSE`) are
unaffected. Verified: web_db compiles, templates parse, default+only-flagged == all on both pages (/qb hides
13 possible-dup rows; /payments 0 currently), `no`==default. Additive query/UI only — CLI pipeline untouched.

### /qb overhaul: defaults, store-review gate, layout, reference columns, memo (2026-09-16)

- **Default to "Not yet entered"** (Part 1): `qb_list`/`qb_export` signatures default `unentered="1"` and use
  `unentered != "0"`; the template's **All** toggle links `unentered=0` explicitly.
- **Store-review safety net** (Part 2): `classify_emails.apply_store_review_gate(rec, is_payment=False)` —
  called right after `apply_card_store_map` in all three loops — forces `needs_human_review=True` + a review
  note when `store_matched` is null/blank/"unknown", so an unidentified store can't reach QB silently.
  **Excluded-vendor invoices/credit memos are skipped** (they never reach QB via skip_qb_import, so an
  unknown store is harmless — avoids pointless review churn, e.g. Parmed); **payments are ALWAYS gated**
  (all vendors enter QB as payments) via `is_payment=True` at the payment call site. Flips only False→True.
  Verified (8-case unit test): excluded invoice unknown-store → not flagged; non-excluded invoice → flagged;
  payment always flagged regardless of vendor; real store → never flagged.
- **Reference columns** (Part 5): split `QB_EXPORT_COLUMNS` into `QB_DATA_COLUMNS` (Vendor…Memo — the
  reorderable/exported set) and `QB_REFERENCE_COLUMNS` (Possible Duplicate, File Link, Gmail Link — always
  shown at the row end, but **excluded from CSV export / Copy** unless opted in via `qb_export_include_refs`).
  New `_sanitize_qb_refs`; `qb_list` passes `reference_columns`/`export_include_refs`/`copy_columns`;
  `qb_columns` saves the opt-in (and reset clears it); `qb_export` appends opted-in refs. Copy-rows JS uses
  `copy_columns`. `_sanitize_qb_columns` now validates against `QB_DATA_COLUMNS`.
- **Ribbon reorg** (Part 6): three groups matching /duplicates polish — (1) store **pill tabs** (All stores +
  each store, active highlighted) + entered toggle; (2) collapsible **Filters ▾** disclosure holding
  Run/Type/dup/Vendor/Search/Date/Amount/show-ignored (same GET params, auto-open when any is active); (3)
  right-aligned actions (Export CSV, Copy, Columns ⚙). Store is now **remembered** per user
  (`qb_last_store`): an explicit `store` in the URL (incl. ""=All) is saved; a bare `/qb` visit restores the
  last used store. Store pills carry all other filters via `pillq`.
- **Removed "Standard QB order" button** (Part 4) + updated the Columns-panel help text (backend
  `layout=standard` param left intact, just unused).
- **Memo format** (Part 7): `_qb_memo` now emits the doc link unlabeled first, email link prefixed `E:`
  (e.g. `http://h/l/AAA | E: http://h/l/BBB`); dropped `QB_MEMO_LABELS`.
- **Verified**: classify_emails + review_app compile; qb.html renders (store pills, Filters disclosure, ref
  headers + panel opt-in, copy_columns = data-only by default, no Standard-QB-order); unit tests for the
  store gate (all unknown/blank variants flip review, real store doesn't, appends to existing reason), memo
  format, and both sanitizers. Additive/UI except the classify_emails safety net (new records going forward).
- **Part 3 audit (read-only, no writes — awaiting go-ahead)**: with the excluded-vendor rule confirmed
  (skip excluded invoices/credit memos; always gate payments), the existing-row cleanup targets are:
  **(a) auto-fix 1** — payment id 50 (card 8504 → Maple Pharmacy, deterministic); **(b) to review 7** —
  invoices 330/331 (NJNG utility) + payments 9/18 (Twin Rocks Water), 60/170/171 (JCP&L). The 22
  excluded-vendor invoices (21 Parmed + 1 Auburn) are intentionally left store-unknown (QB-excluded, harmless).
  Mechanism for (b) TBD with Joel (direct /edit vs. review_queue), since surfacing already-recorded rows on
  /review needs the reprocess path (API) not available in-session.
- **Resolution (2026-09-16)**: the 7 group-(b) rows are all missing their source files (unresolvable), so per
  Joel they were **marked ignored** — invoices 330/331 + payments 9/18/60/170/171 set `duplicate_ignored=TRUE`
  with reason "Store unresolved & source file missing — ignored for now", `ignored_by=js` + edit_log (direct
  write, no duplicate-sibling side effects). Verified none appear on /qb or /payments; still on Records for
  audit; reversible via undo/restore. Group-(a) payment id 50 (card 8504 → Maple) NOT yet applied — left for
  Joel's call.

### Short links durable across a future host/domain change (2026-09-16)

`/l/{code}` codes are stable and the host PREFIX is already computed fresh per view (`LINK_BASE_URL` env or
request base_url). The gap was the stored **destination**: `file`/`records` targets were host-absolutized at
mint time (`_abs(file_preview_url(...))`), so a code minted on `localhost`/LAN redirected to that baked-in
host on its second hop even after the domain moved.
- **Part 1 — mint relative** (`review_app.py`): `qb_list` + `qb_export` now register the RAW app-relative
  target (`file_preview_url(...)` → `/invoice_files/…` etc.; `/browse?email=…`) instead of `_abs(...)`. A
  Drive/http file url stays absolute (durable already), `email` (Gmail) untouched. `_qb_cell_value`'s
  File Link/Memo lookups were switched to the same raw key so the CSV keeps emitting short links (falls back
  to an absolute url only when a target somehow isn't shortened).
- **Part 2 — resolve host at click time**: the `/l/{code}` handler now absolutizes a `startswith("/")`
  target against `web_db.LINK_BASE_URL or request.base_url` at click time, so one stored code works from any
  host. Absolute targets (Drive) pass through unchanged. No `LINK_BASE_URL` value needed for correctness —
  it's a separate config-only switch when a domain is ready.
- **Verified**: review_app compiles; short_links_for stores target as-given (relative stays relative);
  existing absolute rows still redirect fine pre-migration (handler passes non-`/` through).
- **Part 3 — superseded by click-time normalization (2026-09-16)**: the planned "strip host → relative"
  UPDATE (2140 rows) is **not viable** — `links.target_url` is UNIQUE and the data has cross-host duplicates
  (same target under localhost AND 192.168.5.60) plus 770 targets that already have a relative row (the app
  was restarted, so Part 1 has been minting relative rows since). Blindly stripping would collide (639
  groups / 1768 rows) and require **deleting ~1129 codes** — orphaning short links already pasted into QB
  memos. **Non-destructive fix instead**: the `/l/{code}` handler now normalizes ANY app-owned target
  (relative OR absolutized under any old host — `/{review_pending,payment_files,invoice_files,browse}…`) to
  the current host at click time, via one regex; external Drive/Gmail targets untouched. Achieves the same
  durability with zero writes and no orphaned codes. Verified (8-case unit test): relative + both old hosts
  → current host; Drive + Gmail left as-is.
- **Links table cleanup DONE (2026-09-16)**: Joel confirmed no codes are pasted into QuickBooks yet, so the
  dedup was safe to run. One verified transaction: deleted 1730 absolute rows that had a relative twin + 169
  duplicate absolutes, rewrote the remaining 241 to relative. Result: **0** app-owned absolute rows, **0**
  duplicate target_urls; 865 Gmail + 842 Drive rows untouched (4617 → 2718 total). Every app-owned target is
  now a single relative row, so `short_links_for` reuses one stable code per target going forward, and the
  click-time normalization keeps them host-durable.

### Store display labels + canonical ordering everywhere (2026-09-16)

DISPLAY-ONLY labels: canonical `store_matched` / `stores.store_name` values (used by AI extraction, matching,
card/sender/contact maps, class rules) are **unchanged**; a separate label is shown to humans.
- **Schema/UI** (A): `stores.display_name TEXT` (WEB_DDL ALTER); added to `_REF["stores"]["cols"]` +
  `REFERENCE_PAGES["stores"]` so Setup → Stores edits it. Data set (7 rows, spellings confirmed to match):
  Maple Pharmacy→sort 1 "Maple (NY)", Madison→2 "Madison", Refuah→3 "Refuah", Westgate→4 "Westgate",
  3M Pharmacy→5 "3M (Maple NJ)", BMW→6 "BMW", Lakewood Rx→7 "Lkwd Rx".
- **Helper** (B): `web_db.store_label(value)` / `store_display_map()` (30s TTL cache; falls back to the
  canonical value for blank/legacy) + registered as a Jinja filter `store_label`.
- **Applied** (C): `| store_label` on every store *render* — row cells (browse/history/ignored/payments/qb/
  review_list/review_detail/sales_orders/record_detail/duplicates), the cards assigned-store text, both
  duplicates confirm() dialogs, and the copy/export data-store attrs on qb.html + payments.html (they feed
  Copy). CSV Store column via `_qb_cell_value`. **Filter VALUES stay canonical** — only visible option/pill
  text is labeled.
- **Filter lists** (D): /qb, /payments, /browse+/history store filters now source `store_names()` (canonical,
  sort-ordered) instead of `distinct_values("store_matched")` — fixes ordering and drops "unknown".
- **Edit page** (E): the store field is now a canonical dropdown (labels shown, value canonical; legacy value
  preserved as "(not in list)"); `edit_form` passes `stores=store_names()`.
- **Verified**: web_db + review_app compile; all 12 touched templates parse; schema live; data committed;
  `store_label` resolves ("Maple Pharmacy"→"Maple (NY)", legacy→itself); rendered /qb shows labels in order
  with canonical `store=` values in pill hrefs. Additive/UI — classify_emails and historical data untouched.

### /qb + /payments: pagination, lighter rows, Copy Selected (2026-09-17)

Fixes the client-side weight of rendering all ~738 rows at once (server was already fast: query 0.07s +
short-links 0.05s + render 0.12s; the 1.3 MB DOM was the cost — not the link migration, which left the
table clean with 0 dead tuples).
- **Copy Selected** (Part 1): renamed from "Copy visible rows", moved into the bulk-action bar next to
  "Mark selected as Entered in QB", and now copies the **ticked rows** (`input.rowchk:checked`) — the same
  set the bulk-entered action uses — instead of whatever's on screen. Empty-selection guard added. Same on
  /payments (`copyPay`).
- **Pagination** (Part 2): `_paginate()` slices the full filtered set to one page (default **150**, options
  150/500/**All** escape hatch); page-size choice persists per user (`qb_page_size` / `payments_page_size`),
  page number does not. Filters/sort/store carry across pages. **Export CSV stays un-paginated** (full
  filtered set); **select-all + Copy Selected + bulk-entered are page-scoped** (operate on the DOM's current
  page). Pagination bar (Prev/Next, page x/y, Rows/page selector) on both pages.
- **Lighter rows** (Part 3): the ~150 per-row "mark entered" `<form>`s replaced by one plain button
  (`.entered-btn` + data-rt/rid/entered) and a single delegated click handler on the table body that POSTs
  the same endpoint (server redirects back) — identical behavior, far fewer DOM nodes.
- **Verified**: review_app compiles, both templates parse; `_paginate` unit test (default/·all·/500/clamp);
  rendered /qb page 1 = **222 KB vs 1336 KB** before (150 of 738 rows), **0 per-row entered forms**, Copy
  Selected in the bulk bar, pagination bar present; /payments same. Export routes confirmed to NOT paginate.

### Missing-date review gate + date picker + M/D/YY in CSV/copy (2026-09-17)

- **Date-review gate** (Part 1): `classify_emails.apply_date_review_gate(rec, date_field)` — called after
  `apply_store_review_gate` in all three loops (invoices→`invoice_date`, credit_memos→`credit_memo_date`,
  payments→`payment_date`) — forces `needs_human_review` + a note when the record's date is null/blank, so a
  dateless record can't reach QB silently. Flips only False→True. (Unlike the store gate it does NOT skip
  excluded vendors — per briefing.) Verified: null/blank/spaces flag; a real date doesn't; appends to any
  existing reason.
- **Edit page date picker** (Part 3): `edit.html` special-cases the `date` field to `<input type="date">`
  (record[col] already renders YYYY-MM-DD, exactly what the picker's value needs) — same pattern as the
  store dropdown.
- **M/D/YY in CSV/Copy** (Part 4): the two spots that built raw date values now go through `us_date`:
  `_qb_cell_value`'s Date case + qb.html `data-date` (feeds Copy Selected), and `payments_export`'s
  payment_date + payments.html `data-date`. Every other date render already used `us_date`. `date_from`/
  `date_to` filter inputs left raw (type=date needs ISO). Verified: `us_date('2026-08-27')` → `8/27/26`.
- **Part 2 audit (read-only, no writes — awaiting go-ahead)**: null-date & not-already-review rows =
  invoices **23** (10 excluded-vendor/never-reach-QB, 3 already `duplicate_ignored`, **10 genuinely need a
  date**: SRFax×3, Zahler×2, NJNG×3, Herbal Health×2), credit_memos **0**, payments **12** (4 already
  ignored, **8 genuinely need a date**). So ~**18 rows** would benefit from a human date; cleanup mechanism
  (same as store cleanup) pending Joel's confirmation.
- **Part 2 resolution (2026-09-18)**: Joel chose "edit them". Re-verified with a single excluded-list load
  (avoids the per-call `is_excluded_vendor` DB-reopen that times out): 10 invoices (184, 377, 470, 472, 864,
  1088, 1089, 1291, 1409, 1725) + 8 payments (63, 64, 110, 111, 113, 114, 193, 194) genuinely need a date;
  The IQ Diet (1116, 1833) confirmed excluded alongside the 8 Parmed rows. Handed Joel the `/edit/...` links
  to set each date himself via the new date picker (no DB writes from Claude — dates aren't ours to invent).
  Saving a date clears the gate automatically going forward.

### BillerGenie link-fetch: follow "View Invoice" to the real PDF (2026-09-18)

- **Bug**: `render_url_to_pdf()` stopped at the BillerGenie *summary* page (a thin balance/"Pay By Card"
  widget) and screenshot-rendered that — missing vendor address + line items, which live one click deeper.
- **Fix (Part 1)**: after load + networkidle + blank-check, look for `a[href*="ExportToPdf" i]` (fallback
  `a:has-text("View Invoice")`); if found, resolve the href absolute and fetch it via `page.request.get`
  (reusing the page's session), and if the response is `application/pdf` save the raw bytes directly (a real
  document, not a print-render). If absent/not-PDF, fall back to exactly today's `page.pdf()` — no regression.
  Selector verified against 3 live Herbal Health invoices; live fetch of #2793 produced a 106 KB PDF with
  full address + all 8 line items + totals (vs. the old widget). Same trusted domain — no new-domain trust
  decision; Drive-allowlist idea explicitly shelved (no confirmed need).
- **Part 2 audit (read-only)**: replayed the fetch-path gate over all recorded invoices/credit_memos →
  **5 records, all Herbal Health USA** (inv 492/2740, 864/2773, 1363/2750, 1423/2740-dup, 1653/2793;
  0 credit_memos; 78 records' source .eml not on disk, uninspectable). All captured only the widget;
  id 1653 IS the Drive PDF Joel hand-confirmed as the thin widget. vendor/inv#/store/amount all landed
  correctly (on the widget too); dates on 3/5 (864 missing → already on the edit-links list; 1423 missing
  but it's an ignored duplicate); line items missing on all 5 but unused by QB entry; none entered in QB.
  Recommendation given: leave history as-is (fix only 864's date) vs. optional reprocess-for-real-PDFs of the
  4 live records (needs Joel's API key) — awaiting Joel's choice; no writes.

### /qb: blank Account cell links straight to the fix in Setup (2026-09-18)

- A blank Account on /qb means either (a) vendor standardized but no Vendor→account row yet
  (`standard_vendor` set, `standard_account`/`qb_account` blank), or (b) vendor not standardized at all
  (`standard_vendor` null). The row didn't carry `standard_vendor`, so the template couldn't tell them apart.
- **Part 1** (`web_db.list_qb_ready`): added `standard_vendor` to all three SELECT spots (outer + both UNION
  arms).
- **Part 2** (`review_app.reference_page` + `reference.html`): new `prefill` query param, threaded into the
  Add form — only the shared `standard_vendor_name` column is prefilled (text input `value` / textarea body /
  `ref_select` pre-select). `ref_select` shows a non-matching value as "(not in list)", so a select still
  carries it.
- **Part 3** (`qb.html` `qb_cell` Account case): when `qb_account` is blank and there's a vendor, render an
  amber pill — case (a) → `/reference/vendor_account_assignment?prefill=<standard_vendor>` ("assign account");
  case (b) → `/reference/vendor_name_map?prefill=<vendor_matched>` ("add vendor"). Invoice/credit-memo only
  (payments don't standardize).
- Verified: `standard_vendor` now returned; case (b) links live-confirmed (Herbal Health USA etc.). Case (a)
  state is real (4 inv + 6 CM have `standard_vendor` w/o account) but its only current rows are `skip_qb`
  store-transfers, so none show in /qb today — the link is correct by construction for when one does. Both
  modules compile, both templates parse. Final loop (save row in Setup → reprocess email clears the blank
  Account) needs a classify run (Joel's API key). Needs web-app restart for the `reference_page` param.

### Setup reference pages: Filter box searches stored values, not DOM text (2026-09-18)

- **Bug**: `#refFilter` on every Setup reference page filtered rows by `tr.innerText`. Where a column is a
  `<select>` (e.g. Vendor → account's Vendor + Account) the current value isn't dependably in `innerText`, so
  the Filter box silently matched nothing — Joel saw "search not working" on Vendor → account. (Native Ctrl+F
  can't see inside a closed `<select>` either; that's an unfixable browser limitation, not a code issue.)
- **Fix** (`templates/reference.html`, shared by all reference pages): each `<tr>` now carries
  `data-search="{{ (searchbits|join(' '))|lower }}"` built server-side from every column's ACTUAL stored value
  (`it[c.name]`), and the Filter oninput checks `tr.dataset.search` instead of `innerText`. One change covers
  Stores, Excluded vendors, Vendor names, Account names, Vendor → account, Sender/Contact/Card → store, Class
  rules.
- Verified by standalone render: Vendor → account "Zahler" row → `data-search="zahler purchase:vitamins"`
  (matches the filter; unrelated rows don't); text-only pages (Excluded vendors, Vendor names) still populate
  and filter — no regression. Templates auto-reload, so no restart needed for this one.

### Auto-recheck unentered records when a vendor mapping is fixed (2026-09-18)

- **Problem**: `apply_vendor_standardization()` runs only at classify time against the mapping tables as
  they were THEN, writing standard_vendor/standard_account/qb_vendor/qb_account permanently. Adding a Vendor
  Names alias or a Vendor → account row later didn't update already-recorded rows (Joel added "Advanced
  Nutrition by Zahler" alias; HIS3433/Maple kept showing "add vendor").
- **Part 1** (`classify_emails.reapply_vendor_standardization_backfill()`, additive, beside
  `apply_vendor_standardization`): for UNENTERED invoices/credit_memos (payments have no standardization
  columns — skipped) where standard_vendor/standard_account/qb_account is NULL, reloads VENDOR_STD fresh and
  re-runs `apply_vendor_standardization()` on a `{vendor_matched, store_matched}` dict; if it NOW resolves
  (qb_account set) writes the four fields back, strips ONLY the two vendor/account review sentences it emits
  (`_strip_vendor_std_reasons` via `_VENDOR_STD_REASON_PATTERNS`; quoted name matched with `[^']*`, so an
  apostrophe name just won't strip — safe), and clears needs_review only if no other reason remains. Preserves
  a fuzzy-match audit note. Every field change logged to `edit_log` (changed_by="auto-resolved by
  vendor/account backfill"). Returns (checked, resolved). Never touches entered_in_qb=TRUE, payments, store
  matching, class rules, dedup, or `apply_vendor_standardization()` itself.
- **Part 2** (`review_app.py`): `_reference_saved_redirect(table)` re-runs the backfill after a successful
  add/edit on `vendor_name_map` / `vendor_account_assignment` and redirects with `?resolved=N`; `reference_page`
  takes a `resolved` param; `reference.html` shows "Rechecked existing records — N resolved." Delete handler
  deliberately NOT hooked.
- **Part 3 one-time catch-up (RAN, committed with Joel's explicit go-ahead 2026-09-18)**: dry-run first
  (read-only) surfaced a far larger scope than the single HIS3433 row implied — **checked=1674, resolved=1042**
  (backlog predating the current mapping tables). Reported the number before writing per Joel's request; he
  said yes. Committed: 1042 rows resolved, 5143 edit_log entries. HIS3433 (inv 66) now
  qb_vendor=Zahler/qb_account=Purchase:Vitamins (fully resolved, no /qb link). 632 rows remain unresolved
  (vendors genuinely not yet mapped). Second run resolves 0 → confirms the Part 2 trigger's per-save cost is
  just the shrinking unresolved remainder. Needs web-app restart to pick up the Part 2 route/handler changes.

### Vendor Setup diagnosis + cross-table vendor lookup box (2026-09-18)

- **1a**: A Vendor → account row for Zahler DOES exist (id 477, Zahler → Purchase:Vitamins), so searching
  "Zahler" there is not a search bug. The confusion was searching the ALIAS ("Advanced Nutrition by Zahler")
  on Vendor → account — that table only holds the standard name "Zahler", so the alias correctly shows 0.
- **1b**: Backfill catch-up already ran + committed earlier today (checked=1674, resolved=1042). HIS3433
  (inv 66) is fully resolved (standard_vendor=Zahler, qb_account=Purchase:Vitamins) — shows NO /qb link at all.
- **1c — heaviness ROOT CAUSE CONFIRMED**: `ref_select()` renders the FULL option list inside every row's
  `<select>`. Measured rendered HTML: vendor_name_map 1.35 MB / 0 options (all text — fine),
  account_name_map 0.52 MB / 0 options (fine), **vendor_account_assignment 34.64 MB / 575,823 `<option>`
  elements** (629 rows × 2 selects × (660 vendor + 252 account options)). Only the one page with select columns
  is pathological. **FIXED (datalist swap, per Joel's decision 2026-09-18)**: `vendor_account_assignment` page
  def gets `"use_datalist": True`; reference.html renders each large option list ONCE as a shared `<datalist>`
  (`id="opt-<options_from>"`) and every row's vendor/account cell is a text `<input list=...>` (new
  `ref_datalist_input` macro) instead of a per-row `<select>`. Free-text "(not in list)" tolerance is native to
  a text input. Scoped to this page only — other select pages (sender/card store maps) still use `ref_select`.
  Measured after: **34.64 MB → 0.88 MB**, **575,823 → 912 `<option>`** (two datalists, 660 + 252), 0 `<select>`,
  stored values preserved.
- **Part 2 — cross-table lookup (built, read-only)**: `web_db.vendor_lookup(term)` case-insensitively matches
  across vendor_name_map (standard name / aliases / per-store name_*), vendor_account_assignment (standard
  vendor name), and account_name_map (standard account name / per-store name_*); returns per-vendor cards
  (aliases, per-store names, in_name_map flag, assigned_account-or-None) plus account hits. Shared partial
  `templates/_vendor_lookup.html` included at the top of the three vendor Setup pages (guarded by
  `_VENDOR_LOOKUP_TABLES` / `show_vendor_lookup`); GET `?vlookup=` param on `reference_page`. Each card shows
  the assigned account (green) or a "none assigned" + prefilled "assign account" link (reusing the prefill
  mechanism). No-match → plain "No vendor/account data matches '<term>'." Verified: "Zahler" → 1 card w/
  aliases + Purchase:Vitamins; alias "Advanced Nutrition" → same card; "Purchase:Vitamins" → account card;
  unassigned vendor → assign-account link; box hidden on non-vendor pages. Needs web-app restart (route change).

### /qb: "Needs action" / "Complete" filter toggle (2026-09-18)

- 3-state toggle (All | Needs action | Complete) in `.qbrow` between Filters ▾ and Export CSV, so Joel can
  bulk-enter all COMPLETE rows first, then work the incomplete ones separately.
- **Backend** (`web_db.list_qb_ready`): new `needs_action=None` param → `q.qb_account IS NULL` ("yes") /
  `IS NOT NULL` ("no") / no filter (None). Same condition that decides the add-vendor/assign-account pill;
  qb_account already in the SELECT, no schema change.
- **Routes** (`qb_list`, `qb_export`): `needs_action: str = ""` threaded to `list_qb_ready` and (qb_list) into
  the template context + `params` (so sort links keep it); CSV export respects the same filter as screen.
- **Template** (`qb.html`): `na_extra` appended onto store-pill (pillq), both entered-toggle (baseq), and
  Export CSV (exportq) links; hidden `needs_action` input in the Filters form; toggle links built from the
  existing `exportq` (which excludes needs_action) so no duplicate param. Kept OUT of base/pb by design.
- Verified: All 779 = Needs action 149 (all qb_account NULL) + Complete 630 (all set); `needs_action=None`
  reproduces today's behavior; rendered links carry needs_action with 0 duplicates; export link respects it.
  Payments untouched (qb_account is invoices/credit_memos only). Template auto-reloads but the route param
  needs the pending restart to function (degrades gracefully meanwhile — no StrictUndefined).

### /qb: "which action" sub-filter (2026-09-23)

- A second row directly below the Needs action / Complete toggle, splitting "Needs action" by WHICH fix the
  Account-cell pill asks for: **Any action | Add vendor needed | Assign account needed | No vendor/account
  issue**. Uses the EXACT conditions qb.html's pill uses, so the filter and the pill can never disagree.
- **Backend** (`web_db.list_qb_ready`): new `action=None` param → outer clauses (all treat blank as `COALESCE(...,'')=''`,
  matching the template's `not` truthiness):
  - `add_vendor` = `qb_account`='' AND `standard_vendor`='' AND `vendor_matched`<>'' (the "add vendor ↗" pill)
  - `assign_account` = `qb_account`='' AND `standard_vendor`<>'' (the "assign account ↗" pill)
  - `none` = `qb_account`<>'' (populated — nothing but the entry checkbox)
  ANDs with `needs_action` + store/date/amount/etc.; no schema change (qb_account/standard_vendor already selected).
- **Routes** (`qb_list`, `qb_export`): `action: str = ""` threaded to `list_qb_ready`, the template context, and
  `params` (sort links keep it); CSV export applies the same filter as screen.
- **Template** (`qb.html`): new `act_extra` (mirrors `na_extra`) appended onto store-pill, entered-toggle,
  needs_action-toggle, Export CSV, and pagination links; hidden `action` input in the Filters form; the new
  row's own links set `action=` and carry `exportq` + `na_extra` (kept OUT of base/pb, so no duplicate param).
  Also appended `na_extra`+`act_extra` to pagination `pageq` (fixes a pre-existing gap where paging dropped
  the needs_action toggle).
- Verified against the live DB: needs_action=yes (76 blank-account rows) = add_vendor 70 + assign_account 6
  (clean partition); needs_action=yes+action=none = 0 (correct contradiction); needs_action=no+action=none =
  705. Modules compile, qb.html Jinja-parses.
- **Grouped UI + conflict reconciliation (2026-09-23, after live restart):** the two toggles were regrouped
  into one bordered `.qbactionbox` with captioned tiers — **Status** (blue) and **Which fix** (amber, echoing
  the add-vendor/assign-account pills). Because both tiers describe the SAME dimension (qb_account state),
  some combos are always empty (Complete × Add vendor / Assign account; Needs action × No issue) — Joel hit
  Complete × Add vendor and saw 0 rows. Fixed so each tier's links carry the OTHER tier's value ONLY when
  compatible (`na_for_blank`/`na_for_filled`/`act_for_needs`/`act_for_complete`): clicking a value CLEARS a
  conflicting selection in the other tier, preserves compatible ones. No guaranteed-empty state can persist;
  verified every exit from the stuck combo lands on a populated view.

### Prompt caching on the classify API call (2026-09-22)

- **Why**: Anthropic flagged this account's (the pipeline's own API key, not Claude Code) prompt-cache hit
  rate as low. The system prompt (`get_system_prompt()` + learnings_text) is byte-identical across every email
  in a run (~6,064 tokens before learnings, well over the 1,024-token cache minimum), so it's an ideal cache
  breakpoint.
- **Change** (single call site — `classify_email()` ~line 2058 is the ONLY `client.messages.create()`;
  `_classify_oversized_scan` → `classify_with_retry` → `classify_email`): `system=system` (plain str) →
  `system=[{"type":"text","text":system,"cache_control":{"type":"ephemeral"}}]`. anthropic SDK 0.116.0 accepts
  the dict form. `get_system_prompt()` output unchanged; content/output parsing unaffected (caching only
  changes usage/billing fields, not `response.content`).
- **TTL = default 5-minute ephemeral**: a cache READ refreshes the TTL, so a back-to-back run stays warm as
  long as no gap between calls exceeds 5 min (one email classify, even with a PDF render, is well under that).
  Cheaper write (1.25×) than 1h (2×). Fall back to 1h (`"ttl":"1h"`) only if telemetry shows cache_creation
  recurring mid-run.
- **Telemetry added**: one print per classify — `cache: <written> written, <read> read (input=…, output=…)` —
  so the caching is verifiable each run. Expected: first email of a run shows `N written, 0 read`; later emails
  `0 written, N read`.
- **VERIFIED LIVE (Joel's run 20260922T121243)**: first API call `cache: 11974 written, 0 read` (cache
  created); a later call in the same run `cache: 0 written, 11974 read` (cache hit at ~10% cost) → ~90% saving
  on the ~11,974-token system block per subsequent email. 5-min TTL stayed warm across the run (no mid-run
  re-writes). Records saved normally — output/parsing unaffected. (Live figure 11,974 > the 6,064 static
  estimate because it includes store/vendor/card substitutions + the 23 learned rules.)
- **Unrelated observation from that run**: `INV0800123 - EuroPharma Invoice` failed with
  "Failed to read file: 'multipart/mixed'" — a pre-existing email-parsing gap (failed BEFORE any API call, no
  cache line), NOT caused by this change. Left for Joel to decide whether to chase.

### Fix: nested-multipart attachments silently failed the whole email (2026-09-22)

- **Bug**: `_read_eml`'s attachment loop called `part.get_content()` on whatever `msg.iter_attachments()`
  yielded. When a sender nests the invoice as `multipart/mixed → [multipart/alternative body,
  multipart/mixed → the PDF]`, iter_attachments() yields the inner **multipart/mixed container** itself;
  `get_content()` on a multipart raises `KeyError(content_type)` → the whole email failed
  ("Failed to read file: 'multipart/mixed'") and the real invoice PDF inside was silently dropped. Hit on
  `INV0800123 - EuroPharma Invoice` in the 20260922 run.
- **Fix** (`classify_emails._read_eml`): new `_iter_leaf_attachments(part)` recurses into any multipart an
  email nests as an attachment and yields only LEAF (non-multipart) parts, so the actual document is reached;
  the loop now iterates it instead of `msg.iter_attachments()`. Also wrapped each `get_content()` in try/except
  so one unreadable part is skipped with a note instead of failing the whole email (prevents future silent
  drops). Additive; `.msg` path unchanged.
- **Verified**: EuroPharma now reads its 505 KB PDF (application/octet-stream → application/pdf by extension),
  body + html intact. Regression: 200 random processed .eml read 0 errors / 195 attachments intact. Corpus scan
  found only 3 emails with this nested-multipart shape (EuroPharma, a Frier & Levitt fwd already processed, a
  DSN bounce) — all now read cleanly. EuroPharma is still in `test_emails/`, so the next normal run will classify
  it automatically (no manual step).

### Statement reconciliation matches through Vendor Names aliases (2026-09-22)

- **Gap**: `database.reconcile_statement_lines()` matched a statement line to a recorded invoice by LITERAL
  vendor text (`upper(btrim(vendor)) = ...`), never consulting vendor_name_map/VENDOR_STD. So an invoice
  recorded under a different spelling than the statement lists → false "missing" (Crown Care Management
  statement #224718 vs recorded "Crown Care").
- **Fix — additive second pass** (bulk fast-path SQL untouched): after the bulk match + excluded-vendor
  interception, re-check only the still-open `missing`/`amount_mismatch` lines using classify_emails' OWN
  standardization. New `classify_emails.resolve_standard_vendor(raw, std)` (exact-then-fuzzy via
  `_norm_vendor_std_key` + `_best_fuzzy_vendor_match`, same primitives as `apply_vendor_standardization`) is
  the single source of truth. A line flips to `matched` (or `amount_mismatch`) iff BOTH its vendor AND a
  candidate invoice sharing the invoice_number resolve to the SAME standard vendor; amount within 1¢ decides
  matched vs mismatch; sets recorded_amount. Only widens matching — pass-1 raw-text matches untouched. No-op
  when VENDOR_STD unseeded. Skips dismissed/excluded/in_flight/pre_cutoff.
- **Deadlock avoided**: the second pass loads tables via database.py's OWN `load_vendor_standardization()` +
  `ce._norm_vendor_std_key` re-key — NOT `ce.load_vendor_standardization()`, which routes through
  `_database().init_db()` whose ALTERs would block on reconcile's own open transaction (hung >120s until fixed).
- **Save hook** (`_reference_saved_redirect`): after the QB backfill on a vendor_name_map/vendor_account
  save, also runs reconcile and reports NEWLY-cleared exceptions as `flagged_before − flagged_after`
  (`database.count_flagged_statement_lines()`), not the recompute's raw alias_resolved (which re-rescues every
  run). Flash: "Rechecked existing records — N QB records resolved, M statement exceptions resolved"
  (pluralized; suppressed when both 0).
- **Verified on Joel's example**: Crown Care alias already existed ("Crown Care" ← "Crown Care Management").
  Reconcile flipped statement lines 1026 (#224718 $106.63) and 1030 (#224656 $53.31), Refuah, from `missing`
  → `matched` with recorded_amount — no invoice record touched. Save-hook math: flagged 4 → 2, stmt_resolved=2.

### /browse Group link now surfaces review-queue rows too (2026-09-22)

- **Bug**: `/browse?group=<id>` filtered `list_all_records()` on `duplicate_group_id = %s` alone. The
  `all_records` view hardcodes `NULL::text` for that column on the review + credit_memo arms (review_queue has
  no group id), so a Group link could only ever show invoice/payment rows — never the review-queue entries for
  the same document. Joel's case: invoice 52099183 / Garden of Life had 1 recorded invoice + 3 review rows, but
  the Group link showed only the 1.
- **Fix** (`web_db.list_all_records`): `group` handling moved inside the connection block — resolves the id to
  its cluster's vendor+number (`SELECT vendor_matched, number FROM all_records WHERE duplicate_group_id=%s
  LIMIT 1`), then widens the clause to `(duplicate_group_id = %s OR (lower(vendor_matched)=lower(%s) AND
  number = %s))`. Same clustering as `list_duplicate_candidates()` (vendor+number, not the sparsely-stamped
  group id). Widening only — every row that matched before still matches. Unknown/old id degrades to today's
  exact-only match. Not store-scoped (a store mismatch on same vendor+number is itself worth seeing).
  duplicate_group_id stamping is untouched — only how the Group VIEW resolves related records.
- **Verified**: `group=1AB59F25` → all 4 rows (3 review 9/14+9/15+9/18 + the 8/27 invoice), matching the manual
  invoice-number search. Bad id → 0 rows, no error. Composes with other filters (record_type/vendor) with
  correct param alignment. Web_db change → needs the pending web-app restart.

### Flagged records left with no source document — recovery + render robustness (2026-09-22)

- **Pattern**: a flagged record (e.g. a possible-duplicate copy of an order confirmation) whose
  `_write_record_document` produced nothing → "No source file recorded" for the reviewer. Cause:
  `_match_attachment` returns None for `source_document=email_body` (by design), then the email-body
  render (`render_html_to_pdf`→`render_text_to_pdf`) also failed.
- **Part 2 finding (the real cause)**: both of Joel's source emails RENDER FINE now (Edge + LibreOffice
  present; #2632 html→10 MB, #2629 text→12 KB), so the run-time failure was TRANSIENT — `render_html_to_pdf`
  used `page.goto(wait_until="load")`, which on forwarded/marketing HTML blocks on EVERY remote image +
  tracking pixel and can hit the 30s timeout, then bailed producing nothing. **Fix**: catch the goto timeout
  and still `page.pdf()` the partially-loaded page — a partial render beats no document. Also `_log_no_source_file`
  persists every no-source-file event (+ recoveries) to `output/no_source_file.log` so it's diagnosable
  after the fact instead of a console print that scrolls away.
- **Part 1 (recovery)**: in `save_review_files`, when a flagged record's own generation returns "",
  `_recover_review_file_from_sibling` looks for a SAME-email twin with the SAME vendor+number+amount (strict,
  all three) via `_find_document_sibling`, then (a) copies that sibling's saved LOCAL file, or (b) if it's a
  Drive URL now, re-derives from the sibling's `source_document` attachment. Only reuses an already-produced
  document, never fabricates; `_match_attachment` logic untouched.
- **Verified**: recovery unit-tested — copy-local, re-derive-from-Drive-sibling, strict-match guard (diff
  amount → no match), no-sibling → "". NOTE: Joel's two items had NO recorded twin, so they were single flagged
  records (Part 1 can't help those — Part 2 does). Fixed them live via the now-working path: #2629 Fun Salon
  #25290 → its scanner PDF (872 KB); #2632 Wellspring #1730473 → order-confirmation render (10.4 MB),
  review_queue.file_path set for both.
- **Follow-up worth noting**: the #2632 body render is 10.4 MB (forwarded marketing HTML renders every embedded/
  remote image full-res) — heavy for a review doc / Drive upload; a future improvement could strip remote images
  before rendering the email-body fallback.

### Diagnosis: Smiling Herbs #12874 not auto-ignored — stale flags, not a match bug (2026-09-22)

- **Records** (all invoice #12874, $548.4, Maple, invoice_date 8/10 — the "8/10/13/15" are EMAIL dates):
  147 (email 19fed1ce, run none, original, kept), 196 (email 19ffbfcc, run 20260813, correctly
  duplicate_ignored), 283 (email 1a0047c4, run 20260817, NOT ignored — the anomaly).
- **Not a current-code field-match bug**: ran the CURRENT `check_duplicate` on 283's real stored values vs
  147/196 → returns **`status='duplicate'`** (auto-ignore). vendor (`_norm_vendor_dedupe`), number (`_norm`),
  amount (`_amounts_equal`, 548.4≡548.40) all match. The auto-ignore path works today.
- **Root cause = stale flags from OLD code**: 283 was recorded 8/17 under the PRE-FIX `check_duplicate` (the
  filename-collision self-match bug corrected later this session by the "AMOUNT not filename" self-match fix).
  Generated filenames collide by design when store/vendor/number/amount/date all match, so the old code mis-read
  283 as "self" and didn't ignore it. Its stored `duplicate_reason` uses the OLD phrasing ("…for the same
  amount ($548.4)"), confirming it's never been re-evaluated. Duplicate status is computed ONLY at
  record-creation, so a record mis-classified under old logic stays mis-classified. 196 (8/13) predates the bug
  and was ignored correctly.
- **Same-run-history ruled out**: the three are in three separate runs (none / 20260813 / 20260817), each saw
  the prior in history — not the per-run history-snapshot gap.
- **Resolved by hand** (authorized): `set_duplicate_ignored('invoice', 283, True, duplicate_of=147's email)` →
  283 now ignored + joined group A051C9CE. Cluster complete (147 kept, 196 + 283 ignored).
- **Standing gap (NOT fixed — flagged for discussion, per the briefing's caution against broader auto-resolve)**:
  there is no RETROACTIVE duplicate re-evaluation when the dedupe logic improves — old records keep their
  old-logic flags until reprocessed. A "re-run check_duplicate over already-recorded, still-open records"
  backfill (analogous to the vendor/account backfill) would close it, but it AUTO-IGNORES records (higher
  stakes than the QB-name backfill), so it needs an explicit decision + audit-first before building.

### Duplicate re-evaluation backfill — READ-ONLY AUDIT (2026-09-22)

Chronological replay of the CURRENT `check_duplicate` / `check_duplicate_payment` over every recorded,
non-legacy row (each checked against only PRIOR-recorded rows, exactly like a live run; real dups not added
to the running history). Candidates counted only when currently unflagged + open (entered_in_qb=FALSE,
duplicate_ignored=FALSE, duplicate_suspect=FALSE). **No writes.**
- **Invoices (2327 replayed)**: **18 newly 'duplicate'** (clean auto-ignore candidates), **80 'possible'**,
  1 store_conflict. The 80 possibles are noisy: **43 have a NULL amount** (mismatch is just a missing amount,
  not a real revision) and **56 are McKesson** (per-shipment/order numbers that repeat) — only ~23 are
  non-McKesson/non-NULL genuine revision candidates, and all 'possible' need a human by definition.
- **Payments (194 replayed)**: **4 newly 'duplicate'** (Anda ×3 same-day same ref+amount, Garden of Life),
  **3 'possible'**.
- **Clean auto-ignore candidates total = 22** (18 inv + 4 pay). Sample lists (vendor, #, $, store, this-id/date
  vs matched prior-id) reported to Joel for sanity-check.
- **Cause** (same as the #12874 diagnosis): these were recorded under the old filename-collision `check_duplicate`
  and never re-evaluated; today's logic correctly flags them.
- **COMMITTED (2026-09-22, Joel's go-ahead)**:
  - **22 auto-ignores** (18 invoices + 4 payments) via `set_duplicate_ignored(..., duplicate_of=prior_email)` —
    ignored + suspect + duplicate_of + shared group id + edit_log, same path as record 283. Verified 18/18 +
    4/4 now `duplicate_ignored`.
  - **23 'possibles' surfaced** to /duplicates (set `duplicate_suspect` + reason + shared group id tied to
    their prior; NOT ignored) for Joel to eyeball. Of these, **18 are Sageteq #29365** (one invoice number,
    different amounts per store/line — a split invoice, clusters together as one group; likely a
    "don't-dedupe" case to confirm), and **5 genuine** (Rx Labels #782282, Nutri-Supreme #68130/#68131,
    Prodermix, Hold End Dist). The other 57 'possibles' (43 NULL-amount, McKesson non-unique numbers) were
    left untouched as noise. Verified 23/23 now `duplicate_suspect`; /duplicates shows 13 clusters.
  - Still no retroactive re-eval wired into the pipeline — this was a one-time backfill of the existing backlog.

### FIX: ignoring one duplicate copy over-cleared siblings in a 3+-way set (2026-09-22, HIGH PRIORITY)

- **Bug**: `web_db.set_duplicate_ignored(ignored=True)` ran an UNBOUNDED sibling-clear — it set
  `duplicate_suspect=FALSE` on EVERY non-ignored, still-suspect record sharing the group id / vendor+number.
  Correct for a 2-copy set (un-flags the lone survivor), but in a 3+-copy set ignoring ONE flagged copy
  silently un-flagged the OTHER still-unresolved copies too — they vanished from /duplicates (which filters on
  duplicate_suspect) while staying live + unentered in /qb: a silent double-entry risk. (/duplicates is the only
  UI that sets duplicate_ignored — there's no per-record Ignore button — so nothing else could catch it.)
- **Fix**: before clearing, COUNT non-ignored records remaining in the cluster (this one now ignored, excluded);
  only run the clear when `remaining <= 1` (the ignore resolved the set to a single survivor). With 2+ still
  remaining, leave their suspect flags — each needs its own explicit review. Verified with a self-reverting
  3-copy test: ignoring one leaves the other flagged; the final ignore clears the sole survivor. 2-copy behavior
  unchanged.
- **Audit of PAST damage** (read-only, grouped by vendor+number+amount over live rows = non-ignored +
  unentered): **16 live invoice duplicate-sets, 0 payment sets**. Split:
  - **2 FULLY INVISIBLE** (0 flagged copies — the actual silent risk): **McKesson #7656342453 $1430.94 ids
    1515+1516** (has an ignored sibling 1518 → the bug fingerprint; both survivors over-cleared) and **Auburn
    #008870 $2159.02 ids 353+354** (0 ignored siblings → NOT this bug; a pair check_duplicate never flagged,
    store 'unknown' — separate gap).
  - **14 VISIBLE** (>=1 flagged copy) already showing on /duplicates for normal resolution (mostly 2-copy
    McKesson original+suspect pairs, the Sageteq #29365 surfaced possibles, Smiling Herbs #12884 4-copy set).
  - My earlier 22-record auto-ignore backfill did NOT create invisible victims (checked: none of its ignores
    left 2+ identical live copies). The one bug victim predates it.
- **Per the briefing, NOTHING re-flagged** — reported for Joel to decide on the 2 invisible sets.
- **Resolution (Joel, 2026-09-22)**: both invisible sets are EXCLUDED vendors (McKesson, Auburn — confirmed via
  is_excluded_vendor), so they never enter QuickBooks and a silent duplicate among them is harmless (also why
  /duplicates excludes excluded-vendor invoices by default). No action needed on them. Net: the CODE FIX is the
  deliverable; past silent damage had **zero real QB-entry risk**.

### /duplicates: color only the outlier amount, not the whole set (2026-09-23)

- **Was**: `templates/duplicates.html` computed one boolean `amt_differ = (distinct amounts in set) > 1`
  and applied the red `.amtdiff` class to EVERY amount cell whenever any two differed — so a 3-copy set
  with two matching copies ($2,333.78, $2,333.78) and one real outlier ($2,393.78) showed all three red,
  hiding which one is off.
- **Now**: group by value and flag only the lone value(s). Build a per-record 2-dp string key
  (`'%.2f'|format(amount)`, so float noise like 548.4/548.40/548.400001 never splits an equal pair; None →
  its own `'∅'` key) via a `namespace`. `amt_multi` = the set has >1 distinct key. A cell reddens only when
  `amt_multi AND ns.keys.count(k) == 1` — a value with agreement (count ≥ 2) stays normal; the lone outlier
  goes red. All-identical → nothing red (unchanged). All-distinct (no consensus) → every value is a lone key
  → all red (reasonable). Two even pairs → neither red (both have agreement).
- Contained to duplicates.html (the `amt_differ` set-level flag replaced by the namespace key list + a
  per-cell test; `.amtdiff` CSS unchanged, comment updated). Verified across 2+1 / all-equal / all-distinct /
  two-pair / float-noise / one-None cases — only the true outlier(s) redden. Template Jinja-parses. Template
  auto-reloads (no restart needed for this one).

### Amount search: exact-by-default with an opt-in range toggle (2026-09-22)

- **Audit of every amount filter** (grepped, don't-assume): only TWO UI pages actually have one —
  **/qb** (qb.html → `qb_list`/`qb_export` → `list_qb_ready`) and **/browse** (browse.html → `browse` route),
  which covers BOTH **Records** (`list_all_records`) and **History** (`list_invoice_history`). Review queue,
  /duplicates, and /payments have NO amount filter (briefing's assumption was off). `history.html` is dead
  (unrendered). `chat_qa.py` has amount_min/max but it's the LLM natural-language tool interface, not a form —
  left as-is.
- **New behavior**: default is a single exact **Amount** field (new `amount=` param); a "range ⇄" toggle
  reveals Min/Max (existing `amount_min`/`amount_max`). Distinct params keep both kinds of bookmarked links
  working; opens in range mode only when a min/max is set and no exact amount is.
- **Exact match precision**: compared as `ROUND(amount::numeric, 2) = ROUND(%s::numeric, 2)` in all three
  backend functions — 548.4 matches "548.40" and float noise never defeats it. Exact takes precedence over
  min/max when both are somehow present.
- **Shared implementation**: one `amount_filter(amount, amount_min, amount_max)` macro in `_macros.html` +
  one global `amtToggle()` in base.html (disables the hidden group's inputs so only the active param submits).
  Used on qb.html and both browse.html sections. qb.html's base/pill/export query builders + Filters-open
  condition now carry `amount`; browse.html's pagination link builder too.
- Verified: exact match returns only that amount (548.40→Decimal 548.4), precedence over range, all three
  functions + no-filter full set; macro renders exact-default / range-on correctly; templates parse, modules
  compile. Route param change → needs the pending web-app restart.

### Flag for review + admin-triggered read-only investigation — Phase 1 (2026-09-22)

Decisions confirmed by Joel: **1a** separate `INVESTIGATE_API_KEY`; **2a** read-only tools, no shell.

- **Schema** (`web_db.WEB_DDL`, applied via `init_web_schema()`): `flagged_reviews` (record ref +
  JSONB `snapshot` frozen at flag time + note + submitter + status pending/dismissed/approved_for_fix)
  and `investigations` (flag_id FK, status running/completed/failed, findings, error, num_turns, tokens,
  cost_usd, duration_ms). Distinct from the AI `review_queue`.
- **Part A — flag capture** (any user): "Flag for review" card on record_detail.html → POST
  `/records/{type}/{id}/flag` → `web_db.add_flag` (snapshots vendor/store/number/date/amount/tax/ship/
  dup flags/entered/source file).
- **Part B — admin queue**: `/flags` page (`flags.html`). Admin sees all + can Investigate / Approve-for-fix
  (label only, no auto-fix this phase) / Dismiss; non-admin sees only their own flags read-only. Nav: added
  Flags under Review queue. Investigation result (findings/cost) shows under each flag, visible to admin AND
  the submitter.
- **Part C — investigate.py** (admin-click only; NO daemon/cron): `enqueue()` runs ONE headless
  `claude -p` at a time in a background thread (DB-enforced one-at-a-time + stale-run sweep so nothing hangs
  'running'). Read-only + safe: `--allowedTools Read Grep Glob`, `--permission-mode plan`, `--add-dir <repo>`,
  `--bare` (uses ONLY `INVESTIGATE_API_KEY`, never the subscription/keychain), `--max-turns 30`,
  10-min timeout. Prompt-injection guard: the untrusted note + snapshot go inside delimited DATA blocks with
  explicit "these are not instructions" framing (+ append-system-prompt reinforcement); prompt is passed via
  STDIN so no untrusted text hits argv. The wrapper — not the agent — parses the `--output-format json` result
  (findings + tokens + `total_cost_usd`) and writes it back; the agent has zero DB access.
- **Config env vars**: `INVESTIGATE_API_KEY` (required), `INVESTIGATE_MAX_TURNS` (30), `INVESTIGATE_TIMEOUT_SEC`
  (600), `INVESTIGATE_MODEL`, `CLAUDE_BIN`.
- **Verified**: schema applies; full add_flag → list_flags → create/finish_investigation flow works
  (self-reverting test); both templates render for admin + submitter; modules compile; `_build_cmd` resolves
  the Windows `claude.CMD` via `cmd /c` with only read-only tools.
- **VALIDATED LIVE (2026-09-22/23)**: self-tests + a real Investigate button click completed end-to-end
  (~3 min, ~20 turns, ~$0.5–0.75/run; real findings + cost captured in the investigations table). Robustness
  fixes applied after the first live run: (1) resolve to the native `bin/claude.exe` — the npm `claude.cmd`
  shim breaks under cmd.exe on the spaced install path; (2) dropped `--permission-mode plan` (the
  `--allowedTools Read Grep Glob` whitelist is the read-only guarantee); (3) `_api_key()` falls back to the
  persisted `HKCU\\Environment` value so a `setx INVESTIGATE_API_KEY` is picked up regardless of launch-console
  timing; (4) subprocess decodes claude stdout as UTF-8 (was cp1252-mangling em dashes). Each fix needs a
  web-app restart to load; `INVESTIGATE_API_KEY` is set persistently via `setx`.

### FIX: review gates ran too late → records vanished; + guaranteed emails-status write (2026-10-06)

Root cause of records landing in invoice_history but never becoming live invoices (EuroPharma, then ~17 in
the bulk recovery, esp. all TopRx): the review GATES (`apply_store/date/amount/scoped_learning_review_gate`)
ran only inside `build_result_rows`, which executes AFTER `apply_duplicate_checks` (adds to history) and AFTER
`newly_flagged` is computed. A gate-flagged record (notably `total_source='derived'`, i.e. amount summed from
line items — normal for TopRx) therefore fell into a crack: in history, NOT recorded, NOT queued for review,
emails-row status never updated. The model-flagged record-LESS branch (`if not invoices… : if
needs_human_review: continue`) had the same shape — dropped from new_rows, so `_upsert_email` never ran → 237
emails frozen at status='fetched'/run_id NULL back to March.

- **Fix 1 — gate ordering:** new `apply_review_gates(classification)` runs all gates in
  `process_source_email` right after classification (before assign_invoice_files / save_review_files /
  newly_flagged / apply_duplicate_checks), so `needs_human_review` is FINAL first. Gate-flagged records now
  get a split file + a review-queue row + are skipped from history. build_result_rows keeps the gate calls as
  an idempotent safety net. Verified: derived-total + no-date records flag, clean ones don't.
- **Fix 2 — guaranteed status write:** new `database.stamp_handled_emails` + a step in `main()` that stamps
  status/email_type/run_id for every email handled this run that produced NO recorded row (all-flagged → 
  'review', unreadable → 'review'). Closes the frozen-status gap for both branches. Verified self-reverting.
- Both compile; takes effect on the next classify run.

### Bulk-recover the /reconcile backlog — IN PROGRESS (2026-10-06)

- **After the gate fix**, re-requeued the **17 gate-stuck emails** (incl. all 9 TopRx; excluded the 1 false
  orphan "Maple Scanner…1a07d4e4", already recorded as Nutri-Supreme "6…") — 49 orphan rows cleared, files in
  test_emails/, awaiting Joel's reclassify to validate the fix + record them (derived-total ones now route to
  REVIEW, not limbo).
- **237-ish record-less backlog** (status='fetched'/run_id NULL, no records): my criteria find **186 stuck**
  (≈ the diagnostic's 237) = **48 stale-ledger** (already in review_queue/ignored) + **138 no-trace** (96 real
  vendor docs + 42 blank scanner-type). **Backfilled the 48 stale-ledger** (24→'review', 24→'processed') — a
  safe status-only write, no reprocess. **96 real no-trace: HELD** (requeue after the 17 validate the fix, so
  we don't repeat the vanishing at scale). **42 blank "Maple Scanner"/"Scan to E-mail" entries: flagged** for
  Joel to confirm genuinely empty before touching.
- Prior-run tally: 60 invoices / $59,082.31 recorded; statement recon matched 353 / missing 13.

The reconcile check surfaced a historical backlog (going back to March): **117 orphaned ledger rows** (107
invoice + 10 payment — the invoice-only diagnose missed the payments), **69 source emails**, **$87,785.92**
gross. 104/117 are email status='fetched' (classify never completed — same ad-hoc-bypass root cause as
EuroPharma, at scale). Joel's decisions: **requeue + he reclassifies** (safest — reclassify deletes-and-
replaces each email's records, so no dupes + extracts all attachments); **small sanity batch first**.

- **False positives exist** (important): the reconcile check matches by vendor + EXACT number, so a row
  recorded under a VARIANT number counts as a false orphan. Confirmed: Nutri-Supreme "#6" ($251.48) is
  already recorded under a fuller number "6…" — a direct-from-history insert would have DUPLICATED it. The
  requeue+reclassify method avoids this (it replaces the email's records). So the true outstanding is < $87.8K.
- **3 processed-invoice anomalies** (classify completed, still no live row): #2418 Westgate ($26.70) + SRFax
  #6294572 ($9.10) are genuinely missing; Nutri-Supreme #6 is the false positive above. The 10 payment orphans
  are mostly status='processed' too — a separate category to look at.
- **`recover_errored_email.py` extended**: now targets explicit filename(s) (any status), `--orphaned` (every
  email with an orphaned ledger row = the /reconcile backlog), or default status='error'. Clears orphaned
  invoice_history AND payment_history rows (matched via norm_vendor_dedupe). Calls init_db first (schema
  helpers) before opening its txn. Dry-run default; `--commit` requeues (move file→test_emails/, status→
  'fetched', clear orphans) — then `classify_emails.py` records them through the FULL pipeline.
- **Sanity batch (3) requeued**: "Invoice 4991 for Madison Pharmacy", "Invoice 0180009 from Infoshield LLC",
  "Invoice 110864 from MM Tires" — clean single-invoice 'fetched' ones. Files in test_emails/, /reconcile
  117→114. AWAITING Joel's `python classify_emails.py` to record them + verify, THEN requeue the remaining 66.
- Heads-up for Joel (per plan): when the full backlog reclassifies, ~100+ invoices will appear on /qb at once
  — not new, just finally recovered.

### FIX (robust/airtight): build_result_rows late gates could still strand records (2026-10-06)

After Fix 1/Fix 2 above, **10 more emails / ~20 rows / $8,707.66** still broke in run 131234 (TopRx, Kenover,
Easynurse, Herbari, Natural Roots, Nutranext, Kay & Co, Symphony, Natural Path). Diagnosis: `apply_review_gates`
is correctly placed (covers both classify paths) and the gates are identical in both locations, but a record a
gate flips to needs_review *inside* `build_result_rows` (one apply_review_gates didn't already catch) was still
dropped from new_rows with its history row already added by `apply_duplicate_checks` → the same history-only
orphan / frozen-status crack, just later in the flow. Rather than chase the exact asymmetry, made the fate
airtight regardless of WHERE a gate fires:
- **`build_result_rows` now returns `(rows, review_dropped)`** — it collects every record it drops at
  `if routes_to_review(...): continue` as `(result, rtype, rec)`.
- **New `route_dropped_to_review(review_dropped, results, history, payment_history, newly_flagged)`** strips the
  just-added `invoice_history`/`payment_history` row (matched by source_file + the record's own number, the same
  keys `apply_duplicate_checks` writes) AND adds the owning email to `newly_flagged` (deduped). So a late-flagged
  record gets a review-queue row (`build_review_rows` keys it to the whole email if it has no split file) and a
  stamped status, and never lingers as a ledger orphan.
- **Both callers updated:** `main()` (line ~4645, runs before save_history/save_results so the strip + flag
  persist) and the sales-order-release reconcile path (line ~3346). Credit memos aren't matched (never added to
  the ledger); empty numbers skipped so no blank-number row is ever hit.
- Verified with a self-contained test (2-tuple return; orphan strip + dedup flag; no-op on empty). Compiles.
  Takes effect on the next classify run.

### Recovery + housekeeping after the airtight fix (2026-10-06)

- **Requeued the 11 residual orphaned-ledger emails** (`--orphaned --commit`): the 10 above + the Nutri-Supreme
  false-orphan email. **21 orphaned ledger rows cleared**, files → test_emails/, status→'fetched'. /reconcile
  orphan count for these is now 0; they record cleanly on Joel's next run (derived-total ones route to REVIEW).
- **Nutri-Supreme "#6" (housekeeping) — resolved by that requeue.** The stale ledger row belonged to
  `Maple Scanner - 1a07d4e4f94faee6.eml` and was one of the 21 cleared. Its live row **id=1544 (`'6...'`,
  $251.48, qb=False)** is intact; the email is staged to reprocess-and-**replace** id=1544 (save_results deletes
  by email_filename), re-extracting the garbled `'6...'` number. This is the delete-and-replace remedy; qb=False
  so no QB mismatch. (Note: this email was conservatively *excluded* from the earlier 17-batch; it's now handled.
  Reversible before Joel's run by moving the file back + status→'processed'.)
- **42 "blank scanner" emails (housekeeping) — checked: NOT blank.** All 42 are `status='fetched', run_id NULL,
  no error` = fetched but **never classified**. **41 carry a real scanned PDF** (340 KB–1.4 MB) sitting in
  processed_emails/ — unrecorded real documents a normal run won't pick up (pipeline reads only test_emails/).
  **1** (`RE Scan to E-mail Server Job - 19ff13ee41f71642.eml`) is a text-only reply, no attachment → marked
  `status='processed'`, email_type='correspondence'. The 41 are listed in
  scratchpad/`requeue_scanner_backlog.txt`, **held** for Joel to requeue after the 11-batch validates the fix.
- **`recover_errored_email.py` gained `--from-file=PATH`** (additive): reads a newline-delimited filename list
  (blank / `#`-comment lines ignored) and requeues them like explicit args — for batches too large for the
  command line (the 41 scanner docs). Dry-run verified against the 41-file list.

### Hardening after the airtight fix: by-construction backstop, Phase-1 check, monitoring tile (2026-10-06)

Three follow-ups so this whole bug CLASS (record extracted → email status never advances) is closed by
construction and visible if it ever recurs:
- **By-construction status backstop (ask: move the status write to the single file-move point).** New
  `database.stamp_moved_from_queue(entries, run_id)` + a step in `main()` right after the `pending_moves` loop
  (the single place source emails leave test_emails/). For every file that left the inbox it advances the
  emails row off the fetch-time status, derived from the move destination (REVIEW_PENDING_DIR→'review', else
  'processed'). GUARDED on status still 'fetched'/NULL/'' so it NEVER clobbers a status the known-good writes
  (`_upsert_email`/`clear_stale_error_status`/`stamp_handled_emails`) already set — so a healthy run corrects
  **0**, and a **non-zero count prints a WARNING** naming the gap. A future code path that moves a file without
  stamping can no longer silently strand it. Verified: 0 on empty/nonexistent; never changes an already-set row.
- **Phase-1 split-resolution path — CHECKED, not a real instance.** When a review item resolves from its
  per-record SPLIT file (Filename ≠ Source Email), `_upsert_email` keys the emails row by the SPLIT name, not
  the original source email (84 such phantom 'processed' rows, 71 invoices recorded under split names). BUT this
  does NOT reproduce the orphan bug: `invoice_history.source_file` and `invoices.email_filename` BOTH use the
  split name, so they match — **0 split-named history orphans**, and **0 originals stuck at 'review'** (the
  original was already 'processed' from its clean records, or resolves as a whole). The phantom split rows are
  benign (already excluded from the /emails ledger by the `gmail_message_id/.eml` filter). Left as-is.
- **New dashboard tile "Never classified"** (same pattern as Orphaned-ledger-rows): counts emails
  `status='fetched' AND run_id IS NULL`, aged >1 day, via the SAME labeled CTE `/emails` uses (so the tile and
  `?disposition=never_classified` agree — a fetched/NULL email that somehow has records/review is claimed by an
  earlier CASE branch, not counted). New `never_classified` disposition + label. **Currently 66** (the real
  never-processed backlog, incl. the 41 scanner docs). A recurrence of the never-classified class now shows on
  the dashboard instead of needing someone to spot a blank row on /emails.
- All four files compile; web_db/review_app changes need Joel's web-app restart to show the tile/disposition.

### Airtight fix VALIDATED + never-classified backlog mapped (2026-10-06)

- **Validated.** Joel ran the scanner `--from-file` requeue + `classify_emails.py` (runs 20261006T135351, T140127).
  The requeued residual emails + 41 scanner docs recorded/queued through the fixed pipeline with **orphaned_history
  now 0** — zero new history-only orphans, confirming the airtight routing + backstop hold end to end. TopRx backlog
  recorded cleanly across ~20 emails. Scanner backlog resolved (≈23 recorded / 16 in review / 2 leftover).
- **"Never classified" tile reads 66** = 64 non-scanner + 2 leftover scanner. The 64 are real emails fetched into
  `processed_emails/` with run_id NULL that NO run ever read (a normal run only reads test_emails/): 21 invoices,
  13 payments/receipts, 21 order-ack/confirmations (informational), 5 utility/SaaS bills, 1 statement reminder
  (22 attachments), 1 credit memo, 2 other — so ~43 are real financial docs never recorded. Lists written:
  `requeue_never_classified_all.txt` (64) and `requeue_never_classified_financial.txt` (43, no order acks).
  Recommended: requeue ALL 64 (`recover_errored_email.py --from-file=... --commit` then classify) so the tile
  actually drops to ~0; dedup guards against anything already entered.

### ROOT CAUSE of the never-classified backlog FOUND + fixed at source (2026-10-06)

Traced the fetch→placement path. `gmail_fetcher.fetch()` writes every message to **test_emails/** (correct) and
immediately labels it InvoicesFetched (so it's never re-fetched). The ONLY code that moves a file INTO
processed_emails/ is `main()`'s `pending_moves` — and only for a CLASSIFIED result. So the 64 were classified,
not skipped. The gap: an **INFORMATIONAL** email (classified cleanly, produced NO record, not flagged — order
acks, shipment notices, statements, correspondence) hits `pending_moves → PROCESSED_DIR` (line ~4628) but
`_upsert_email` runs only for RECORDED rows, and the stamp step only covered `newly_flagged`/`unreadable`. So
its emails row was never written → frozen at fetch-time `status='fetched'`/`run_id`-NULL even though it was fully
handled and moved. This is the THIRD instance of the handled-but-status-frozen bug class, and the one that built
the 64-email backlog (21 of them are literal order-ack/informational; the rest are notification-style emails the
model classified without producing a record).
- **Source fix:** the `main()` stamp step now has a third loop that stamps every handled `to_results` entry with
  no recorded row that isn't flagged/unreadable/errored → `status='processed'` + its classified `email_type`.
  So informational emails advance off 'fetched' at the source. Verified with a 7-case logic test (recorded →
  skip, flagged/unreadable → 'review', order-ack/statement → 'processed', read/infra-error + no-classification →
  skip). The `stamp_moved_from_queue` pending-moves backstop remains the by-construction net (now expected to
  stay at 0 since the source writes the status).
- **Existing 64 backlog** still needs the one-time requeue (they're already in processed_emails/, so no future
  run re-reads them) — after requeue+classify they'll be stamped correctly by the new loop. Takes effect next run.

### FEATURE: "Flag set for review" — ONE flag per duplicate set, full snapshot (2026-10-06)

The /duplicates page can now flag a whole possible-duplicate SET for review as a single flag, instead of one flag
per record. Why one flag with everything embedded: the flag investigator (`investigate.py`) runs headless with
NO DB access — it sees only the flag's stored `snapshot` JSON + note + the codebase. A per-record flag (or a note
that just says "part of a set") can't show the bot the sibling copies, defeating the purpose.
- **`web_db.add_duplicate_set_flag(records, note, submitted_by)`** (right after `add_flag`): snapshots the FIRST
  record flat at the top level (so /flags' existing `s.vendor`/`s.amount` summary line keeps working) AND embeds
  every record's `_record_snapshot` under `snap['duplicate_set']` for side-by-side comparison. One `flagged_reviews`
  row. Verified with a self-reverting DB roundtrip (2-invoice set → 1 row, `duplicate_set` len 2, each entry has
  vendor/amount/store/number/date; row then deleted).
- **`POST /duplicates/flag_set`** (review_app.py): parses `records` tokens (`invoice|payment:id`), calls the new
  fn ONCE, redirects back with `?flagged=1`. `duplicates_page` takes `flagged` → green confirmation banner.
- **templates/duplicates.html**: per-group form with a hidden `records` field for every copy in the set
  (ignored copies included — context for why it looked ambiguous) + a required note, posting to flag_set.
- **investigate.py `build_prompt`**: set-aware — when `snapshot.duplicate_set` has >1 entry, the task line tells
  the bot it's looking at a SET of N records (point it at the array) instead of the single-record framing.
- **templates/flags.html**: summary line shows a `(set of N)` badge when `s.duplicate_set` exists.
- All compile / parse; web_db + review_app + templates need a web-app restart; investigate.py applies next run.

### FIX: kept copies join the duplicate ledger; skip-QB for an unlisted vendor goes to review (2026-10-12)

Joel approved (a) + (b) + (c) on 2026-10-12. Data changes are logged in edit_log as `claude (approved by joel, 2026-10-12)`.

**(a) Data: two more duplicate sets settled** (`web_db.keep_and_ignore_others`):
- Incento Magic II #12598 $586.88 @ Madison: kept #3381, ignored #3446 (the 10/11 scan) and #3451 (a $5,586.88 misread).
- Costco payment #20751521485459184: kept #65 ($214.77, the final charge), ignored #66 ($219.07, the temporary card
  authorization). Both now share group C74129B7.

**(b) A copy a person keeps joins the duplicate ledger** ([database.py](database.py), [classify_emails.py](classify_emails.py)):
- `load_history()` / `load_payment_history()` append `_kept_copies_missing_from_ledger()` rows. These are live,
  not-flagged copies in a duplicate group whose own file has no ledger row (not skip-QB, not legacy, not a waiting
  payment reminder).
  - That is exactly the copies a person kept or restored on Possible duplicates: the pipeline leaves a flagged copy out
    of the ledger while a person decides, and nothing added it after the keep.
  - `source_email_id` comes from another ledger row for the same file (a reprocessed split keeps its original
    email), else the file itself. The sender is joined in, so `_earlier_store_copy` sees a kept scan.
  - Derived on EVERY load and persisted by that run's `save_history`. Race-free: `save_history` rewrites the whole
    ledger from memory, so a row the web wrote during a run would have been wiped. The run log says
    `LEDGER: added N live … copies`.
  - Live on 2026-10-12: 6 invoices (#2473, #2474, #3378, #3381, #3474, #3492) and 4 payments (Garden of Life #26, plus
    Anda #36/#38/#44, live originals whose ledger rows were missing). They join on the next classify run.
- `find_live_invoice` / `find_live_payment` also return `duplicate_ignored`.
  - In `apply_duplicate_checks`, a store_conflict / store_preferred match on an already-ignored copy now calls
    `_flag_ignored_prior`, which flags the new copy for a person.
  - Before, it "superseded" the ignored copy again and kept the new copy clean beside the kept one. That is how
    Incento #3446 stayed live.

**(c) Real bills excluded because the billed store is on the excluded-vendor list:**
- Prompt: a paragraph right after the excluded-vendor list. It says the list names the vendor that issued the
  document, never the store billed or shipped to (some entries are our own pharmacies), and to decide
  `skip_qb_import` only by the issuer.
- Code: `apply_unlisted_skip_gate(rec, rtype, classification)`, the other direction from `apply_vendor_exclusion`.
  - When the model sets `skip_qb_import` on an invoice / credit memo whose vendor isn't on the list, it clears the
    flag and sends the record to review. The reason names the billed store when that store is on the list.
  - Exempt: excluded vendors, held sales orders, payments, and the multi-store cost-split shape (the same number for
    several stores in one email, or the model's cost-split note). Also exempt: an email reprocessed with a reviewer's
    answer (`classification["_reviewer_answered"]`, set in `process_source_email`), so it can't loop.
  - Runs last in `apply_review_gates`, before documents are saved, so a real bill gets its own review document
    instead of none. Re-run in `build_result_rows` as the usual safety net.
- Data:
  - Joel: "EasyFast should be on the excluded list". Ticked Exclude from QB on Vendor names row 163 (EASY FAST) via
    `web_db.exclude_vendor` + `reapply_excluded_vendor_backfill`.
  - Easyfast #11021 ($384) stays excluded. 4 more Easyfast invoices became skip-QB (#1436 $1,536, #1556 $576, #2213
    $768, #2215 $1,152; none entered). The new spelling matches only Easyfast records.
  - Re-included the other 7 ($5,507.24): Crown Care #224718, A&S Sales #17448, STIAC Laundry #3283, RX Labels #782622,
    Ashpa #3231467, KayCo #2562631 and #2581435.
    - For each: `skip_qb_import` → FALSE, with a note on the record and an edit_log row.
  - Skip-QB invoices get no saved document (`_record_needs_file`). So each bill's PDF attachment was rebuilt from its
    source email with `_write_record_document`, and the dry run was checked page by page.
  - The PDFs were uploaded with `upload_local_files_to_drive.py --commit`; all 7 now link to Drive.
- Tests: [scratchpad] `test_kept_ledger_and_skip_gate.py` 26/26; full regression 14 suites / 267 checks green.
- No web change; the pipeline parts apply on the next classify run.

### FIX + BACKFILL: QB "no match" rows kept only when they can still match; the Oct 8 upload's rows saved (2026-10-12)

The pending-unmatched-rows feature (see "FEATURE: persist QB export no-match rows", 2026-10-08) went in the
afternoon after the only QB upload (batch 6, `All 7 stores.xlsx`, 10/08 10:50), so `qb_unmatched_rows` was empty.
Backfilling it showed two problems with the no-match branch itself:
- **Flood.** The export is year-to-date. Of batch 6's 10,888 no-match rows:
  - 10,445 are dated before the app's start (08/07/2026);
  - ~1,300 are documents the app already has. Entered, excluded and ignored records never reach the matching
    pool, so their QB rows came out "no match".
  - Only 584 could still match a later email.
  - The next upload would have put ~10,900 rows on the Unmatched page, which isn't paginated.
- **No dedup.** Every upload repeats the earlier rows, and each would have been saved again.

**Fix** ([qb_match.py](qb_match.py)): `UnmatchedKeeper`, built once per upload, keeps a no-match row only when it
meets all three conditions:
- **Recent:** dated on/after `UNMATCHED_SINCE` (2026-08-07) and at most `UNMATCHED_WINDOW_DAYS` (120) before the
  upload. 99% of the app's records arrived within ~4 months of their invoice date (p50 2 days, p95 52, p99 125).
- **Not already in the app:**
  - no record in the same store, in ANY status, is the same document (`record_on_file`: same number plus the same
    amount or the same standard vendor);
  - no confirmed review-list match already linked the row to a record (that link can rest on vendor + amount +
    date, which `record_on_file` doesn't use).
- **Not already saved:** an earlier upload didn't save the same row (any status, counted per copy, so a dismissed
  row doesn't come back).

Other details:
- `n_no_match` still counts every no-match row. The server log prints how many were kept.
- Each upload's file is now kept as `qb_imports\batch_<id> - <name>` (`keep_upload_copy`, best effort, after the
  commit). Until now the app didn't keep the file.
- `resolve_unmatched_for_new_records` is unchanged.

**Backfill** ([backfill_qb_unmatched.py](backfill_qb_unmatched.py), dry run by default):
- **Input.** It re-reads the uploaded file. The original was still in Downloads, and it has the same 12,393
  Bill/Credit rows. It applies the same rule under batch 6, and also:
  - review-list rows confirmed at upload are skipped; rejected ones go through the rule;
  - a row that exactly matches a record NOT yet marked entered is saved `resolved`, and the record is marked
    entered (edit_log, `joel via QB unmatched backfill`), the same as an email arriving after an upload.
- **Records it marks entered (4):**
  - Smartcell #072026-MP (inv #3360);
  - R & T credit #T-023717 (cm #51; its vendor was mislabeled L&R until 11:56 on 10/08);
  - Bluebonnet #1613348-IN (inv #322, the kept copy);
  - Zahler #INV/2026/51147 (inv #3334).
- **One QB row is one document.** When a matching record is already entered, another not-entered match is a
  second copy. It's reported, not marked (see §10):
  - Reismans inv #890;
  - Prime Packaging cm #112;
  - Lantev inv #162.
- **Dry run counts:**
  - 584 to keep. By month: Aug 323, Sep 245, Oct 16. By store: Maple 176, 3M 171, Refuah 107, Madison 67,
    Westgate 58, BMW 3, Lakewood 2.
  - 4 to mark entered.
  - 10,445 dated before 08/07.
  - 1,292 already in the app.
  - 68 confirmed at upload.

**Verified** with a self-reverting test on the live DB. It used one connection with commit as a no-op, rolled back
at the end:
- the dry run writes nothing;
- `--apply` gives 584 pending + 4 resolved rows under batch 6, marks the 4 records with 4 edit_log rows, leaves
  the 3 copies untouched, and saves nothing dated before 08/07;
- a re-run adds nothing;
- a fresh upload of the same file keeps 0 new rows;
- the live DB is unchanged afterwards.

**Joel:** restart the web app BEFORE the next QB upload. The running app still has the old no-match branch and would
save ~10,900 rows. Then run the backfill with `--apply` (§10).

### FIX: duplicate steps that still compared PRINTED vendor spellings, + narrower "differs from matched vendor" pill (2026-10-12)

Since 2026-10-08 `check_duplicate` matches by STANDARD vendor (`_dedupe_vendor_key`). The steps around it still
compared the printed spelling. So when the vendor's copy and the store scan spelled the vendor differently
('Maxi Health' vs 'MaxiHealth'):
- the store-preferred supersede couldn't find the vendor's copy, and flagged the scan instead;
- the vendor copy stayed live;
- Possible duplicates split the copies into separate sets.

Joel approved the fix + the data fix on 2026-10-12.
1. **Store-preferred / store-conflict supersede** ([classify_emails.py](classify_emails.py) `apply_duplicate_checks`):
   - What changed: `find_live_invoice` is now called with the PRIOR's own spelling + number (falls back to this
     copy's). `database.find_live_invoice` / `find_live_payment` also accept a standard-name match.
   - Why both: a real run is `python classify_emails.py`, i.e. `__main__`. The `import classify_emails` inside
     database.py then loads a SECOND module copy whose `VENDOR_STD` is empty, so the DB-side standard-name compare
     falls back to the printed spelling in a real run (spotted by the parallel session).
   - The call-site change is what makes it work in a real run; it's tested under a simulated `__main__` second copy.
2. **Possible duplicates** ([web_db.py](web_db.py) `list_duplicate_candidates`):
   - The SQL EXISTS matches record type + number + a qualifying suspect, without the vendor.
   - Python then groups by (record type, standard-vendor key, number) via `_duplicate_vendor_keyer()`; vendor
     tables are cached 60 s in `_vendor_tables()`.
   - A vendor copy and a differently-spelled scan now show as ONE set. Another vendor with the same number stays out.
3. **Post-save backstop** `resolve_store_preferred_duplicates`: cluster key is `_dedupe_vendor_key`. Dry run
   on 2026-10-12: resolves 0, and skips #803634 (7 scans) and #803670 (2 scans) for a person, as designed.
4. **`recheck_duplicates.py`**: `find()` keys on `ce._dedupe_vendor_key`; `main()` loads `ce.VENDOR_STD` first.
5. **Record-page pill** ([review_app.py](review_app.py) `_record_detail_view` → `web_db.vendor_mapping_doubt`):
   - Shown only when the printed spelling isn't one Setup › Vendor names lists for that standard vendor (a fuzzy
     guess), or is listed under a DIFFERENT vendor (a stale mapping).
   - Down from 970 records to 43, all fuzzy matches. It never shows if the vendor tables can't load.
   - The tooltip says what to do: check the vendor; if right, add the spelling in Setup.
6. **Data fix — MaxiHealth #803670 $6,682.27 @ 3M:**
   - Kept scan #3492; ignored scan #3493 and vendor copy #3174 (`web_db.keep_and_ignore_others`; edit_log
     changed_by `claude (approved by joel, 2026-10-12)`).
   - #3175 ($6,957.40, un-flagged by Joel 10/08) left alone, as agreed.
- Tests: [scratchpad] `test_std_vendor_fixes.py` 19/19. Full regression 13 suites / 241 checks green.
  - Four older scratch tests had stale expectations and were updated; no code changes were needed:
  - the fakes now return save_results' `(protected, recorded)`;
  - cleanup / count checks are baseline-relative, because Joel's real 10/08 `drive-backfill` and
    `recheck --apply` rows share those names;
  - test_dup_fixes C sets `USE_DB` like a real run.
- **Restart the web app** for items 2 + 5. Items 1, 3 and 4 apply on the next classify / recheck run.
- Not changed: `database.find_recorded_invoice_ref` (sales-order supersede) still compares printed spellings.
  Low impact, because the released order goes through the duplicate check anyway.

**Found while verifying (NOT fixed — need Joel's decision; see §10):**
- (a) A copy a person KEEPS on Possible duplicates never enters the duplicate ledger (`invoice_history`).
  - In addition, `find_live_invoice` doesn't skip copies that are already ignored.
  - Result, Incento Magic II #12598 $586.88 @ Madison:
    - Joel kept scan #3381 on 10/09.
    - On 10/11 a new scan, #3446, matched only the ledger's vendor copy #3355, which was already ignored.
    - It "superseded" #3355 again and stayed clean.
    - So #3381 and #3446 are BOTH clean and live, a double-entry risk. #3451 ($5,586.88, a misread) is flagged.
  - Six live invoices are in this "kept, but the ledger knows only another copy" state.
- (b) 24 real bills are excluded from QB (`skip_qb_import`) because the BILLED STORE (3M / Refuah / Westgate /
  Madison Pharmacy) is on the excluded-vendor list, and the model applied it to the store.
  - The record notes even say so.
  - `apply_vendor_exclusion` only ever flips False→True, so nothing corrects it.
  - 8 of them are live with no other copy going to QB: $5,891.24.

### INVESTIGATION: records whose document never reached Drive (2026-10-08)

336 live records (277 invoices/credit memos, 59 payments) have a LOCAL `file_path` (`invoice_files/` or
`payment_files/`) instead of a Drive link. The file exists for all of them, so the web app still shows it,
but only on this PC and not through Drive. Classified by the run that saved each record (`emails.run_id`):

| Cause | Runs | Records |
|---|---|---|
| Run with Drive upload OFF — the 7 backlog-recovery runs 20261006T114541 → 20261007T114023 | 7 | 153 |
| Run with Drive upload OFF — earlier direct runs, 20260813 → 20261005 | 19 | 144 |
| `recover_errored_email.py` path (RECOVERY-20261005-EuroPharma) | 1 | 1 |
| Upload FAILED inside a normal `--drive` run (1–2 per run, random vendors) | 30 | 38 |

- **Drive is off unless asked for.** `USE_DRIVE = "--drive" in argv or UPLOAD_TO_DRIVE == "1"`.
  - `Fetch New Invoices.bat` runs `python run_all.py --drive`, so the launcher always uploads.
  - A direct `python classify_emails.py …` does not, and `UPLOAD_TO_DRIVE` isn't set.
  - The Oct 6–7 recovery instructions in this file told Joel to run plain `python classify_emails.py`
    (see "Airtight fix VALIDATED…": runs 20261006T135351, T140127).
- **Failed uploads fall back silently.** `drive_uploader.upload` makes 3 API calls with no retry, and
  `upload_record_files` catches any error. It prints `Drive upload failed for …` to the console only
  (no run logs are kept) and keeps the local path. 1–2 per run across 30 runs fits transient
  network/proxy (Techloq) errors. That's inferred — no logs survive.
- **Fixed the same day, all three (Joel: "do all three"):**
  - **(a) Drive is on by default in DB mode.** `classify_emails._drive_enabled()`: `--no-drive` or
    `UPLOAD_TO_DRIVE=0` win; otherwise it's on with `--drive`, `UPLOAD_TO_DRIVE=1`, or DB mode. Excel-only
    mode is unchanged (off).
    - A run with Drive off says so at the start.
    - If Drive can't start at all (expired login, missing credentials.json — google_auth raises
      SystemExit), it's switched off for the rest of that run instead of failing the run or retrying per
      file.
    - Every file that stays local is collected in `DRIVE_FALLBACKS` and named in an end-of-run `DRIVE:`
      note pointing at the backfill.
    - Released sales orders now upload a local snapshot file at release (`reconcile_sales_orders`). No
      held order has one today; this is a safeguard.
  - **(b) Retries in `drive_uploader.py`.** `execute(num_retries=4)` on every call: googleapiclient
    2.198.0 retries SSL errors, timeouts, ConnectionError, ECONNRESET, 5xx and 429 with backoff. The
    whole upload is attempted 3× with a short pause, and a copy uploaded but not yet shared is deleted
    before the next try. An expired/revoked login (RefreshError) is raised at once.
  - **(c) `upload_local_files_to_drive.py`.** Dry run today lists **336** records (274 invoices, 3 credit
    memos, 59 payments), 336 distinct files, 131 MB, none missing. `--commit` does the following:
    - uploads each file once and switches every record using it;
    - updates only if the link is still the same local path, one commit per record, logged to edit_log
      as `drive-backfill`;
    - keeps the local files, so older QuickBooks memo short-links still resolve;
    - reports failures and leaves those records as they were. It's re-runnable.
  - Verified with `test_drive_fixes`: fake Drive service and fake uploader, never the real Drive; the
    backfill ran on synthetic records in a rolled-back transaction. Full regression re-run green.
- **TO DO (Joel):** run `python upload_local_files_to_drive.py --commit` once. It needs your Google login,
  and takes a few minutes for 336 files.

### FIX: recheck_duplicates --apply left two copies ungrouped + no-reference payments invisible (2026-10-08)

Joel ran `recheck_duplicates.py --apply` at 12:15. All 5 findings were flagged as intended.
- **Grouping bug.** `_apply` set the target's group with `coalesce(duplicate_group_id, gid)`, and `''`
  (the insert default) isn't NULL. So MasterKo #2571 and Garden of Life #272 kept a blank group.
  - Fixed: `''` now counts as unset. The tool also no longer touches `duplicate_of`: like the pipeline, it
    sets that only when a copy is actually ignored.
  - Repaired the two rows to their pair's existing group (#2653 → F3E4A358, #26 → 526FDB7D), logged as
    `recheck_duplicates (group repair)`.
- **No-reference payments never appeared on Possible duplicates.** `list_duplicate_candidates` clusters
  by vendor + number and drops blank numbers. So a flagged payment with no reference number (pipeline
  "no reference # to confirm" matches, or the re-check's Garden of Life pair) was hidden from /payments
  by default AND absent from /duplicates.
  - Fixed: those sets are now built from the shared `duplicate_group_id` (key `payment|~group|<gid>`),
    with every member included and no record repeated across sets.
  - Possible duplicates now has 8 sets, including all 5 re-check pairs.
  - Needs a web restart.
- Verified with `test_recheck_apply_fix` (synthetic rows, rolled back). `test_dup_fixes`' live
  `--apply` checks no longer apply, now that the real run happened.

### FIX SWEEP: the review spec's "Known gaps" worked through (2026-10-08)

Eleven of the fourteen gaps listed on the review-logic spec are fixed. The three housekeeping ones
(vestigial tables, the dormant auto-resolve helper, leftover `invoice_files/` copies) are waiting on
Joel's call. Working gap 5 also turned up **two live duplicate bugs**, both fixed here. Pipeline changes
take effect on the next classify run. Web changes (pages, Setup) need a web-app restart (use
`notice.py` first). Schema changes are additive (`IF NOT EXISTS`) and apply on init. Every piece was
verified with self-reverting or rolled-back tests, listed at the end.

**1. A replace no longer deletes an email's other records.** `database.save_results(new_rows,
touched, run_id, replace_records=None)` takes optional **scoped deletes**: `{"filename",
"record_type", "vendor", "number", "amount"?}`. Each one deletes only the matching record:
- same `email_filename`;
- same vendor via `norm_vendor_dedupe`;
- same number, ignoring case and spaces;
- same amount to the cent, when the spec gives one.

Scoped deletes get the same human-activity backstop and the same entered-in-QB marking as whole-file
deletes (`_mark_entered_rows(cur, filenames, replace_records)` and `_scoped_where`).
- **The two callers now use it:**
  - `_supersede_reprocessed_records` returns one spec per stale record instead of old filenames.
    `main` passes them as `replace_records`, and Excel mode mirrors this with `_replaced_by`.
  - `reconcile_sales_orders` releases with `touched=set()` plus one spec per released order. The
    order's original email keeps its other records.
- **Guard for the case the old bug "accidentally" handled:** `_flag_orders_already_recorded`. If the
  order's own email already has a recorded invoice from the same vendor with the same amount under a
  different number, the order is probably the same document read twice. It goes to review with a
  note instead of being recorded twice.
- **Releases wait while someone is working on the email.** New `database.recently_human_touched()`.
  An order whose email had human activity in the last 10 minutes stays held for one more run.
  Before, the backstop skipped the insert but the order was still marked released, so it was lost.

**2. Released sales orders don't cycle any more.** New `apply_released_order_check(c)` runs in
`process_source_email` right before `apply_review_gates`. When a released `pending_sales_orders` row
already exists for the same vendor and number (`database.find_released_sales_order`), it:
- clears `is_sales_order`;
- adds a note: `[Sales order #… was already released from its hold (held mm/dd/yyyy) — recorded as an
  invoice instead of being held again.]`

The record then goes through the normal checks. Answering a released order's review item now records
it instead of holding it again. This covers the 7 open items from released orders. Superseded orders
are untouched, because their invoice is on file.

**3. Review ids are kept across runs.**
- `database.save_review_queue` keeps each row's existing `Id` and inserts it explicitly via
  `_insert_review_row(cur, row, review_id)` with `COALESCE(%s, nextval(...))`. New rows get the next
  id. If two rows claim one id, the first keeps it. A hand-edited id above the sequence moves the
  sequence past it.
- `classify_emails.carry_review_ids(new_rows, old_rows, unit_file, taken)` gives a re-flagged item's
  fresh row its old row's id. It goes to the new row with the same review key, else to the only new
  row, and never guesses between two. Called next to `carry_review_assignments` in `main`.
- `web_db.restore_ignored_review` restores under the old id when it's free.
- `/review/<id>` links, `--retry-id`, the Browse "In review" links and edit_log history now stay on the
  same item. It also closes a hazard: an answer submitted from a page opened before a run could land on
  a different item that had inherited the id.

**4. Records saved before the store/date/amount checks are now visible.**
- `web_db._missing_info_sql(datecol)` lists what a record lacks (`store`, `date`, `amount`). The
  definitions match the gates: store blank or `unknown`, date blank, amount NULL or ≤ 0.
- `list_qb_ready(missing=)` and `list_payments(missing=)` return a `missing_info` column and take a
  `missing=1` filter. `missing_info_counts()` feeds a banner.
- /qb and /payments show:
  - a yellow banner, e.g. "⚠ 13 records waiting for QuickBooks entry are missing a store, date or
    amount", with a **Show them** link (`missing=1&entered=no&dup=all`);
  - a Filters checkbox;
  - red `no store` / `no date` / `no amount` tags in the cells. Display only: the copy and CSV data
    attributes are unchanged.
- CSV exports honour the filter. Today: **13** bills and **15** payments.

**5. Duplicate re-check tool + two duplicate bugs it surfaced.**
- **New `recheck_duplicates.py`.** Dry run by default. It walks live invoices and payments in arrival
  order and reports the copies today's rules would catch:
  - missed duplicate;
  - missed revision;
  - recorded twice;
  - no-reference payment match;
  - payment reminder + its invoice.

  The rules mirror `check_duplicate` and `check_duplicate_payment`: the same-email rules, an applied
  invoice # keeps payments apart, and skip-QB invoices and legacy records are left out unless you pass
  `--include-legacy`. A pair where both copies are already entered in QB is reported as "check QuickBooks
  for a double entry" and never changed.
- **`--apply`** only marks the target copy as a **possible** duplicate, so a person decides on
  /duplicates. The target is the later copy, or the payment reminder. It sets `duplicate_suspect`, a
  `[Re-check mm/dd/yyyy] …` reason and a shared group id, and logs to edit_log as `recheck_duplicates`.
  It never ignores, deletes or un-flags anything, and skips copies already entered in QB.
- **Today's dry run found 5:**
  - A&S Sales #17496 — two Madison scans, $1,214.75;
  - Boiron #FI26144209 — two Refuah scans, $196.78;
  - MasterKo #35045 — a payment-reminder record plus the real invoice, $584.30;
  - Micro Merchant Systems payment ref ARC_2TG28BJLQ8AU — $5,397.45 vs $238.42 from two emails;
  - Garden of Life — no-reference payments of $152.83 from two emails.

  None of them is entered in QB yet. Waiting on Joel: run `--apply`, or fix them by hand.
- **Bug A fixed (`check_duplicate`).** With the vendor's own send already on file, the store-preference
  branch returned "store_preferred" at the first match. So a SECOND store scan was also "preferred" and
  both scans stayed live. That's how A&S and Boiron happened. New `_earlier_store_copy()`: when an
  earlier store-scanned copy is already in the ledger (same vendor, number, amount and store, another
  email, sender in `sender_store_map`), the new scan is a plain `duplicate` of it ([first-arrived]).
- **Bug B fixed (`apply_duplicate_checks`).** A payment reminder that arrives before its invoice is
  recorded live with a "NO matching invoice is on file" note and kept out of the ledger. So the real
  invoice arriving later matched nothing, and both stayed live. That's how MasterKo happened.
  - New `database.list_open_reminder_records()`, loaded once per pass by `_load_waiting_reminders`.
  - `_resolve_waiting_reminders` runs when a regular invoice is recorded with status `none`. It
    auto-ignores the waiting reminder via `supersede_duplicate_invoice`.
  - If the reminder was already entered in QB, the invoice is flagged as a possible duplicate instead.
  - The same email re-read never ignores itself. 5 reminders are waiting today.

**6. `total_source` is saved.**
- New columns `total_source` on invoices, credit memos and payments (`stated` / `derived` / NULL;
  NULL on everything saved before today).
- `build_result_rows` writes "Total Source" via `_total_source(rec)`, which normalizes unknown values
  to ''. The insert helpers and the `_invoice_row` / `_credit_row` / `_payment_row` readers carry it.
- The record page shows "· printed total" or "· added up from line items (no printed total)" next to
  the amount.

**7. Tracked outcomes follow the item, not the file name.**
- New `tracked_reviews.review_key` column. `track_review` stores it and won't double-track a
  re-flagged copy, matching on filename OR key.
- `list_tracked` matches in this order: queue by filename, then by key (`_TRACK_KEY_SQL` computes the
  key for older rows with the classifier's formula), then the recorded copy by filename, then the
  ledgers (`_TRACK_RECORD_SQL`: same vendor and number under the source email or any file
  `invoice_history` / `payment_history` traces back to it).
- With no own-email record, "No record" adds "the same vendor and # is recorded from another email".
- A re-flagged tracked item now reads **Still flagged** instead of No record.

**8. Setup › Store rules (vendor-scoped store guards).**
- New table `store_scope_rules` (reference, store_matched, applies_to, vendors one per line, note).
- Seeded once: an emptied table stays empty when edit_log shows its rows were deleted in Setup. The
  three seed rows:
  - Carepax → BMW, only for Sageteq and Microsoft (the old hard-coded guard);
  - Accuscript → Lakewood Rx, only for East Coast Building Maintenance;
  - 112 Hillside → Westgate Pharmacy, for every vendor except Braun.
- `apply_scoped_learning_gate` now loops over `STORE_SCOPE_RULES`, loaded per run in `main` with the
  seed list as fallback.
  - **only these vendors:** a record resolved to the store from a reference mention, on any other
    vendor, goes to review.
  - **every vendor except these:** a record mentioning the reference but resolved to a different store
    goes to review. A blank or `unknown` store is left to the store check.
  - The reference is matched in `store_raw`, ignoring case and spacing. A vendor matches when one of
    the rule's names appears in the vendor name.
- Setup page `/reference/store_scope_rules` (menu: Setup › Store rules): `REFERENCE_PAGES`, `_REF`,
  and the `store_rule_kinds` option source.
- Replaying the rules over every saved invoice and credit memo flags **none**. So the two new guards
  only catch future leaks, with no review noise on existing data.

**10. Applied invoice # is shown.**
- /payments: "for invoice #…" under the reference.
- /duplicates: an "Applied to invoice" row on payment sets where any copy has one.
- The payment record page.
- Display only; the CSV and copy columns are unchanged.

**11. `verify_model_signals.py`.** A read-only report of the AI-dependent signals, for checking them
after live runs:
- sales orders held / released / superseded, including released orders whose invoice turned up later
  under another number;
- payment reminders auto-resolved vs. waiting;
- `total_source` split and derived-total vendors;
- not-received citations;
- open review items by kind of reason.

Today, last 30 days: 24 held, 99 released, 5 superseded; 16 reminders auto-resolved, 5 waiting;
101 open items (35 vendor not in Vendor names, 32 multiple versions, 18 no date, 9 derived total,
6 store, 2 no amount, 23 the AI's own question).

**12. Per-vendor "Trust line-item totals".** New `vendor_name_map.trust_line_item_totals` (default
FALSE; **nobody is ticked**), shown as a checkbox on Setup › Vendor names.
`database.load_vendor_standardization()` returns `trusted_totals`. `_trusts_line_item_totals(rec)`
matches the vendor EXACTLY on any spelling, never fuzzily. For a ticked vendor,
`apply_amount_review_gate` accepts a derived total with a note instead of sending it to review. A
missing or zero amount is still flagged. Applies to documents read after it's ticked.

**Housekeeping (Joel decided 2026-10-08, same day):**
- **9. Done.** Deleted the dormant `web_db.auto_resolve_store_vs_vendor_duplicates()` (and its
  `_AUTO_RESOLVE_ACTOR`). It had never been run: 0 edit_log rows.
- **13. Done.** `excluded_vendors` / `statement_recon_excluded_vendors` were renamed to
  `archived_excluded_vendors` (27 rows) / `archived_statement_recon_excluded_vendors` (1 row).
  - The rename is an idempotent `DO $archive$` block in MIGRATIONS: it skips tables already archived
    and never fails if an old process re-created an empty table.
  - Removed the CREATE TABLEs from DDL and the two `_REF` entries; updated the stale comments.
  - Applied live via `init_db()`. Exclusions still load from Vendor names (39 spellings / 5 statement-recon
    spellings).
- **14. Left as is (Joel).** Then investigated why some records' documents aren't on Drive — see
  "INVESTIGATION: records whose document never reached Drive (2026-10-08)".

**Verified** (scratchpad tests, all passing):
- `test_scoped_replace` — live, self-reverting, plus a stubbed reconcile;
- `test_stable_ids` — session-temp shadow tables;
- `test_tracked_and_pages` — rolled-back transaction plus TestClient;
- `test_missing_info` — read-only, matched against an independent query;
- `test_store_rules_trust` — unit, live seed, replay, and Setup add/edit/delete rolled back;
- `test_dup_fixes` — unit, plus `--apply` rolled back;
- `test_total_source_e2e` — live, self-reverting;
- yesterday's three suites re-run green.

One incident: an early page test used a real user (Amy) and saved a `payments_page_size: all`
preference. It was removed right away; she had no other preferences then. Tests now use a user id
that doesn't exist.

### FEATURE: show QuickBooks vendor on the record-detail page (2026-10-08)

The /records/<type>/<id> page showed only the matched vendor, so a record filed under the wrong
QB vendor (see R&T/L&R fix below) was invisible. Now shows a **QuickBooks vendor** row (invoices/
credit_memos only) with an amber **"⚠ differs from matched vendor"** pill when `standard_vendor`
diverges from `vendor_matched`.
- **Backend (needed, not template-only):** `_record_detail_view()` in [review_app.py](review_app.py)
  was normalizing the raw row down to a fixed key set and DROPPING standard_vendor/qb_vendor — added
  both keys (payments/review return None). **Template:** new row in [record_detail.html](templates/record_detail.html).

### FEATURE: 3-state "Entered" filter on /qb (2026-10-08)

Replaced the 2-way Not-yet-entered / All toggle with **Not yet entered / Entered / All**, so entered
records can be viewed in isolation.
- `list_qb_ready(entered="no"|"yes"|"all")` in [web_db.py](web_db.py) (was `only_unentered` bool).
  `qb_list`/`qb_export` in [review_app.py](review_app.py) take `entered` (default "no"). [qb.html](templates/qb.html)
  3-way toggle + every `unentered=` link/hidden-input switched to `entered=`.

### DATA FIX: 9 "R & T Distributors" records mis-mapped to L&R (2026-10-08)

Root cause: a 2026-09-18 backfill set `standard_vendor='L&R Distributors, INC / LR- Eshop'` on all
`R & T Distributors` records when the vendor table didn't yet resolve R&T. The backfill's guard only
touches rows where standard_vendor/account/qb_account IS NULL, so these (wrongly) non-NULL rows were
structurally excluded from ever being re-swept — today's R&T alias fixed resolution going forward but
could never reach them.
- **Audit (read-only):** confirmed `resolve_standard_vendor('R & T Distributors')` now → `R & T Dist.`
  / `Purchase:OTC Purchase`. Found a **9th** row beyond the 8 named: **credit_memos id 51**. The
  non-skip/non-entered mismatch set is EXACTLY these 9; **0 mismatched rows were entered in QB**; all
  other audit "mismatches" are excluded (skip_qb_import) vendors (moot) or same-vendor name refinements
  (e.g. NDC Distributors → NDC Distributors LLC — left alone).
- **Fix (bounded, authorized, dry-run-previewed):** NULLed standard_vendor/standard_account/qb_vendor/
  qb_account on the 9 ids (invoices 33,438,861,1475,1526,1527,1528,1669 + credit_memos 51), logged all
  36 field changes to edit_log (`changed_by='manual correction — R&T/L&R vendor mismatch'`), then ran
  the sanctioned `reapply_vendor_standardization_backfill()` (checked=10 resolved=9). Verified from a
  fresh connection: all 9 → `R & T Dist.`, **0 stale R&T rows remain**. qb_vendor/qb_account carry the
  configured per-store spellings.
- **Follow-up spelling fix:** corrected the Madison store typo `R & T DIst.` → `R & T Dist.` in
  `vendor_name_map` id 474 (`name_madison`) and refreshed the one recorded invoice using it (id 1475
  `qb_vendor`); both logged to edit_log (`manual correction — Madison R&T spelling typo`). `name_maple`
  stays the deliberate short form `R & T`.

### FIX: MMS applied-invoice backfill + notes fallback (duplicate guard) (2026-10-08)

Root cause of the recurring MMS "duplicate" flag: the applied-invoice duplicate guard keys on the
structured `payments.invoice_number`, but for 4 MMS rows sharing one reference # the model wrote the
applied invoice into free-text `notes` only, leaving `invoice_number` NULL — so two payments on the
same ref settling DIFFERENT invoices looked like a revision of each other.
- **Data fix (bounded, authorized, dry-run-previewed):** backfilled `invoice_number` on payments
  196/198 → `TRINV-688744`, 197/199 → `CINV-668468`, and cleared 199's stale `duplicate_suspect`.
  5 edit_log entries (`manual correction — MMS invoice_number backfill …`). Verified fresh-connection:
  TRINV pair = 196 live / 198 ignored; CINV pair = 199 live / 197 ignored — each real payment counted once.
- **Permanent fix #1** ([classify_emails.py](classify_emails.py)): new `_applied_invoice_from_notes(notes)`
  (regex `against invoice <NUM>`), called in `apply_duplicate_checks` payment loop to backfill
  `invoice_number` from notes before `check_duplicate_payment` — and since the same dict feeds
  build_result_rows, it also persists into the column.
- **Permanent fix #2** ([recheck_duplicates.py](recheck_duplicates.py)): payments SELECT now pulls
  `notes`; applies the same fallback to `applied` before dedup. Imports the helper. (Heads-up: this will
  surface the same gap for OTHER vendors' recorded payments on the next run — review before `--apply`.)
- Verified: both files parse; helper extracts correctly from all 4 MMS notes + edge cases.

### FEATURE: persist QB export no-match rows + resolve on email arrival (2026-10-08)

A QB export row that matched NOTHING at upload (no candidate at all — the email hadn't arrived yet) is
now frozen instead of discarded, and resolved automatically when the matching email is later recorded.
Reuses the Tier-1 vendor+number+amount+store logic — no new scheduled task, no new UI.
- **New table `qb_unmatched_rows`** ([database.py](database.py), beside qb_match_candidates): freezes the
  QB-side fields incl. `qb_std_vendor` (resolved at parse time), `status` pending|resolved, and the
  resolved record link. Created via init_db (already applied).
- **`save_results()`** now returns `(protected, recorded)` — `recorded` = filenames that got a fresh
  invoice/credit_memo/payment row this run. All 3 callers updated (classify_emails ×2, migrate_from_excel).
- **`process_qb_export()`**: the Tier-2 `else` (no-match) branch now INSERTs the row into
  qb_unmatched_rows (qb_std already computed). *(2026-10-12: only rows that can still match are kept, and
  repeats are skipped. See "FIX + BACKFILL: QB 'no match' rows…".)*
- **`resolve_unmatched_for_new_records(filenames, actor)`** (new, [qb_match.py](qb_match.py)): for each
  newly-recorded, not-yet-entered, non-skip record, finds a pending unmatched row in the same store
  matching vendor+number+amount; marks the record entered_in_qb + the row resolved + edit_log. Called
  right after each `save_results()` via a LOCAL `import qb_match` (avoids the circular import, since
  qb_match imports classify_emails).
- **`recheck_pending_candidates()`** extended: also sweeps pending qb_unmatched_rows against the current
  eligible pool (symmetric case — vendor now resolves), folding its count into the flash summary.
- **Verified:** bounded self-reverting functional test (synthetic email+invoice+unmatched row, unique
  sentinel store/vendor so nothing real could match) — resolve returned 1, record entered, row resolved,
  edit_log written, then all synthetic rows deleted (0 remain). Also fixed a bug in the spec's edit_log
  INSERT (unterminated SQL string literal — added the closing quote).
- **Browse/dismiss page (added same day):** `/qb-reconcile/unmatched` lists frozen rows with a
  Pending/Resolved/Dismissed/All filter; **Dismiss** marks a row that will never arrive (status
  `dismissed`, app record untouched, logged), **restore** undoes it. web_db: `qb_list_unmatched`,
  `qb_unmatched_pending_count`, `qb_dismiss_unmatched`, `qb_restore_unmatched`. Routes:
  GET `/qb-reconcile/unmatched`, POST `…/{id}/dismiss` and `…/{id}/restore`. Template
  [qb_reconcile_unmatched.html](templates/qb_reconcile_unmatched.html); linked from the reconcile
  page. Verified with a bounded self-reverting test (list/dismiss/restore + 2 edit_log entries, 0
  leftover).

### FIX: loosen QB number-matching for cosmetic-only differences (2026-10-08)

`_num_eq()` in [qb_match.py](qb_match.py) now falls back to a **digit-core** comparison after the exact
check: `_digit_core(v)` = longest digit run with leading zeros stripped (`'INV-000146'`/`'146'` → `'146'`),
so vendor prefixes (`IN`/`INV`/`G`/`W00`/`SINV`), leading zeros, and trailing affixes (`1618282-IN`) no
longer block a match — while genuinely different numbers still don't match (digits must be equal once
extracted). Used by Tier-1 auto-match, Tier-2 detection, the statement pass, AND the existing recheck
button, so it covers new uploads and sweeps the current backlog in one click. `import re` added.
- **Refinement (same day):** `_digit_core` now strips punctuation (`re.sub(r"[^0-9A-Za-z]","")`)
  BEFORE finding digit runs, so an internal `/` or `-` doesn't split one number into two runs
  (`INV/2026/47830` → `202647830`, matching `INV/202647830`; `161230-9-IN` → `1612309`). Letters still
  separate, so a vendor prefix isn't glued onto the digits. Re-verified: the earlier Zahler
  `INV/2026/07773` vs `INV/2026/0773` STILL does not match (`202607773` ≠ `20260773`), and `2026/146`
  vs `146` does not match (no bare-vs-year-prefixed false positive). After this, 2 more currently-
  pending candidates resolve on the recheck button (both clean cosmetic cases).
- **OCR fold (same day):** `_digit_core` also folds the classic OCR confusions I/i→1, O/o→0 — but ONLY
  for a letter FLANKED BY DIGITS on both sides (mid-number noise like `86I0721`→`8610721`, Lantev), so
  a letter affix such as the `IN` in `1618282-IN` is left intact. (The spec's plain `translate` version
  regressed the `-IN` suffix cases — caught in edge testing and corrected to the flanked rule.) Verified:
  Lantev matches, `-IN` suffixes still match, Zahler still rejected; live sim resolves the pending Lantev
  card (119), 0 suspicious.
- **Tightened (2026-10-12): every digit run must agree.** The longest-run rule let letters inside a number
  be ignored, so `SUB2026M08` / `SUB2026M09` (a fixed-amount monthly bill) or `S1234A01` / `S1234A02`
  matched on `2026` / `1234` alone; a false Tier-1 match marks the wrong bill entered and the real one
  silently never shows for entry. Now `_digit_runs(v)` (same punctuation strip + flanked OCR fold, each
  run's leading zeros stripped) returns ALL runs and `_num_eq` requires the lists to be equal.
  `_digit_core` = the longest of them, still used only as `_num_bucket`'s index (equal numbers have equal
  run lists, so they still share a bucket). A number this turns away still reaches the review list on
  vendor + amount + date. Every documented case unchanged (`INV-000146`/`146`, `W00123`, `-IN`,
  `INV/2026/47830`, `161230-9-IN`, Lantev `86I0721`, `U-13944`, `R204825` match; Zahler and `2026/146`
  don't). Live replay: 0 of the 32 logged QB-marked records and 0 of the 771 `qb_match_candidates` change;
  0 same-vendor/same-amount/same-store invoice pairs in the app that the old rule conflated. Preventive.

### BUG (data-fixed, code fix pending decision): store-preference fails within a single run (2026-10-09)

The existing "keep the store-scanned copy, auto-ignore the vendor copy" logic (`check_duplicate` →
`store_preferred` / `store_conflict`, handled in `apply_duplicate_checks`) relies on
`find_live_invoice`, which queries the **DB**. When BOTH copies arrive in the **same classify run**, the
prior copy isn't persisted yet (save_results writes at run end), so the lookup returns None and the
handler falls to its `else` branch — mis-flagging the NEW store-scan copy as a suspect and leaving the
vendor copy live (backwards). Confirmed on invoice #1351 Authentic Health (3351 vendor / 3378 scan, same
run 20261008T160046); `duplicate_reason` even said "keeping THIS store-scanned copy… auto-ignoring the
earlier vendor copy" while the flags were reversed.
- **Data fix (applied):** superseded vendor copy 3351 (ignored), kept store copy 3378 (clean), shared
  group 6CB2C3E2, 2 edit_log entries. Only 1 record was affected.
- **Code fix — IMPLEMENTED (post-save backstop, Joel's pick):** `resolve_store_preferred_duplicates(actor,
  dry_run=False)` in [classify_emails.py](classify_emails.py) sweeps live invoice clusters sharing
  normalized vendor + number + amount + store; when >=1 member is a duplicate_suspect and EXACTLY ONE
  member is from a self-scan sender (sender_store_map) with >=1 vendor copy, it keeps the store scan and
  supersedes the vendor copy(ies) (shared group, edit_log). Skips two-scan / no-scan clusters. Never
  touches an entered/skip/ignored copy. Hooked into `main()` right after save_history (auto each run).
  Standalone [resolve_store_preferred.py](resolve_store_preferred.py) (dry-run default / `--commit`).
  Verified: self-reverting test (resolve + two skip cases, 0 leftover); real-data dry-run resolves 0.

### FIX: single record shown in /duplicates review (vendor-suffix grouping mismatch) (2026-10-09)

Two copies of one document whose vendor names differed only by a legal suffix ('Authentic Health' vs
'Authentic Health Inc', #1351) each appeared as their OWN one-record "duplicate set". Cause: the SQL
EXISTS in `list_duplicate_candidates` ([web_db.py](web_db.py)) clusters with `norm_vendor_dedupe`
(strips 'Inc'), but the Python `_key` regrouping used plain `.strip().lower()` — so it split the
already-pulled pair into two singletons. Fixed `_key` to use `classify_emails._norm_vendor_dedupe`
(byte-identical to the SQL fn), matching the cluster. Added a `len(recs) < 2` guard so a genuine lone
suspect (sibling gone / stale flag) never surfaces as a one-record set. Verified: the pair now renders
as one 2-record comparison; 0 singletons remain.

### UI: /browse Records table — compact rows + sortable Received/Sender (2026-10-09)

- **Compact rows** ([browse.html](templates/browse.html), scoped `.recs` class): the long sender email
  was `nowrap` and hogging width, squeezing Store/Vendor/Outcome into narrow wrapping columns. Now the
  Sender (~150px) and Outcome (~190px) truncate with ellipsis (full value on hover), Store/Vendor are
  `nowrap` (one line), row padding is tighter, and the **Flags** cell uses smaller pills/buttons.
- **Sortable Received + Sender:** added `"received": "received_at"` and `"sender": "sender"` to
  `_RECORDS_SORT` ([web_db.py](web_db.py)); the two `<th>` now use `sort_th`. Verified both directions
  run against live data (received oldest↔newest, sender A↔Z).

### FEATURE: auto-correct per-store vendor spellings from QB recon history (2026-10-08)

Learns vendor_name_map's per-store `name_<store>` spellings from `qb_match_candidates` history — what
QuickBooks itself calls a vendor at a store. Two confidence_reason buckets are evidence: **A** ("number
+ amount match; vendor did not resolve…") = strong, may fill OR override; **B** ("vendor + amount match…
number missing/mismatched") = weak, fill-empty only. Conservative rules (skip rather than guess):
non-rejected candidates only; >1 distinct qb_name in a bucket → skip that group; fill empty = any single
occurrence; **override existing = bucket A + ≥2 distinct batches agreeing** (a one-off QB typo in one
batch is excluded). Every change logged to edit_log.
- **qb_match.py:** `STORE_COL`, `_vendor_name_evidence(cur)`, `apply_vendor_name_corrections_from_qb_history(actor, dry_run=False)`;
  called at the end of `process_qb_export` (fresh connection, sees the just-committed batch) with a console summary.
- **[apply_vendor_name_corrections.py](apply_vendor_name_corrections.py)** (new): one-time catch-up for
  pre-existing history; dry-run by default, `--commit` to write.
- **Ran it:** applied **2** (PAS PACKAGING @ Madison `name_madison`→`Pharmacy Automation`; Bordova
  Innovations @ Lakewood Rx `name_lakewood_rx`→`Bordova Innovations Inc`); correctly **skipped** the
  R&T Dist. `R & T DIst.` typo (1 batch, override needs ≥2). 2 edit_log rows; re-run is idempotent (0 applied).

### FIX: /qb Copy Selected + Export CSV — 3 fixes (2026-10-08)

- **Copy Selected used the wrong columns** when a custom export layout differs from the on-screen view:
  `copy_columns` in [review_app.py](review_app.py) `qb_list` now resolves `view_columns if export_follow
  else export_columns` (+ include_refs), matching what `qb_export` actually produces.
- **Copy Selected now includes a header row** ([qb.html](templates/qb.html) `buildQbHtml` + `copyQb`):
  QB_VIEW labels prepended to both the HTML `<th>` row and the TSV (CRLF-separated, unchanged).
- **Export CSV now respects ticked rows**: `qb_export` gains an `ids` param (comma-separated
  `type:id` tokens) that filters `rows` to the selection; none ticked = full filtered set (unchanged).
  `exportQbClick(a)` in qb.html fills `ids` from the current selection just before navigating; caption
  updated. (/payments export left as-is — already has a header + fixed columns; mirror on request.)
- **Follow-up:** Copy Selected's **Account** column came out empty — the row `<tr>` was missing
  `data-account` (QB_DATA_ATTR maps Account→account, read from the row's data-attrs). Added
  `data-account="{{ r.qb_account or '' }}"`, matching the displayed Account cell. CSV export unaffected.

### FEATURE: manual per-record "Ignore" (exclude from QB) (2026-10-08)

Per-record toggle to exclude any invoice/credit_memo/payment from QuickBooks import, independent of
vendor-exclusion rules and duplicate detection. Record stays fully visible; drops off /qb + /payments
(which hard/soft-filter skip_qb_import). Reuses the existing `skip_qb_import` flag (confirmed never
flipped back to FALSE by any backfill) + two new audit columns to tell a manual ignore from a
vendor-rule skip.
- **Migration** ([database.py](database.py)): `ignored_by TEXT` + `ignored_at TIMESTAMPTZ` on invoices/
  credit_memos/payments. **View** ([web_db.py](web_db.py) all_records): the two columns APPENDED to each
  of the 4 arms (not mid-inserted — CREATE OR REPLACE VIEW only allows appending, can't reorder).
- **web_db:** `set_record_ignored(record_type, record_id, ignored, username)` — sets/clears
  skip_qb_import + stamps + edit_log. `list_payments` now also returns skip_qb_import/ignored_by/at.
- **review_app:** `POST /records/{type}/{id}/ignore` (ignored=1 ignore / 0 restore); `_record_detail_view`
  passes ignored_by/at through.
- **UI:** `outcome()` macro shows "Ignored by X" ahead of the vendor-skip line. Ignore/Restore on
  browse.html + record_detail.html; one-way **ignore** button (delegated handler) on qb.html + payments.html
  (Restore only on /browse + detail, by design). flags.html links out to the record page (no inline change).
- **Edge case (documented, not a bug):** editing the Vendor field re-derives skip_qb_import from the
  vendor name, so it can override a manual ignore. Verified: self-reverting toggle test (ignore→restore,
  2 edit_log toggles, view exposure, 0 leftover).

### DATA FIX + investigation: record #113 OCR misread on upside-down scan (2026-10-08)

Joel flagged invoice #113 (Marine Park Distribution / Maple / #231008): amount was $572, should be $72;
and the Drive PDF is stored upside-down. Root cause: a scanned invoice arrived upside-down and the LLM
misread it (same misread also produced raw vendor "Marine Ark" — though it fuzzy-resolved correctly to
standard_vendor "Marine Park Distribution").
- **Data fix (applied, verified):** amount 572 → **72**; raw `vendor_matched` "Marine Ark Distribution"
  → "Marine Park Distribution" (now consistent with standard_vendor). entered_in_qb was false. 2 edit_log
  entries (`manual correction — OCR misread on upside-down scan (Joel flag #113)…`).
- **Investigation (libs checked):** PIL / PyMuPDF(fitz) / pypdf available; **no Tesseract, pytesseract,
  cv2, or numpy** — so a deterministic OSD orientation backstop would need a new system dependency.
- **Upside-down PDF (fixed):** downloaded the Drive file, visually confirmed it was 180° inverted
  (reading it also CONFIRMED the data fix: Total $72.00, 3×$24.00, MARINE PARK DISTRIBUTION), rotated
  180° (lossless /Rotate), re-rendered to confirm upright, then replaced the Drive file **in place**
  via `files().update` (same fileId → `file_path` URL unchanged) and corrected the filename to
  `Maple - Marine Park Distribution - 231008 - $72.00 - 8.5.2026.pdf`. Verified re-download shows
  rotation 180 (upright).
- **Prevention — analyzed, fuzzy-flag NOT shipped:** the obvious no-dep idea (flag fuzzy-vendor matches
  for amount review) proved too noisy — fuzzy scores are 90-97% and MOST are legitimate recurring
  vendors (M.R. Health ×9, R&T ×8, Venture Respiratory ×5, …), indistinguishable by score from the
  OCR garble. Shipping it would repeatedly flag known-good vendors. Recommendation: the only robust
  backstop is deterministic orientation detection (Tesseract OSD), which needs Tesseract + pytesseract
  installed (not present). Deferred to Joel's call on adding that dependency; would implement + verify
  once installed (guarded no-op if absent).

### CLEANUP: orphaned account-assignment rows + Abbott invoice backfill (2026-10-08)

Post-merge tidy (both verified display-only — same QB account on both sides, so no routing change):
- Deleted orphaned `vendor_account_assignment` rows id 634 (`Abbott` → `Purchase:OTC Purchase`, target
  `Abbot Nutrition` same account) and id 92 (`Pharmacy Automation` → `Bags, Packacking & Labels`, target
  `PAS PACKAGING` same account — the leftover flagged in the PAS merge entry).
- Backfilled invoices 1604/1605 (Maple, not entered) `standard_vendor`+`qb_vendor` `Abbott` →
  `Abbot Nutrition` to match `resolve('Abbott')` after the Abbott→Abbot Nutrition merge; accounts stay
  `Purchase:OTC Purchase`. Confirmed `apply_vendor_standardization` produces the same values (name_maple
  = `Abbot Nutrition`). 6 edit_log entries (2 deletes + 4 fields). Bounded, dry-run-previewed, verified.

### FIX: merged duplicate vendor rows — Pharmacy Automation → PAS PACKAGING (2026-10-08)

Two vendor_name_map rows for one company: id 428 `PAS PACKAGING` (what the 7 live invoices use) and id
435 `Pharmacy Automation`. An alias can't change a row's OWN self-resolve, so `resolve('Pharmacy
Automation')` (the QB-side name) kept returning `Pharmacy Automation`, never matching the invoices.
Since 0 records depended on `Pharmacy Automation`, retired it: added it as an alias on row 428
(`edited_by='claude code'`), DELETEd row 435, logged both to edit_log (`manual correction — merged
duplicate vendor rows …`). Verified: `resolve('Pharmacy Automation')` and `resolve('PAS Packaging')`
both → `PAS PACKAGING`; 7 PAS PACKAGING invoices intact. (Harmless leftover: an orphaned
`vendor_account_assignment` row for `Pharmacy Automation` → same account `Bags, Packacking & Labels`;
never referenced now. Not removed.)
- **Verified read-only** (no writes): simulated `recheck_pending_candidates` under old vs new rule —
  **28** currently-pending candidates newly auto-resolve, **all with identical digit cores** (0
  suspicious), the other pending stay untouched. Unit tests pass incl. Zahler `INV/2026/07773` vs
  `INV/2026/0773` → NOT matched (digits differ), off-by-one → not matched, empty/no-digit → not matched.
- **To sweep the backlog:** click "Re-check pending matches against current vendor list" on
  /qb-reconcile/review once (Joel does this — it writes). Needs a web/CLI restart first.

### UI: character-level diff on QB match review cards (2026-10-08)

Each Tier-2 candidate's `confidence_reason` names exactly one differing field; the card bolds the reason
line and reddens **only the characters that differ** between the QB value and the app value (both
directions) — e.g. `U13944` vs `U-13944` reddens just the inserted `-`, not the whole value.
- **Backend:** new Jinja filter `diff_against(value, other)` in [review_app.py](review_app.py) —
  `difflib.SequenceMatcher` (autojunk off), escaped output, differing runs wrapped in
  `<span class="diffchar">`.
- **Template** ([qb_reconcile_review.html](templates/qb_reconcile_review.html)): Vendor/Number/Amount
  rows render the mismatching field's two cells through `diff_against` (Vendor diffs the name only, not
  the `(std_vendor)` parenthetical; Amount diffs the formatted `money()` strings). Substrings:
  `'vendor did not resolve'` → Vendor, `'number missing/mismatched'` → Number,
  `'amount differs beyond tolerance'` → Amount. Store (always same-store) and Date (±10-day proximity,
  not exact) stay unhighlighted. `.diffchar` = red text + light-red background.
- **Row marker:** the mismatching row's left label turns red and gets a red ▶ arrow (`.diffarrow`), so
  the problem row is obvious even when the char-diff is a single subtle character.

### FEATURE: "Re-check pending matches" on QB match review (2026-10-08)

Button on /qb-reconcile/review that re-evaluates EVERY still-pending doubtful candidate against the
CURRENT vendor_name_map — so adding a spelling alias resolves other same-vendor matches without
re-uploading. Reuses the exact upload-path matching (_std_eq/_num_eq/_amounts_equal — no new logic).
- **qb_match.recheck_pending_candidates(triggered_by):** per pending candidate, re-fetch its linked
  record from the SAME eligible pool the upload uses (not entered, not skip_qb_import, invoices-only
  not duplicate_ignored). Gone from the pool → candidate 'rejected' (stale). Still eligible and now an
  exact Tier-1 match (vendor+number+amount) → mark record entered_in_qb + edit_log + candidate
  'confirmed'. Else unchanged. Atomic (own txn). Returns a one-line summary for the flash.
- **review_app:** POST /qb-reconcile/review/recheck (run_in_threadpool, like upload) → flash on
  /qb-reconcile/review. **templates:** button + msg flash at top of qb_reconcile_review.html (shown
  when candidates exist).
- NOTE: operates GLOBALLY on all pending candidates. Verified the logic (a candidate resolves on a
  matching vendor, a candidate whose record became ineligible goes stale) then fully reverted the
  live rows the verification touched. Web needs a restart.
- Heads-up: ~697 of the current 769 pending candidates are already stale (linked record entered/skip/
  dup-ignored), so the first real Re-check will dismiss most of them as "no longer applicable" — that's
  correct cleanup, not data loss.

### FIX: qb_match.py matched skip_qb_import records (noise on QB match review) (2026-10-08)

The shared candidate query selected by store + entered_in_qb=FALSE only, so records the user marked
`skip_qb_import` (reference-only, never entering QB — e.g. the MMS payment breakdown) were still matched
against the export, adding noise to both the Tier-1 auto-mark and the Tier-2 doubtful queue even though
the outcome is irrelevant. Fix: added `AND skip_qb_import IS NOT TRUE` to the shared query (both invoices
and credit_memos have the column — no table branching). Excludes them from BOTH tiers. Verified
self-reverting: a skip_qb invoice that would otherwise Tier-1 match is now no-match (not auto-marked, no
candidate).

### FIX: qb_match.py crashed on any 'Credit' row (duplicate_ignored on credit_memos) (2026-10-08)

`process_qb_export`'s shared candidate query filtered `duplicate_ignored IS NOT TRUE` for both the
invoices and credit_memos paths, but credit_memos has no `duplicate_ignored` column (only invoices/
payments have the duplicate-suspect workflow) — so any export with a 'Credit' row (156 on the Maple
tab alone) threw psycopg2 UndefinedColumn and 500'd the whole /qb-reconcile/upload. Fix: made the
clause invoices-only (`dup_clause = " AND duplicate_ignored IS NOT TRUE" if table == "invoices" else
""`); every not-yet-entered credit memo is a valid candidate. Checked the rest of the shared qb code —
all other columns used on {table} (standard_vendor, entered_in_qb, entered_qb_by/at, number/date cols)
exist on credit_memos, so this was the only invoices-only assumption. Verified self-reverting: a Credit
row now Tier-1 auto-matches a credit memo (no crash); reverted.

### FEATURE: "Requests" — user-submitted app change/feature requests (2026-10-08)

Any user can submit a change/feature/bug request about the APP itself, with the same lifecycle as
Flags (pending → dismiss/approve → optional read-only AI investigation → build → done), not tied to
any accounting record.
- **Schema (web_db WEB_DDL):** new `change_requests` (title, description [UNTRUSTED], category, submitted_by,
  status pending|dismissed|approved_for_build|done, resolved_by/at). GENERALIZED the existing
  `investigations` table to serve both: `flag_id` made nullable (+ ALTER for existing DBs), new
  `request_id` FK, and a CHECK `investigations_one_subject` (exactly one of flag_id/request_id). One
  runner, different prompt — no second investigation engine. Migrated live + verified (constraint rejects
  both-set and both-null).
- **web_db:** add_change_request / get_change_request / list_change_requests (admin=all, else own; same
  LEFT JOIN LATERAL investigations pattern as list_flags) / set_request_status (_REQUEST_STATUSES) /
  build_request_prompt (mirrors build_fix_prompt: title+desc+category+findings+standard closing).
  `create_investigation(flag_id=None, request_id=None, triggered_by=None)` generalized.
- **investigate.py:** `build_prompt(kind, subject)` (new 'request' branch: feasibility/affected-files/risks,
  `<change_request>` DATA block with the same not-an-instruction guard), `_run(inv_id, kind, subject)`,
  `enqueue(kind, subject_id, triggered_by)`. Flag behavior unchanged.
- **review_app:** GET /requests (submission form + list, build_prompt prebuilt for approved), POST
  /requests/submit (any user), /{id}/dismiss|approve|done|investigate (admin). Dashboard "Change requests"
  tile + d['requests_pending']/['requests_approved']. flag_investigate updated to enqueue('flag', …).
- **templates:** requests.html (submission form on top, collapsible cards, 4-state pill incl. ✓ done,
  description body [no snapshot/open-record], investigation + build-prompt blocks). base.html nav: Requests
  added to the Review queue group.
- Verified end-to-end self-reverting (add/get/list-scoping/approve/build_prompt/investigation/constraint/
  request-prompt). All compile/parse; web needs a restart.

### FEATURE: statement exceptions split out of /review into own /statements page (2026-10-08)

Statement-reconciliation exceptions were unioned into /review at read time, cluttering the regular
review list; moved to their own page.
- **web_db:** new `statement_filter_options(run_id)` (distinct open-statement vendors; no store).
- **review_app:** `review_list()` no longer merges `list_open_statement_exceptions` (that block deleted;
  it now ONLY handles real review_queue rows). New `GET /statements` route — same assign-rank sort
  (mine/unassigned/others) + fix_gmail_link pass, filters Run/Vendor/Search/Assigned (no store, no
  show-answered). Dashboard tile split: "Review queue" (d['review'] only, with per-assignee breakdown)
  + new "Statement exceptions" (d['statement'] → /statements).
- **templates:** new `statements.html` (own table: Flagged/Vendor/Invoice#/Status/Why flagged/Source
  email/action — Store & Amount dropped, always "—" for statements; keeps the assign dropdown +
  Resolve/dismiss form). `review_list.html` lost the `{% if it.kind=='statement' %}…{% else %}…{% endif %}`
  wrapper — regular rows only. base.html nav: "Statements" added as first child of the Review queue group.
- Verified: both templates render via the app Jinja env (statements shows all 27 rows; review_list has
  no 📄 Statement pills; nav has /statements). All compile/parse; web needs a restart.
- Spec artifact updated to v4 (ST-05 rewritten for /statements + Updated chip; MN-01 tile; UI-02/05/06 +
  overview-table stale review-queue references fixed; "What changed" box).

### FEATURE: QuickBooks reconciliation import (qb_match.py + /qb-reconcile) (2026-10-07)

Reusable page to avoid double-entering invoices/credit memos already in QB, and auto-resolve stale
'missing' statement flags QB actually has. User uploads a QB register export (one worksheet per store)
before each QB-entry session.
- **Schema (database.py):** `qb_import_batches` (one row per upload + summary counts) and
  `qb_match_candidates` (Tier-2 doubtful matches: frozen QB-side fields + linked record_id). Migrated live.
- **qb_match.py `process_qb_export(file_bytes, filename, uploaded_by) -> batch_id`:** parses each store
  tab via openpyxl (NOT pandas), columns mapped by HEADER TEXT (Type/Date/Num/Name/Amount, case-insensitive,
  order-independent). Bill→invoices (Amount negative→abs), Credit→credit_memos (positive), other Types
  skipped. Candidate pool per row: same mapped store, entered_in_qb=FALSE, not duplicate_ignored. REUSES the
  app's own matching — `resolve_standard_vendor` (vs each record's stored `standard_vendor`), `_norm` (number),
  `_amounts_equal` (0.005). **Tier 1** (vendor+number+amount) → UPDATE entered_in_qb=TRUE + edit_log (actor
  '<user> via QB reconcile import', QB Num/Amount/Date in new_value). **Tier 2** (vendor+amount+date≤10d but
  number off; OR number+amount but vendor unresolved; OR vendor+number but amount off) → qb_match_candidates
  pending. **Statement pass** (whole workbook): a 'missing' statement_line with a Tier-1-quality Bill match →
  status='dismissed' + edit_log. STORE_MAP tab→store confirmed. Vendor-standardization loaded BEFORE the txn
  (init_db deadlock gotcha).
- **web_db.py:** qb_list_batches / qb_batch / qb_list_candidates (joins record fields for side-by-side) /
  qb_pending_count / qb_confirm_candidate (atomic: entered_in_qb + edit_log + candidate confirmed) /
  qb_reject_candidate. dashboard_counts gains `qb_pending`.
- **review_app.py:** GET /qb-reconcile (upload + history), POST /qb-reconcile/upload (xlsx only, threadpool),
  GET /qb-reconcile/review?batch_id= (summary + pending cards), POST .../{id}/confirm|reject. Dashboard
  "QB match review" tile. Nav: "QB reconcile" under QB entry.
- **templates:** qb_reconcile.html (upload + batch history), qb_reconcile_review.html (side-by-side cards,
  Confirm/Not-a-match — duplicates.html visual pattern).
- **Fixed during build:** invoice_date/credit_memo_date are TEXT (not DATE) — parse before the Tier-2 date diff.
- Verified end-to-end self-reverting (Tier-1 mark + Tier-2 candidate + statement dismiss, all reverted) and
  web_db reads. All compile/parse; web needs a restart.

### CHANGE: statement-recon cutoff now keys on STATEMENT ARRIVAL, not invoice date (2026-10-07)

`reconcile_statement_lines` pre_cutoff basis changed from the invoice's TRANSACTION date
(`sl.invoice_date < cutoff`) to the STATEMENT email's ARRIVAL date (`emails.received_at`, fallback
`date_added`, via a new `se` lateral join) — so a late-arriving invoice (old printed date, emailed
AFTER go-live) on a post-go-live statement is now correctly flagged 'missing' instead of silently
suppressed. Pure statement-arrival was too noisy (missing 8→115; 94 of them genuinely-ancient
2022–2025 balances vendors carry forward), so added a HYBRID guard: also pre_cutoff any line whose
`invoice_date` is >60 days before go-live (`CROSS JOIN golive`, `sl.invoice_date < golive.d0 -
interval '60 days'`). Result: **missing 8 → 52** (21 dated on/after go-live + 31 late-arrivals dated
within 60d before it; oldest now 2026-06-08, no pre-2026 junk). Ran reconcile so the DB reflects it.
Reversible: drop the `se`/arrival condition to restore transaction-date behavior, or tune the 60-day
window. Takes effect every reconcile (classify run + web /review load); web picks up the pinned
go-live after its next restart.

### CHANGE: go-live date PINNED to 2026-08-07 (2026-10-07)

The system's first real record is **2026-08-07** (earliest `date_added` across invoices/credit_memos/
payments; 0 records before it — verified). Pinned `STATEMENT_GOLIVE_DATE=2026-08-07` so statement
reconciliation's cutoff is fixed at the true start and can't drift (it otherwise derives go-live from
the earliest `runs.started_at`, which would shift if early runs were deleted). Set three ways:
`setx` (persistent, all future processes) + a `set "STATEMENT_GOLIVE_DATE=2026-08-07"` line in BOTH
launchers (`Fetch New Invoices.bat` → run_all.py, and `Start Invoice Web UI.bat` → run_web.py) since
`reconcile_statement_lines` is called from both the classify run AND the web app. Verified the value
flows into the reconcile cutoff. NOTE: 2026-08-06 is the purge tool's `--before` boundary (one day
before first record); 2026-08-07 is the actual first record and the correct go-live. Operational split
for manual vs system: on/after 2026-08-07 the system has it; on/before 2026-08-06 → handle manually
(the Gmail scanner's `newer_than:30d` window can't reach pre-go-live mail now anyway).

### CHANGE: fetch default 100→200 + pagination (2026-10-07)

`gmail_fetcher.MAX_RESULTS` default raised 100→200 (still overridable via `GMAIL_MAX_RESULTS`), and
`fetch()` now PAGINATES: `MAX_RESULTS` is the TOTAL across pages, collected via `nextPageToken` with
≤500 per page (Gmail's per-page max). Previously a single `list` call silently capped any run at 500;
now a cap above 500 actually fetches that many (e.g. 650 → pages of 500+150), and it stops early when
there's no next page. Per-run cap still self-paces (fetched emails are labeled; rerun for the next batch).
Verified: default=200, env override honored, page-size math correct.

### FIX: classify hung forever — anthropic SDK (0.116.0) ignores timeout; route via raw httpx (2026-10-07)

Every classify run froze silently on the FIRST API call (any email). Isolated it: a direct httpx POST
to /v1/messages with the same key + ca_bundle returned HTTP 200 in ~2s (test_api_raw.py), while the
anthropic SDK's `client.messages.create` hung indefinitely even with explicit client- AND per-call
timeouts (test_api.py). Worked the day before with no code change → an auto-update of `anthropic`
(0.116.0) / `httpx` (0.28.1) regressed the SDK's timeout handling with a custom http_client (the
custom client is REQUIRED — the ca_bundle is the corporate proxy's TLS; default certifi fails SSL).
- **Fix:** new `_create_message(client, *, model, max_tokens, system, messages, timeout)` sends the
  request via raw `httpx.post` (verify=ca_bundle, honored timeout) and returns a SimpleNamespace shaped
  like an SDK response (.content[].type/.text, .usage.*, .stop_reason). `classify_email` now calls it
  instead of `client.messages.create`; the oversized-scan path goes through classify_email too, so both
  are covered. Request/response schema identical (document blocks are already plain raw-API dicts;
  system keeps cache_control ephemeral → prompt caching still works). Non-200 is raised with wording
  that `_is_auth_error`/`_is_invalid_request_error` still classify; httpx timeouts/connect errors hit
  classify_with_retry's (now visible) retry.
- Also added earlier this session: client-level timeout + a visible retry print on the exception path
  (so a stall is never again a silent freeze), and a running `(i/total)` counter on the progress line.
- Verified: test_api.py (the real `_create_message` path) → SUCCESS ~1.7s; full run processes normally.
- Diagnostics left in repo: test_api.py (SDK/new-path check), test_api_raw.py (raw-httpx check).
- FOLLOW-UP (optional): pin `anthropic` to a known-good version so a future auto-update can't reintroduce
  this; not required since the pipeline no longer depends on the SDK for the call.

### FIX: late-flagged review items had no document preview (2026-10-07)

**Symptom:** 29 open review items showed "No source file recorded for this item." Their row pointed at the
WHOLE source email (`filename` = source email, `file_path` blank), so answering one reprocessed every record in
that email: on the 8-invoice TopRx email, answering one item regenerated all 8 rows (wiping their answers and
assignees), and two answered rows from the same email were reprocessed twice in one run.

**Root cause (two paths, same "decided too late" class as the 2026-10-06 gate fixes):**
- **Vendor/account check (23 items).** `apply_vendor_standardization()` (no vendor mapping / no account) ran only in
  `build_result_rows` — AFTER `assign_invoice_files` had filed the record's document under `invoice_files/` as a clean
  record and after `save_review_files` had decided split files. `apply_review_gates` (2026-10-06) covered the
  store/date/amount/Carepax gates but not this check. Each rerun also left another orphan copy in `invoice_files/`.
- **Released sales orders (6 items).** A sales order the AI flagged while it was held got no document at all (flagged →
  no invoice file; held → no review file). When `reconcile_sales_orders` released it into review, the row had nothing.

**Forward fix (`classify_emails.py`):**
- New `apply_vendor_standardization_gate(rec)`, called in `apply_review_gates` for invoices + credit memos after the
  amount gate. It only decides the flag: a vendor that resolves cleanly is left to the record-time call (which fills
  standard/QB names as before); it flags early only when the check would flag AND the vendor isn't excluded
  (excluded vendors keep today's record-time path: deduped, then review-suppressed). `is_excluded_vendor()` (a DB
  read) is consulted only in the would-flag case. A flagged record now gets its own `review_pending/` split file.
- Backstop for anything still flagged late (released sales orders, any future record-time check):
  `route_dropped_to_review` gives each genuinely late record (no `_review_attempted`) its own `review_pending/`
  document via new `_late_review_document()` — a COPY of the document it already got (`invoice_files/`, or the
  local copy behind a Drive link: `upload_record_files` now keeps `_local_file_path`), else re-derived from the
  source email (`_find_source_file`: test_emails/ → review_pending/ → processed_emails/) with the record's own
  `source_document`/`source_pages`, only when that document is in the email (never a stand-in for a portal page or
  manual upload). Returns `{late, documents}`. `save_review_files` stamps `_review_attempted` on records it routed,
  so a logged generation failure isn't retried. The run log now counts only truly late records ("Routed N record(s)
  to review that a record-time check flagged after dedup (… M given their own review document)") — the old line
  counted every flagged record.
- **Doubled reason text fixed.** The gates run twice (classification time + the record-time safety net) and each
  appended its sentence both times — 24 open items showed e.g. "No invoice date found" twice. New `_append_text()`
  (append once) is now used by the store/date/amount/Carepax gates and `apply_vendor_standardization` (both review
  sentences and the fuzzy-match audit note).
- **Intended side effects:** vendor-mapping flags now skip the duplicate check like any flagged record (before,
  dedupe ran first, so a store_conflict/store_preferred match could auto-ignore an earlier live copy just before the
  new copy went to review); `enrich_flagged_with_duplicate_info` adds the "Multiple versions" notice instead. No more
  orphan `invoice_files/` PDFs or Drive uploads for such records.

**Backfill — `fix_review_previews.py` (dry-run default, `--commit`; RAN 2026-10-07).** For each open row with an empty
`file_path` and `filename` = source email: (1) released sales order → rebuild the document from the held order's
snapshot attribution; (2) else copy the document filed at flag time in `invoice_files/` (name match; never a file a
recorded record references; identical copies are interchangeable; when copies DIFFER, only a copy whose pages trace
back to the row's own source email by per-page image fingerprint); (3) else the email's single attachment, as a
preview only. Rows with their own document become split items (`filename` + `file_path` → the new `review_pending/`
copy); all-or-nothing per source email. Result: **25 converted to split items, 4 preview-only, 0 left.** The 4 are the
first TopRx email (1a0aa4ae…): 2 of its records had no filed copy, so all 4 keep whole-email reprocess and got
previews (2 their own page, 2 the full 6-page attachment). 54 `edit_log` rows (`changed_by = 'fix_review_previews'`);
nothing deleted, orphan copies left in place. Page tracing caught a real mismatch: Nutranext CS0107488 had 6
differing copies from 2 emails, and "newest copy" would have shown #3231 the store scan's version.

**Verified:** 29 self-contained tests (no API, no DB writes; real TopRx email with a stubbed classification): appends
once; the early gate for mapped / unmapped / no-account / excluded / unseeded vendors; end to end, unmapped records get
split files holding only their pages, nothing filed under `invoice_files/`, review rows point at the splits, a mapped
vendor and an excluded vendor in the same email record exactly as before, a sales order stays held; an old-path
simulation (early gate disabled) where the backstop copies the `invoice_files/` documents; rebuild from the source
email; no stand-in for a portal document; attempted records left alone; Drive local path kept. Live, read-only after
the commit: 0 open items without a document; 29/29 previews served by the web app (TestClient GET of the static
path → 200 + real PDF bytes); every split item's reprocess file exists. Takes effect on the next classify run; the
web app needs no restart (no web code changed).

**Found while fixing, not changed** (see §10): a reprocess dropped `entered_in_qb` on the email's records (fixed the
same day — next section); released sales orders that were flagged at hold can cycle back into review; orphan copies
remain in `invoice_files/`.

### FIX: a reprocess keeps "entered in QuickBooks" (2026-10-07)

**Problem:** `save_results` replaces an email's records by deleting every row under its `email_filename` and
re-inserting the new ones, and `_insert_invoice` / `_insert_credit_memo` / `_insert_payment` never write
`entered_in_qb` / `entered_qb_by` / `entered_qb_at`. So reprocessing an email whose records were already keyed into
QuickBooks (a deliberate requeue, a whole-email review item, a sales-order release, a churned-filename supersede)
brought them back on /qb as "not entered" — a double-entry risk. `REPROCESS_PROTECT_MINUTES` only covers the last 10
minutes of human activity. Worse case: an entered record that a stricter gate now sends to review is deleted, and
when the item is resolved later it is recorded fresh as "not entered".

**Fix (`database.py`, additive; every insert/delete goes through `save_results` — main, `reconcile_sales_orders`,
`migrate_from_excel.py`):**
- New pipeline table **`qb_entered_marks`** (DDL, created by `init_db`; applied live): one row per entered record a
  reprocess deleted — type, match keys (`norm_vendor_dedupe` vendor, upper/trimmed number, amount to the cent, date),
  the original `entered_qb_by/at`, the deleted row id, and once matched `restored_record_id/restored_at/restore_note`.
  Partial index on open marks.
- `save_results`: before the DELETE, `_mark_entered_rows()` copies the touched emails' entered rows into marks; after
  the inserts, `_restore_entered_marks()` gives every OPEN mark (this run's, or an earlier run's whose record went to
  review) back to the matching just-recorded row: entered TRUE with the ORIGINAL who/when, the mark closed, an
  `edit_log` row (`changed_by = 'reprocess:keep-entered'`, new value "True (carried over from record #N)").
  - Match = same record type + vendor + number + amount; when there's no number (no-reference payments) the date
    must match too. A live copy wins over an ignored duplicate. Only ever sets entered TRUE.
  - Same type + vendor + number but a DIFFERENT amount → still the same document and it IS in QuickBooks, so the
    status comes back, with a note on the record ("[Entered in QuickBooks as $X before a reprocess read $Y — check the
    QuickBooks entry.]") and a `QB-ENTERED WARNING` line in the run log.
  - An entered record this reprocess did NOT re-record keeps an open mark and prints a `QB-ENTERED WARNING` naming
    it; its status re-attaches whenever it's recorded again (in this run or a later one).
  - Both steps run inside a savepoint (`_entered_step`): a problem in the safety net is printed and skipped, never
    blocks saving the records.
- Run log: `QB-ENTERED: kept 'entered in QuickBooks' on N re-recorded record(s): …` plus the warnings above.

**Verified:** self-reverting live-DB test (test-only filenames + a fake vendor; every record, email, mark and
edit_log row it created deleted afterward, confirmed 0 left) — 17 checks: an identical reprocess replaces the rows
(new ids) and keeps entered + who/when on the invoice, credit memo and the right no-reference payment (matched by
date); a never-entered record stays not entered; edit_log rows written; all marks consumed; an amount change keeps
the status with the note + warning; a record sent to review gets an open mark + warning and comes back ENTERED when
recorded later under another file; the live copy beats an ignored copy; the 10-minute reprocess-protect still skips
a just-entered email; a simulated safety-net failure is reported and the records still save. Takes effect on the
next classify run (schema already applied); no web restart needed (web code unchanged). Live today: only 2 invoices
are marked entered, so this matters more as the Entered-in-QB toggle gets used.

### FIX: re-flagged review items keep their identity — assignee, notes, uploads, source email (2026-10-07)

**Problem:** when an answered (or uploaded / retried) split item is reprocessed and comes back still flagged,
`build_review_rows` writes a FRESH queue row. That row (1) started unassigned, and (2) took its "Source Email" from
the file it was read from — the split PDF, not the original email. Because the review key is built from the source
email, the fresh row also got a NEW key, so its internal notes and uploads no longer attached; on the next retry
`ORIGINAL_EMAIL` and the From/To/Cc header recovery (`PROCESSED_DIR / Source Email`) pointed at a file that doesn't
exist, so sender/contact store hints were lost; and the item's "records" link and Gmail receive time went blank. A
re-flag that produced no new split file of its own (record-less, or the document couldn't be produced) also pointed
at the old split, which the end-of-run cleanup then deleted. Latent: 0 open, ignored or tracked rows show it today.

**Fix (`classify_emails.py`):**
- `build_review_rows` separates the file a result was read from (`unit_file`, still used for Filename / File Path when
  there's no new split) from the original email (`source_email = _record_source_id(unit_file)`, i.e. `ORIGINAL_EMAIL`),
  which now drives "Source Email", the review key and the Gmail link. Phase-2 (first-run) rows are unchanged.
- New `carry_review_assignments(new_rows, old_rows, unit_file)`, called in `main()` as the new rows are built: a fresh
  row takes the assignee of the old row with the same review key, else the one assignee shared by the old row(s) of
  the reprocessed file (covers an answer that changed the vendor or number); never guesses between two assignees and
  never overwrites one. Run log: "Kept the assignee on N re-flagged review item(s)."
- New `_delete_unused_review_files(paths, queue_rows)` replaces the end-of-run delete loop: a superseded
  `review_pending/` file is kept if any queue row still points at it (Filename or File Path).

**Doc fixes:** `apply_vendor_standardization()` no longer says a flagged record is "still recorded normally" (it goes to
review); `investigate.py`'s header no longer mentions `--permission-mode plan` (not used — the `--allowedTools`
whitelist is the read-only guarantee).

**Verified:** 15 self-contained checks (no API, no DB writes; real TopRx email, stubbed classification): first-run rows
unchanged; a re-flagged split reprocessed through the real `process_source_email` keeps the original email as Source
Email, the same review key, the Gmail link, gets its own new split file, and keeps its assignee; key-changed fallback,
no guessing between two assignees, no overwrite, nothing carried from an unassigned item; record-less and no-document
re-flags keep their file; the cleanup deletes the superseded file and keeps referenced ones. The 29-check preview suite
still passes. Takes effect on the next classify run; no web restart needed.

### FEATURE: "Assign to" on statement-reconciliation rows in the Review queue (2026-10-06)

Statement-exception rows (grouped one-per-statement, built at read time from statement_lines) can now
be assigned to a user like regular review_queue rows. statement_lines persists across runs, so the
assignment sticks with no carry-over logic.
- **Schema:** `statement_lines.assigned_to TEXT` (CREATE TABLE + `ALTER … ADD COLUMN IF NOT EXISTS`;
  applied to live DB).
- **web_db:** `list_open_statement_exceptions(assigned=None)` selects `sl.assigned_to` and filters on it
  (same `__unassigned__` sentinel as the regular queue); `_statement_group_row` returns `assigned_to`
  (first line represents the group); new `assign_statement_lines(line_ids, assigned_to, username)` sets/
  clears all lines of a statement, per-line edit_log audit (mirrors assign_review / dismiss_statement_lines).
- **review_app:** review_list guard relaxed from `if not store and not assigned` → `if not store` (statements
  carry no store so a store filter still excludes them, but they're assignable now so the assigned-to filter
  is passed through, not hidden); new `POST /statement-line/assign` route (mirrors /dismiss-group).
- **template (review_list.html):** assign dropdown + 👤 pill in the statement row's status cell, same control
  the regular rows use. Mine-first/unassigned/others sort already reads `it.assigned_to` generically — no change.
- Verified self-reverting: assign → group row + `assigned` filter reflect it, `__unassigned__` excludes it;
  reverted + test edit_log cleaned. All compile/parse; web needs a restart.

### MMS payment cleanup: skip-QB reference-only payments + dedup root-cause (2026-10-06)

Three related items on the Micro Merchant Systems payments (ref ARC_52AFLAM16TUJ).
- **Item 1 — keep 261/262/263/264 out of QB (reference-only).** Only payment id 69 ($5,721.48, the
  real ACH bank receipt) is cash to enter; 261 ($5,483.06) + 262 ($238.42) are the per-invoice
  remittance breakdown from the invoice email (same money as 69), and 263/264 the exact resend. Set
  `skip_qb_import=True` + note ("Same cash movement as payment id 69 — reference only, do not enter")
  on all four (audited to edit_log as 'mms-cash-cleanup'); id 69 left untouched/enterable.
  **Gap found + fixed:** the Payments queue (`list_payments`) and the dashboard "payments" count did
  NOT honor skip_qb_import (payments were historically always-entered) — so 261/262 would still show.
  Added `show_skipped` to list_payments (hidden by default, like show_ignored; reachable via a new
  "Reference-only" toggle on /payments and `?skipped=1`), threaded through payments_page + export, and
  added `AND skip_qb_import IS NOT TRUE` to the dashboard count. Verified: 261-264 off the to-enter
  queue, id 69 still on it, 261/262 reachable with the toggle.
  **Also un-staged** the two MMS source emails (were still in test_emails/ from the earlier reprocess
  requeue) → moved to processed_emails/, status processed, so a reprocess can't recreate 261-264
  without the skip_qb decision. Reprocess had no remaining benefit (invoices recorded, dedup resolved).
- **Item 2 — dedup asymmetry root-caused.** Why 263 auto-ignored but 264 only flagged "possible":
  within email A the 2nd line (262, $238.42) shares the SAME reference as 261 but a different amount, so
  `check_duplicate_payment` treats it as a SAME-SOURCE "self" match and does NOT add it to payment
  history. So email B's resend 264 ($238.42) finds no exact twin in history (only 261 $5,483.06, same
  ref) → "possible"; 263 ($5,483.06) matched 261 exactly → "duplicate"/ignored. Reproduced empirically.
  The `invoice_number` disambiguation already added IS the fix: with it populated, 262 vs 261 is NOT a
  self-match (different invoice#) so 262 enters history and 264's resend auto-ignores — proven
  empirically (both resends → duplicate). No code change; reference-only fallback keeps the old
  behavior by design (can't tell two legit lines from one scanned twice without invoice#).
- **Item 3 — invoice_number extraction is fully wired** (confirmed: payments JSON-schema field +
  dual-record reinforcement in the system prompt + `Applied Invoice #` threading in build_result_rows
  and _insert_payment + the dedup match). 261-264 show NULL only because they were written by run
  115328, BEFORE that change landed; they won't be reprocessed (un-staged, and they're skip_qb anyway).
  Future MMS-style emails will populate it. No code change needed.

### FIX: manual-import emails invisible to reprocessing-dedup (gmail_message_id) (2026-10-06)

Manual-import `emails` (source='manual', ~Aug 7–Sep 1, filenames like "Maple - <vendor> - <#> - $<amt>
- <date>.pdf") were created with `gmail_message_id` NULL even though the record's own `gmail_link`
encodes the real Gmail message id (…#all/<id>). The skip logic keyed only on filename, so when the
Gmail scanner later re-pulled the SAME message it got a NEW filename ("Maple Scanner - <id>.eml"), no
matching gmail_message_id on file, and reprocessed it as a brand-new record. 111 manual invoices were
resolvable to a message id; 13 had been re-pulled by run 20261006T140127 (4 already auto-dismissed, 1
Vita Vocal under review, 4 live un-flagged dups, 3 produced no invoice / in review).
- **Forward fix — backfill:** `database.backfill_gmail_message_ids()` fills `emails.gmail_message_id`
  from a matching invoices/credit_memos/payments `gmail_link` (regex `#[^/]+/([0-9a-fA-F]+)`), idempotent.
  Ran it: **111 email rows filled** (22 manual rows have no record-link id and stay NULL — nothing to do).
- **Forward fix — skip gate:** `database.email_message_ids()` + Phase-2 skip now also skips a file whose
  gmail_message_id was already handled under a DIFFERENT (processed) filename, and ARCHIVES it
  (pending_moves → processed_emails/, stamped `duplicate_repull`) so it doesn't linger. Verified: both
  confirmed re-pull filenames (1a07d56d → HE-013809, 1a04558e → Vita Vocal) now hit the mid-skip; a
  genuinely new mid does not. Filename-based skip unchanged.
- **2996 investigation (per the briefing's "investigate before dismissing"):** `check_duplicate`'s logic
  is sound — it DOES flag 2996 as a duplicate when the original ledger row (hid=170432, from gmail copy
  1308, msg 1a06276b) is present (empirically reproduced). The run-140127 miss could NOT be reproduced
  from current state (ledger row predates the run; same run also mis-extracted 2998's vendor as "Maple
  Pharmacy", so that run's extractions were noisy) — most likely transient, not a standing bug. The
  gmail_message_id gate makes it moot: the re-pull (msg 1a07d56d, == manual import 1591's backfilled id)
  is now skipped before it ever reaches dedup.
- **Cleanup (per spec, via `set_duplicate_ignored`, audited as 'manual-import-cleanup'):** ignored 2993
  (→1276, keep $1,365), 2990 (→709), 2991 (→702), 2998 (→2091), 2996 (→1591). Kept copies stay live.
- **HE-013809 resolved:** all three copies had ended up ignored (1308 + 1591 were already ignored before
  today). Per Joel, RESTORED id 1308 (the gmail original) — un-ignored + cleared its suspect flag — so it's
  the single clean live copy (now on /qb for entry); 1591 + 2996 stay ignored pointing at it. 1 live copy.
- No schema change; backfill + skip-gate take effect immediately / next run. web unaffected.

### FEATURE: sitewide maintenance notice + reprocess-overwrite backstop (2026-10-06)

Warn active users before a disruptive op (code restart, or a bulk reprocess/recovery that
overwrites existing live records), and hard-protect records a human just touched.
- **Schema:** `system_notices` table (message, created_at, active_until, created_by). Only the
  latest still-active row matters. Migration applied to live DB.
- **Functions (canonical in `database.py`, thin wrappers in `web_db.py`):** `set_notice(message,
  minutes, user)` (minutes None/≤0 = until cleared, else auto-expires), `clear_notice(user)`,
  `get_active_notice()`. Verified: set → supersede → until-cleared → clear lifecycle.
- **Web:** `render()` injects `notice` on every page (first-load banner, no flash); `GET /api/notice`
  poll endpoint; base.html shows a yellow banner + polls every 20s so someone already on a page sees
  it appear/disappear without navigating. Admin-only `POST /admin/notice` + `/admin/notice/clear`,
  with a control on the dashboard (admin-only). `role=='admin'` gated.
- **CLI `notice.py`:** `python notice.py set "…" --minutes 5` / `python notice.py clear` (imports
  database). STANDARD PROCEDURE before a restart or a bulk reprocess that touches EXISTING records:
  set notice → wait ~5 min → do it → clear. A routine classify run on BRAND-NEW emails doesn't need it.
- **Hard backstop (`save_results`):** before the reprocess delete-and-replace, `_recently_human_touched`
  finds touched emails whose records a human acted on in the last `REPROCESS_PROTECT_MINUTES` (=10) and
  SKIPS them (not deleted, new rows not inserted; left exactly as the human left them), printing a
  `REPROCESS-PROTECT:` line and returning the skipped list. **Signals chosen deliberately:**
  `entered_qb_at`, `duplicate_ignored_at` (each has a username companion, no system writer) and a
  recent `flagged_reviews` row (always human). **`edited_at` is EXCLUDED** — the pipeline itself writes
  it (apply_vendor_exclusion / needs_review backfill at classify_emails.py:1095,1151), so it's not a
  reliable "a human touched this" signal. Verified self-reverting: recent entered_qb_at + fresh flag
  protect; a 2-hour-stale entry doesn't.
- All files compile / parse; schema migrated live. Web changes need a restart; the save_results
  backstop + notice.py take effect immediately (next run / next CLI call).

### FIX: paid invoices recorded payment-only + shared payment ID mis-deduped (MMS) (2026-10-06)

MMS-style emails whose PDF shows a paid invoice WITH a "Payment against invoice #X" remittance line were
recorded as payment-only (no invoice), and a single payment ID applied to MULTIPLE different invoices
mis-matched during dedup — the only key (vendor + reference_number) can't tell "same payment, same invoice,
re-sent" from "same payment ID covering two different invoices". Confirmed case (FW Invoices from Micro Merchant
Systems — 1a01ab42…/1a020b69…): payment ARC_52AFLAM16TUJ settled TRINV-685027 ($5,483.06) AND CINV-662750
($238.42); the re-forward's $238.42 (264) flagged "possible" against the $5,483.06 row (261) instead of
auto-ignoring vs its true twin (262). Joel: record BOTH invoices normally (not skipped for being paid) + the
payment(s); the exact re-forward should then self-resolve.
- **Schema:** `payments.invoice_number` + `payment_history.invoice_number` (DDL + `ALTER … ADD COLUMN IF NOT
  EXISTS`, migration applied to live DB + verified). `_insert_payment` writes it from the result row key
  `"Applied Invoice #"` (payments already use `"Invoice #"` for reference_number, so the applied-invoice number
  gets its own key). load/save_payment_history (DB + xlsx fallback + PAYMENT_HISTORY_FIELDNAMES) thread it through.
- **Prompt:** new payments-schema field `invoice_number` (distinct from reference_number); and an explicit
  dual-record trigger after the "invoice AND CONCRETE payment" rule — a paid invoice showing its own remittance
  is a paid INVOICE (record the invoice AND a payment with invoice_number set); one payment settling MULTIPLE
  invoices → one payment record per invoice, each with its own invoice_number, never collapsed/payment-only.
- **Dedup (`check_duplicate_payment`):** when BOTH current + prior state an invoice_number, it must also match
  before a ref match counts (`inv_num_conflicts` guards the `ref==pref` branch); falls back to reference-only
  when either side lacks one. Verified with a 4-case test (re-forward of each line auto-ignores its true twin;
  same-ref different-invoice within one email → none; no-invoice-number back-compat → unchanged).
- **Recovery (item 4):** the invoices were ALREADY recorded by run 115328 (2925/2926, 2927/2928) — only the
  payments lacked invoice_number and 264 was mis-flagged. Requeued both emails (→ test_emails/, status fetched);
  Joel's next `classify_emails.py` re-extracts with invoice_number and 264 auto-ignores vs 262. Needs the run.
- **Item 5 (show invoice_number on /duplicates + /payments) — DEFERRED** (nice-to-have): would need a column
  added to every arm of the shared `all_records` view; not worth the ripple risk for display polish right now.
- classify_emails.py + database.py compile; schema migrated live. Prompt/dedup take effect on the next run;
  the two requeued emails record correctly then.

### FIX: /duplicates pulled in skip_qb multi-store cost-split invoices (2026-10-06)

Sageteq's monthly multi-store cost-split invoice (one #, split per store with different amounts, every record
`skip_qb_import=TRUE` per the cost-split prompt rule) showed up as a "possible duplicate" set even though none
of its per-store records enter QB individually. `list_duplicate_candidates` qualified a flagged invoice copy
via a VENDOR-level check (`_excluded_vendor_sql("exclude", "f.vendor_matched")`) — but Sageteq isn't globally
excluded; only these specific records are skip_qb at the record level, so the vendor check missed them.
**Fix:** check the record's own column instead — `f.record_type = 'payment' OR f.skip_qb_import IS NOT TRUE`
(the `excl`/`_excluded_vendor_sql` call, used only here, removed from the function). `apply_vendor_exclusion`
already sets `skip_qb_import=TRUE` for every Setup-excluded vendor, so this is a strict superset — no coverage
lost, and it additionally catches cost-split (and any other per-record skip). Verified: Sageteq #29479 (7
records, all skip_qb, 6 suspect) no longer appears; the only remaining cluster (Rockland Cosmetics #34968,
both skip_qb=FALSE) still shows; excluded-vendor duplicates stay excluded. Needs the pending restart.

### Dashboard: consolidate tiles pointing at the same page (2026-10-05)

13 tiles → 9: merged the three groups that linked to the same href — "Review queue" + "Statement exceptions"
(→ one **Review queue** tile, count = review + statement, note breaks out each, per-assignee breakdown kept);
"QB entry — invoices" + "QB entry — credit memos" (→ **QB entry**, count = invoices + credit memos, note
carries the vendor/account-setup sub-count); "Flags — pending" + "approved for fix" + "Investigations" (→
**Flags**, summed, note breaks out each). Tiles-list-only change in `dashboard()` (no new queries — all counts
already in `dashboard_counts`). Zero-dimming unchanged (a merged tile dims only when all its parts are 0).
Verified sums (23+6=29, 850+20=870, 0+0+1=1). Needs the pending restart.

### Recover EuroPharma + fix the "extracted-but-never-recorded" gap class (2026-10-05)

EuroPharma INV0800123 ($3,152.40, Refuah, 9/18) was correctly extracted into invoice_history on 9/22 but
never became a live record across 3 manual retries; its emails row stayed status='error' for 2 weeks. All 4
asks implemented:

1. **Recovered** (confirmed with Joel first: NOT yet entered in QB → `entered_in_qb=FALSE`). Built the invoice
   via the SAME path a normal run uses (`database.save_results` → `_insert_invoice` + `_upsert_email`), with
   vendor standardization (NULL here — EuroPharma isn't in vendor_name_map, so it shows an "add vendor" pill
   on /qb exactly like any unmapped vendor, not a special-case blank). `file_path` → the unsuffixed saved PDF;
   deleted the `__2`/`__3` redundant copies; `duplicate_group_id` left NULL (didn't reuse orphaned 841B7611,
   also cleared it off the history row); emails row → status='processed' + recovery run_id; logged to edit_log
   as `__recovered__`. Now invoice **#2836**, displays normally on /qb + /browse. (Gotcha hit + fixed:
   `ce.load_vendor_standardization()` calls `init_db` which takes ACCESS-EXCLUSIVE locks — must run it before
   opening any long-lived transaction / use an autocommit connection, else deadlock.)
2. **Status-update safety net** (`database.clear_stale_error_status`): wired into `main()` after save — any
   email successfully handled this run (classified, non-error) that's still stuck on status='error' is cleared
   to 'processed'. GUARDED on status='error' (never disturbs other rows). Closes the gap where an email
   handled via a path that didn't reach `save_results`/`_upsert_email` stayed 'error' forever.
3. **Reconcile check** (`web_db.orphaned_history_records` + `dashboard_counts["orphaned_history"]` tile +
   `/reconcile` page): surfaces ledger rows (invoice_history/payment_history) with no matching live record
   (norm-vendor + number). Self-reverting test confirmed it catches a deliberately-induced orphan.
4. **Proper recovery path** (`recover_errored_email.py`): requeues a status='error' email (move file →
   test_emails/, status→'fetched', delete its orphaned ledger rows so dedup doesn't shadow the reprocess) so
   the next `classify_emails.py` run records it through the FULL pipeline — no more ad-hoc `process_source_email`
   calls that bypass the save. Dry-run default; `--commit` to act.

- **BIGGER FINDING surfaced by #3 — NEEDS TRIAGE:** the reconcile check found **112 orphaned ledger rows
  across ~58 source emails**, not just EuroPharma. 99 are at email status='fetched', 13 'processed' (0 'error'
  now). Spot-checked 4 (TopRx #10839990, KayCo #2562596, Dart Media #32145, Nutri-Supreme #68793) — all have
  ZERO live invoices; 64 source emails have NO record of any type. These are genuinely unrecorded, a broader
  version of the EuroPharma gap (they never showed as errors because their status is 'fetched', not 'error').
  Whether they're real untracked bills vs a known pending-reprocess backlog / migration cruft is a SEPARATE
  investigation — flagged to Joel, not fixed here. (This is exactly what the reconcile tile is for.)
- Verified: all modules compile, reconcile.html parses, recovery + reconcile tested, recover_errored_email
  dry-run clean (0 error emails). CLI change (#2) takes effect next run; web routes (#3) need the restart.

### /review: cap "Why flagged" cell height (2026-10-05)

A statement-exception row's `question` lists every open line (e.g. TopRx with 85 missing invoices →
85 inline lines), blowing the row to enormous height. Wrapped the question text in a scrollable box
(`white-space: pre-line; max-height: 140px; overflow-y: auto;`) in BOTH the table layout (review_list.html
~l87) and the card/mobile layout (~l139) — the full list is still reachable by scrolling within the cell; the
Group link in the card layout stays below the box, always visible. Skipped the optional
`_statement_group_row` list-truncation (scroll box alone is enough). Template-only — auto-reloads, no restart.

### "Flag for review" now available on review-queue items too (2026-10-05)

Flag-for-review only rendered on live invoice/credit_memo/payment pages (record_detail.html). Items still in
review_queue use a separate page (/review/{id} → review_detail.html) with no flag form. Wired it through (4
changes, no new route — the existing `/records/{type}/{id}/flag` endpoint takes record_type from the URL):
- **web_db `_record_snapshot`**: added `"review": "invoice_number"` / `"review": "date_flagged"` to the
  num_col/date_col maps, so a review-item flag snapshots its number+date (were blank before).
- **web_db `build_fix_prompt`**: the "Record page" line now branches — `/review/<id>` for a review-type flag,
  `/records/<type>/<id>` otherwise (there is no /records/review/<id> page).
- **review_app `flag_record`**: whitelist extended to include `"review"`; success redirect branches the same
  way (`/review/<id>?flagged=1` for review items — previously a 404).
- **review_app `review_detail`** + **review_detail.html**: route accepts/passes a `flagged` param; template
  gained the same "Flag for review" card (posting to `/records/review/{id}/flag`), placed after the Ignore
  card.
- Verified end-to-end (self-reverting): flag a real review item → snapshot carries number+date+vendor; fix
  prompt links to `/review/<id>`; modules compile, template parses. Route change needs the pending restart.

### Amount-review gate + stated-vs-derived total tracking (2026-09-30)

From a flag investigation (record #2569, ABC Baby Care, $1,063.17): the total-bearing page was missing, no
printed total was visible, yet the pipeline recorded an amount summed from line items and never flagged it.
There was no `apply_amount_review_gate` (store + date gates existed; amount didn't), and the prompt had no
"flag when NO total is printed" rule.

- **FIX 1 — `apply_amount_review_gate(rec, amount_field)`** (new, next to apply_date_review_gate; called at
  all 3 sites after the date gate — invoices/credit_memos/payments). Deterministic: amount missing / zero /
  negative → needs_human_review=True. NOT skipped for excluded vendors (a wrong amount matters to AP even
  off-QB). Only flips False→True.
- **FIX 2 — stated-vs-derived total**: new `total_source` field ('stated' | 'derived') added to the
  invoice/credit_memo/payment JSON schema blocks + prompt guidance (set 'derived' when NO printed
  Total/Amount Due/Balance exists anywhere, or a "Page X of Y" shows the total page is missing). The same gate
  flags `total_source == 'derived'` — the actual fix for the ABC Baby Care case (a derived-but-present amount
  the null/zero check can't catch). **Missing/None total_source is a NO-OP** (absence is treated as neither
  derived nor stated) so older-pattern model responses that don't set it aren't flagged.
- **Spot-check (no flood)**: the gate is FORWARD-ONLY (fires at classify time, never retroactively), so the
  existing 124 null-amount invoices / 14 null payments (utilities) / 6 zero-amount records are NOT mass-
  flagged. Going forward, genuinely missing/derived amounts get flagged — the intent. Note: line-items-only
  vendors (no printed summary total) will flag each time as 'derived', and utility payment confirmations with
  no parsed amount will flag — both are the desired "confirm before QB" behavior.
- Verified: the deterministic gate is unit-tested across all cases (stated/derived/null/zero/negative/missing-
  source/credit/payment/reason-preservation) — all pass; module compiles. The MODEL side (does it correctly
  set total_source='derived' on a missing-total doc?) can only be validated on a live classify run with the
  API key — re-run the ABC Baby Care email and confirm needs_human_review=true + the new review_reason +
  total_source='derived'. total_source is acted on at classify time and surfaced via review_reason; it is NOT
  persisted to the DB as its own column (add later if an audit trail of stated-vs-derived is wanted).
- CLI change — takes effect on the next classify run.

### FIX: dead Group links on flagged "multiple versions" items (2026-09-29)

Reported: the "Multiple versions exist — see the Group link" prompt on flagged review items resolved to
nothing (e.g. group D7F7C83F on review item #2727, Schwartz & Company CPA's PLLC #236738).

- **Part 1 — ordering (applied):** `enrich_flagged_with_duplicate_info(newly_flagged, history)` ran AFTER
  `save_history(history)` in main(), so the `duplicate_group_id` `_group_id_for()` stamps onto the matched
  PRIOR (in `history`) was dropped from that run's history write. Moved the call to BEFORE `save_history`
  (matching the mutate-then-save order `apply_duplicate_checks` already uses). This persists the history-side
  gid — its real benefit is 3rd-copy gid REUSE consistency (a later copy matching the same history prior now
  reuses the id instead of minting a new one, keeping the cluster cohesive). `newly_flagged`/`history` are
  both fully populated by that point; enrichment is read-only, so the move is safe.
- **Part 2 — the ACTUAL dead-link cause (found by audit):** Part 1 alone does NOT revive the link, because
  /browse's group lookup reads the `all_records` view, not history. Empirically **7 of 8** flagged group-ids
  already resolved — the live-row stamp (`find_live_invoice` → `set_group_id`) is an immediate DB write,
  independent of the ordering bug. The ONE orphan (D7F7C83F) had its prior only in `invoice_history` with
  **no live invoices row** (a data inconsistency — #236738 was recorded to BMW/$1800 once, later left only in
  history), so `find_live_invoice` returned None and no live row carried the gid. `list_all_records(group=X)`
  bootstraps by `SELECT … WHERE duplicate_group_id = X LIMIT 1` to get the cluster's vendor+number, then
  widens — but the `all_records` **review arm hardcoded NULL** for `duplicate_group_id`, so with no live copy
  carrying it, step 1 found nothing → dead link.
- **Fix:** the review arm now exposes `rq.duplicate_group_id` (credit_memo still NULL). Step 1 can now
  bootstrap from the flagged item itself, so the link resolves even with a history-only prior. **8/8 flagged
  group-ids now resolve.** Bonus: Group links now surface review-queue items too — including linking MULTIPLE
  review items to each other (group 1AB59F25 → 3 review copies), closing the noted "a group id can never link
  two review_queue items" gap. /duplicates unchanged (clusters by vendor+number). D7F7C83F now resolves to
  its flagged item; its second copy (BMW/$1800) is history-only so still won't display, but the link is no
  longer dead — as predicted, that specific orphan's missing prior stays invisible, which is expected.
- Modules compile; the `all_records` view was recreated (`init_web_schema`). The CLI reorder takes effect on
  the next classify run; the view change needs the pending web-app restart (already applied live via
  init_web_schema, so /browse benefits immediately).

### FIX: doubled account_name_map cells (two QB accounts joined by " / ") (2026-09-24)

Spotted while diagnosing the CSV `·` issue: invoice #368's `qb_account` held TWO account paths joined by
" / " — a CSV-import artifact in `account_name_map` where a per-store cell got two candidate QB accounts
concatenated instead of one chosen. A swept audit found **20 such doubled cells** across 12 account rows
(distinct from ~17 LEGIT " / " values that are part of a real account name — `Due To / From Related`,
`Over / Short`, `SDI / PFL`, `Capital One 1246 / 0962` — left untouched).

- Joel's call: "make your own decision which to keep/drop; I'll fix any QB mismatch manually on the QB side."
- Picked one value per cell by matching the sibling stores' value / the standard account name, keeping the
  parent account where the alt was a redundant sub-account, and keeping the numbered QB account (`63300 ·`,
  `12100 ·`, etc.) where present. Applied by ` / `-split index (no retyping), each edit logged to edit_log
  (`claude:fix-doubled-account-map-2026-09-24`). Examples: #135 Repairs→`…67200 · Repairs and Maintenance`
  (matches other stores); #89→`…:Membership Fee` (matches standard); #91→`…:63300 · Insurance Expense`;
  #76 Payroll→the parent account for all four stores.
- Re-resolved `qb_account` on the 4 invoices that carried a doubled value (#368/#370 Braun→Madison Repairs,
  #1079/#1175 Ashpa→3M Garbage Removal) via `apply_vendor_standardization` — the standardization backfill
  skips them (they already HAD a qb_account), so they were refreshed directly + logged. **0 doubled
  qb_account left**; the 17 remaining " / " cells are all legitimate account names.
- Takes effect for future runs immediately (map is data the CLI reads); the 4 corrected invoices are already
  clean in the DB.

### FIX: CSV export UTF-8 BOM (Excel showed "Â·" for "·") (2026-09-24)

QB account names legitimately contain a middle dot ("61700 · Computer and Internet Expenses" — QuickBooks'
number·name convention), stored in qb_account/standard_account/account_name_map. The CSV export wrote UTF-8
without a BOM, so Excel opened it as ANSI/cp1252 and rendered the `·` (UTF-8 `0xC2 0xB7`) as `Â·`. Fixed both
CSV routes (`qb_export`, payments export) to prepend a `﻿` BOM and declare `charset=utf-8`, so Excel
detects UTF-8. Data was correct; only the encoding hint was missing. Route change — needs the pending restart.

### /qb: omit Store from Copy Selected + CSV export (2026-09-24)

The store isn't part of the QuickBooks import (store maps to class/location separately), so it's now
withheld from both the "Copy Selected" TSV and the CSV download while STILL showing on screen for context.
New `review_app.QB_COPY_EXPORT_HIDE = {"Store"}`; `copy_columns` (drives the Copy Selected JS via
`copy_columns|tojson`) and the `qb_export` `cols` list both filter it out (after all layout branches, so it's
dropped regardless of follow-view / custom-export prefs). `view_columns` (the on-screen table) is untouched.
Template-independent — takes effect for the copy button immediately on reload; the CSV route needs the
pending restart. Copy Selected also no longer includes the header row (data rows only) — CSV export keeps
its header (QuickBooks import maps columns by header). Copy Selected joins rows with `\r\n` (not bare
`\n`): QuickBooks Desktop's batch-entry grid only recognizes row boundaries on CRLF — with `\n` it dumped
the whole blob into the focused cell (Excel accepted `\n`, which masked it). Both the clipboard-API and
execCommand fallback paths share the one tsv string, so the one change covers both.

The `\r\n` fix alone was insufficient — reading Joel's raw OS clipboard confirmed the TSV was well-formed,
so the real cause is that QuickBooks Desktop's grid splits a paste into cells by reading the **CF_HTML**
clipboard format (like Excel), not plain CF_UNICODETEXT — a plain-text paste from Chrome collapses into the
focused cell, which is why the Chrome→Excel→QB round-trip worked (Excel re-emits CF_HTML). Fixed by writing
BOTH representations via the async Clipboard API: `copyQb()` now builds a bare `<table>` of the same data
rows (`buildQbHtml`, shared `qbCellValue` helper) and does `navigator.clipboard.write([new ClipboardItem({
'text/plain': tsv, 'text/html': html })])`, falling back to `writeText(tsv)` → `execCommand` where
`ClipboardItem` is unavailable. Additive: plain-text targets (Notepad, terminals, the Excel round-trip) are
unaffected since text/plain is still written every time.

That ClipboardItem attempt STILL landed in one cell — because the async Clipboard API
(`navigator.clipboard` / `ClipboardItem`) requires a **secure context** (https or localhost). Joel opens the
app over plain http on a LAN IP, where `navigator.clipboard` is undefined, so the new HTML code never ran and
it fell through to the old `execCommand` on a **textarea** (plain text only) → one cell. Fixed with a
`copyHtmlViaExec()` fallback: it builds a real hidden `contenteditable` `<table>`, selects it, and
`execCommand('copy')` — which puts BOTH text/html (CF_HTML) and text/plain on the clipboard AND works in
insecure contexts. Flow now: secure context → `ClipboardItem` write; otherwise (or on failure) →
`copyHtmlViaExec`; last resort → the plain-text textarea. So the HTML/CF_HTML representation is written
regardless of http-vs-https. If a direct QB paste STILL fails after this, it's likely QB reads only an
Excel-proprietary clipboard format (CSV/SYLK/Biff) a browser can't write — then Export CSV → Excel → QB.

**CONCLUSION (2026-09-24): direct browser → QuickBooks paste is not achievable.** Joel opens the app at
`localhost:8000`, which browsers treat as a SECURE context even over http — so the `ClipboardItem`
text/plain+text/html path DID run and Chrome DID place CF_HTML on the clipboard, yet QB still collapsed it
into one cell. That rules out the secure-context theory and confirms QB Desktop's grid does not read the
CF_HTML a browser writes; it reads an Excel-proprietary registered clipboard format (Csv/SYLK/Biff) that the
sandboxed Clipboard API forbids a web page from writing. The working paths therefore keep the Excel hop:
**Copy Selected → paste into Excel → copy → paste into QB**, or **Export CSV → open in Excel → copy → paste
into QB** (Excel re-emits the format QB needs). The Copy Selected code (CRLF TSV + CF_HTML via ClipboardItem
with an execCommand-table fallback) is kept — it makes the Excel paste land in cells in one step and is the
best a browser can do; it just can't feed QB directly. No further clipboard-format attempts planned.

### Unify vendor lists — exclusions merged into vendor_name_map (2026-09-24, Part 2)

Replaced the two flat exclusion tables with two boolean columns on vendor_name_map — ONE vendor list.
Joel's decisions: exclusion matches standard name **+ aliases + per-store spellings** (more correct); an
UNCHECK stops excluding NEW records but leaves existing `skip_qb_import=TRUE` for manual review (symmetric
with Part 1).

- **Audit (read-only, first)**: vnm 670 / excluded_vendors 27 / statement_recon 1; 14 excluded + 1 recon had
  no vnm row; no collisions. A/B over 260 live vendors: the new view-based exclusion is byte-identical to the
  old flat-list behavior — **0 regressions, 0 changes** (1a's alias matching applies going forward).
- **Schema**: `vendor_name_map.exclude_from_qb` + `exclude_from_statement_recon` BOOLEAN NOT NULL DEFAULT
  FALSE. New view `vendor_exclusion_spellings` (created in `_apply_standing_rules`) unnests every excluded
  row's standard name + 7 store columns + newline-split aliases, carrying both flags — the single source all
  exclusion reads use.
- **Migration** (edit-logged): set the flag on the matching vnm row (case-insensitive) or created a minimal
  row for exclude-only vendors. Reconciled: **every** current excluded (27) and recon-excluded (1) vendor is
  covered by a checked vnm row; 26 vnm rows carry exclude_from_qb, 78 spellings in the view.
- **Reads repointed**: `database.load_excluded_vendors` / `load_statement_recon_excluded_vendors` (→ view),
  `web_db._excluded_vendor_sql` (used by list_all_records / list_duplicate_candidates / list_invoice_history →
  EXISTS against the view), `database.reconcile_statement_lines` (both exclusion checks → view). Reconcile
  status distribution UNCHANGED; all filter queries execute.
- **Writes repointed**: `web_db.exclude_vendor` (one-click) now sets `exclude_from_qb` on the vnm row (creates
  a minimal one if needed), idempotent via is_excluded_vendor. Reference framework gained a `checkbox` column
  type (`_ref_clean` bool coercion; always-emitted so an uncheck sticks; template renders checkboxes in the
  add form + row cells). vnm Setup page gained the 2 checkboxes right after "Standard vendor". Saving a vnm
  row now also runs `reapply_excluded_vendor_backfill()` (Part 1) so checking the box skips existing records;
  the count shows in the page flash.
- **Seed** (fresh installs): `_seed_reference_tables` now seeds DEFAULT_EXCLUDED_VENDORS /
  DEFAULT_STATEMENT_RECON as flagged vnm rows (guarded; never touches a migrated DB).
- **Nav**: removed the "Excluded Vendors" and "Statement recon" Setup tabs; REFERENCE_PAGES entries removed.
- **Old tables**: LEFT physically in place (vestigial — nothing reads/writes/seeds them) rather than dropped,
  so this is fully reversible. Dropping `excluded_vendors` + `statement_recon_excluded_vendors` (and the
  vestigial web_db `_REF` entries) is a deferred follow-up once Joel confirms the merged UI post-restart.
- Verified: all modules compile, templates parse, init_db idempotent (seed guard skips), self-reverting
  round-trip of one-click exclude + checkbox check/uncheck (uncheck does NOT un-skip, per decision), main page
  data functions all execute. **Needs the web-app restart** to load the new routes/config/columns.

### FIX: excluding a vendor didn't backfill already-recorded records (2026-09-24)

Adding a vendor to `excluded_vendors` (the "ignore this vendor" button or the Setup page) only affected
records processed AFTER — `is_excluded_vendor()` is read fresh so NEW records skip correctly, but nothing
re-evaluated `skip_qb_import` on already-recorded, not-yet-entered rows. Confirmed live: Amazon Business was
excluded, but its already-recorded invoice (#1411) still sat on /qb with `skip_qb_import=FALSE`.

- **`classify_emails.reapply_excluded_vendor_backfill()`** (mirrors `reapply_vendor_standardization_backfill`):
  scans `entered_in_qb IS NOT TRUE AND skip_qb_import IS NOT TRUE` rows in invoices + credit_memos and flips
  `skip_qb_import → TRUE` for any where `is_excluded_vendor(vendor_matched)` is now True. Reuses
  `is_excluded_vendor` directly (ONE source of truth — same normalized-substring-either-direction logic +
  same live list), only ever flips FALSE→TRUE (never un-excludes), payments untouched, each flip logged to
  edit_log (`auto-skipped by excluded-vendor backfill`). Returns the flip count.
- **Triggers**: `review_app._backfill_excluded_skips()` wrapper is run after the one-click `/vendors/exclude`
  route AND in `_reference_saved_redirect` for `table == 'excluded_vendors'` (the Setup page add) — count
  surfaced in the flash.
- Verified live: found + fixed exactly #1411 (Amazon Business) → `skip_qb_import=TRUE`, edit-logged;
  re-run flips 0 (idempotent). Modules compile. Web app needs the pending restart for the routes; the backfill
  function itself is available to the CLI immediately.

### Unify vendor normalization SQL-side — norm_vendor_dedupe() (2026-09-24)

Follow-up to the find_live_invoice fix: replace the scattered plain `lower()`/`btrim()` vendor
comparisons (which don't strip legal suffixes) with ONE SQL normalizer mirroring
`classify_emails._norm_vendor_dedupe`, so "same vendor, different legal suffix" matches consistently
everywhere instead of being fixed one call site at a time.

- **New Postgres function** `norm_vendor_dedupe(text)` (plpgsql, IMMUTABLE): lowercases + strips trailing
  pllc/llc/llp/lllp/inc/incorporated/corp/corporation/co/company/pc/ltd/cpa/cpas, punctuation-tolerant,
  repeated ("Co., Inc."). Defined as `_NORM_VENDOR_FN_DDL` in database.py and created idempotently in
  `_apply_standing_rules` (runs on every `init_db`, which both the CLI and `web_db.init_web_schema` call —
  so it always exists).
- **Dry-run gate FIRST (per the brief), before wiring anything**: compared the SQL function vs Python
  `_norm_vendor_dedupe` over all 282 distinct `vendor_matched`/`vendor` values across invoices, credit_memos,
  payments, review_queue, statement_lines → **0 disagreements** (byte-identical, incl. edge cases like
  "Schwartz & Company CPAs PLLC" → "schwartz &" and apostrophes). Only after that clean result were the
  queries changed.
- **Wired into every flagged call site**:
  - `web_db.list_duplicate_candidates` (~2070) — /duplicates sibling grouping.
  - `web_db.set_duplicate_ignored` cluster_match (~1312) — the sibling-clear on ignore.
  - `web_db.list_all_records` group-widen (~2028) — Group view.
  - `database.insert_sales_order` held-order dedup + `find_recorded_invoice_ref` (~1173/1214).
  - `database.reconcile_statement_lines` — the 3 vendor comparisons vs invoices / review_queue /
    pending_sales_orders (kept invoice-number comparison as `upper(btrim())`).
- **Zero behavioral change on current data, verified**: re-ran `reconcile_statement_lines()` — status
  distribution IDENTICAL before/after (dismissed 8 / excluded_vendor 63 / in_flight 1 / matched 60 /
  pre_cutoff 893); a targeted probe found 0 currently-missing statement lines that would newly match only via
  suffix-normalization. Every wired query executes without error; `find_recorded_invoice_ref('…Company',
  '9IB1317')` now finds the '…Corp' invoice (returned None before). The change only widens matching to
  genuine same-vendor/different-suffix pairs going forward — exactly what check_duplicate already does.
- Both modules compile. Web app needs the pending restart to load the wired queries; the function itself is
  already created live and is (re)created on every init.

### FIX: find_live_invoice/payment missed a prior across a legal-suffix spelling (2026-09-24)

`check_duplicate()` correctly treated "Lantev Distributing Corp" and "…Company" as the same vendor (via
`_norm_vendor_dedupe`, which strips inc/llc/corp/co/company/… suffixes) and returned `store_preferred`. But
`apply_duplicate_checks`'s follow-up call to `database.find_live_invoice(dup_of, vendor, number)` compared
`lower(vendor_matched) = lower(%s)` with NO suffix normalization → "company" ≠ "corp" → returned None. The
caller's fallback then flagged the NEW store copy as `duplicate_suspect` instead of superseding the earlier
vendor copy, and the reason text ("auto-ignoring the earlier vendor copy") described an action not actually
taken. Result: BOTH spellings stayed live → a real double-entry risk.

- **Fix** (`database.find_live_invoice` ~1639, `find_live_payment` ~1660): reuse the SAME
  `classify_emails._norm_vendor_dedupe` the write path uses (function-level import — same circular-import
  avoidance pattern as `load_vendor_standardization`). Query by exact `email_filename` + number, then filter
  candidates by normalized vendor (newest-id wins). email_filename+number already scope the match tightly, so
  relaxing only the suffix can't cause a wrong match. Verified: Company-spelling lookups now find the Corp
  rows (#2418/#2417); a genuinely different vendor still returns None.
- **Immediate cleanup** (logged to edit_log, `claude:cleanup-lantev-suffix-orphan-2026-09-24`): the 4 orphaned
  rows were all live/unentered. Superseded the earlier vendor "Corp" copies **#2418** (9IB1317) and **#2417**
  (9IB1318) → `duplicate_ignored=TRUE`, dup_of=the Scan email, gid kept; cleared the `duplicate_suspect` flag
  on the kept store-scanned "Company" survivors **#2473**/**#2474**. Now exactly ONE copy per invoice number
  would enter QB. (No QB correction — none was entered.)
- **Broader audit (report-only — same normalizer-inconsistency class, THIRD spot this class has appeared):**
  grepped database.py + web_db.py for vendor comparisons using plain `lower()`/`btrim()` where the write path
  normalizes with suffix-stripping. Findings, none fixed this pass (each has a mitigating path; find_live_*
  was the only one with NO fallback, hence the only one that actually mis-behaved here):
  - `web_db.list_duplicate_candidates` (~2070) and `set_duplicate_ignored` cluster_match (~1312) — the
    /duplicates grouping + sibling-clear match siblings by `lower(vendor)=lower(vendor)` AND number, but BOTH
    also match by shared `duplicate_group_id` (OR-branch), which covered the Lantev case (both pairs shared a
    gid). Latent only for suffix-differing clusters that never got a gid stamped. (This is the 2026-09-23
    "orphaned duplicates" mismatch, now understood as this same class.)
  - `web_db.list_all_records` group-widen (~2028) — display grouping only.
  - `database.insert_sales_order` held-order dedup (~1147), `find_recorded_invoice_ref` (~1194/1201),
    statement-reconciliation vendor match (~1461/1469/1473) — exact vendor match; statement recon additionally
    has an alias-based second pass. Lower-impact / separate subsystems.
  - Recommendation (deferred): unify vendor normalization on the SQL side — e.g. a Postgres function
    mirroring `_norm_vendor_dedupe`, or a precomputed normalized column — so these all match consistently
    without per-call Python filtering. Not done now to keep this change focused on the firing bug.
- `database.py` change takes effect on the next classify run (and in the web app's dedup re-eval after the
  pending restart). Compiles.

### FIX: vendor alias containing a comma never resolved (2026-09-24)

An alias that legitimately contains a comma — "EnlivenHealth (FDS, Inc.)" (Refuah, invoice IN1170010) —
stayed stuck on "add vendor" no matter how often it was re-saved.

- **Root cause**: `database.load_vendor_standardization()` split the aliases textarea on
  `re.split(r"[\n,]+", …)`, treating a comma like a newline. So "EnlivenHealth (FDS, Inc.)" shattered into
  "EnlivenHealth (FDS" + "Inc.)" and could never match a real `vendor_matched` again. The save-time backfill
  fired correctly but re-ran this mangling every time.
- **Audit first (per convention)**: checked all 40 non-empty aliases blobs for the legacy risk the fix would
  introduce — multiple DISTINCT aliases comma-joined on ONE line (which newline-only splitting would stop
  separating). Found 4 rows with a comma on a line (#35 Applied Medical "…Technology, Inc.", #264 InfoShield
  "InfoShield, LLC", #171 EnlivenHealth "(FDS, Inc.)", #350 Megafood "(Foodstate, Inc.)") — EVERY one is a
  single name with an intra-name legal-suffix comma, NONE a legacy CSV of joined vendors. **No migration
  needed.**
- **Fix**: split aliases on NEWLINES ONLY (`.splitlines()`) in `database.load_vendor_standardization()` —
  one alias per line, matching how the Setup page edits them.
- **Same bug, second path**: `web_db.attach_vendor_spelling()`'s "already present?" check used the identical
  `re.split(r"[\n,]+", …)`, so it never recognized a comma alias as already there and re-appended a copy on
  every save — that's why #171 had accumulated ~5 identical "EnlivenHealth (FDS, Inc.)" lines. Fixed to
  `.splitlines()` too. Verified: re-attaching the same comma alias now no-ops (6 lines before/after, no new
  duplicate).
- **Cleared the backlog**: re-ran `reapply_vendor_standardization_backfill()` (resolved 2). IN1170010 (#1495)
  now resolves to standard_vendor "EnlivenHealth / FDS" + account "Selling Expenses:PSAO Reconciliation";
  **0** full-name EnlivenHealth/FDS invoices remain unresolved. Every unentered invoice whose raw vendor
  contains a comma (Applied Medical, InfoShield, Megafood, L&R, Paskesz, Healthy Power, EnlivenHealth) now
  has a standard_vendor.
- CLI-safe/additive (DB-only path; newline-only is strictly less destructive). Pre-existing duplicate alias
  LINES (#171, #350) are harmless — all map to the same standard vendor (the lookup dedups) — left as-is; an
  optional cosmetic collapse-to-one is available if wanted. `database.py` change takes effect on the next
  classify run; the `web_db` change needs the pending web-app restart.

### One-click "Ignore this vendor" — reusable across pages (2026-09-23)

Adding an excluded vendor used to mean navigating to /reference/excluded_vendors and retyping the name.
Now a single button excludes a context-known vendor in one POST (mirroring the vendor-alias "Yes — same
as X" one-click pattern), confirmed first.

- **`web_db.exclude_vendor(vendor_name, username)`**: adds to `excluded_vendors`, IDEMPOTENT — returns
  'added' / 'exists' / None (blank). ON CONFLICT (vendor_name) DO NOTHING, plus a SMART pre-check via
  `classify_emails.is_excluded_vendor` so a name already covered by the pipeline's normalized-substring
  match (e.g. "Abbott" when "Abbott Laboratories" is excluded) reports 'exists' instead of adding a
  near-duplicate row. Insert logged to edit_log like a reference add.
- **Route** `POST /vendors/exclude` (review_app.py): reusable endpoint; takes `vendor_name` + optional
  `return_to`. `_safe_return_to` guards against open-redirect (relative, single-'/' only; falls back to
  Referer then /qb). `_redirect_with_flash` appends a one-line `flash` to the return URL.
- **Global flash**: base.html now renders `?flash=…` as an `.ok-msg` on ANY page (so the action works from
  anywhere without per-page plumbing).
- **Reusable macro** `_macros.html:ignore_vendor_button(vendor, return_to, label, cls, style)`: renders a
  tiny inline `<form>` POSTing to /vendors/exclude with a JS `confirm()` (generic text — no vendor name
  interpolated, so an apostrophe can't break the JS). No-op when vendor is blank.
- **Placements** (where it now appears):
  1. **Add-vendor flow** (/reference/vendor_name_map) — an amber "Not a real vendor?" card, shown whenever
     `prefill` is set, so it covers BOTH the suggestion view and the blank-add-form fallback. Prefilled with
     the raw spelling; returns to the page with a flash. (Primary placement.)
  2. **/qb Account cell** — an "ignore vendor" link right next to the add-vendor/assign-account pills (only
     where the pill shows: blank account + a vendor). return_to falls back to the Referer (keeps /qb filters).
  3. **Record detail** (/records/{type}/{id}) — a "Not a real vendor?" line under the record table, for
     invoices/credit memos only (payments always enter QB regardless of vendor) and only when not already
     skip_qb_import. Returns to the same record.
- Behavior note: like the reference-page add, this sets the exclusion "going forward" — the next
  classify run's `apply_vendor_exclusion` skips the vendor. It does NOT retroactively flip skip_qb_import on
  existing records, so already-recorded invoices stay on /qb until reprocessed (matches the briefing's
  "going forward" framing; a retroactive sweep is an easy follow-up if wanted).
- Verified: modules compile, all 5 templates Jinja-parse, self-reverting DB test — added→exists (idempotent)
  →exists (near-dup substring, only 1 row created)→None (blank), is_excluded_vendor live-True, cleanup to 0.
  Route change needs the pending web-app restart; templates auto-reload.

### FIX: standing-learning scope leak — Carepax→BMW onto a non-Sageteq vendor (2026-09-23)

From a flag + automated investigation on Abbott invoice #1605 (store wrongly BMW). Root cause: standing
learnings are injected into the classifier prompt for EVERY email (`database._apply_standing_rules` →
`learnings` table → `_rows_to_learnings_text` → system prompt), and the "applies ONLY to Sageteq"
restriction on the Bill-To rule (learning #2509) is just prose the model is trusted to honor. It leaked:
the Carepax→BMW mapping fired on Abbott invoices whose bill-to entity was "Carepax". "Carepax" is not a
store.

- **Data fix**: `web_db.update_record_field('invoice', 1605, 'store', 'Maple Pharmacy', …)` — logged to
  edit_log (BMW→Maple Pharmacy). Confirmed against the SOURCE .eml (`processed_emails/abbott invoice -
  1a08a38821e5f2d4.eml`): forwarded by Leah Hackner <Lhackner@maplerx.com>, a **Maple Pharmacy** DME
  specialist, for a Yedei Chesed residential account (PO YCG…). The Carepax@382 Rte 59 bill-to is the
  shared back-office address, not the store. #1605 is entered_in_qb=false → no QuickBooks correction.
- **Twin found**: the same email carried a SECOND Abbott invoice, **#1604** ($694, inv 622312457), with the
  identical store_raw + leak, also BMW, also not entered. Same confirmed-Maple source. NOT yet corrected —
  it wasn't named in the briefing (only #1605 was); recommended + awaiting Joel's ok. After #1605, #1604 is
  the ONLY remaining leaked record in the DB.
- **Systemic fix (code-enforced vendor scope)**: new `apply_scoped_learning_gate(rec)` in classify_emails.py,
  wired per-record for invoices + credit_memos right after `apply_store_review_gate` (same apply_* safety-net
  convention — only ever flips needs_human_review False→True). When `store_matched=='BMW'` AND
  `'carepax' in store_raw` AND the vendor is NOT on `_CAREPAX_BMW_ALLOWED_VENDORS` (`sageteq`, `microsoft`)
  → force needs_human_review with a clear reason. Exemptions reflect the TWO authorized Carepax→BMW rules:
  Sageteq (#2509) and **Microsoft-365 (#2514)** — the 5 Microsoft/Carepax→BMW records are intended, not a
  leak. Unit-tested: Abbott flagged; Sageteq/Microsoft exempt; literal "BMW Technologies LLC" (no Carepax)
  not flagged; existing review_reason preserved. Affects FUTURE runs only. Needs the pending web-app restart
  for the pipeline change to load (but the CLI `classify_emails.py` run picks it up immediately).
- **Audit of OTHER vendor-scoped learnings (report-only, per briefing — no changes made)**: the leak-prone
  SHAPE (a vendor-scoped raw→store/alias mapping enforced only by prompt prose) also appears in:
  **#2510 Braun** (ignore the 112 Hillside billing address, use first line), **#2516 Emerson/Fullscript**
  (trust card-store over billing for the Maple/3M pairing, don't flag), **#2517 East Coast Building
  Maintenance** ("Accuscript LLC" → Lakewood Rx), and a milder **#2505** (ignore "Institute for Advanced
  Judaic Studies" invoices). Checked live data for actual leaks: **Accuscript on a non-East-Coast vendor = 0**;
  **"112 Hillside" on non-Braun vendors = 309 but overwhelmingly legitimate Westgate** invoices (112 Hillside
  really is Westgate's address and those invoices name Westgate). So none of these has demonstrably leaked the
  way Carepax did. Left unchanged pending Joel's call; if we want defense-in-depth, the same gate pattern
  generalizes.

### Flags: auto-generate a copy-paste fix prompt on "Approve for fix" (2026-09-23)

Phase-1 "Approve for fix" was a status label only (no automated action). Joel has hand-written a fix
prompt for Claude Code each time (record details + note + investigation findings + a closing "update
PROJECT_STATUS.md and report back"). Now that prompt is generated automatically — still NOT a fix, just
the text he'd otherwise type.

- **`web_db.build_fix_prompt(f)`** (pure text, no side effects): takes a `list_flags` row (flag + `.snapshot`
  dict + latest-investigation join) and returns a plain-text prompt with (1) a one-line title
  (`Fix needed — Type · Vendor · Store · #Number · Amount`), (2) the RECORD SNAPSHOT (type/vendor/store/
  number/date/amount/tax/ship/entered/dup/source file/source email/record page — blanks skipped),
  (3) the submitter's note under `FLAGGED BY <who> ON <when>:`, (4) `AUTOMATED INVESTIGATION FINDINGS`
  when a completed investigation exists on the flag, and (5) the standard closing "Please investigate and
  implement an appropriate fix. Update PROJECT_STATUS.md and report back."
- **Route** (`flags_page`): pre-builds `f["fix_prompt"]` for every `approved_for_fix` flag.
- **Template** (`flags.html`): on approved flags, a green-bordered "Fix prompt for Claude Code" block with a
  **Copy fix prompt** button + a readonly monospace `<textarea>` holding the prompt (re-copyable any time,
  not a one-shot popup; click-to-select fallback). Copy JS uses the async clipboard API with an
  `execCommand('copy')` fallback and a transient "copied ✓". Shown to whoever can see the flag (only admins
  can approve, so it only appears on admin-approved flags).
- Still NOT automated: it triggers nothing, doesn't call Claude Code — just produces the text.
- Verified: modules compile, flags.html Jinja-parses, `build_fix_prompt` output checked on a synthetic flag
  (all 5 sections incl. findings). 0 approved flags in the DB currently, so nothing renders live until one is
  approved; needs the pending web-app restart for the route/`web_db` function to load.

### Orphaned "possible duplicate" suspects — corrected diagnosis (2026-09-23)

Triggered by the first real automated /flags investigation (flag on #1996). The investigation's
root-cause hypothesis — vendor-name drift + strict-vs-loose vendor matching splitting clusters — was
**checked against the data and is NOT what's happening**:
- The code inconsistency IS real: `list_duplicate_candidates()` and `set_duplicate_ignored()` group by
  `lower(vendor_matched)` while `check_duplicate()` detects with `_norm_vendor_dedupe()` (strips legal
  suffixes/periods). BUT a full audit found **0 drift-split cases** — no suspect is orphaned because a
  loose-match sibling is missed by strict matching. All three #1996 rows have identical
  `vendor_matched='Hold End Dist'` and the same group id. So Fix #1 (align the normalizers) would prevent a
  FUTURE drift bug but resolves **zero** current orphans — NOT implemented pending Joel's call (risk to working
  dup logic for no current gain).
- **Real cause**: **6 orphaned suspects** are alone on /duplicates because their same-vendor+number siblings
  are already **ignored (or entered)**, and the surviving copy's `duplicate_suspect` flag was never cleared —
  the pre-fix tail of the sibling-clearing bug (now fixed going forward). All 6 share a group id with their
  resolved siblings; most are **revision chains** (different amounts):
  - 818 Nutri-Supreme #68130 (sib $10623.82 ignored) · 817 Nutri-Supreme #68131 (sib $2502.66 ignored)
  - 1695 Prodermix #9152 · 527 Rx Labels #782282 · 1996 Hold End Dist #WB-001987 (2 higher amounts ignored)
  - **253 Garden of Life #52070177 — EXACT dup ($1550.51) of id 18 which is ALREADY ENTERED IN QB → a genuine
    double-entry risk; should almost certainly be ignored.**
- **#1996 specifically**: a revision chain — 3 copies, same store/vendor/#/date, amounts $1177.01 / $1211.96 /
  $1248.41. The two higher were ignored; the lowest ($1177.01) is the live survivor. Which is the correct final
  invoice is a business call.
- **Resolution chosen by Joel (2026-09-23)**: rather than act on a text list, surface the full clusters on
  /duplicates so he can eyeball + act in the normal UI. `list_duplicate_candidates()` now ALSO pulls the
  already-IGNORED copies of any cluster that still has a live suspect (matched on type+vendor+number,
  `number = ANY(nums)`) and appends them as context — so an orphan no longer shows alone; the whole document's
  copies render side by side. duplicates.html renders ignored columns as a grey "Ignored" header with a
  **Restore** action (un-ignore), and "Keep this one" now operates only over the LIVE copies (ignored ones stay
  ignored). Never spawns a new all-ignored cluster (fully-resolved sets stay off the page). Verified: all 6
  orphan clusters now show their full copy sets (WB-001987 → 3 cols, etc.). No records were auto-changed — Joel
  resolves each set by hand on /duplicates. Web_db change → needs a web-app restart. (Fix #1, the vendor-norm
  alignment, still NOT done — 0 current cases; deferred.)

### To-do dashboard — Part A discovery (read-only inventory, 2026-09-23)

"Needs action" queues found, with current counts + existing UI:
1. Review queue — open `review_queue` rows (12, all unanswered) — `/review`. **Has `assigned_to`** (3 assigned).
2. Statement recon exceptions — `statement_lines.status IN ('missing','amount_mismatch')` (2) — `/review`.
3. Duplicates — live suspects `duplicate_suspect AND NOT ignored AND NOT entered` (59 records / 14 sets) — `/duplicates`.
4. QB entry invoices — not entered, not skip/ignored/suspect (760; ~149 blank `qb_account` = need vendor/account setup) — `/qb`.
5. QB entry credit memos (19) — `/qb`.
6. Payments not entered (186) — `/payments`.
7. Flags pending (0) + approved_for_fix awaiting manual fix (1) — `/flags`.
8. Investigations running/failed (1) — `/flags`.
9. Sales orders held `pending_sales_orders.status='held'` (9) — `/sales-orders`.
10. Cards → store: seen card last-4 with no store mapping (4 of 19) — `/cards`.
11. Failed emails `emails.status='error'` (1) — `/emails`.
Not queues: tracked_reviews (opt-in), ignored_reviews (resolved), emails 'fetched' (auto next run).

**Assignment inventory (key design input)**: checked every table for assigned_to/owner/assignee — **ONLY
`review_queue.assigned_to` exists**. Every other queue is an UNASSIGNED SHARED POOL. So a true per-user
breakdown across all queues needs schema changes. Recommended: Option 1 = counts-per-category dashboard now
(no schema change) + review-queue-by-assignee sub-split; Option 2 = add ownership to the other queues (a much
larger "add assignment app-wide" effort). **Part B (build) held pending Joel's choice.**

### To-do dashboard — Phase 2 build, Option 1 (2026-09-23)

New **`/dashboard`** page (nav: first top-level item "Dashboard"). `web_db.dashboard_counts()` computes 12
live counts (no caching — small COUNTs) matching each page's semantics: review queue (12), statement
exceptions (2), duplicate SETS (14, via `list_duplicate_candidates`), QB invoices (760, with the blank-account
setup subset 143), QB credit memos (19), payments (186), flags pending (0) / approved-for-fix (1),
investigations running+failed (1), sales orders held (9), unmapped cards (4, via `list_seen_cards`), failed
emails (1). Each tile links to its existing page; zero-count tiles render muted. The review queue tile also
shows a per-assignee breakdown (e.g. Yishai·3, unassigned·9) — the only queue with `assigned_to`; everything
else stays a single shared-pool count (Option 2 / app-wide ownership NOT in scope). `dashboard.html` is a
simple responsive tile grid. Needs a web-app restart (route + web_db change).

### Sender-email gap diagnosis + store-vs-vendor auto-resolve dry-run (2026-09-23)

- **Part A (why Sender Email is blank)**: `emails.from_address` is written in ONE place — `gmail_fetcher.py`
  (line ~170) from the Gmail "From" header at fetch time. Anything not fetched via Gmail, or fetched before the
  feature existed, is NULL. #52070177's two copies: id 253 = a scanner email ("Scan to E-mail Server Job",
  `gmail_message_id` NULL) → non-Gmail source, sender never captured; id 18 = Gmail-fetched but pre-feature NULL.
  Neither is `legacy_migrated`. **Broader gap**: 261/2367 invoices (~11%) + 36/212 payments (~17%) have NULL
  from_address — split 102 non-Gmail-source / 159 Gmail-but-NULL. So ~89% of records DO have a sender; the NULL
  subset just falls back to manual for Part B (as #52070177 does — both senders NULL).
- **Part B (dry-run, READ-ONLY — NOT wired to auto-apply)**: rule = 2+ live (non-ignored) copies with same
  normalized vendor + number + EXACT amount, ALL copies have a non-NULL sender, exactly ONE resolves to a store
  via `sender_store_map`, the other(s) resolve to no store, and no to-ignore copy is already entered → keep the
  store copy, ignore the rest. Over the 45 current live exact-amount sets, **1 qualifies**: Garden of Life
  #52119589 $2765.60 → keep id 189 (scanner@maplerx.com→Maple), ignore id 1755 (Nestle vendor send). Conservative
  as intended (44 disqualified by NULL sender / no-or-multi store mapping). **Auto-apply HELD pending Joel's
  confirmation** after seeing the sample; no records changed.
- **CORRECTION + re-scoped finding (2026-09-23)**: that "1 qualifying set" was a BUG in the throwaway audit
  script — it grouped without `record_type`, and `id` isn't unique across invoices+payments, fusing unrelated
  rows ("id 189" is SmartSource $192.50, not the Garden of Life record). Properly scoped (`web_db.
  auto_resolve_store_vs_vendor_duplicates`, dry-run): **0 live sets qualify** — because `check_duplicate`
  ALREADY auto-dedupes exact copies each run, keeping the FIRST-ARRIVED copy and ignoring later ones, so no
  unresolved live store-vs-vendor pairs exist. The real lever is WHICH copy wins: in **37 already-resolved
  sets** the pipeline kept the VENDOR copy and ignored the STORE scan (opposite of the preference); all 37 are
  flippable (0 entered in QB). QB numbers are identical either way — only the kept source DOCUMENT differs.
  Reported the 3 options to Joel (leave as-is / prefer-store going-forward via a check_duplicate tweak /
  also backfill-flip the 37); **held, nothing changed**. The scaffolded function targets the now-empty live-set
  case and is NOT wired to anything.

### check_duplicate: prefer the store-scanned copy over the vendor send (forward-only, 2026-09-23)

Joel chose "prefer store copy going forward" (NOT the 37-set backfill). On an EXACT duplicate to the SAME store,
`check_duplicate` now keeps the store-scanned copy regardless of arrival order:
- Plumbing: each copy's sender travels with it — `database.load_history()` LEFT JOINs `emails.from_address`;
  new `database.email_from_addresses()` maps filename→sender; `apply_duplicate_checks` tags each new record
  `inv["from_address"]` and carries it into appended history. Excel-mode history = None (DB-mode only feature).
- Rule (in the exact-match, same-store branch): resolve both senders via `SENDER_STORE_MAP`. ONLY when BOTH
  senders are known AND exactly ONE resolves to a store: if the NEW copy is the store scan → new status
  **`store_preferred`** (caller supersedes the earlier vendor copy, same guardrails as `store_conflict` —
  never overrides a prior already in QB); if the PRIOR is the store scan → keep it (first-arrived already =
  store) with a note. NULL sender either side / both-store / neither → unchanged **first-arrived**.
- Auditable: reasons tagged `[store-preferred]` vs `[first-arrived]`. web_db's `check_duplicate` re-eval is
  unaffected (no from_address passed + empty SENDER_STORE_MAP → branch skipped).
- Verified: unit-tested all 6 branches (new=store→store_preferred; new=vendor/prior=store→keep prior; both
  vendor / NULL sender / both store → first-arrived; different amount → possible unchanged). Live read paths
  confirmed (load_history carries from_address over 1998 rows; 181 scanner@maplerx.com emails present).
  Forward-only: takes effect on the next classify run; the 37 historical sets are untouched.

### Add-vendor suggestions: attach a near-miss spelling instead of retyping (2026-09-23)

When /qb's "add vendor" is clicked (raw unmatched spelling below classify's 88% auto-apply threshold), the
Setup > Vendor names page now SUGGESTS existing vendors the spelling likely matches, so a human attaches it in
one click instead of typing a new row.
- **`web_db.suggest_vendor_matches(raw, limit=5, floor=60)`**: scores the raw spelling (via classify's own
  `_norm_vendor_std_key` + rapidfuzz/difflib — reused, NOT re-thresholded) against each vendor_name_map row's
  standard name, EVERY alias, AND every per-store spelling column; returns top candidates + which spelling
  matched. Floor 60 (human-confirmed, lower than the 88% auto-apply). Surfaces real near-misses, e.g. "Schwartz
  & Company CPA's PLLC"→Schwartz & Company (63%), "NJNG (New Jersey Natural Gas)"→NJ Natural Gas (78%, on the
  Westgate spelling column).
- **`web_db.attach_vendor_spelling(std, raw, store, user)`**: adds the raw spelling to the EXISTING vendor's
  row — into `name_<store>` when the store is known and empty, else appended to `aliases`; idempotent; logged
  to edit_log. (`_STORE_KEY` maps store→column.)
- **Wiring**: /qb add-vendor link now also passes `&store=`; `reference_page` computes `suggestions` for
  vendor_name_map+prefill; new route `POST /reference/vendor-name-map/attach-spelling` attaches then reuses
  `_reference_saved_redirect` (fires the standardization backfill so the invoice resolves immediately + flashes
  "N resolved"); reference.html shows a suggestions card above the Add form with one-click "Yes — same as X"
  buttons and a "None of these? Add as new below" fallback (the existing manual path is untouched).
- Constraints honored: does NOT change `VENDOR_FUZZY_THRESHOLD`/`VENDOR_FUZZY_MARGIN` or classify's silent
  auto-apply; manual add-new path preserved. Verified: suggester on real unmatched vendors, self-reverting
  attach test (store-col vs aliases vs idempotent), suggestions card render. Needs a web-app restart (route +
  web_db changes).

### Fix: "add vendor" re-prompts forever when the resolved vendor has no account (2026-09-23)

- **Reported**: clicking "Yes — same as Monsey View" on /qb did nothing; the add-vendor prompt kept returning.
- **Diagnosis (payments hypothesis was WRONG)**: "The Monsey View" is an INVOICE, not a payment. It DOES resolve
  via its existing alias to standard vendor "Monsey View" — but "Monsey View" has NO `vendor_account_assignment`,
  so `apply_vendor_standardization` sets `standard_vendor` and leaves `qb_account` NULL. The bug was in
  `reapply_vendor_standardization_backfill`: `if not rec.get("qb_account"): continue` DISCARDED the resolved
  `standard_vendor`, so the row stayed `standard_vendor=NULL` and /qb kept showing "add vendor" (which is shown
  only when standard_vendor is NULL). Payments were a red herring — /qb (`list_qb_ready`) is invoices+credit
  memos only, and payments.html has no Account column / add-vendor link, so payments never show it.
- **Fix (classify_emails.reapply_vendor_standardization_backfill)**: persist as long as the VENDOR resolves —
  even without an account — so the record moves "add vendor" → "assign account" (standard_vendor set, qb_account
  still NULL, carrying the "no account assigned yet" reason). Reason rebuilt = strip our two vendor/account
  sentences from the old reason + re-add whichever still applies; `needs_review = bool(reason)`; only writes when
  something changed; counts as "resolved" when a vendor is newly standardized or an account newly assigned.
  Also added `skip_qb_import IS NOT TRUE` to the candidate query — excluded vendors never enter QB, so they must
  not be standardized/flagged.
- **Cleanup (applied)**: ran the fixed backfill → **33 records progressed** (the 6 Monsey View invoices + others
  now show "assign account"); **reverted 19 excluded store-transfer records** the first run had wrongly flagged
  `needs_review` (stripped the account sentence, cleared the flag). Re-run confirms idempotent (0 new, 0 excluded
  re-flagged). Current stuck records are fixed now; the web app needs a restart to use the fixed backfill for
  future save-triggered runs.

## 6. Design decisions (why things are the way they are)
- **One script, not a package** — small enough; the user runs one command.
- **Excel outputs, not CSV/JSON** — user reviews and copy/pastes into QuickBooks.
- **Duplicate detection in Python, not the LLM** — the model can't see prior records; a
  "reminder"/"revised" wording is not evidence of a duplicate.
- **Flagged items leave results and go to a review queue** — keeps results clean/QB-ready;
  the review loop is how ambiguous items get resolved without wasting API calls.
- **Learnings persist** — a review answer's "Notes to Learn" become permanent so future
  similar emails auto-resolve (e.g. the Maple address rule).
- **Files only for billable/payment records** — "anything not landing in qb_ready doesn't
  need a file," with payments kept as proof of payment.
- **Per-record page splitting** — a multi-invoice scan yields one clean file per invoice.
- **Model attribution (`source_document`/`source_pages`)** rather than guessing which
  attachment/pages belong to which record.
- **Config is machine-local** (paths relative to the script); portable by copying the folder.

---

## 7. Environment / setup
- **Python 3.12** (installed via winget at `%LOCALAPPDATA%\Programs\Python\Python312`).
- Packages (`requirements.txt`): `anthropic`, `extract-msg`, `openpyxl`, `pypdf`, `Pillow`
  (image auto-rotation, §5d), `google-auth`, `google-auth-oauthlib`,
  `google-api-python-client`, `psycopg2-binary`, `playwright` (HTML-email → PDF fallback, §5ac).
- **LibreOffice** (`soffice`) — plaintext email-body → PDF fallback; auto-located, optional.
- **Playwright + system Edge** (`pip install playwright` then `playwright install msedge`) — renders
  the real HTML email body → PDF for the no-attachment fallback document (§5ac); optional, falls back
  to LibreOffice plaintext if absent.
- `ANTHROPIC_API_KEY` required. `DATABASE_URL` enables Postgres. `GMAIL_QUERY` /
  `GMAIL_MAX_RESULTS` tune the fetch. Drive upload is on by default in DB mode; `UPLOAD_TO_DRIVE=0`
  (or `--no-drive`) turns it off, `UPLOAD_TO_DRIVE=1` (or `--drive`) turns it on in Excel-only mode.
  `SALES_ORDER_HOLD_DAYS` (default 2) is the sales-order maturity window (§5v).
  `STATEMENT_RECON_GRACE_DAYS` (default 14) is the statement-reconciliation grace window, and
  `STATEMENT_GOLIVE_DATE` (`YYYY-MM-DD`, optional) overrides the derived go-live cutoff (§5ab).
- Web UI env: `SECRET_KEY` (session signing), `PORT` (default 8000),
  `ADMIN_USER`/`ADMIN_PASSWORD` (seed first admin), **`LINK_BASE_URL`** (optional host prefix for the
  QB-memo short links, §5t — set to the cloud domain at Phase 2C; defaults to the current LAN URL),
  and **`ANTHROPIC_API_KEY`** in the web
  process too (only for the Chat tab, §5i — the rest of the UI doesn't need it). New packages:
  `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `bcrypt`, `itsdangerous`.
- Model: `claude-sonnet-5`, `max_tokens=20000` (§5ad — raised from 8192; the max SAFE value for
  this pipeline's **non-streaming** call, since the anthropic SDK hard-raises "Streaming is
  required" above ~21,333 = 128000·600s/3600s), no `temperature` (deprecated for this model).
- **Google auth:** `credentials.json` (OAuth Desktop-app client secret, user-provided) →
  one consent flow (`gmail.modify` + `drive.file`) → cached `token.json`.
- **`ca_bundle.pem`** (Techloq root + certifi) is applied to ALL HTTPS: Anthropic
  (httpx verify), and Google OAuth/Gmail/Drive via `google_auth.py` (env
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` + `httplib2(ca_certs=...)` + a verified requests
  Session). It does NOT apply to Postgres (that's not HTTPS — put SSL in `DATABASE_URL`).

---

## 8. Files (high level)
- `classify_emails.py` — classify/extract/persist; storage dispatch (DB vs Excel), Gmail-link
  stamping, optional Drive upload. Backward compatible (no DATABASE_URL → Excel as before).
  Also exposes `is_excluded_vendor()` (reads the `excluded_vendors` table) + `check_duplicate()`
  reused by the UI, and `build_system_prompt()` (rebuilds the prompt from the `stores` /
  `excluded_vendors` tables) + `_sender_store_hint()` (self-scan store signal).
- `gmail_fetcher.py` — fetch invoice emails → `test_emails/` + `gmail_links.json` (+ emails table);
  captures each email's **From** address and Gmail **`internalDate`** (authoritative receive time);
  labels fetched messages so they aren't re-pulled.
- `backfill_received_at.py` — one-off: fill `emails.received_at` for old rows (sidecar or the
  `.eml`'s `Received` header). Dry-run default; `--commit` to write.
- `requeue_email_date_flags.py` — one-off: requeue review items falsely flagged on the email-date
  discrepancy so they reprocess with the true received date. Dry-run default; `--commit`.
- `requeue_all_reviews.py` — requeue **every** current review-queue item so it reprocesses from
  scratch with the CURRENT rules/learnings (use after adding a rule like Sageteq / a new store). Moves
  each item's source `.eml` back to `test_emails/`, `emails.status='fetched'`, removes stale split
  files, deletes its review rows — then `classify_emails.py` re-classifies (resolved items leave the
  queue; still-ambiguous ones return). Safe/additive: backs up `review_queue` to a timestamped table
  first; only requeues an email whose `.eml` is on disk (a missing-`.eml` item is LEFT in the queue,
  never dropped); re-reading already-recorded clean records doesn't double-count (save_results
  replaces by filename; the dup check self-matches on source-email identity). Dry-run default;
  `--commit`. DB mode only.
- `database.py` — Postgres schema + load/save helpers (same dict shapes as the Excel code).
  Also the reference tables (`stores`, `excluded_vendors`, `sender_store_map`) + their seed
  defaults + readers (see §5a).
- `drive_uploader.py` — `DriveUploader.upload(subfolder, path)` → share link; makes
  `Pharmacy Invoices/invoice_files` + `/payment_files`. Every Drive call retries transient errors
  (`execute(num_retries=4)`) and a whole upload is tried 3× (a half-done, unshared copy is deleted
  between tries); an expired Google login isn't retried (2026-10-08).
- `upload_local_files_to_drive.py` — backfill: uploads the documents of records whose File link is
  still a local path and switches them to the Drive copy (dry-run default; `--commit`; one commit per
  record, logged as `drive-backfill`, re-runnable). Needs Joel's Google login.
- `google_auth.py` — shared one-flow OAuth for both scopes, Techloq-aware TLS.
- `migrate_from_excel.py` — one-time Excel → Postgres load (preserves history + Entered-in-QB).
- `run_all.py` — `gmail_fetcher.py` then `classify_emails.py` (forwards flags).
- **Phase 2B web UI** (additive; CLI untouched):
  - `review_app.py` — FastAPI app (auth, sessions, review/qb/edit/browse/learnings/history/users
    pages **+ the Stores / Vendors / Senders / Card→store reference editors + the Cards discovery
    tab**, see §5a / §5k).
  - `web_db.py` — web-only schema (`users`, `edit_log`, audit columns, `all_records` view) +
    UI DB helpers (incl. `list/add/edit/delete_reference`); reuses `database.connect()` and the
    pipeline's skip/dup rules.
  - `run_web.py` — launcher: prints LAN URL + the Windows Firewall rule, starts uvicorn on 0.0.0.0:PORT.
  - `templates/` — Jinja2 pages.
- `fix_review_previews.py` — one-off (ran 2026-10-07): gives open review items that had no source document a
  preview, converting them to per-record split items where their own document can be found. Dry-run default;
  `--commit`. See "FIX: late-flagged review items had no document preview".
- `recheck_duplicates.py` — re-checks saved invoices/payments for duplicates with today's rules (dry-run default;
  `--apply` only flags missed copies as POSSIBLE duplicates for review; `--include-legacy`). Matches vendors by
  STANDARD name since 2026-10-12. See "FIX SWEEP: the review spec's Known gaps", item 5.
- `verify_model_signals.py` — read-only report on the AI-dependent review signals (sales orders, payment
  reminders, total_source, not-received markup, open review items by reason). `--days N` (default 30).
- `requirements.txt`, `ca_bundle.pem`, `PROJECT_STATUS.md`, `Internal notes/` (reference).

## 8b. Web review UI (Phase 2B)

Run: **`python run_web.py`** → serves `http://<this-PC-ip>:PORT` (default 8000) over plain HTTP
on the LAN. Colleagues open the printed LAN URL; the launcher also prints the exact
`netsh advfirewall` rule to allow the port on private networks.

- **Stack:** FastAPI + Jinja2 + Starlette signed-cookie sessions + uvicorn. Stateless process;
  ALL state in PostgreSQL → moving to a cloud VM = run the same app against the same DB, add
  HTTPS + logins, nothing more.
- **Auth/audit:** per-person logins (`users` table, bcrypt hashes). Admin creates/disables users;
  first admin seeded from `ADMIN_USER`/`ADMIN_PASSWORD` on first run. Audit columns:
  `review_queue.reviewed_by/at`, `invoices|credit_memos|payments.entered_qb_by/at` (next to `entered_in_qb`)
  and `edited_by/at`, plus an `edit_log` (record, field, old, new, user, timestamp).
- **Pages:** (1) Review queue — flag reason + fields + inline PDF/image preview of the split file +
  Gmail link (account-pinned via `fix_gmail_link`); reviewer submits Answer + Notes to Learn,
  written back onto the `review_queue` row EXACTLY as the CLI Phase-1 loop expects (UI does NOT
  classify — next `classify_emails.py` run resolves it). An **AI-assist panel** next to Notes to
  Learn drafts BOTH a record-specific answer and a generalizable learning rule from a rough note —
  "Use in my answer" fills both fields (read-only; §5n). A **Track outcome** checkbox keeps the item in
  the **Tracked** tab so its resolution can be confirmed before writing a rule (derived live, no API;
  §5p). An **"Add PDFs & reprocess"** panel uploads documents (e.g. from an unreadable zip) that the
  next classify run reprocesses the record with (§5r). A one-click
  **Ignore / dismiss** (list button + item-page control) removes an item from the queue instantly with
  an undoable snapshot — see the **Ignored** tab and §5o. Answered items stay
  listed with an **"Answered — pending reprocess"** status until a run resolves them (§5f); a
  read-only **"Other records from this email"** breadcrumb shows recorded siblings (§5e). A free-form **Notes** thread
  (add/list) lives on the item — stored in **`review_notes`** keyed by `review_queue.filename` (stable
  across classify runs; the row id is not) with author + timestamp. Posting a note NEVER writes
  answer/notes_to_learn/reviewed_by/at (a note can't resolve an item — it stays "Needs review") and
  redirects back to the item detail. The queue list shows an amber 📝 note-count badge in the Status
  cell (`list_review_notes` / `add_review_note` / **`get_review_note_counts`** — a SEPARATE query,
  never JOINed into `list_open_reviews` since `review_notes.id` would collide with its `ORDER BY id`;
  `POST /review/<id>/note`). Separate from "Notes to Learn" (which trains the AI). Reviewers are
  **add-only, no edit**; **admins can delete** a note (`delete_review_note` → `POST
  /review/<id>/note/<note_id>/delete`, mirrors the Learnings delete: admin-only + confirm, logs an
  `edit_log` row `table_name='review_notes'`; non-admins see no control and are blocked server-side).
  Deleting a note only removes the note row — it never touches the item's answer/resolution. Purely
  additive — the CLI pipeline (`classify_emails.py`, `database.load_review_queue`/`save_review_queue`)
  is untouched.
  (2) QB entry — non-skip invoices + credit memos (credit negative), Entered-in-QB
  checkbox (sets flag + by/at), Possible-Duplicate flag; **filters: not-yet-entered + store
  (`distinct_values`) + run + Possible-Duplicate (all / only flagged / only not)**, all
  combinable; **clickable column-header sorting**; shows sales-tax/shipping columns; a clickable
  **Source email** column groups all records from one email (→ `/browse?email=`). **Bulk-mark
  entered:** a checkbox per row + a select-all-visible header box + a "Mark selected as Entered
  in QB" button (`POST /qb/bulk_entered`) that reuses `set_entered_in_qb` per row (same
  `entered_in_qb` + `entered_qb_by/at` stamp as the single toggle). **Export CSV**
  (`GET /qb/export.csv`) downloads the CURRENTLY FILTERED rows in QuickBooks-entry column order
  (vendor, store, invoice #, date, amount, sales tax, shipping, possible-duplicate, file link,
  gmail link; local file links made absolute). **Copy visible rows** copies the same columns as
  TSV client-side (from each row's `data-*` attributes) for direct paste into QuickBooks/Excel.
  **Per-user column view** (§5q): a **Columns ⚙** panel lets each user pick which of the canonical
  columns show + their order (saved to `users.preferences`); the table, Copy, and — by default — the
  CSV all follow it, with a **Standard QB order** button as the always-canonical fallback.
  (2b) **Payments** — dedicated tab (`/payments`, `list_payments`) listing every payment record
  (vendor, store, amount, payment date, reference/method, source-file + Gmail links, Entered-in-QB
  + `entered_qb_by/at`). Payments are ALWAYS QB-entered (never excluded/duplicate-checked), so it
  has the same **Entered-in-QB toggle + bulk-mark** as QB entry (reusing `/qb/{record_type}/…/entered`
  and `/qb/bulk_entered` with `record_type='payment'`), filters (store, entered/not-entered, run,
  payment-date range), clickable column sorting, and **Export CSV** (`/payments/export.csv`) +
  **Copy visible rows** (same as QB entry). The payment-date range ranges on `payment_date` but
  **falls back to `date_added`** for rows with a null/non-ISO `payment_date`, so a date filter
  never silently drops a payment. (3) Edit —
  inline edit of vendor/store/#/date/amount/tax/shipping; logs old→new + user; re-evaluates
  `skip_qb_import` + Possible-Duplicate via the pipeline's own functions. (4) **Records** — the
  merged Browse + History tab (§5h): a **source toggle** switches between *Records* (the
  `all_records` view — recorded invoices/credit-memos/payments **plus in-review items** marked
  "In review") and *History ledger* (the READ-ONLY `invoice_history` dedup ledger). Each source
  keeps its full column + filter set, **clickable column-header sorting**, file + Gmail links, and
  a shared **Excluded-vendors filter** (include / exclude / only, default *exclude*).
  (4b) **Chat** — read-only grounded Q&A over the data (§5i).
  (5) Learnings editor — list/add/edit/delete rows in the SAME `learnings` table the pipeline
  injects each run (edits take effect next run; UI re-implements nothing); create/edit/delete
  logged to `edit_log` keyed by rule text (the pipeline's DELETE+re-INSERT churns ids/audit
  columns, so text-keyed edit_log is the durable audit); delete is admin-only + confirmed.
  (6) **Cards** (`/cards`, §5k) — discovery worklist of every card last-4 seen across invoices,
  credit memos & payments (count, last seen, example vendor, current store); unmapped first; inline
  store `<select>` → `/cards/assign` upserts into `card_store_map` so the next classify run
  auto-assigns that store and stops flagging those records merely for an uncertain store. Editor for
  existing mappings at `/reference/card_store_map` (**Card → store**).
  (7) Users (admin) — create user, enable/disable, **plus per-user Edit** (display name, role
  admin/reviewer, activate/deactivate, password reset). Admin-only; every change logged to
  `edit_log` (table `users`) — the password VALUE is never logged, only that it was reset.
- **Column sorting:** Review queue, QB entry, and Users have **clickable column headers** (the
  `sort_th` macro) that set `sort`/`direction` query params — same server-side ORDER BY mechanism
  Browse/History use — while keeping the active filters. Sort keys are whitelisted per query
  (`_order_clause` in `web_db.py`), so a bad/hostile sort value can never inject SQL.
- **Large previews:** the inline PDF/image preview (Review item + Edit pages) fills ~82% of the
  viewport height, is user-resizable (drag the bottom edge), and has an **Open full size ↗** link
  that opens the raw document in a new tab (`.preview` rules in `base.html`).
- **Nav:** six grouped dropdowns (primary link + caret-opened secondary items); the open page's item
  is highlighted and its parent group underlined via `request.url.path` matching in `base.html` — so
  it's always clear which page/section you're on. See §5j "Grouped dropdown nav".
- **Reads always fresh:** `web_db.connect()` returns an autocommit connection so every request
  reflects the latest committed data (no stale MVCC snapshot); the one multi-statement write
  (`update_record_field`) flips autocommit off for atomicity.
- **Previews:** `invoice_files/`, `payment_files/`, `review_pending/` mounted read-only; Drive links
  used directly when a `file_path` is an https URL.
- **Additive guarantee:** the CLI never creates or reads the web-only columns/tables; `database.init_db`
  is unchanged. Web schema is created only by `web_db.init_web_schema()` at web startup.

---

## 9. Current state (as of 2026-08-07)
- **First LIVE pipeline run succeeded** (Gmail → Claude → PostgreSQL + Excel export).
  `gmail_fetcher.py` pulled 31 emails; the classifier processed them all with **0 errors**:
  30 emails / 40 result rows (32 invoices, 5 payments, 1 statement, 1 shipment_notice,
  1 correspondence), 14 qb_ready rows, 3 review-queue items (each its own split file),
  14 invoice files + 5 payment files. PostgreSQL is the live store; `--excel` exports the
  full DB. Google auth (both scopes, Techloq TLS) confirmed working. Drive (`--drive`) not
  yet exercised live.
- **Two bugs found & fixed during first runs:**
  1. **Skip bug** — `gmail_fetcher` pre-creates each email's `emails` row (to hold its
     gmail_link); the old skip check treated any error-free `emails` row as "processed", so
     freshly-fetched emails were skipped. Fixed with a `status` column: fetcher sets
     `'fetched'`, classifier sets `'processed'`; skip-logic counts only `'processed'`.
  2. **Excel export was per-run in DB mode** — `--excel` now re-reads the FULL database
     (`database.load_all_result_rows`) so the workbooks are a complete snapshot.
- **Gmail query broadened & tuned** — now `in:inbox newer_than:30d -category:promotions
  -category:social` (was `has:attachment ...`). Rationale on record:
    - dropped `has:attachment` so body-only invoices/payment requests are fetched too;
    - the `newer_than` window is a **double-entry safety rail** (not dedup — the
      `InvoicesFetched` label already prevents re-fetch with no API call): it stops the
      pipeline from surfacing old, already-paid invoices as "new" and re-entering them in QB;
    - excluded Promotions/Social as obvious non-invoice noise;
    - `GMAIL_MAX_RESULTS` (default 100) is a per-run cost cap only — runs self-pace via the
      label, so it's not a coverage limit.
  Override any of this with `GMAIL_QUERY` / `GMAIL_MAX_RESULTS`. OPEN: whether to drop the
  `newer_than` window to backfill full inbox history (needs deliberate handling of the
  old-invoice double-entry risk).

---

## 10. Open issues / next steps
- [ ] **Watch cost/volume:** with the broadened query, every inbox email is fetched +
      classified (non-invoices land as spam/correspondence, `route_to: none`). Scope via
      `GMAIL_QUERY` (label/from:) if the inbox is noisy.
- [ ] Verify **payments** created this run genuinely had payment info (not bare "Paid");
      spot-check Garden of Life / Maxi Health / Natures Cure rows.
- [ ] `BluPax Pharma T3 Statement` classified once as `statement`, once as `shipment_notice`
      — minor consistency nit to watch.
- [ ] Exercise `--drive` live (upload + share links).
- [ ] Consider a review UI over the DB (review queue + gmail_link are ready for it).
- [ ] Decide whether **payments** should also appear in `qb_ready` (currently invoices + credit memos).
- [x] ~~A reprocess resets `entered_in_qb`~~ — FIXED 2026-10-07 (`qb_entered_marks`; see "FIX: a reprocess keeps
      'entered in QuickBooks'").
- [x] ~~A replace can delete OTHER records filed under the same email~~ — FIXED 2026-10-08 (scoped
      `replace_records`; see "FIX SWEEP: the review spec's Known gaps", item 1).
- [x] ~~Released sales orders flagged at hold can cycle~~ — FIXED 2026-10-08 (`apply_released_order_check`, item 2).
- [ ] **Orphan copies in `invoice_files/`** from earlier late flags (1–6 unreferenced copies per record, e.g.
      Easynurse ×4, Nutranext ×6). Harmless; deliberately not deleted. Optional cleanup — waiting on Joel (gap 14).
      CORRECTION 2026-10-08: not every record links to Drive.
      - `invoice_files/`: 1,638 files (640 MB). 1,361 aren't linked from any record: local twins of
        Drive-uploaded documents, superseded copies, and ~297 `__N` extras. 277 ARE the File link of a live
        invoice/credit memo.
      - `payment_files/`: 59 files are the File link of a live payment.
      - Those 336 records were saved without a Drive upload. Whole runs on 10/6–10/7 uploaded nothing, plus a
        few single records in otherwise-uploaded runs.
      - Any cleanup must skip linked files. Worth checking why those runs didn't upload: `--drive` /
        `UPLOAD_TO_DRIVE` not set, or upload failures.
- [x] ~~Doubled gate sentences on open review items~~ — 0 left as of 2026-10-08 (they cleared on reprocess).
- [ ] **5 missed duplicates found by `recheck_duplicates.py` (2026-10-08)** — A&S #17496, Boiron #FI26144209,
      MasterKo #35045 (reminder + invoice), Micro Merchant Systems payment ARC_2TG28BJLQ8AU, Garden of Life
      $152.83 payments. None entered in QB. Joel ran `--apply` (12:15); all 5 are now on Possible duplicates
      (Garden of Life after the web restart) — settle each there.
- [x] ~~Housekeeping decisions~~ — 2026-10-08: tables archived (gap 13), dormant helper deleted (gap 9),
      leftover copies left as is (gap 14).
- [x] ~~336 records' documents on this PC only, not Drive~~ — DONE: Drive on by default + retries (2026-10-08);
      Joel ran `upload_local_files_to_drive.py --commit` (336 `drive-backfill` edit_log rows; 0 local-only links on 2026-10-12).
- [x] ~~Store preference + Possible duplicates still compare PRINTED vendor spellings~~ — FIXED 2026-10-12 (see
      "FIX: duplicate steps that still compared PRINTED vendor spellings"); MaxiHealth #803670 settled (kept #3492).
      Still printed-spelling: `database.find_recorded_invoice_ref` (sales-order supersede) — low impact.
- [x] ~~"⚠ differs from matched vendor" pill is too broad~~ — FIXED 2026-10-12: fuzzy / stale mappings only
      (970 → 43 records). Needs the web-app restart.
- [x] ~~Incento Magic II #12598: two clean live copies~~ — SETTLED 2026-10-12 (Joel-approved): kept #3381,
      ignored #3446 + #3451. See "FIX: kept copies join the duplicate ledger …".
- [x] ~~A copy a person keeps never enters the duplicate ledger~~ — FIXED 2026-10-12, see "FIX: kept copies join
      the duplicate ledger …". Built as a load-time rule in `database.load_history` / `load_payment_history`
      instead of the web keep actions (proposal 1 below): `save_history` rewrites the whole ledger from memory,
      so a row the web wrote during a run would have been wiped. Original finding, for reference:
  - **A copy a person keeps never enters the duplicate ledger (found 2026-10-12).**
      - `keep_and_ignore_others` / un-flag clear the flag but don't add the kept copy to `invoice_history`.
        The pipeline adds only survivors it decided itself (`append_to_history`).
      - Later copies therefore match only the ledger's OTHER (often already-ignored) copy.
      - `find_live_invoice` doesn't skip ignored copies, so the store-preferred branch "supersedes" an
        already-ignored copy and keeps the newcomer clean (the Incento case above).
      - 6 live invoices are in this state: #3381, #3378, #2473, #2474, #3492, and #3493 (now ignored).
      - Proposed fix:
        (1) the web keep actions add the kept copy to the ledger;
        (2) in the store-preferred / store-conflict branches, an already-ignored prior → flag this copy for a
            person instead of keeping it clean;
        (3) a one-off ledger backfill for the kept copies.
- [x] ~~24 real bills excluded from QB because the billed store is on the excluded-vendor list~~ — FIXED 2026-10-12
      (prompt + `apply_unlisted_skip_gate`). Data: Joel said Easyfast SHOULD be excluded, so it was added to the list
      (#11021 stays out; 4 more Easyfast invoices became skip-QB). The other 7 were re-included, with their
      documents on Drive. See "FIX: kept copies join the duplicate ledger …". Original finding:
  - **24 real bills excluded from QB because the billed STORE is on the excluded-vendor list (found 2026-10-12).**
      - 3M / Refuah / Westgate / Madison Pharmacy are on the list (for inter-store transfers). When a real vendor
        bills one of them, the model sometimes sets `skip_qb_import`. The notes even say "Refuah is the store,
        Easyfast is the vendor".
      - `apply_vendor_exclusion` only ever flips False→True, so nothing corrects it.
      - None of the 8 vendors below is, or ever was, on the list.
      - Live with no other copy going to QB, $5,891.24 in total:
        - Crown Care #224718 $106.63
        - A&S Sales #17448 $648.00
        - STIAC Laundry #3283 $28.79
        - Easyfast #11021 $384.00
        - RX Labels #782622 $3,554.82
        - Ashpa #3231467 $260.00
        - KayCo #2562631 $681.00
        - KayCo #2581435 $228.00
      - Crown Care #224656 $53.31 was entered in QB anyway. The rest have another copy going to QB, or are
        ignored duplicates.
      - Fix options:
        - prompt: the list applies to the bill's ISSUER, never the store billed;
        - code: a skip for a vendor NOT on the list (other than the multi-store cost-split shape) goes to review
          instead of silently excluding;
        - data: re-include the 8 after checking QB they weren't entered by hand.
      - Checked 2026-10-12: none of the 8 is in the Oct 8 QB export (rows through 10/07). If one is entered by hand
        later, the QB upload counts its row as already in the app (`record_on_file` includes excluded records).
        So it won't sit on Unmatched.
- [x] ~~`recheck_duplicates.py` dry run, 2026-10-12: 3 findings~~ — Incento and Costco SETTLED 2026-10-12
      (Joel-approved: kept #3381 / payment #65). MaxiHealth #803670 left as Joel decided. Original list:
  - **`recheck_duplicates.py` dry run, 2026-10-12: 3 findings.**
      - Incento #12598 (above).
      - MaxiHealth #803670: #3175 $6,957.40 vs kept #3492 $6,682.27. Joel un-flagged #3175 on 10/08; `--apply`
        would re-flag #3492.
      - Costco payment #20751521485459184: #65 $214.77 (final charge) vs #66 $219.07 (the temporary card
        authorization), same order under two spellings. Suggested: keep #65, ignore #66.
- [ ] After the next few live runs, run `python verify_model_signals.py` to sanity-check the AI-dependent
      signals (sales orders / reminders / total_source), and consider ticking "Trust line-item totals" for
      vendors that only ever send line items (Zahler, Freeda Health, Maxi Health had repeat flags).
- [ ] **Save the Oct 8 QB upload's unmatched rows (2026-10-12).** First restart the web app, so the next upload uses
      the filtered no-match branch. Then run
      `python backfill_qb_unmatched.py "C:\Users\joel scher\Downloads\All 7 stores.xlsx" --apply`.
      It saves 584 pending rows, marks 4 records entered, and copies the file to `qb_imports\`.
      See "FIX + BACKFILL: QB 'no match' rows…".
- [ ] **3 undetected duplicate copies of QB-entered records (found 2026-10-12 by the backfill).** None is entered.
      - Reismans #R204825 $311.85 @ Madison: inv #890 (scan) is a copy of entered #1131.
      - Prime Packaging credit #AR092424 $1,200 @ Maple: cm #112 is a copy of entered #104.
      - Lantev #8610704 $584.74 @ Refuah: inv #162 is a copy of entered #257, whose scan read the number as
        '86I0704'. The duplicate check doesn't fold OCR I/1; the QB matcher does.
      - Ignore the copies. Otherwise the next QB upload's Tier 1 marks them entered (existing behavior). That still
        prevents a double entry, but hides the duplicate.
- [ ] **Vita Vocal Health #002875 @ Refuah: QB has $81,900.00, the app's invoice #1473 has $819.00.**
      Probably a decimal slip in QB. Check the QB entry.
- [ ] Unmatched page: no pagination or bulk dismiss. After the backfill it holds ~584 rows, growing roughly
      250/month, mostly QB bills that never come by email. Consider hiding rows older than ~4 months, or a bulk
      dismiss.
- [ ] A review-list row a person REJECTS doesn't become a pending unmatched row; only rows with no candidate at
      all do. The backfill handles batch 6's rejected rows. Small: 14 rejected vendor+amount+date rows in batch 6,
      mostly dated before 08/07.

### Incident + recovery (2026-08-12) — invalid API key
- A classify run executed with the permanent `ANTHROPIC_API_KEY` set to the literal
  placeholder `sk-ant-your-key`, so every Claude call returned **401 invalid x-api-key**.
  ~188 emails exhausted retries and were routed to the review queue as `unreadable`
  ("Automatic reading failed…"). These were NOT genuine review items.
- **Recovery:** `recover_api_failures.py` (additive, dry-run by default, backs up first).
  It targets ONLY `review_queue WHERE email_type='unreadable' AND question LIKE
  'Automatic reading failed%'`, backs up `review_queue` to a timestamped table, moves each
  `filename` from `review_pending/` back to `test_emails/`, sets `emails.status='fetched'`,
  and deletes those queue rows. Genuine flags (invoice/payment with real vendor/store/amount)
  are untouched.
- **Result (verified):** 188 requeued (0 missing), review_queue down to the 18 genuine items
  (0 "Automatic reading failed" left), 188 `emails.status='fetched'`, backup
  `review_queue_backup_20260812_162011` kept. NEXT: `python classify_emails.py` with the fixed
  key will re-classify the 188 (~188 real API calls + attachments; cost/time) — good ones land
  in results/qb_ready, only genuinely ambiguous ones return to the queue.
- **Recurrence (same day):** the reprocess run hit an **intermittently flaky** Techloq/network
  (many `Connection error`s), which re-queued ~188 emails as "unreadable" again AND a mid-loop
  crash (a `shutil.move` on a vanished file) aborted the run **before the end-of-run DB save** —
  leaving 12 emails moved to `processed_emails/` but never saved (orphaned).
- **Resilience fixes to `classify_emails.py` (this is a real robustness upgrade, not cosmetic):**
  1. **Fail fast on 401** — an auth error now stops the whole run immediately with "API key
     invalid" instead of mangling every email.
  2. **Connection/API errors are `infra_error`** — the email is LEFT in `test_emails/` for a later
     retry (not moved, not recorded, not queued), so a flaky network no longer pollutes the review
     queue or scatters files. Only genuine empty/unparseable responses become "unreadable".
  3. **Crash-safe moves** — `_safe_move()` logs+skips a missing/locked file instead of aborting the
     whole run.
- **Recovered again:** re-ran `recover_api_failures.py --commit` (188 back to test_emails, backup
  `review_queue_backup_20260812_164038`) + a one-off orphan mover for the 12. State now:
  `test_emails/`=200 (to reprocess), `review_pending/`=18 genuine, `processed_emails/`=67 saved.
- **Move-after-save (FIXED):** source-email moves are now DEFERRED — collected into `pending_moves`/
  `pending_deletes` during the loop and executed only AFTER all DB writes (results, history, review
  queue, learnings) succeed. If anything raises first, nothing is moved and every email stays in
  `test_emails/`/`review_pending/` for a clean retry — no more move-but-unsaved orphans. Also
  reordered so `save_history` runs after `save_results` (a later failure can't double-count history
  on retry, since the email is already `status='processed'`). Residual: the derived invoice/split
  files (`invoice_files/`, `review_pending/` split PDFs) are still written in-loop, so a crash can
  leave a few regenerable orphan artifacts (not data loss; overwritten/re-created on reprocess).

### Oversized-image 400 loop (2026-08-19)
- **Symptom:** an email with a large `image001.jpg` attachment failed on EVERY run with
  `Error code: 400 … invalid_request_error … image dimensions exceed max allowed size: 8000 pixels`,
  printed as "API unreachable … left for retry". Because the old code treated ALL non-401 API
  exceptions as `infra_error` (transient → leave in `test_emails/` and retry), this deterministic
  400 would retry forever and the email would never process.
- **Two-part fix (both additive, CLI-safe):**
  1. **Downscale before sending** — `build_attachment_blocks` now runs every image through
     `_downscale_image_for_api` (Pillow), capping the long edge at `MAX_IMAGE_EDGE = 1568` (aspect
     preserved; within-limit images and any error pass through untouched). The API downscales past
     ~1568 px internally anyway, so there's no legibility/cost downside — and it clears the 8000-px
     hard limit, so this class of email now classifies normally.
  2. **Don't retry a deterministic 400** — `classify_with_retry` detects `invalid_request_error` /
     `error code: 400` (`_is_invalid_request_error`) and returns a PLAIN error (not `infra_error`),
     so any still-unfixable 400 routes to the review queue as "unreadable" for a human instead of
     looping. 401 still fails the run fast; genuine connection errors are still `infra_error`.
- **Result:** re-running `classify_emails.py` reprocesses the stuck email — the image is downscaled
  and read normally. No live API needed to verify the resize path; unit-tested with real oversized
  JPEGs (9000×500 → 1568×87, tall 500×12000 → 65×1568, identity for within-limit/at-cap, safe
  fallback on corrupt bytes) and the error-classification split.

### Gotcha: "Connection error" was actually a whitespace-in-key bug (2026-08-12)
- Root cause of the repeated "API call failed: Connection error." on EVERY email: the
  `ANTHROPIC_API_KEY` env var had leading/trailing spaces (` sk-ant-… `). An HTTP header can't
  have surrounding whitespace, so httpx raised `LocalProtocolError: Illegal header value` — which
  the Anthropic SDK re-labels as `APIConnectionError: Connection error.` It was NOT a network issue
  (plain connectivity tests with a clean key returned 401 fine).
- **Fix:** `main()` now does `api_key = os.environ.get("ANTHROPIC_API_KEY","").strip()` and rejects
  an empty/placeholder key (`your-key`, or not starting with `sk-ant-`) up front. So a stray space
  or placeholder can never again masquerade as a network failure.

### Gmail deep-link fix (2026-08-12)
- Bug: links were `https://mail.google.com/mail/u/0/#all/<id>` — `u/0` = the viewer's FIRST
  Google account, not the invoice mailbox, so they opened the wrong inbox and never found the
  message.
- Fix: `gmail_fetcher.py` now reads the authenticated mailbox from the API
  (`getProfile()['emailAddress']` → `maplegroupap@lakewoodrx.com`, not hardcoded) and builds
  `https://mail.google.com/mail/u/0/?authuser=<mailbox>#all/<id>`. It also captures the RFC822
  `Message-ID` header per message (stored in the sidecar) so a `#search/rfc822msgid:<id>` fallback
  (`build_gmail_link_rfc822`) is available if `#all/<hex>` proves unreliable in the browser.
- Backfill: `backfill_gmail_links.py` (dry-run default, `--commit`, idempotent) rewrote the 603
  already-stored links to insert `?authuser=<mailbox>` — sidecar (268), emails (257), review_queue
  (17), invoices (47), payments (9), credit_memos (5). 0 broken remain.
- **NEEDS a human browser check (Claude can't):** confirm a corrected link lands on the specific
  message (not just the mailbox). If `#all/<id>` doesn't, switch links to the `rfc822msgid` form
  (new fetches already have the Message-ID; old links would need a re-fetch of the header).
- Not done (explicitly optional): 429 backoff/Retry-After.

### Recently completed
- **Web UI pages 5 & 6** (additive): Learnings editor (curate the pipeline's `learnings` table
  in-browser; edit_log audit) and read-only Invoice-history viewer (searchable/paged, links to
  source docs). Plus two web fixes: TemplateResponse moved to the new request-first Starlette
  signature; read connections set autocommit so pages show freshly-committed rows without a restart.
- **Phase 2B web review UI** (FastAPI, additive over the same Postgres): per-person logins +
  audit, review-queue answering (feeds the CLI resolve loop), QB-entry checkboxes, inline field
  edits (reusing the pipeline's skip/dup rules), browse/filter, inline file previews, LAN launcher
  with firewall guidance. All modules compile; routes + every template render-tested; LAN-IP
  detection verified. NOT yet run against a live DB/browser (needs DATABASE_URL + the office LAN).
- **Automated pipeline upgrade:** Gmail fetch, PostgreSQL storage (Excel optional via `--excel`),
  Google Drive uploads (`--drive`), Gmail deep-links carried onto every record, one-flow OAuth
  with Techloq-aware TLS, Excel→Postgres migration, `run_all.py`.
- Per-record review flagging + per-record split-file reprocess (no false duplicates);
  `Possible Duplicate` rename; `max_tokens`→8192 + `stop_reason` diagnostics; review loop +
  learnings; per-record files; credit memos (negative in QB); paid-order split; revised-invoice
  flagging; route_to consistency; octet-stream detection; Maple address; separate invoice/payment
  folders; multi-record PDF page-splitting.

## 11. Known bugs / limitations
- Page-splitting relies on the model reporting correct `source_pages`; on a wrong/absent
  value it safely falls back to the whole PDF (no data loss, but the file may hold extra pages).
- Page auto-rotation (§5d) likewise relies on the model's `page_rotations`; a wrong/absent
  value just means a page saves at its original orientation (display-only, never data loss).
- Two simultaneous runs would clobber outputs — run one at a time.
- If a file is open in Excel during a run, the write fails (close it first).
- Reprocessing a review item regenerates its files under new names; old orphan files may
  remain in `invoice_files_prev_run/` (backups) but not in the live folders after a clean run.

## 12. Deferred to "phase 2" (per user)
- Handling payment-demand / reminder emails — auto-reply portion still deferred. (Auto-RESOLVE
  of reminders that reference a prior invoice IS now built — see §5e below.)
- Acting on recall notices and utility auto-pay confirmations (currently ignored).

### 5e. Payment-reminder auto-resolve (built 2026-09-23)
Some emails are not a new bill but a reminder/past-due/dunning notice about an EARLIER invoice.
Previously these were classified as normal invoices, so a same-number reminder auto-ignored as a
duplicate (~79 historical, creating phantom ignored records) while an amount-differing reminder
(late fee, running balance) surfaced on /duplicates as a "possible" revision (misrouting), and a
reminder for an invoice never received was recorded as a clean new live invoice with no signal.
Now:
- Prompt: added `is_payment_reminder` (bool, default false) to each invoice record, plus an
  instruction (just above `route_to guidance`) telling the model to set it true ONLY when the email
  clearly points at a PRIOR specific invoice (references its #/amount, asks for payment of it), still
  filling vendor/invoice_number/invoice_amount so the pipeline can match it. A first-time bill that
  merely says "due today/upon receipt" is a NORMAL invoice; a reminder listing SEVERAL prior invoices
  is a `statement` (use statement_invoices). The flag is NOT a duplicate judgment — the model never
  sees our records; the pipeline decides on-file vs. not.
- Gate in `apply_duplicate_checks` (invoice loop, right after `check_duplicate`): for a record with
  `is_payment_reminder`, if ANY prior match exists (status != "none") -> auto-resolve
  (`duplicate_ignored=True`, cleared review, NOT added to history), matching regardless of amount so a
  late-fee reminder no longer clutters /duplicates. If NO match (status "none") -> do NOT auto-resolve:
  left as a live record with a "no matching invoice on file — possible missed invoice" note appended to
  `notes` (surfaces on /qb for a human, per the one case that must still be reviewed).
- Additive/safe: default-false flag means absent → gate skipped → existing behavior unchanged. The
  model-side detection (does it correctly set the flag?) can only be validated on a live classify run
  with the API key; the deterministic gate is unit-tested (on-file / not-on-file / normal invoice).
- Reconciles with §11's rule at prompt line ~500 ("don't infer DUPLICATION from 'reminder' wording"):
  that still holds — is_payment_reminder classifies the email KIND, not duplication.

### 2026-10-08 — Duplicate detection by STANDARD vendor + /qb Export Excel
- Bug: every duplicate check (check_duplicate, check_duplicate_payment, _earlier_store_copy,
  _flag_orders_already_recorded, payment-reminder matching) compared the raw `vendor_matched`
  spelling, so the same invoice recorded as e.g. 'Costco (Same-Day/Instacart)' vs 'Costco',
  'MaxiHealth' vs 'Maxi Health', 'Prestige Sales Inc.' vs 'A&S Sales' was never matched. Found 37
  live same-invoice pairs on /qb (27 with one copy already entered in QB).
- Fix: new `_dedupe_vendor_key()` = resolve_standard_vendor() (same lookup as standardization) then
  _norm_vendor_dedupe; falls back to the raw spelling. Left unchanged on purpose: _stable_review_key
  (review identity — changing it would orphan notes), _replaced_by (same-file scoping),
  _norm_vendor_std_key. Verify on the real DB: `python verify_dedupe_std_vendor.py`.
- Cleanup of existing pairs: `cleanup_std_vendor_dups.py` (dry run; `--apply` writes) — keeps the
  QB-entered copy (else the vendor-email copy / lowest id) via web_db.keep_and_ignore_others.
- /qb now has an **Export Excel** button (/qb/export.xlsx, same handler/filters/ticked rows/columns
  as the CSV). Invoice # and other text columns stored as text, so long numbers (Costco
  20751521485459184 showed as 2.07515E+16 in the CSV) and leading zeros survive.
- Still open: some single emails produced several identical records (TopRx #10850190 ×4 from one
  email; several McKesson scans ×2–3) — separate bug, not yet investigated.

### FIX: Store-spelling edits in Setup now reach already-processed records (2026-10-08)

- Bug: `qb_vendor` / `qb_account` are snapshotted on each invoice/credit memo when classified. Editing
  a store column (e.g. `name_maple`) in Vendor Names / Account Names never updated existing records
  (the reprocess backfill only touches rows with NULL standard vendor/account), so the QB export kept
  the old name (seen: Vitality Plus @ Maple).
- Fix: `web_db.refresh_qb_names(cur, map_table, username, standard_names=None, dry_run=False)` —
  re-derives qb_vendor / qb_account (store column if filled, else standard name — same rule as
  apply_vendor_standardization) for records with `entered_in_qb IS NOT TRUE`; logs each change to
  edit_log. Called from `edit_reference` for vendor_name_map / account_name_map, and from
  `qb_match.apply_vendor_name_corrections_from_qb_history` after it applies corrections.
- **[refresh_qb_names.py](refresh_qb_names.py)** (new): one-time catch-up; dry-run by default,
  `--commit` to write. NOT yet run against the live DB.
