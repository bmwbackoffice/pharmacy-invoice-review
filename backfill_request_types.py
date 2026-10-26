"""One-time (2026-10-16): give a request type (emails.topic, database.CORR_TOPICS) to emails already on
the Correspondence page, so type-based rules ('payment reminders -> forward to X') can act on them.
New emails get it from the classifier automatically.

  python backfill_request_types.py          -> Open + Follow-up emails without a type
  python backfill_request_types.py --all    -> every Correspondence-page email without a type (incl. handled/ignored)
Uses the SAME REQUEST TYPE wording the classifier uses. Needs ANTHROPIC_API_KEY. Re-runs skip typed emails.
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
from classify_emails import MODEL, CA_BUNDLE, SYSTEM_PROMPT_TEMPLATE

m = re.search(r"REQUEST TYPE:.*?(?=\n\n|\Z)", SYSTEM_PROMPT_TEMPLATE, re.S)
if not m:
    sys.exit("Couldn't find the REQUEST TYPE rule in the classifier prompt -- stopping.")
RULE = m.group(0)
ALL = "--all" in sys.argv

PROMPT = """An email in a pharmacy group's AP inbox. Apply this rule to it and return ONLY JSON {"topic": "<key>"}:

%s

From: %s
To: %s
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
conn.autocommit = True        # never hold a lock on emails while looping
try:
    cur = conn.cursor()
    cur.execute("SELECT filename, gmail_message_id FROM emails "
                "WHERE (email_type = 'correspondence' OR needs_response IS TRUE) AND topic IS NULL "
                "AND coalesce(status, '') <> 'fetched' "
                + ("" if ALL else "AND coalesce(corr_status, 'open') IN ('open', 'follow_up') ")
                + "ORDER BY coalesce(received_at, updated_at) DESC")
    todo = cur.fetchall()
    print(f"Emails to type: {len(todo)}")
    labels = dict(database.CORR_TOPICS)
    done = failed = 0
    for filename, gid in todo:
        try:
            hdr, body, atts = corr_forward._load_original(filename, gid)
            text = PROMPT % (RULE, hdr.get("From", ""), hdr.get("To", ""), hdr.get("Subject", ""),
                             ", ".join(a[0] for a in atts) or "none", (body or "")[:6000])
            resp = client.messages.create(model=MODEL, max_tokens=50, messages=[{"role": "user", "content": text}])
            raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            mm = re.search(r"\{.*\}", raw, re.S)
            topic = (json.loads(mm.group(0)).get("topic") if mm else "") or "other"
            topic = topic if topic in database.CORR_TOPIC_KEYS else "other"
            cur.execute("UPDATE emails SET topic = %s WHERE filename = %s", (topic, filename))
            done += 1
            print(f"  {labels[topic][:28]:<28} | {hdr.get('Subject', filename)[:70]}")
        except Exception as e:
            failed += 1
            print(f"  (skipped {filename}: {e})")
    print(f"\nTyped {done}" + (f", {failed} skipped" if failed else "") + ".")
finally:
    conn.close()
