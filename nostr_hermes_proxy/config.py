"""Configuration loading and validation.

The file is TOML (``tomllib``), secrets are environment-interpolated
``${VAR}`` placeholders, and a literal ``nsec1...`` in the file is always a
configuration error. See DESIGN.md §10.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_STATE_DIR = "~/.local/state/nostr-hermes-proxy"
DEFAULT_ACK_TEXT = "👀"
DEFAULT_ACK_TTL_SECONDS = 1200
DEFAULT_HERMES_CONFIG_PATH = "~/.hermes/config.yaml"
DEFAULT_WATCHDOG_INTERVAL_SECONDS = 60.0
DEFAULT_WATCHDOG_FAILURE_THRESHOLD = 3
DEFAULT_WATCHDOG_STALL_SECONDS = 2 * 60 * 60
DEFAULT_CONTROL_PORT = 8787
DEFAULT_CONTROL_TOKEN_PATH = "~/.config/nostr-hermes-proxy/control-token"
DEFAULT_OUTBOUND_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_OUTBOUND_EXPIRY_MAX_SECONDS = 7 * 24 * 60 * 60

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LITERAL_NSEC_RE = re.compile(r"nsec1[0-9a-z]{10,}", re.IGNORECASE)

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


class ConfigError(ValueError):
    """Raised when the proxy configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Minimal bech32 (BIP-173) support: enough to validate and decode an npub
# without importing nostr-sdk. Deliberately no encoding side.
# ---------------------------------------------------------------------------


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                chk ^= generator
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [
        ord(char) & 31 for char in hrp
    ]


def _bech32_decode(value: str) -> tuple[str, list[int]]:
    if not value or value.lower() != value:
        raise ValueError("bech32 strings must be lowercase")
    position = value.rfind("1")
    if position < 1 or position + 7 > len(value):
        raise ValueError("missing bech32 separator or checksum")
    hrp = value[:position]
    data_part = value[position + 1:]
    try:
        data = [_BECH32_CHARSET.index(char) for char in data_part]
    except ValueError as exc:
        raise ValueError("invalid bech32 character") from exc
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("invalid bech32 checksum")
    return hrp, data[:-6]


def _convert_bits(
    data: list[int], from_bits: int, to_bits: int, *, pad: bool,
) -> list[int] | None:
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = ((accumulator << from_bits) | value) & max_accumulator
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None
    return result


def npub_to_hex(value: str) -> str:
    """Convert an ``npub1...`` or 64-char hex pubkey to lowercase hex.

    Raises :class:`ConfigError` for anything else. This is intentionally a
    light validation path: it checks bech32 shape/checksum and payload length,
    not whether the point is on the secp256k1 curve.
    """
    candidate = (value or "").strip()
    if _HEX64_RE.match(candidate):
        return candidate.lower()
    try:
        hrp, payload = _bech32_decode(candidate.lower())
    except ValueError as exc:
        raise ConfigError(f"allow_npub is not a valid npub: {exc}") from exc
    if hrp != "npub":
        raise ConfigError(
            f"allow_npub must be an npub1... or 64-char hex pubkey (got hrp {hrp!r})"
        )
    decoded = _convert_bits(payload, 5, 8, pad=False)
    if decoded is None or len(decoded) != 32:
        raise ConfigError("allow_npub payload is not a 32-byte public key")
    return bytes(decoded).hex()


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonalityConfig:
    name: str
    nsec: str
    relays: tuple[str, ...]
    model: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class Config:
    hermes_url: str
    allow_npub: str
    allow_hex: str
    ack_text: str
    ack_ttl_seconds: int
    personalities: tuple[PersonalityConfig, ...]
    state_dir: Path
    hermes_config_path: str = DEFAULT_HERMES_CONFIG_PATH
    watchdog_interval_seconds: float = DEFAULT_WATCHDOG_INTERVAL_SECONDS
    watchdog_failure_threshold: int = DEFAULT_WATCHDOG_FAILURE_THRESHOLD
    watchdog_stall_seconds: float = DEFAULT_WATCHDOG_STALL_SECONDS
    control_port: int = DEFAULT_CONTROL_PORT
    control_token_path: str = DEFAULT_CONTROL_TOKEN_PATH
    outbound_rate_limit_per_minute: int = DEFAULT_OUTBOUND_RATE_LIMIT_PER_MINUTE
    outbound_expiry_max_seconds: int = DEFAULT_OUTBOUND_EXPIRY_MAX_SECONDS
    source: Path | None = None

    def personality(self, name: str) -> PersonalityConfig | None:
        for personality in self.personalities:
            if personality.name == name:
                return personality
        return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _interpolate_env(text: str, environ: Mapping[str, str], source: str) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = environ.get(name)
        if value is None or value == "":
            missing.append(name)
            return ""
        return value

    interpolated = _ENV_RE.sub(replace, text)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"{source}: required environment variable(s) not set: {names}"
        )
    return interpolated


def _expect_mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be a TOML table")
    return value


def _canonicalize_relays(raw: Any, *, owner: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        entries = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        entries = [str(part).strip() for part in raw]
    else:
        raise ConfigError(f"{owner}: relays must be a list of wss:// URLs")
    relays: list[str] = []
    for entry in entries:
        if not entry:
            continue
        if not entry.startswith("wss://") or len(entry) <= len("wss://"):
            raise ConfigError(
                f"{owner}: relay {entry!r} is not a wss:// URL"
            )
        canonical = entry.rstrip("/")
        if canonical not in relays:
            relays.append(canonical)
    if not relays:
        raise ConfigError(f"{owner}: at least one relay is required")
    # Relay hygiene: nos.lol first, everything else stable.
    relays.sort(key=lambda url: 0 if "nos.lol" in url else 1)
    return tuple(relays)


def _parse_personality(
    raw: Any, *, index: int, fallback_relays: tuple[str, ...],
) -> PersonalityConfig:
    table = _expect_mapping(raw, f"personality #{index + 1}")
    name = str(table.get("name", "")).strip()
    if not name:
        raise ConfigError(f"personality #{index + 1}: name is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ConfigError(
            f"personality {name!r}: name may only contain letters, digits, "
            "'-' and '_'"
        )
    nsec = str(table.get("nsec", "")).strip()
    if not nsec:
        raise ConfigError(f"personality {name!r}: nsec is required (use ${{VAR}})")
    if not (nsec.startswith("nsec1") or _HEX64_RE.match(nsec)):
        raise ConfigError(
            f"personality {name!r}: nsec must be an nsec1... string or "
            "64-char hex from the environment"
        )
    relays = _canonicalize_relays(
        table.get("relays", fallback_relays), owner=f"personality {name!r}",
    )
    model = table.get("model")
    provider = table.get("provider")
    model = str(model).strip() if model is not None else None
    provider = str(provider).strip() if provider is not None else None
    if model == "":
        model = None
    if provider == "":
        provider = None
    if model and provider is None and "/" in model:
        provider, model = model.split("/", 1)
    return PersonalityConfig(
        name=name,
        nsec=nsec,
        relays=relays,
        model=model,
        provider=provider,
    )


def _parse_hermes_config_path(raw: Any, *, source: str) -> str:
    """Validate the gateway ``config.yaml`` path (read live, never cached)."""
    if raw is None:
        return DEFAULT_HERMES_CONFIG_PATH
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{source}: hermes_config_path must be a path string")
    return raw.strip()


def parse_config(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    source: str = "<config>",
) -> Config:
    """Parse a config document. Exposed separately for tests and --check."""
    environment = os.environ if environ is None else environ
    literal = _LITERAL_NSEC_RE.search(text)
    if literal:
        raise ConfigError(
            f"{source}: literal nsec found. Secrets must come from the "
            "environment (${NSEC_<NAME>}); remove the key from the file."
        )
    interpolated = _interpolate_env(text, environment, source)
    try:
        data = tomllib.loads(interpolated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc

    hermes_url = str(data.get("hermes_url", "")).strip()
    if not hermes_url:
        raise ConfigError(f"{source}: hermes_url is required")
    if not (hermes_url.startswith("http://") or hermes_url.startswith("https://")):
        raise ConfigError(f"{source}: hermes_url must be an http(s) URL")

    allow_raw = str(data.get("allow_npub", "")).strip()
    if not allow_raw:
        raise ConfigError(f"{source}: allow_npub is required")
    allow_hex = npub_to_hex(allow_raw)
    allow_npub = allow_raw.lower() if allow_raw.lower().startswith("npub1") else allow_hex

    ack_text = data.get("ack_text", DEFAULT_ACK_TEXT)
    if not isinstance(ack_text, str) or not ack_text.strip():
        raise ConfigError(f"{source}: ack_text must be a non-empty string")
    ack_ttl_raw = data.get("ack_ttl_seconds", DEFAULT_ACK_TTL_SECONDS)
    try:
        ack_ttl = int(ack_ttl_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{source}: ack_ttl_seconds must be an integer") from exc
    if isinstance(ack_ttl_raw, bool) or ack_ttl < 60:
        raise ConfigError(f"{source}: ack_ttl_seconds must be >= 60")
    hermes_config_path = _parse_hermes_config_path(
        data.get("hermes_config_path"), source=source,
    )

    raw_personalities = data.get("personality", data.get("personalities", []))
    if not isinstance(raw_personalities, list) or not raw_personalities:
        raise ConfigError(f"{source}: at least one [[personality]] is required")

    fallback_relays = _canonicalize_relays(
        data.get("relays"), owner=f"{source}: relays",
    ) if data.get("relays") is not None else ()
    personalities = tuple(
        _parse_personality(
            raw, index=index, fallback_relays=fallback_relays,
        )
        for index, raw in enumerate(raw_personalities)
    )
    names = [personality.name for personality in personalities]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(
            f"{source}: duplicate personality name(s): {', '.join(duplicates)}"
        )

    state_dir = Path(
        str(data.get("state_dir", DEFAULT_STATE_DIR))
    ).expanduser()
    try:
        watchdog_interval = float(
            data.get("watchdog_interval_seconds", DEFAULT_WATCHDOG_INTERVAL_SECONDS)
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{source}: watchdog_interval_seconds must be a number"
        ) from exc
    if watchdog_interval <= 0:
        raise ConfigError(f"{source}: watchdog_interval_seconds must be > 0")
    try:
        watchdog_threshold = int(
            data.get("watchdog_failure_threshold", DEFAULT_WATCHDOG_FAILURE_THRESHOLD)
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{source}: watchdog_failure_threshold must be an integer"
        ) from exc
    if watchdog_threshold < 1:
        raise ConfigError(f"{source}: watchdog_failure_threshold must be >= 1")
    try:
        watchdog_stall = float(
            data.get("watchdog_stall_seconds", DEFAULT_WATCHDOG_STALL_SECONDS)
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{source}: watchdog_stall_seconds must be a number"
        ) from exc
    if watchdog_stall <= 0:
        raise ConfigError(f"{source}: watchdog_stall_seconds must be > 0")

    control_port_raw = data.get("control_port", DEFAULT_CONTROL_PORT)
    try:
        control_port = int(control_port_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{source}: control_port must be an integer") from exc
    if isinstance(control_port_raw, bool) or not 0 <= control_port <= 65535:
        raise ConfigError(
            f"{source}: control_port must be between 0 and 65535 "
            "(0 disables the control plane)"
        )

    control_token_path = data.get(
        "control_token_path", DEFAULT_CONTROL_TOKEN_PATH,
    )
    if not isinstance(control_token_path, str) or not control_token_path.strip():
        raise ConfigError(f"{source}: control_token_path must be a path string")
    control_token_path = control_token_path.strip()

    rate_raw = data.get(
        "outbound_rate_limit_per_minute",
        DEFAULT_OUTBOUND_RATE_LIMIT_PER_MINUTE,
    )
    try:
        rate_limit = int(rate_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{source}: outbound_rate_limit_per_minute must be an integer"
        ) from exc
    if isinstance(rate_raw, bool) or rate_limit <= 0:
        raise ConfigError(
            f"{source}: outbound_rate_limit_per_minute must be > 0"
        )

    expiry_max_raw = data.get(
        "outbound_expiry_max_seconds",
        DEFAULT_OUTBOUND_EXPIRY_MAX_SECONDS,
    )
    try:
        expiry_max = int(expiry_max_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{source}: outbound_expiry_max_seconds must be an integer"
        ) from exc
    if isinstance(expiry_max_raw, bool) or expiry_max <= 0:
        raise ConfigError(
            f"{source}: outbound_expiry_max_seconds must be > 0"
        )

    return Config(
        hermes_url=hermes_url.rstrip("/"),
        allow_npub=allow_npub,
        allow_hex=allow_hex,
        ack_text=ack_text,
        ack_ttl_seconds=ack_ttl,
        personalities=personalities,
        state_dir=state_dir,
        hermes_config_path=hermes_config_path,
        watchdog_interval_seconds=watchdog_interval,
        watchdog_failure_threshold=watchdog_threshold,
        watchdog_stall_seconds=watchdog_stall,
        control_port=control_port,
        control_token_path=control_token_path,
        outbound_rate_limit_per_minute=rate_limit,
        outbound_expiry_max_seconds=expiry_max,
        source=Path(source) if source != "<config>" else None,
    )


def load_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Read and validate a TOML config file."""
    config_path = Path(path).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    return parse_config(text, environ=environ, source=str(config_path))


__all__ = [
    "Config",
    "ConfigError",
    "DEFAULT_ACK_TEXT",
    "DEFAULT_ACK_TTL_SECONDS",
    "DEFAULT_CONTROL_PORT",
    "DEFAULT_CONTROL_TOKEN_PATH",
    "DEFAULT_OUTBOUND_EXPIRY_MAX_SECONDS",
    "DEFAULT_OUTBOUND_RATE_LIMIT_PER_MINUTE",
    "DEFAULT_STATE_DIR",
    "PersonalityConfig",
    "load_config",
    "npub_to_hex",
    "parse_config",
]
