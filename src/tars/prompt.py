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
    "- open_followup / close_followup: only when the user explicitly asks.\n"
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
            "name": "open_followup",
            "description": "Track a promise. Requires the note_id of the source note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "due_at_iso": {"type": "string", "description": "ISO 8601 timestamp."},
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
            "description": "Close a follow-up. Requires both the followup_id and a resolving_note_id.",
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
