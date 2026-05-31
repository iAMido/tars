"""Stateless Agent class.

`Agent.chat(thread_key, user_text, tier)` is the public surface. It:
  1. ensures the conversation row exists (idempotent)
  2. loads the last N messages of history
  3. constructs [system] + history + [user] (frozen prefix at index 0)
  4. calls the LLM router
  5. if tool calls came back, runs them, appends results, loops (capped)
  6. persists every turn (cost, model, tier) into messages

The Agent holds NO per-conversation state on `self`. Thread keys like
`tg:{chat_id}`, `job:morning_briefing`, `web:asaf` namespace conversations.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from tars.db import Database
from tars.prompt import SYSTEM_BLOCK, TOOLS
from tars.router import LLMResponse, call
from tars.tools import run_tool

log = logging.getLogger("tars.agent")

HISTORY_LIMIT = 40

# Anti-hallucination guardrail.
#   We saw the LLM reply "Noted. [note:17]" without ever calling save_note —
#   the user thinks his note was saved, but nothing happened. Hard fix: after
#   every chat turn, if the assistant cites a note id that was NOT produced or
#   read by a tool call this turn, strip the citation and prepend a warning.
#   Citations from search_memory / get_note / save_note results are trusted.
_NOTE_CITE_RE = re.compile(r"\[note:(\d+)\]")
_NOTE_ID_IN_TOOL_RESULT_RE = re.compile(r'"(?:note_id|doc_id|id)"\s*:\s*(\d+)')
# Interactive chat. Legitimate longest flows:
#   open_reminder:  save_note -> get_current_time -> open_followup -> final = 4
#   close_reminder: search_memory -> list_followups -> save_note ->
#                   close_followup -> final = 5
# Plus headroom for an exploratory search at the start. Beyond 6 the model is
# usually thrashing. Router's max_tokens cap bounds each individual turn.
# /research can override with a larger value via the chat() parameter.
TOOL_LOOP_MAX = 6


class Agent:
    def __init__(self, db: Database, cfg) -> None:
        self.db = db
        self.cfg = cfg
        # Tools (run_tool) discover cfg via this side-channel so we don't
        # have to thread cfg through every call signature.
        db._cfg = cfg  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _ensure_thread(self, thread_key: str) -> None:
        row = await self.db.fetch_one(
            "SELECT 1 FROM conversations WHERE thread_key = ?", (thread_key,)
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO conversations(thread_key, created_at, meta) VALUES (?, ?, ?)",
                (thread_key, int(time.time()), "{}"),
            )

    async def _load_history(self, thread_key: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        """Load prior turns for context.

        Only user + final-assistant turns are loaded. Intermediate assistant-
        with-tool-calls turns and tool-role responses are intra-turn implementation
        details — replaying them across invocations would require persisting
        tool_call_id linkages, and any drift between them produces a 400 from
        OpenAI's strict tool-call validation. Skip them entirely.
        """
        rows = await self.db.fetch_all(
            "SELECT role, content FROM messages "
            "WHERE thread_key = ? AND role IN ('user','assistant') AND tool_calls IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (thread_key, limit),
        )
        # rows are reverse-chronological; flip them.
        return [{"role": r["role"], "content": r["content"]} for r in reversed(list(rows))]

    async def _save_turn(
        self,
        thread_key: str,
        role: str,
        content: str,
        *,
        tool_calls: list[dict] | None = None,
        model: str | None = None,
        cost: float = 0.0,
        tier: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO messages("
            " thread_key, ts, role, content, tool_calls, cost_usd, model, tier"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_key,
                int(time.time()),
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                cost,
                model,
                tier,
            ),
        )

    # ------------------------------------------------------------------
    # Public chat surface
    # ------------------------------------------------------------------

    async def chat(
        self,
        thread_key: str,
        user_text: str,
        tier: str = "interactive_fast",
        tool_loop_max: int = TOOL_LOOP_MAX,
    ) -> dict[str, Any]:
        await self._ensure_thread(thread_key)
        history = await self._load_history(thread_key)

        # The frozen prefix MUST sit at index 0. History tails. User input is the
        # final element. Do not f-string anything into SYSTEM_BLOCK.
        messages: list[dict] = [{"role": "system", "content": SYSTEM_BLOCK}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        await self._save_turn(thread_key, "user", user_text)

        # Track every note id that a tool surfaced this turn — used by the
        # citation guardrail at the end. Includes save_note returns,
        # get_note returns, and any doc_id mentioned in search_memory hits.
        # Also pre-seed with any ids that appear in the INPUT user_text:
        # callers like morning_briefing pass a JSON payload containing
        # `"id": N` for each note already retrieved, and the LLM is free to
        # cite those without needing a tool call to "re-verify" them.
        verified_note_ids: set[int] = set()
        for m in _NOTE_ID_IN_TOOL_RESULT_RE.finditer(user_text):
            try:
                verified_note_ids.add(int(m.group(1)))
            except ValueError:
                pass

        total_cost = 0.0
        for step in range(tool_loop_max):
            resp: LLMResponse = await call(
                messages=messages,
                tools=TOOLS,
                tier=tier,
                cfg=self.cfg,
                db=self.db,
                thread_key=thread_key,
            )
            total_cost += resp.cost_usd
            log.info(
                "tier=%s model=%s prov=%s tokens=%d/%d cached=%d cost=$%.6f step=%d",
                tier,
                resp.model,
                resp.provider,
                resp.prompt_tokens,
                resp.completion_tokens,
                resp.cached_tokens,
                resp.cost_usd,
                step,
            )

            if resp.tool_calls:
                # In-flight tool calls only live inside `messages` for this
                # invocation. We do NOT persist intermediate assistant-with-
                # tool-calls or tool-role turns to the messages table — they're
                # implementation details, and re-loading them across calls
                # leads to orphaned-tool_call validation errors.
                messages.append(
                    {
                        "role": "assistant",
                        "content": resp.text or "",
                        "tool_calls": resp.tool_calls,
                    }
                )
                for tc in resp.tool_calls:
                    name = (tc.get("function") or {}).get("name") or ""
                    args = (tc.get("function") or {}).get("arguments") or "{}"
                    result = await run_tool(self.db, name, args)
                    log.info("tool=%s result=%s", name, result[:200])
                    # Harvest any note ids the tool surfaced for the
                    # citation guardrail below.
                    if name in ("save_note", "get_note", "search_memory"):
                        for m in _NOTE_ID_IN_TOOL_RESULT_RE.finditer(result):
                            try:
                                verified_note_ids.add(int(m.group(1)))
                            except ValueError:
                                pass
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or "",
                            "content": result,
                        }
                    )
                continue  # loop back into the LLM with tool results in context

            # No tool calls: this is the final assistant turn.
            final_text = _strip_unverified_note_citations(
                resp.text, verified_note_ids, thread_key,
            )

            await self._save_turn(
                thread_key,
                "assistant",
                final_text,
                model=resp.model,
                cost=resp.cost_usd,
                tier=tier,
            )
            return {
                "text": final_text,
                "cached_tokens": resp.cached_tokens,
                "cost_usd": total_cost,
                "model": resp.model,
                "provider": resp.provider,
                "steps": step + 1,
            }

        # Loop exhausted. The model spent every iteration calling tools and
        # never produced a user-facing reply. The tools may well have
        # SUCCEEDED (e.g. close_followup committed) — so returning a bare
        # "exhausted" string would lie to the user. Do ONE final tools-off
        # call to force a text summary based on the tool results in context.
        log.warning(
            "tool loop exhausted for thread %s — forcing final text turn", thread_key,
        )
        try:
            messages.append({
                "role": "user",
                "content": (
                    "[system] Tool budget exhausted. Summarize what you "
                    "actually accomplished in 1-2 short lines, TARS voice. "
                    "No more tool calls."
                ),
            })
            final_resp: LLMResponse = await call(
                messages=messages,
                tools=None,           # tools=None ⇒ tool_choice not sent ⇒ text only
                tier=tier,
                cfg=self.cfg,
                db=self.db,
                thread_key=thread_key,
            )
            total_cost += final_resp.cost_usd
            final_text = _strip_unverified_note_citations(
                final_resp.text or "Done.", verified_note_ids, thread_key,
            )
            await self._save_turn(
                thread_key, "assistant", final_text,
                model=final_resp.model, cost=final_resp.cost_usd, tier=tier,
            )
            return {
                "text": final_text,
                "cached_tokens": final_resp.cached_tokens,
                "cost_usd": total_cost,
                "model": final_resp.model,
                "provider": final_resp.provider,
                "steps": tool_loop_max + 1,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("forced-final call failed (%s); returning generic", e)
            return {
                "text": "Done (tool budget exhausted before I could summarize).",
                "cached_tokens": 0,
                "cost_usd": total_cost,
                "model": "",
                "provider": "",
                "steps": tool_loop_max,
            }


def _strip_unverified_note_citations(
    text: str, verified_ids: set[int], thread_key: str,
) -> str:
    """If the model cited any [note:N] for an id that was not surfaced by a
    note-touching tool this turn, the id is unverified — likely hallucinated.

    Action: replace the citation with `[note:?unverified]` and log loudly so
    we can audit. We do NOT silently delete: the user should see that the
    model claimed a note id, and we want to flag the claim as suspicious."""
    if not text:
        return text
    cited = {int(m.group(1)) for m in _NOTE_CITE_RE.finditer(text)}
    if not cited:
        return text
    bogus = cited - verified_ids
    if not bogus:
        return text

    log.warning(
        "citation guardrail: thread=%s bogus_ids=%s verified=%s — stripping",
        thread_key, sorted(bogus), sorted(verified_ids),
    )

    def _sub(m: re.Match[str]) -> str:
        nid = int(m.group(1))
        # Drop the bogus token entirely (and a single leading space if any) so
        # the surrounding prose reads naturally. Leave verified citations alone.
        return "" if nid in bogus else m.group(0)

    cleaned = _NOTE_CITE_RE.sub(_sub, text)
    # Tidy: collapse double spaces left by the deletion + trim trailing
    # whitespace before sections.
    cleaned = re.sub(r" {2,}", " ", cleaned).rstrip()
    n = len(bogus)
    plural = "s" if n != 1 else ""
    return cleaned + f"\n\n_(⚠ {n} unverified citation{plural} removed)_"

