from __future__ import annotations

import re

import pytest

from nostr_hermes_proxy.chunking import fence_state_after, truncate_message

MARKER_RE = re.compile(r" \(\d+/\d+\)$")


def strip_marker(chunk: str) -> str:
    return MARKER_RE.sub("", chunk)


def test_short_message_is_unchanged():
    text = "hello there"
    assert truncate_message(text, 4096) == [text]


def test_long_prose_respects_limit_and_markers():
    text = " ".join(f"word{index}" for index in range(1000))
    chunks = truncate_message(text, 200)
    assert len(chunks) > 1
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        assert len(chunk) <= 200
        assert chunk.endswith(f" ({index}/{total})")
    # No dropped words: markers removed and whitespace normalised.
    rejoined = " ".join(chunks)
    for index in range(1000):
        assert f"word{index}" in rejoined


def test_code_fence_is_closed_and_reopened():
    code = "\n".join(f"line_{index} = {index}" for index in range(200))
    text = f"Before\n```python\n{code}\n```\nAfter"
    chunks = truncate_message(text, 160)
    assert len(chunks) > 2
    for chunk in chunks:
        body = strip_marker(chunk)
        assert body.count("```") % 2 == 0, body
    assert strip_marker(chunks[1]).startswith("```python\n")


def test_unclosed_fence_is_closed_at_end():
    text = "Intro\n```python\ndef boom():\n    return 1\n" + (
        "    x = 1\n" * 40
    )
    chunks = truncate_message(text, 120)
    for chunk in chunks:
        assert strip_marker(chunk).count("```") % 2 == 0
    final_body = strip_marker(chunks[-1])
    assert final_body.rstrip().endswith("```")


def test_fence_language_survives_reopen():
    text = "```rust\n" + ("let x = 1;\n" * 80) + "```"
    chunks = truncate_message(text, 110)
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert strip_marker(chunk).startswith("```rust\n")


def test_page_markers_are_sequential():
    text = "x" * 500
    chunks = truncate_message(text, 100)
    total = len(chunks)
    assert total > 1
    assert all(
        chunk.endswith(f" ({i}/{total})")
        for i, chunk in enumerate(chunks, 1)
    )


def test_tiny_limit_does_not_hang():
    text = "abcdefghijklmnopqrst"
    chunks = truncate_message(text, 12)
    assert len(chunks) >= 2
    assert "".join(strip_marker(chunk) for chunk in chunks) == text


def test_invalid_max_length_rejected():
    with pytest.raises(ValueError):
        truncate_message("abc", 0)


def test_fence_state_toggles_with_language():
    assert fence_state_after("```json\n{}", False, "") == (True, "json")
    assert fence_state_after("```python\nx\n```", False, "") == (False, "")
    assert fence_state_after("```", True, "python") == (False, "")
