"""Stale thread summarize — Sundays at 17:00 (before the 18:00 reconcile).

Finds conversations with no activity in the last STALE_DAYS, summarizes
the messages into a single note, and tags the conversation as archived
via scheduler_state. The original messages stay (we don't delete history
silently), but they no longer count toward future stale-thread queries.

The summary note is tagged ['thread_summary', '<thread_key>'] so retrieval
finds it without it polluting "real" notes.
"""

from __future__ import annotations

import json
import logging
import time

from tars.scheduler.email_summary import state_get, state_set

log = logging.getLogger("tars.scheduler.stale_thread_summarize")

STALE_DAYS = 30
MIN_MESSAGES_TO_SUMMARIZE = 6  # below this, not worth the LLM call

PROMPT_TEMPLATE = (
    "TARS voice. Compress this conversation thread into a single concise note "
    "(3-6 sentences max). Capture: who/what was discussed, decisions reached, "
    "open questions. No greeting, no commentary. Cite specific note IDs if "
    "referenced in the messages.\n"
    "\n"
    "Thread key: {thread_key}\n"
    "Messages (oldest first):\n{messages}\n"
    "\n"
    "Summary:"
)


async def stale_thread_summarize_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await stale_thread_summarize(rt.agent, rt.db, rt.cfg)


async def stale_thread_summarize(agent, db, cfg) -> dict:
    t0 = time.time()
    now = int(t0)
    cutoff = now - STALE_DAYS * 86400

    # Conversations with last activity older than cutoff AND not already archived.
    rows = await db.fetch_all(
        "SELECT c.thread_key, "
        " (SELECT MAX(ts) FROM messages m WHERE m.thread_key = c.thread_key) AS last_ts, "
        " (SELECT COUNT(*) FROM messages m WHERE m.thread_key = c.thread_key) AS msg_count "
        "FROM conversations c "
        "HAVING last_ts IS NOT NULL AND last_ts < ?",
        (cutoff,),
    )

    summarized = 0
    skipped = 0
    for r in rows:
        thread_key = r["thread_key"]
        # Already archived?
        if await state_get(db, f"stale_thread_summarize.archived.{thread_key}"):
            skipped += 1
            continue
        if int(r["msg_count"]) < MIN_MESSAGES_TO_SUMMARIZE:
            # Tiny thread — just mark archived without summarizing.
            await state_set(db, f"stale_thread_summarize.archived.{thread_key}", str(now))
            skipped += 1
            continue

        # Pull the messages.
        msg_rows = await db.fetch_all(
            "SELECT role, content FROM messages "
            "WHERE thread_key = ? AND role IN ('user','assistant') "
            "ORDER BY id LIMIT 100",
            (thread_key,),
        )
        transcript = "\n".join(
            f"[{m['role']}] {(m['content'] or '').strip()[:500]}" for m in msg_rows
        )
        if not transcript.strip():
            continue

        try:
            out = await agent.chat(
                thread_key=f"job:stale_thread_summarize:{thread_key}",
                user_text=PROMPT_TEMPLATE.format(
                    thread_key=thread_key, messages=transcript[:8000],
                ),
                tier="cron_default",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("stale_thread_summarize: LLM failed for %s (%s)", thread_key, e)
            continue
        summary = (out["text"] or "").strip()
        if not summary:
            continue

        # Save the summary as a note.
        cur = await db.execute(
            "INSERT INTO notes(created_at, source, body, tags) VALUES (?, ?, ?, ?)",
            (
                now, "thread_summary",
                f"Thread summary [{thread_key}]:\n\n{summary}",
                json.dumps(["thread_summary", thread_key]),
            ),
        )
        note_id = cur.lastrowid

        # Mark thread archived.
        await state_set(db, f"stale_thread_summarize.archived.{thread_key}", str(now))
        summarized += 1
        log.info(
            "summarized thread %s -> note #%s (%d msgs, $%.6f)",
            thread_key, note_id, int(r["msg_count"]), out.get("cost_usd", 0.0),
        )

    elapsed = time.time() - t0
    log.info(
        "stale_thread_summarize: summarized=%d skipped=%d elapsed=%.2fs",
        summarized, skipped, elapsed,
    )
    return {"summarized": summarized, "skipped": skipped, "elapsed_s": elapsed}
