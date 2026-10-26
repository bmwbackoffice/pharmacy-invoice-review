"""READ-ONLY. Loads /correspondence (each tab) against the real database and prints the full error."""
import traceback
import review_app as ra
from fastapi.testclient import TestClient

ra.current_user = lambda request: {"id": 0, "username": "diagnose", "role": "admin"}
client = TestClient(ra.app, raise_server_exceptions=True)
for q in ("", "?status=follow_up", "?status=handled", "?status=ignored", "?status=all", "?kind=reply"):
    try:
        r = client.get("/correspondence" + q)
        print(f"/correspondence{q}: OK ({r.status_code})")
    except Exception:
        print(f"/correspondence{q}: FAILED")
        traceback.print_exc()
        break
