"""
Read-only chat Q&A over the invoice/payment data.

Claude answers grounded questions by calling READ-ONLY database query TOOLS (never
model-authored SQL). Each tool runs a fixed, parameterized SELECT via web_db.connect()
(autocommit reads) — the chat can never write. Reuses the Anthropic client + ca_bundle
(Techloq TLS) and DATABASE_URL from the rest of the app. Login is enforced by the caller
(review_app); secrets (API key, DATABASE_URL, users) are never queried or returned.

Public entry point: ask(question, history) -> {"answer": str, "tools_used": [str, ...]}.
"""

import datetime
import decimal
import json
import os

import httpx
import psycopg2.extras
from anthropic import Anthropic

import classify_emails as ce  # reuse MODEL + CA_BUNDLE (Techloq TLS)
import web_db

MAX_TOOL_ITERS = 8            # cap the tool loop so one question can't run away
SEARCH_LIMIT = 25
ANSWER_MAX_TOKENS = 1500

SYSTEM = (
    "You are a careful, READ-ONLY assistant for a multi-location pharmacy group's invoice "
    "system. Answer ONLY from the database query tools provided — never guess, estimate, or "
    "invent records, amounts, vendors, or reasons. If a tool returns nothing, say the data "
    "wasn't found rather than inventing an answer. Cite concrete records by type + id and their "
    "invoice/reference number and amount (e.g. 'invoice #596 (id 12), $100.06'). "
    "When asked WHY something is (or isn't) a possible/duplicate, call explain_duplicate and "
    "report its exact stored reason and the matched record — never speculate about duplicates. "
    "Amounts are USD; credit memos are negative. You cannot make any changes to the data. "
    "Keep answers concise and factual."
)

TOOLS = [
    {
        "name": "search_records",
        "description": ("Search recorded invoices, credit memos, payments, and in-review items. "
                        "All filters optional; text filters are case-insensitive substring matches. "
                        "Returns matched records with their type + id."),
        "input_schema": {
            "type": "object",
            "properties": {
                "vendor": {"type": "string"},
                "store": {"type": "string"},
                "invoice_number": {"type": "string", "description": "invoice # or payment reference"},
                "record_type": {"type": "string", "enum": ["invoice", "credit_memo", "payment", "review"]},
                "status": {"type": "string", "description": "email/record status, e.g. processed or 'In review'"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD, filters on date recorded"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "amount_min": {"type": "number"},
                "amount_max": {"type": "number"},
                "entered": {"type": "string", "enum": ["yes", "no"], "description": "entered in QuickBooks"},
                "limit": {"type": "integer", "description": f"max rows (default {SEARCH_LIMIT}, cap 100)"},
            },
        },
    },
    {
        "name": "get_record",
        "description": "Get the full details of one record by its type and id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_type": {"type": "string", "enum": ["invoice", "credit_memo", "payment", "review"]},
                "record_id": {"type": "integer"},
            },
            "required": ["record_type", "record_id"],
        },
    },
    {
        "name": "explain_duplicate",
        "description": ("Explain, deterministically, why an invoice or payment is (or isn't) flagged as a "
                        "possible/auto-ignored duplicate. Returns the concrete stored reason plus the "
                        "matched record(s) sharing the same vendor + invoice/reference number. Use this for "
                        "any 'why is this a duplicate?' question."),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_type": {"type": "string", "enum": ["invoice", "payment"]},
                "record_id": {"type": "integer"},
            },
            "required": ["record_type", "record_id"],
        },
    },
    {
        "name": "list_review_items",
        "description": "List items in the human review queue (default: only still-open items).",
        "input_schema": {
            "type": "object",
            "properties": {
                "only_open": {"type": "boolean", "description": "default true (unanswered items only)"},
                "limit": {"type": "integer", "description": "max rows (default 25, cap 100)"},
            },
        },
    },
    {
        "name": "totals",
        "description": ("Aggregate count and summed amount over recorded records (excludes in-review items "
                        "unless record_type='review'). Optionally group by store, vendor, record_type, or status."),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["store", "vendor", "record_type", "status"]},
                "record_type": {"type": "string", "enum": ["invoice", "credit_memo", "payment", "review"]},
                "vendor": {"type": "string"},
                "store": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "entered": {"type": "string", "enum": ["yes", "no"]},
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def _rows(cur):
    return [{k: _jsonable(val) for k, val in r.items()} for r in cur.fetchall()]


def _one(row):
    return {k: _jsonable(v) for k, v in row.items()} if row else None


# --------------------------------------------------------------------------- #
# Read-only tool implementations (fixed, parameterized SELECTs)
# --------------------------------------------------------------------------- #

def _search_records(vendor=None, store=None, invoice_number=None, record_type=None,
                    status=None, date_from=None, date_to=None, amount_min=None,
                    amount_max=None, entered=None, limit=SEARCH_LIMIT):
    clauses, params = ["TRUE"], []
    if vendor:
        clauses.append("vendor_matched ILIKE %s"); params.append(f"%{vendor}%")
    if store:
        clauses.append("store_matched ILIKE %s"); params.append(f"%{store}%")
    if invoice_number:
        clauses.append("number ILIKE %s"); params.append(f"%{invoice_number}%")
    if record_type in ("invoice", "credit_memo", "payment", "review"):
        clauses.append("record_type = %s"); params.append(record_type)
    if status:
        clauses.append("email_status ILIKE %s"); params.append(f"%{status}%")
    if date_from:
        clauses.append("date_added >= %s"); params.append(date_from)
    if date_to:
        clauses.append("date_added <= %s"); params.append(date_to)
    if amount_min is not None:
        clauses.append("amount >= %s"); params.append(amount_min)
    if amount_max is not None:
        clauses.append("amount <= %s"); params.append(amount_max)
    if entered == "yes":
        clauses.append("entered_in_qb IS TRUE")
    elif entered == "no":
        clauses.append("entered_in_qb IS NOT TRUE")
    limit = max(1, min(int(limit or SEARCH_LIMIT), 100))
    conn = web_db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT record_type, record_id, vendor_matched, store_matched, number, "
                "record_date, amount, email_status, skip_qb_import, duplicate_suspect, "
                "duplicate_ignored, entered_in_qb, email_filename "
                f"FROM all_records WHERE {' AND '.join(clauses)} "
                "ORDER BY date_added DESC NULLS LAST LIMIT %s", params + [limit])
            recs = _rows(cur)
    finally:
        conn.close()
    return {"count": len(recs), "records": recs}


def _get_record(record_type, record_id):
    if record_type == "review":
        conn = web_db.connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM review_queue WHERE id = %s", (record_id,))
                r = cur.fetchone()
        finally:
            conn.close()
        return _one(r) or {"error": "not found"}
    if record_type not in web_db._TABLE:
        return {"error": f"unknown record_type '{record_type}'"}
    r = web_db.get_record(record_type, record_id)
    return _one(r) or {"error": "not found"}


def _explain_duplicate(record_type, record_id):
    if record_type not in ("invoice", "payment"):
        return {"error": "duplicate checks apply to invoices and payments only"}
    r = web_db.get_record(record_type, record_id)
    if not r:
        return {"error": "record not found"}
    dign, dsus = r.get("duplicate_ignored"), r.get("duplicate_suspect")
    status = ("auto-ignored duplicate" if dign else
              "possible duplicate" if dsus else "not flagged as a duplicate")
    key_col = "invoice_number" if record_type == "invoice" else "reference_number"
    table = "invoices" if record_type == "invoice" else "payments"
    number = r.get(key_col)
    matched = []
    if number:
        conn = web_db.connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT id, email_filename, vendor_matched, {key_col} AS number, amount "
                    f"FROM {table} WHERE lower(btrim(vendor_matched)) = lower(btrim(%s)) "
                    f"AND lower(btrim({key_col})) = lower(btrim(%s)) AND id <> %s "
                    f"ORDER BY id LIMIT 10",
                    (r.get("vendor_matched") or "", number, record_id))
                matched = _rows(cur)
        finally:
            conn.close()
    return {
        "record": {"type": record_type, "id": record_id, "vendor": r.get("vendor_matched"),
                   "number": number, "amount": _jsonable(r.get("amount"))},
        "status": status,
        "reason": r.get("duplicate_reason") or "No duplicate reason recorded.",
        "duplicate_of_source_email": r.get("duplicate_of"),
        "matched_records_same_vendor_and_number": matched,
    }


def _list_review_items(only_open=True, limit=25):
    limit = max(1, min(int(limit or 25), 100))
    where = "WHERE (answer IS NULL OR answer = '')" if only_open else ""
    conn = web_db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, filename, source_email, vendor_matched, store_matched, invoice_number, "
                f"amount, question, answer, flagged_at FROM review_queue {where} "
                "ORDER BY id LIMIT %s", (limit,))
            items = _rows(cur)
    finally:
        conn.close()
    return {"count": len(items), "items": items}


def _totals(group_by=None, record_type=None, vendor=None, store=None,
            date_from=None, date_to=None, entered=None):
    clauses, params = ["TRUE"], []
    if record_type in ("invoice", "credit_memo", "payment", "review"):
        clauses.append("record_type = %s"); params.append(record_type)
    else:
        clauses.append("record_type <> 'review'")   # totals = recorded records by default
    if vendor:
        clauses.append("vendor_matched ILIKE %s"); params.append(f"%{vendor}%")
    if store:
        clauses.append("store_matched ILIKE %s"); params.append(f"%{store}%")
    if date_from:
        clauses.append("date_added >= %s"); params.append(date_from)
    if date_to:
        clauses.append("date_added <= %s"); params.append(date_to)
    if entered == "yes":
        clauses.append("entered_in_qb IS TRUE")
    elif entered == "no":
        clauses.append("entered_in_qb IS NOT TRUE")
    gb = {"store": "store_matched", "vendor": "vendor_matched",
          "record_type": "record_type", "status": "email_status"}.get(group_by)
    where = " AND ".join(clauses)
    conn = web_db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if gb:
                cur.execute(f"SELECT {gb} AS group_value, count(*) AS n, "
                            f"COALESCE(sum(amount), 0) AS total FROM all_records WHERE {where} "
                            f"GROUP BY {gb} ORDER BY total DESC NULLS LAST LIMIT 100", params)
                return {"grouped_by": group_by, "groups": _rows(cur)}
            cur.execute(f"SELECT count(*) AS n, COALESCE(sum(amount), 0) AS total "
                        f"FROM all_records WHERE {where}", params)
            return _one(cur.fetchone())
    finally:
        conn.close()


TOOL_DISPATCH = {
    "search_records": _search_records,
    "get_record": _get_record,
    "explain_duplicate": _explain_duplicate,
    "list_review_items": _list_review_items,
    "totals": _totals,
}


# --------------------------------------------------------------------------- #
# Anthropic client + agentic loop
# --------------------------------------------------------------------------- #

def _client():
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key or "your-key" in api_key or not api_key.startswith("sk-ant-"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set for the web app — the chat needs it "
                           "(set the same key the pipeline uses).")
    # A finite timeout so a stalled call fails fast instead of hanging the request.
    if ce.CA_BUNDLE.exists():
        return Anthropic(api_key=api_key, max_retries=1,
                         http_client=httpx.Client(verify=str(ce.CA_BUNDLE), timeout=60.0))
    return Anthropic(api_key=api_key, max_retries=1, timeout=60.0)


_DRAFT_LEARNING_SYSTEM = (
    "You help a reviewer resolve a flagged record in an invoice classifier. From the record's "
    "context and the reviewer's rough note, produce TWO things:\n"
    "1. \"answer\": the concrete decision for THIS specific record, written as the reviewer's "
    "authoritative answer for it. It MAY reference this record's specifics (e.g. \"This invoice is "
    "for 3M — 112 Hillside is Braun's default billing address; the first line shows the location\"). "
    "This is applied to resolve the record on reprocess.\n"
    "2. \"learning\": ONE self-contained GENERAL rule for the classifier, which is injected with NO "
    "knowledge of this specific record — so it must name the vendor and/or condition explicitly and "
    "must NEVER refer to 'this/that/it/this record'. State the trigger and the action, and when to "
    "(or not to) flag for review. Keep it to 1-3 sentences, plain and specific.\n"
    "Return ONLY a JSON object, no markdown and no commentary: "
    "{\"answer\": \"...\", \"learning\": \"...\"}."
)

DRAFT_MAX_TOKENS = 700


def _draft_context_block(context):
    """Render the record's context as a compact, labeled block for the model."""
    context = context or {}
    fields = [
        ("Vendor", context.get("vendor_matched")),
        ("Store", context.get("store_matched")),
        ("Invoice/Reference #", context.get("invoice_number")),
        ("Amount", context.get("amount")),
        ("Email type", context.get("email_type")),
        ("Source email", context.get("source_email")),
        ("Why the AI flagged it", context.get("question")),
    ]
    lines = [f"- {label}: {value}" for label, value in fields if value not in (None, "")]
    return "\n".join(lines) or "- (no record context available)"


def _parse_draft_json(text):
    """Parse the model's {answer, learning} JSON, tolerating ```json fences. Returns a
    normalized dict with both keys (strings), or None if it isn't a usable JSON object."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        obj = json.loads(t)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return {"answer": (obj.get("answer") or "").strip(),
            "learning": (obj.get("learning") or "").strip()}


def _draft_once(client, messages):
    """One model call for the draft. Returns (parsed_dict_or_None, raw_text)."""
    resp = client.messages.create(
        model=ce.MODEL, max_tokens=DRAFT_MAX_TOKENS,
        system=_DRAFT_LEARNING_SYSTEM, messages=messages)
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return _parse_draft_json(text), text


def draft_learning(rough_text, context, history=None, learnings=None):
    """From a reviewer's rough note + the record's context, draft BOTH a record-specific
    Answer and ONE generalizable Learning rule. READ-ONLY (saves nothing). `history` is a
    list of prior {role, content} turns (the reviewer's notes + prior drafts) so the
    reviewer can refine and get an updated pair. `learnings` (optional) is the current rule
    list, passed so phrasing stays consistent and near-duplicates are avoided.
    Returns {"answer": <this record's decision>, "learning": <general rule>}."""
    client = _client()
    context_block = _draft_context_block(context)
    existing = ""
    if learnings:
        sample = "\n".join(f"- {r}" for r in list(learnings)[:40] if r)
        if sample:
            existing = ("\n\nExisting learning rules (match their style; do NOT restate one "
                        f"that already covers this):\n{sample}")
    # Prefix folded onto the FIRST user turn so the record context is always present,
    # even after several refinement turns, without breaking user/assistant alternation.
    preamble = (f"Record context:\n{context_block}{existing}\n\n"
                "From my note, produce the record-specific answer AND the general learning rule, "
                "returned as JSON.\nMy note: ")

    hist = [m for m in (history or []) if (m.get("content") or "").strip()][-8:]
    messages = []
    if hist:
        first, *rest = hist
        messages.append({"role": "user",
                         "content": (preamble + first["content"].strip())[:4400]})
        for m in rest:
            role = "assistant" if m.get("role") == "assistant" else "user"
            messages.append({"role": role, "content": m["content"].strip()[:4000]})
        if (rough_text or "").strip():
            messages.append({"role": "user", "content": (rough_text or "").strip()[:4000]})
    else:
        messages.append({"role": "user",
                         "content": (preamble + (rough_text or "").strip())[:4400]})

    parsed, raw = _draft_once(client, messages)
    if parsed is None:
        # Parse miss — retry ONCE with an explicit corrective nudge (append the bad
        # output as the assistant turn first, so user/assistant alternation stays valid).
        messages.append({"role": "assistant", "content": (raw or "(no output)")[:4000]})
        messages.append({"role": "user", "content": (
            'That was not valid. Return ONLY a JSON object exactly like '
            '{"answer": "...", "learning": "..."} with both keys and nothing else.')})
        parsed, raw = _draft_once(client, messages)
    return parsed or {"answer": "", "learning": ""}


def ask(question, history=None):
    """Answer one question, grounded in the DB tools. `history` is a list of prior
    {role, content} text turns (most recent last). Returns {answer, tools_used}."""
    client = _client()
    messages = []
    for m in (history or [])[-8:]:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": question[:4000]})

    tools_used = []
    for _ in range(MAX_TOOL_ITERS):
        resp = client.messages.create(
            model=ce.MODEL, max_tokens=ANSWER_MAX_TOKENS, system=SYSTEM,
            tools=TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"answer": text or "(no answer produced)", "tools_used": tools_used}
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                tools_used.append(b.name)
                fn = TOOL_DISPATCH.get(b.name)
                try:
                    out = fn(**(b.input or {})) if fn else {"error": f"unknown tool {b.name}"}
                except Exception as e:  # never let a tool error crash the chat
                    out = {"error": str(e)}
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, default=str)[:12000]})
        messages.append({"role": "user", "content": results})
    return {"answer": "I couldn't finish (too many lookups). Please narrow the question.",
            "tools_used": tools_used}
