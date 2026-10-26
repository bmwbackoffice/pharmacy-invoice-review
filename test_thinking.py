"""Compare the classifier's thinking settings on real emails (2026-10-23, API cost work).

Claude spends part of every answer "thinking" (billed as output, never shown). On dense scans it thought
until the 21,000-token limit and returned nothing. This runs the SAME email through the real classifier
with each setting and prints tokens, time, cost and whether it produced the same records.
Costs a few cents per email per setting (more for big scans). Changes nothing in the app.

    python test_thinking.py                         # 3 recent emails from processed_emails\\
    python test_thinking.py "review_pending\\Scan to E-mail Server Job - 1a15b5db6cbb723f.eml"
"""
import sys
import time
from pathlib import Path

import classify_emails as ce

VARIANTS = [
    ("default", {}),
    ("effort medium", {"output_config": {"effort": "medium"}}),
    ("effort low", {"output_config": {"effort": "low"}}),
    ("thinking off", {"thinking": {"type": "disabled"}}),
]

base = Path(__file__).parent
if len(sys.argv) > 1:
    files = [Path(a) if Path(a).is_absolute() else base / a for a in sys.argv[1:]]
else:
    files = sorted((base / "processed_emails").glob("*.eml"), key=lambda p: p.stat().st_mtime)[-3:]

_rows, learnings_text = ce.load_learnings()
import os
from types import SimpleNamespace
client = SimpleNamespace(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())

usage_seen = {}
_orig = ce._create_message


def _spy(*a, **k):
    r = _orig(*a, **k)
    usage_seen["u"] = r.usage
    usage_seen["stop"] = r.stop_reason
    return r


ce._create_message = _spy


def summary(c):
    if not c:
        return "NO RESULT"
    recs = []
    for key in ("invoices", "credit_memos", "payments"):
        for r in c.get(key) or []:
            recs.append(f"{key[:-1]}:{r.get('vendor_matched')}#{r.get('invoice_number') or r.get('credit_memo_number') or r.get('reference_number')}"
                        f"=${r.get('invoice_amount') or r.get('credit_amount') or r.get('payment_amount')}")
    return f"{c.get('email_type')} | " + ("; ".join(recs) if recs else "no records")


for f in files:
    print("=" * 100)
    print(f.name)
    text, attachments, _html = ce.read_email_content(f)
    for label, extra in VARIANTS:
        ce.API_EXTRA = extra
        t = time.time()
        try:
            res = ce.classify_email(client, f.name, text, attachments, learnings_text, "")
            u = usage_seen.get("u")
            out, think = (u.output_tokens, u.thinking_tokens) if u else (0, 0)
            inp = (u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens) if u else 0
            cost = ((u.input_tokens * 3 + u.cache_creation_input_tokens * 6 + u.cache_read_input_tokens * 0.3
                     + out * 15) / 1e6) if u else 0
            err = res.get("error")
            print(f"  {label:14} {time.time()-t:5.0f}s  output={out:6} thinking={think:6}  ~${cost:.3f}  "
                  f"{'ERROR: ' + err[:80] if err else summary(res.get('classification'))}")
        except Exception as e:
            print(f"  {label:14} FAILED: {str(e)[:200]}")
ce.API_EXTRA = {}
