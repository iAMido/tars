"""One-time Google OAuth flow for TARS.

Run this ONCE on your dev box (the machine with a real browser).
It opens a browser, you click Allow, and a refresh token is minted into
~/.tars/google_token.json. That token then gets scp'd to the VPS where
TARS uses it from then on. Google refresh tokens don't expire under normal
use as long as the OAuth app stays in the same status.

Usage:
    uv run python scripts/google_oauth_bootstrap.py

Requires:
    ~/.tars/client_secret.json from the Google Cloud console (Desktop OAuth client).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# Read-only scopes. We never modify the user's Gmail or Calendar.
# Gmail readonly is a "restricted" scope — fine for personal use with test users,
# but Google asks for app verification if you ship to broader audiences.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def main() -> int:
    secret_path = Path.home() / ".tars" / "client_secret.json"
    token_path = Path.home() / ".tars" / "google_token.json"

    if not secret_path.exists():
        print(f"ERROR: {secret_path} not found.", file=sys.stderr)
        print(
            "Download it from https://console.cloud.google.com/auth/clients "
            "(Desktop OAuth client -> DOWNLOAD JSON) and save it there.",
            file=sys.stderr,
        )
        return 1

    print(f"Using client secret: {secret_path}")
    print(f"Will mint token to:  {token_path}")
    print()
    print("Browser will open. Sign in as idomosseri@gmail.com and click Allow.")
    print("If you see a 'This app isn't verified' warning, click Advanced ->")
    print("'Go to TARS (unsafe)' — it's safe, you wrote it, Google just hasn't reviewed it.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    # port=0 picks a random free port. Google redirects back to localhost:<port>
    # with the auth code. open_browser=True is default.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    token_path.write_text(creds.to_json(), encoding="utf-8")
    # Lock down on POSIX. On Windows this is a no-op but harmless.
    try:
        token_path.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass

    payload = json.loads(token_path.read_text())
    print()
    print(f"✓ Token saved to {token_path}")
    print(f"  scopes:        {payload.get('scopes')}")
    print(f"  has refresh:   {bool(payload.get('refresh_token'))}")
    print(f"  account hint:  {payload.get('client_id', '')[:30]}...")
    print()
    print("Next: scp this file to the VPS:")
    print(
        "  scp $HOME\\.tars\\google_token.json "
        "tars-vps:/home/tars/.tars/google_token.json"
    )
    print("Then on VPS: chmod 600 ~/.tars/google_token.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
