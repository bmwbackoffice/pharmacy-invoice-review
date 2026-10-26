"""One-time (2026-10-15): the Correspondence page's "System's read" (emails.info_note) is only
captured for emails classified from today on. This writes one for every OLDER correspondence email
that has none, by asking the same model the classifier uses for a one-sentence read of the stored email
(what it is + whether anyone needs to act). Also fills info_vendor when the model names one.

  python backfill_correspondence_summaries.py            -> does the first 5 and shows them (a sample)
  python backfill_correspondence_summaries.py --all      -> does all of them
Re-running only touches emails still without a summary. Needs ANTHROPIC_API_KEY, like a normal run.
"""
import json
import os
import re
import sys
from pathlib import Path

import httpx
from anthropic import Anthropic

import database
import corr_forward
from classify_emails import MODEL, CA_BUNDLE

LIMIT = None if "--all" in sys.argv else 5

PROMPT = """You are triaging an email that arrived in a pharmacy group's accounts-payable inbox. It was
judged NOT to be an invoice, payment or statement. Return ONLY JSON:
{"summary": "<one sentence, max ~30 words: what this email is, and what (if anything) someone needs to do>",
 "vendor": "<the company it is from/about, or null>"}

From: %s
Subject: %s
Attachments: %s

%s"""

api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
if not api_key:
    sys.exit("ANTHROPIC_API_KEY is not set.")
timeout = httpx.Timeout(90.0, connect=15.0)
client = (Anthropic(api_key=api_key, timeout=timeout, http_client=httpx.Client(verify=str(CA_BUNDLE), timeout=timeout))
          if Path(CA_BUNDLE).exists() else Anthropic(api_key=api_key, timeout=timeout))

database.init_db()
conn = database.connect()
conn.autocommit = True   # don't hold a lock on emails while looping (see backfill_needs_response.py)
try:
    cur = conn.cursor()
    cur.execute("SELECT filename, gmail_message_id FROM emails WHERE email_type = 'correspondence' "
                "AND coalesce(btrim(info_note), '') = '' "
                "ORDER BY coalesce(received_at, updated_at) DESC")
    todo = cur.fetchall()
    print(f"Correspondence emails without a summary: {len(todo)}"
          + (f" -- doing a sample of {LIMIT} (use --all for the rest)" if LIMIT else ""))
    done = failed = 0
    for filename, gid in todo[:LIMIT] if LIMIT else todo:
        try:
            hdr, body, atts = corr_forward._load_original(filename, gid)
            text = PROMPT % (hdr.get("From", ""), hdr.get("Subject", ""),
                             ", ".join(a[0] for a in atts) or "none", (body or "")[:6000])
            resp = client.messages.create(model=MODEL, max_tokens=300,
                                          messages=[{"role": "user", "content": text}])
            raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {"summary": raw.strip()}
            summary = (data.get("summary") or "").strip()
            vendor = (data.get("vendor") or "").strip() or None
            if not summary:
                raise ValueError("empty summary")
            cur.execute("UPDATE emails SET info_note = %s, info_vendor = coalesce(info_vendor, %s) "
                        "WHERE filename = %s", (summary, vendor, filename))
            conn.commit()
            done += 1
            print(f"  [{done}] {hdr.get('Subject', filename)[:60]}\n       -> {summary}")
        except Exception as e:
            conn.rollback()
            failed += 1
            print(f"  (skipped {filename}: {e})")
    print(f"\nWrote {done} summary(ies)" + (f", {failed} skipped" if failed else "") + ".")
finally:
    conn.close()
