"""One-time (2026-10-16): run the new needs-response check on emails from the last N days (default 14)
that were processed BEFORE the check existed and aren't pure correspondence (invoices, statements,
payments, ...). Uses the SAME COMMUNICATION rule text the classifier now uses (read from
classify_emails.SYSTEM_PROMPT_TEMPLATE), on the stored email's own text. Emails it flags appear on the
Correspondence page with a NEEDS REPLY tag. Record processing is not touched.

  python backfill_needs_response.py              -> first 5 as a sample, shown
  python backfill_needs_response.py --all        -> all of them (last 14 days)
  python backfill_needs_response.py --all --days=30
  python backfill_needs_response.py --redo-flagged -> re-word the ones already flagged (e.g. after a wording change)
  python backfill_needs_response.py --file="Fwd Statement from QuickWit Tech Corp - 1a0ac7278452232c.eml"
                                                 -> check one specific email (any date) and show the result
Cost control: emails with no person-typed text (scanner jobs, no-reply senders, nothing written above a
forward) are settled as "no reply needed" WITHOUT an AI call (corr_prefilter), and a vendor's repeated
template text is checked once per sender -- a "no reply" verdict is reused for the identical template.
Re-running skips emails already checked. Needs ANTHROPIC_API_KEY.
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
import corr_prefilter
from classify_emails import MODEL, CA_BUNDLE, SYSTEM_PROMPT_TEMPLATE

LIMIT = None if "--all" in sys.argv else 5
DAYS = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--days=")), "14"))
ONE = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--file=")), None)
REDO = "--redo-flagged" in sys.argv
if REDO:
    LIMIT = None
if ONE:
    LIMIT = None

m = re.search(r"COMMUNICATION \(every email.*?When unsure, false\.", SYSTEM_PROMPT_TEMPLATE, re.S)
if not m:
    sys.exit("Couldn't find the COMMUNICATION rule in the classifier prompt -- stopping.")
RULE = m.group(0)

PROMPT = """An email arrived in a pharmacy group's AP (accounts payable) inbox. Its documents were already
processed separately. Apply this rule and return ONLY JSON
{"needs_response": true/false, "request": "...or null", "requested_by": "...or null"}:

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
# Autocommit: never hold a transaction open on `emails` while the loop runs -- that blocked the web
# app's startup (its ALTER TABLE waits for us) and then our own saves queued behind it (2026-10-16).
conn.autocommit = True
try:
    cur = conn.cursor()
    cur.execute("""SELECT filename, gmail_message_id, email_type FROM emails
                    WHERE needs_response IS NULL AND status = 'processed'
                      AND coalesce(email_type, '') NOT IN ('correspondence', 'spam')
                      AND coalesce(received_at, updated_at) >= now() - (%s || ' days')::interval
                    ORDER BY coalesce(received_at, updated_at) DESC""", (str(DAYS),))
    todo = cur.fetchall()
    if REDO:
        cur.execute("SELECT filename, gmail_message_id, email_type FROM emails WHERE needs_response IS TRUE")
        todo = cur.fetchall()
    if ONE:
        cur.execute("SELECT filename, gmail_message_id, email_type FROM emails WHERE filename = %s", (ONE,))
        todo = cur.fetchall() or [(ONE, None, "?")]
    print(f"Emails from the last {DAYS} days not yet checked: {len(todo)}"
          + (f" -- doing a sample of {LIMIT} (use --all for the rest)" if LIMIT else ""))
    done = flagged = failed = skipped = reused = calls = 0
    template_no = set()            # sender|template keys an AI call already judged "no reply needed"

    def tkey(frm, txt):
        a = re.search(r"[\w.+-]+@[\w.-]+", frm or "")
        return (a.group(0).lower() if a else (frm or "")) + "|" + re.sub(r"[\d$#.,/-]+", "", txt.lower())[:160]

    for filename, gid, etype in (todo[:LIMIT] if LIMIT else todo):
        try:
            try:
                hdr, body, atts = corr_forward._load_original(filename, gid)
            except ValueError:
                # not an email at all (a hand-imported PDF etc.) -- nothing anyone wrote to us
                database.save_email_communication(filename, False)
                done += 1; skipped += 1
                continue
            if not ONE and not REDO and not corr_prefilter.worth_checking(hdr.get("From"), hdr.get("Subject"), body):
                database.save_email_communication(filename, False)
                done += 1; skipped += 1
                continue
            key = tkey(hdr.get("From"), corr_prefilter.human_text(body))
            if not ONE and not REDO and key in template_no:
                database.save_email_communication(filename, False)
                done += 1; reused += 1
                continue
            calls += 1
            text = PROMPT % (RULE, hdr.get("From", ""), hdr.get("To", ""), hdr.get("Subject", ""),
                             ", ".join(a[0] for a in atts) or "none", (body or "")[:6000])
            resp = client.messages.create(model=MODEL, max_tokens=300,
                                          messages=[{"role": "user", "content": text}])
            raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            mm = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(mm.group(0)) if mm else {}
            need = bool(data.get("needs_response"))
            database.save_email_communication(filename, need, data.get("request"), data.get("requested_by"))
            done += 1
            if not need:
                template_no.add(key)
            if need:
                flagged += 1
                print(f"  NEEDS REPLY [{etype}] {hdr.get('Subject', filename)[:60]}\n       -> {data.get('request')}")
            elif LIMIT or ONE:
                print(f"  no reply needed [{etype}] {hdr.get('Subject', filename)[:60]}")
        except Exception as e:
            failed += 1
            print(f"  (skipped {filename}: {e})")
    print(f"\nChecked {done} ({calls} AI checks; {skipped} had no person-typed text; {reused} repeated a "
          f"template already cleared); {flagged} need a reply" + (f"; {failed} failed" if failed else "") + ".")
    if flagged:
        database.apply_correspondence_rules("needs-response backfill")
finally:
    conn.close()
