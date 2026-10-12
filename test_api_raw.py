"""
Raw-httpx API test — bypasses the Anthropic SDK entirely to isolate SDK vs network/API/account.
Uses the real key + ca_bundle, a hard 30s timeout (raw httpx honors it reliably). Prints timing,
HTTP status, and the response/error. Also reports whether a proxy env var is set.

    python test_api_raw.py
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx

MODEL = "claude-sonnet-5"
CA = "ca_bundle.pem" if os.path.exists("ca_bundle.pem") else True

key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
print("key set:", bool(key), "| len:", len(key))
for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "NO_PROXY"):
    if os.environ.get(v):
        print(f"  proxy env {v} = {os.environ[v]}")
print("model:", MODEL, "| ca_bundle:", CA)
print("-" * 60, flush=True)

hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
body = {"model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}]}

t = time.time()
try:
    r = httpx.post("https://api.anthropic.com/v1/messages", headers=hdrs, json=body,
                   timeout=httpx.Timeout(30.0, connect=15.0), verify=CA)
    print(f"RESPONSE in {time.time()-t:.1f}s -> HTTP {r.status_code}")
    print(r.text[:600])
except Exception as e:
    print(f"FAILED in {time.time()-t:.1f}s -> {type(e).__name__}: {str(e)[:300]}")
