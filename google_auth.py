"""
Shared Google OAuth for the pharmacy invoice pipeline.

Both gmail_fetcher.py and drive_uploader.py import from here so the user
authenticates ONCE for both scopes (gmail.modify + drive.file). The resulting
token is cached in token.json; client secrets come from credentials.json.

Techloq TLS-inspection proxy: this machine's HTTPS traffic is intercepted, so
every Google call (OAuth token exchange, discovery, Gmail, Drive) must trust
the same ca_bundle.pem used for the Anthropic API. We apply it three ways so it
sticks no matter which HTTP stack a Google library reaches for:
  - env vars REQUESTS_CA_BUNDLE / SSL_CERT_FILE (requests / stdlib ssl),
  - an explicit requests Session (verify=ca_bundle) for token refresh/exchange,
  - httplib2.Http(ca_certs=ca_bundle) for the googleapiclient service.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CA_BUNDLE = BASE_DIR / "ca_bundle.pem"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

# One OAuth flow, both scopes — authenticate once, use for Gmail and Drive.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]

# Point every TLS stack at the Techloq-aware bundle before any Google import
# does its own SSL setup. setdefault so a user-set value still wins.
if CA_BUNDLE.exists():
    _bundle = str(CA_BUNDLE)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
    os.environ.setdefault("SSL_CERT_FILE", _bundle)
    os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", _bundle)


def _verified_request():
    """A google.auth Request backed by a requests Session that trusts the
    Techloq bundle — used for token refresh / exchange."""
    import requests
    from google.auth.transport.requests import Request
    session = requests.Session()
    if CA_BUNDLE.exists():
        session.verify = str(CA_BUNDLE)
    return Request(session=session)


def get_credentials():
    """Return valid OAuth credentials for SCOPES, running the consent flow the
    first time and caching to token.json thereafter."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(_verified_request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise SystemExit(
                    f"Missing {CREDENTIALS_FILE.name}. Download an OAuth 'Desktop app' "
                    "client secret from Google Cloud Console and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            # run_local_server opens a browser once; the token then covers both scopes.
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def build_service(api_name, api_version, creds=None):
    """Build a googleapiclient service whose HTTP transport trusts the Techloq
    bundle (httplib2 ignores REQUESTS_CA_BUNDLE, so we set ca_certs directly)."""
    import httplib2
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp

    if creds is None:
        creds = get_credentials()
    http = httplib2.Http(ca_certs=str(CA_BUNDLE) if CA_BUNDLE.exists() else None)
    authed_http = AuthorizedHttp(creds, http=http)
    return build(api_name, api_version, http=authed_http, cache_discovery=False)


if __name__ == "__main__":
    # Handy one-shot: trigger/refresh auth and confirm both scopes are granted.
    c = get_credentials()
    print("Authenticated. Scopes:", getattr(c, "scopes", SCOPES))
    print(f"Token cached at {TOKEN_FILE}")
