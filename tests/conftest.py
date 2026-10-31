"""Shared test fixtures: synthetic npubs and deterministic clocks."""

from __future__ import annotations

import time

import pytest

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                chk ^= generator
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [
        ord(char) & 31 for char in hrp
    ]


def _to_5bit(data: bytes) -> list[int]:
    accumulator = 0
    bits = 0
    out: list[int] = []
    for byte in data:
        accumulator = (accumulator << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((accumulator >> bits) & 31)
    if bits:
        out.append((accumulator << (5 - bits)) & 31)
    return out


def npub_for(seed_byte: int = 1) -> str:
    """Deterministic test-only npub (not a real key)."""
    data = _to_5bit(bytes([seed_byte]) * 32)
    values = _hrp_expand("npub") + data
    polymod = _polymod(values + [0] * 6) ^ 1
    checksum = [(polymod >> (5 * (5 - index))) & 31 for index in range(6)]
    return "npub1" + "".join(_CHARSET[value] for value in data + checksum)


def hex_for(seed_byte: int = 1) -> str:
    return (bytes([seed_byte]) * 32).hex()


@pytest.fixture
def test_npub() -> str:
    return npub_for(1)


@pytest.fixture
def test_hex() -> str:
    return hex_for(1)


class MutableClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(time.time())
