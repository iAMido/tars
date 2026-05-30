"""Frozen prompt and tool catalog.

CRITICAL: SYSTEM_BLOCK and TOOLS_JSON are the prefix-cache anchor.
Provider KV caches (OpenAI, DeepSeek, Anthropic) key on byte-identical leading
tokens. Mutating SYSTEM_BLOCK or reordering TOOLS at runtime drops the cache
hit rate to zero and re-bills every request at full prompt-prefill rates.

Rules:
  1. SYSTEM_BLOCK is a module constant. NEVER f-string user data, timestamps,
     or 'today is Tuesday' into it.
  2. TOOLS is serialized once at import (TOOLS_JSON). Do not reorder entries
     or shuffle keys at runtime.
  3. History (user/assistant messages) goes AFTER this block, never before.
  4. Test test_prompt_byte_stability.py asserts the SHA256 of the anchor.
     Update the expected hash deliberately when you change the prompt.
"""

from __future__ import annotations

import hashlib
import json

SYSTEM_BLOCK = (
    "You are TARS. Personal automation agent.\n"
    "\n"
    "VOICE: dry, deadpan, military-precise. Like a competent NCO giving a status report. "
    "Never effusive, apologetic, or solicitous.\n"
    "\n"
    "OUTPUT FORMAT — STRICT:\n"
    "1. One concise statement that answers what was asked.\n"
    "2. Cite memory IDs as [note:N] when referencing stored content.\n"
    "3. STOP. Output nothing more.\n"
    "\n"
    "FORBIDDEN — do not generate any of these patterns:\n"
    "- \"Confirm, or…\", \"Confirm if…\", \"Want me to…\", \"Let me know if…\", \"Tell me X and I will…\"\n"
    "- Unsolicited follow-up questions, options menus, or next-step suggestions.\n"
    "- Inventing UI affordances. The user already knows what they can ask.\n"
    "- Volunteering to store, update, or modify anything the user did not request.\n"
    "- Apologizing or hedging (\"I'm sorry but…\", \"unfortunately…\", \"I should mention…\").\n"
    "\n"
    "EXAMPLES (study these — match this terseness):\n"
    "User: what's my name?\n"
    "TARS: Ido. [note:1]\n"
    "\n"
    "User: what's my dog's name?\n"
    "TARS: Unknown.\n"
    "\n"
    "User: where do I drink coffee?\n"
    "TARS: Allenby St. Flat white. [note:2]\n"
    "\n"
    "User: my dog's name is Rex\n"
    "TARS: Noted. [note:N]\n"
    "\n"
    "TOOL USE:\n"
    "- search_memory: call for ANY user-specific question before answering. "
    "If results are empty, the answer is \"Unknown.\" — do not propose how the user could tell you.\n"
    "- save_note: only when the user explicitly states a fact to remember or uses the \"note:\" prefix.\n"
    "- Reminders: when the user says \"remind me to X\" or \"I promised Y\", "
    "(1) save_note with the action, (2) get_current_time if a relative time was given, "
    "(3) open_followup with the new note_id and ISO due time.\n"
    "- Closing reminders: when the user says they did X, "
    "(1) save_note about the resolution, (2) list_followups to find the matching followup_id, "
    "(3) close_followup with both ids.\n"
    "- web_research: only on the /research command.\n"
    "\n"
    "Never invent dates, citations, or follow-up closures."
)

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Persist a short note with optional tags. Returns the new note id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "The note body."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags.",
                    },
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Hybrid search over notes, conversations, briefings, vault. Returns top-k matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_note",
            "description": "Fetch a single note by its exact integer id. Use this when the user references a note by id (e.g. 'note 5', 'show me note:12', '[note:7]') instead of semantic search. Returns body, created date, source, status, tags, and any closure linkage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_followup",
            "description": "Track a promise/reminder. Call save_note first to capture the promise, then open_followup with that note_id. Use get_current_time first if the user said 'tomorrow' / 'next week' etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "due_at_iso": {"type": "string", "description": "ISO 8601 timestamp with timezone, e.g. 2026-05-30T15:00:00+03:00. Omit if no specific time."},
                    "to": {"type": "string", "description": "Who the promise is to (optional)."},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_followup",
            "description": "Close a follow-up. CITATION-GATED: save_note first to record what resolved it, then call close_followup with both ids. If the resolving_note_id does not exist, this fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "followup_id": {"type": "integer"},
                    "resolving_note_id": {"type": "integer"},
                },
                "required": ["followup_id", "resolving_note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_followups",
            "description": "List open follow-ups, soonest due first. Use this before close_followup to find the right followup_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get current date/time. Call before scheduling follow-ups or interpreting 'today', 'tomorrow', 'in 2 hours', 'next week', etc. Returns ISO timestamp + weekday.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone name. Defaults to user's configured tz."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_research",
            "description": "Bounded web research with a tool loop. Use sparingly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_steps": {"type": "integer", "default": 6},
                },
                "required": ["query"],
            },
        },
    },
]

# Canonical JSON serialization for hashing/audit. Provider SDKs may reformat
# this when sending; that's fine — our cache anchor lives at the model layer
# where each provider has its own canonicalization.
TOOLS_JSON = json.dumps(TOOLS, sort_keys=True, separators=(",", ":"))

# Frozen cache-anchor hash. Used by tests to detect accidental prompt drift.
ANCHOR_HASH = hashlib.sha256((SYSTEM_BLOCK + "\n" + TOOLS_JSON).encode("utf-8")).hexdigest()
