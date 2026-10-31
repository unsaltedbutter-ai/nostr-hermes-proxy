"""Markdown-aware reply chunking for NIP-17 DMs.

Ported (semantics, not verbatim) from the Hermes gateway's
``BasePlatformAdapter.truncate_message`` / ``fence_state_after``. A DM is a
single gift wrap, so every extra chunk is another encrypted event: keep the
default near 4096 characters, split on paragraph/line/word boundaries, never
leave an orphaned code fence, and label multi-chunk output ``(1/N)``.
"""

from __future__ import annotations

from typing import Callable, Optional

DEFAULT_MAX_LENGTH = 4096
# Room for the " (XX/XX)" page indicator.
_INDICATOR_RESERVE = 10
_FENCE_CLOSE = "\n```"


def fence_state_after(
    text: str, in_code: bool = False, lang: str = "",
) -> tuple[bool, str]:
    """Walk ``text`` line by line, toggling state on ``` fence lines.

    Returns ``(in_code, language_tag)`` after the last line. The language tag
    is remembered so a reopened fence keeps its highlighter.
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            tag = stripped[3:].split()
            if in_code:
                in_code, lang = False, ""
            else:
                in_code, lang = True, (tag[0] if tag else "")
    return in_code, lang


def _custom_unit_to_cp(s: str, budget: int, len_fn: Callable[[str], int]) -> int:
    """Largest codepoint offset *n* with ``len_fn(s[:n]) <= budget``."""
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def truncate_message(
    content: str,
    max_length: int = DEFAULT_MAX_LENGTH,
    len_fn: Optional[Callable[[str], int]] = None,
) -> list[str]:
    """Split ``content`` into fence-balanced chunks of at most ``max_length``.

    ``len_fn`` overrides ``len`` for transports that count UTF-16 units (the
    proxy does not, but the reference implementation does). Multi-chunk output
    carries a trailing `` (N/M)`` marker on every chunk.
    """
    if max_length < 1:
        raise ValueError("max_length must be positive")
    _len = len_fn or len
    if _len(content) <= max_length:
        return [content]

    chunks: list[str] = []
    remaining = content
    carry_lang: Optional[str] = None
    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        # Budget for the body after the optional reopened fence, the closing
        # fence, and the page indicator. Floored so a tiny max_length can't stall.
        headroom = max_length - _INDICATOR_RESERVE - _len(prefix) - _len(_FENCE_CLOSE)
        if headroom < 1:
            headroom = max(1, max_length // 2)

        # The remainder fits in one final chunk (it still gets an indicator if
        # previous chunks exist, hence the reserve). Close a reopened fence.
        if _len(prefix) + _len(remaining) <= max_length - _INDICATOR_RESERVE:
            final_chunk = prefix + remaining
            if carry_lang is not None and fence_state_after(
                remaining, True, carry_lang,
            )[0]:
                final_chunk += _FENCE_CLOSE
            chunks.append(final_chunk)
            break

        cp_limit = (
            _custom_unit_to_cp(remaining, headroom, _len)
            if _len is not len
            else headroom
        )
        region = remaining[:cp_limit]
        split_at = region.rfind("\n")
        if split_at < cp_limit // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            # Floor at one codepoint: a zero budget would never shrink
            # ``remaining``. Overshooting beats a hang.
            split_at = max(1, cp_limit)

        # Don't split inside an inline code span: an unpaired backtick breaks
        # Markdown renderers.
        candidate = remaining[:split_at]
        backtick_count = candidate.count("`") - candidate.count("\\`")
        if backtick_count % 2 == 1:
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                safe_split = max(
                    candidate.rfind(" ", 0, last_bt),
                    candidate.rfind("\n", 0, last_bt),
                )
                if safe_split > cp_limit // 4:
                    split_at = safe_split

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = prefix + chunk_body
        in_code, lang = fence_state_after(
            chunk_body, carry_lang is not None, carry_lang or "",
        )
        carry_lang = lang if in_code else None
        chunks.append(full_chunk + _FENCE_CLOSE if in_code else full_chunk)

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            f"{chunk} ({index + 1}/{total})"
            for index, chunk in enumerate(chunks)
        ]
    return chunks


__all__ = [
    "DEFAULT_MAX_LENGTH",
    "fence_state_after",
    "truncate_message",
]
