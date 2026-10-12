"""
Confirm the raw-httpx API path the classifier now uses (classify_emails._create_message) works.
Makes ONE tiny request through that exact function. Should print SUCCESS in a few seconds.

    python test_api.py
"""
import os
import sys
import time
import traceback
from types import SimpleNamespace

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import classify_emails as ce

key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
print("key set:", bool(key), "| model:", ce.MODEL, "| ca_bundle:", ce.CA_BUNDLE.exists())
print("-" * 60, flush=True)
if not key:
    print("No key in environment."); sys.exit(1)

client = SimpleNamespace(api_key=key)   # _create_message only needs .api_key
print("Calling classify_emails._create_message (raw-httpx path)...", flush=True)
t = time.time()
try:
    resp = ce._create_message(
        client, model=ce.MODEL, max_tokens=16,
        system=[{"type": "text", "text": "You reply tersely.", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        timeout=30.0,
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    print(f"\nSUCCESS in {time.time()-t:.1f}s -> {text!r}")
    print("usage:", resp.usage)
    print("\nThe pipeline's API path works. Run:  python classify_emails.py")
except Exception as e:
    print(f"\nFAILED in {time.time()-t:.1f}s -> {type(e).__name__}: {str(e)[:300]}")
    traceback.print_exc()
