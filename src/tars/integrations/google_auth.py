"""Google OAuth credential loader with auto-refresh.

The token.json minted by scripts/google_oauth_bootstrap.py contains a refresh
token. When the short-lived access token expires (typically every hour), we
call refresh() with the refresh token and persist the new access token back
to the same file. This way TARS never needs human re-auth under normal use.

Token files contain a refresh token = treat them as a secret. The bootstrap
script chmods to 0600 on POSIX.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

log = logging.getLogger("tars.integrations.google_auth")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def token_path() -> Path:
    return Path.home() / ".tars" / "google_token.json"


def load_credentials() -> Credentials:
    """Load creds, refresh if expired, persist refreshed access token back.

    Raises FileNotFoundError if the token file is missing — that means the
    OAuth bootstrap was never run. Raises if refresh fails (invalid_grant =
    user revoked access on Google's side; need to re-bootstrap)."""
    tp = token_path()
    if not tp.exists():
        raise FileNotFoundError(
            f"{tp} not found. Run scripts/google_oauth_bootstrap.py on a "
            f"workstation, then scp the resulting token.json here."
        )

    creds = Credentials.from_authorized_user_file(str(tp), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("google credentials expired; refreshing")
            creds.refresh(Request())
            tp.write_text(creds.to_json(), encoding="utf-8")
            try:
                tp.chmod(0o600)
            except Exception:  # noqa: BLE001
                pass
        else:
            raise RuntimeError(
                "google credentials invalid and no refresh token. "
                "Re-run scripts/google_oauth_bootstrap.py."
            )
    return creds
