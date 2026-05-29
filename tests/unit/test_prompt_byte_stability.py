"""Cache-anchor stability tests.

The prefix cache on OpenRouter / OpenAI / DeepSeek is keyed on byte-identical
leading tokens. If SYSTEM_BLOCK or the tools schema drifts unintentionally,
every request pays full prompt-prefill cost.

These tests pin both the value and the SHA256 of the anchor so accidental
edits fail CI. When you DELIBERATELY change the prompt, run the test once,
copy the new hash from the failure message, and update EXPECTED_HASH.
"""

from __future__ import annotations

import hashlib
import importlib

import pytest

from tars import prompt


# Pin the canonical anchor hash. Update DELIBERATELY when changing the prompt.
EXPECTED_HASH = "aebf1abca6157738a67522a5e0f1e1512c349318beb8e3ddc1244aeedc06a511"


def test_anchor_hash_stable() -> None:
    """The exported ANCHOR_HASH equals SHA256(SYSTEM_BLOCK + '\\n' + TOOLS_JSON)."""
    expected = hashlib.sha256(
        (prompt.SYSTEM_BLOCK + "\n" + prompt.TOOLS_JSON).encode("utf-8")
    ).hexdigest()
    assert prompt.ANCHOR_HASH == expected


def test_anchor_hash_locked() -> None:
    """Drift detector. Update EXPECTED_HASH only when the prompt change is intentional.

    If this fails: the prompt or tool catalog changed. Confirm the change is
    wanted, copy the actual hash from the assertion error into EXPECTED_HASH,
    commit both files together.
    """
    if prompt.ANCHOR_HASH != EXPECTED_HASH:
        pytest.fail(
            "Cache anchor drifted!\n"
            f"  expected: {EXPECTED_HASH}\n"
            f"  actual:   {prompt.ANCHOR_HASH}\n"
            "If intentional, update EXPECTED_HASH in this test file."
        )


def test_anchor_is_reimport_stable() -> None:
    """Re-importing the module must produce the same hash (no time-based mutation)."""
    h1 = prompt.ANCHOR_HASH
    importlib.reload(prompt)
    assert prompt.ANCHOR_HASH == h1


def test_system_block_has_no_placeholders() -> None:
    """Guard against f-stringing user data, timestamps, or 'today is X' into the prompt."""
    bad_substrings = ["{", "}", "%s", "%(", "today is", "current date", "timestamp"]
    lowered = prompt.SYSTEM_BLOCK.lower()
    for bad in bad_substrings:
        assert bad.lower() not in lowered, f"SYSTEM_BLOCK contains forbidden substring: {bad!r}"


def test_tools_json_is_sorted_and_compact() -> None:
    """TOOLS_JSON must use sort_keys + no spaces — otherwise byte stability is fragile."""
    import json
    reserialized = json.dumps(prompt.TOOLS, sort_keys=True, separators=(",", ":"))
    assert prompt.TOOLS_JSON == reserialized


def test_required_tools_present() -> None:
    names = {t["function"]["name"] for t in prompt.TOOLS}
    assert names >= {
        "save_note",
        "search_memory",
        "open_followup",
        "close_followup",
        "list_followups",
        "get_current_time",
        "web_research",
    }
