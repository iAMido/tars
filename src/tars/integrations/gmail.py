"""Gmail integration — read-only, async wrapper around google-api-python-client.

google-api-python-client is sync. We wrap calls in asyncio.to_thread so the
bot's event loop never blocks on Google round trips.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from googleapiclient.discovery import build

from tars.integrations.google_auth import load_credentials

log = logging.getLogger("tars.integrations.gmail")


def _build_service(creds):
    # cache_discovery=False avoids a noisy first-call HTTPS discovery fetch.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _fetch_unread_since_sync(since_unix_ts: int, max_results: int) -> list[dict]:
    creds = load_credentials()
    svc = _build_service(creds)
    # Gmail search syntax: after:<unix_seconds> is undocumented but works;
    # the safer documented form is after:YYYY/MM/DD which only gives day-level
    # resolution. We use Unix here for the 12h "overnight" window.
    q = f"is:unread after:{int(since_unix_ts)}"
    res = svc.users().messages().list(
        userId="me", q=q, maxResults=max_results
    ).execute()
    msg_refs = res.get("messages") or []

    out: list[dict] = []
    for ref in msg_refs:
        msg = svc.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in (msg.get("payload") or {}).get("headers", [])}
        out.append(
            {
                "id": ref["id"],
                "thread_id": msg.get("threadId"),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": (msg.get("snippet") or "")[:240],
            }
        )
    return out


async def fetch_unread_since(since_unix_ts: int, max_results: int = 20) -> list[dict]:
    """List unread emails received after `since_unix_ts`, newest first.

    Returns list of {id, thread_id, from, subject, date, snippet}.
    Empty list if nothing matches. Logs and re-raises on auth errors —
    the caller (morning_briefing) should catch and degrade gracefully."""
    try:
        msgs = await asyncio.to_thread(
            _fetch_unread_since_sync, since_unix_ts, max_results
        )
    except Exception as e:  # noqa: BLE001
        log.warning("gmail fetch failed: %s", e)
        raise
    log.info("gmail: %d unread since ts=%d", len(msgs), since_unix_ts)
    return msgs


# --- ad-hoc CLI smoke test ---


async def _smoke() -> None:
    import time
    since = int(time.time()) - 12 * 3600
    msgs = await fetch_unread_since(since, max_results=5)
    for m in msgs:
        print(f"  - {m['date']}  {m['from']}\n    {m['subject']}")


if __name__ == "__main__":
    asyncio.run(_smoke())
