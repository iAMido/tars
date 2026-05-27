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
import time
from typing import Any

from tars.db import Database
from tars.prompt import SYSTEM_BLOCK, TOOLS
from tars.router import LLMResponse, call
from tars.tools import run_tool

log = logging.getLogger("tars.agent")

HISTORY_LIMIT = 40
TOOL_LOOP_MAX = 4


class Agent:
    def __init__(self, db: Database, cfg) -> None:
        self.db = db
        self.cfg = cfg

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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or "",
                            "content": result,
                        }
                    )
                continue  # loop back into the LLM with tool results in context

            # No tool calls: this is the final assistant turn.
            await self._save_turn(
                thread_key,
                "assistant",
                resp.text,
                model=resp.model,
                cost=resp.cost_usd,
                tier=tier,
            )
            return {
                "text": resp.text,
                "cached_tokens": resp.cached_tokens,
                "cost_usd": total_cost,
                "model": resp.model,
                "provider": resp.provider,
                "steps": step + 1,
            }

        # Loop exhausted.
        log.warning("tool loop exhausted for thread %s", thread_key)
        return {
            "text": "Tool loop exhausted before reaching a final answer.",
            "cached_tokens": 0,
            "cost_usd": total_cost,
            "model": "",
            "provider": "",
            "steps": tool_loop_max,
        }
