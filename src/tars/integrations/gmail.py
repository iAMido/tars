"""Gmail integration — read-only, async wrapper around google-api-python-client.

google-api-python-client is sync. We wrap calls in asyncio.to_thread so the
bot's event loop never blocks on Google round trips.

Two fetch modes:
  - `fetch_unread_since(ts, max_results)` — metadata only (from/subject/snippet),
    fast, used for /stats and where the body isn't needed.
  - `fetch_unread_since(ts, max_results, include_body=True)` — full message with
    the first ~1500 chars of text/plain extracted, used by morning_briefing to
    let the LLM actually summarize content and extract action items.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

from googleapiclient.discovery import build

from tars.integrations.google_auth import load_credentials

log = logging.getLogger("tars.integrations.gmail")

BODY_MAX_CHARS = 1500   # cap per-email body sent to LLM; keeps prompts bounded


def _build_service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", html)
    return _WS_RE.sub(" ", text).strip()


def _b64_decode_safe(data: str) -> str:
    """Gmail uses URL-safe base64 without padding. Pad and decode."""
    if not data:
        return ""
    pad = (-len(data)) % 4
    data = data + ("=" * pad)
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_body_text(payload: dict) -> str:
    """Recursively walk MIME parts, preferring text/plain over text/html."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        return _b64_decode_safe((payload.get("body") or {}).get("data") or "")

    if mime.startswith("multipart/"):
        # Prefer text/plain anywhere in the tree.
        plain = ""
        html = ""
        for part in payload.get("parts") or []:
            pt = part.get("mimeType", "")
            if pt == "text/plain":
                plain = _b64_decode_safe((part.get("body") or {}).get("data") or "")
                if plain:
                    break
            elif pt == "text/html" and not html:
                html = _b64_decode_safe((part.get("body") or {}).get("data") or "")
            elif pt.startswith("multipart/"):
                nested = _extract_body_text(part)
                if nested:
                    return nested
        if plain:
            return plain
        if html:
            return _strip_html(html)

    if mime == "text/html":
        return _strip_html(_b64_decode_safe((payload.get("body") or {}).get("data") or ""))

    return ""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _fetch_unread_since_sync(
    since_unix_ts: int, max_results: int, include_body: bool,
) -> list[dict]:
    creds = load_credentials()
    svc = _build_service(creds)
    q = f"is:unread after:{int(since_unix_ts)}"
    res = svc.users().messages().list(
        userId="me", q=q, maxResults=max_results
    ).execute()
    msg_refs = res.get("messages") or []

    out: list[dict] = []
    for ref in msg_refs:
        fmt = "full" if include_body else "metadata"
        kwargs: dict[str, Any] = {"userId": "me", "id": ref["id"], "format": fmt}
        if not include_body:
            kwargs["metadataHeaders"] = ["From", "Subject", "Date"]
        msg = svc.users().messages().get(**kwargs).execute()

        payload = msg.get("payload") or {}
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        item = {
            "id": ref["id"],
            "thread_id": msg.get("threadId"),
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": (msg.get("snippet") or "")[:240],
        }
        if include_body:
            body_text = _extract_body_text(payload)
            # Trim noise: replace multiple newlines with single, strip URLs in tracking
            # pixels (basic dedup). Keep readable line breaks.
            body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            item["body"] = body_text[:BODY_MAX_CHARS]
        out.append(item)
    return out


async def fetch_unread_since(
    since_unix_ts: int,
    max_results: int = 20,
    include_body: bool = False,
) -> list[dict]:
    """List unread emails received after `since_unix_ts`, newest first.

    Returns list of {id, thread_id, from, subject, date, snippet} (always)
    plus {body} if include_body=True (first ~1500 chars of text/plain).
    Empty list if nothing matches. Logs and re-raises on auth errors —
    the caller should catch and degrade gracefully."""
    try:
        msgs = await asyncio.to_thread(
            _fetch_unread_since_sync, since_unix_ts, max_results, include_body,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("gmail fetch failed: %s", e)
        raise
    log.info(
        "gmail: %d unread since ts=%d (body=%s)",
        len(msgs), since_unix_ts, include_body,
    )
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
