"""Secret-hygiene guard: no real key material in tracked files.

Fails the suite if any tracked file contains a string that validly decodes
as bech32 ``nsec`` or ``npub`` (checksum-verified, so synthetic placeholders
like ``nsec1synthetictestkeynotreal`` pass), an all-zero npub placeholder
being the only npub exemption, or a bare 64-char hex string outside the
probe logs (where relay event ids legitimately live).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_RE = re.compile(r"(?:nsec|npub)1[" + _CHARSET + r"]{20,}")
_HEX64_RE = re.compile(r"\b[0-9a-f]{64}\b")

_GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]


def _polymod(values: list[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GEN[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: list[int], frombits: int, tobits: int) -> bytes:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return bytes(ret)


def decode_bech32(s: str) -> tuple[str, bytes] | None:
    """Return (hrp, payload bytes) when s carries a valid bech32 checksum."""
    if s != s.lower() and s != s.upper():
        return None  # mixed case is invalid bech32
    s = s.lower()
    sep = s.rfind("1")
    if sep < 1 or len(s) - sep - 1 < 6 or len(s) > 90:
        return None
    hrp, data_part = s[:sep], s[sep + 1:]
    if any(c not in _CHARSET for c in data_part):
        return None
    data = [_CHARSET.index(c) for c in data_part]
    if _polymod(_hrp_expand(hrp) + data) != 1:
        return None
    return hrp, _convertbits(data[:-6], 5, 8)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True,
        cwd=_REPO_ROOT,
    )
    return [Path(p.decode()) for p in out.stdout.split(b"\0") if p]


def test_no_real_key_material_in_tracked_files() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _tracked_files():
        text = (_REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        for match in _BECH32_RE.finditer(text.lower()):
            decoded = decode_bech32(match.group(0))
            if decoded is None:
                continue  # checksum failed: a placeholder, not a real key
            hrp, payload = decoded
            if hrp == "nsec":
                offenders.append((str(path), "nsec"))
            elif hrp == "npub" and set(payload) != {0}:
                # the all-zero npub is the documented test placeholder; a
                # real npub decodes to 32 key bytes plus a zero pad byte
                offenders.append((str(path), "npub"))
        if path.as_posix().startswith("docs/probe/"):
            continue  # relay event ids are 64-hex and legitimately present
        for match in _HEX64_RE.finditer(text.lower()):
            offenders.append((str(path), f"64-hex at offset {match.start()}"))
    assert not offenders, f"key material committed to tracked files: {offenders}"


def test_guard_itself_detects_real_material() -> None:
    """The guard must catch a real-format key, not just pass vacuously."""
    from nostr_sdk import Keys

    # A checksum-valid npub produced by the SDK must decode with our decoder.
    npub = Keys.generate().public_key().to_bech32()
    decoded = decode_bech32(npub)
    assert decoded is not None
    assert decoded[0] == "npub" and len(decoded[1]) in (32, 33)
    # The synthetic placeholder is NOT valid bech32, so the guard ignores it.
    assert decode_bech32("nsec1synthetictestkeynotreal") is None
