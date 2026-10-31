"""Persistent personality → Hermes session mapping.

One JSON document, written atomically (mkstemp + fsync + ``os.replace``) so a
crash mid-write can never truncate the map. ``/new`` swaps the session id while
keeping the personality's model preference.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATE_VERSION = 1
DEFAULT_STATE_DIR = Path("~/.local/state/nostr-hermes-proxy").expanduser()


@dataclass(frozen=True)
class SessionEntry:
    hermes_session_id: str | None
    model: str | None = None
    provider: str | None = None
    created_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "hermes_session_id": self.hermes_session_id,
            "model": self.model,
            "provider": self.provider,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "SessionEntry | None":
        if not isinstance(raw, dict):
            return None
        session_id = raw.get("hermes_session_id")
        if session_id is not None and not isinstance(session_id, str):
            return None
        model = raw.get("model")
        provider = raw.get("provider")
        if model is not None and not isinstance(model, str):
            return None
        if provider is not None and not isinstance(provider, str):
            return None
        try:
            created_at = float(raw.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created_at = 0.0
        return cls(
            hermes_session_id=session_id or None,
            model=model or None,
            provider=provider or None,
            created_at=created_at,
        )


class SessionMap:
    """Thread-safe in-memory map with an atomic JSON backing file."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self._clock = clock
        self._sessions: dict[str, SessionEntry] = {}
        self._lock = threading.RLock()
        self._load()

    # -- reads -----------------------------------------------------------------

    def get(self, name: str) -> SessionEntry | None:
        with self._lock:
            return self._sessions.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: entry.to_json()
                for name, entry in self._sessions.items()
            }

    # -- writes ----------------------------------------------------------------

    def get_or_create(
        self,
        name: str,
        session_id: str,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> SessionEntry:
        """Return the existing session, or create one with ``session_id``.

        A preferences-only entry (``/model`` before the first turn) keeps its
        created_at; otherwise the entry is timestamped now.
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None and existing.hermes_session_id:
                return existing
            entry = SessionEntry(
                hermes_session_id=session_id,
                model=model if model is not None else (
                    existing.model if existing else None
                ),
                provider=provider if provider is not None else (
                    existing.provider if existing else None
                ),
                created_at=(
                    existing.created_at
                    if existing is not None and existing.created_at
                    else self._clock()
                ),
            )
            self._sessions[name] = entry
            self._write_locked()
            return entry

    def set(
        self,
        name: str,
        session_id: str,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> SessionEntry:
        """Set the Hermes session id, preserving created_at when it already exists."""
        if not session_id:
            raise ValueError("session_id must be non-empty")
        with self._lock:
            existing = self._sessions.get(name)
            entry = SessionEntry(
                hermes_session_id=session_id,
                model=model,
                provider=provider,
                created_at=(
                    existing.created_at
                    if existing is not None and existing.created_at
                    else self._clock()
                ),
            )
            self._sessions[name] = entry
            self._write_locked()
            return entry

    def update_model(
        self,
        name: str,
        model: str | None,
        provider: str | None,
    ) -> SessionEntry:
        """Record a model preference, with or without a live session."""
        with self._lock:
            existing = self._sessions.get(name)
            entry = SessionEntry(
                hermes_session_id=(
                    existing.hermes_session_id if existing is not None else None
                ),
                model=model,
                provider=provider,
                created_at=(
                    existing.created_at
                    if existing is not None and existing.created_at
                    else self._clock()
                ),
            )
            self._sessions[name] = entry
            self._write_locked()
            return entry

    def seed_preferences(
        self,
        name: str,
        model: str | None,
        provider: str | None,
    ) -> SessionEntry | None:
        """Apply config defaults without overwriting an existing entry."""
        if model is None and provider is None:
            return self.get(name)
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None:
                return existing
            entry = SessionEntry(
                hermes_session_id=None,
                model=model,
                provider=provider,
                created_at=self._clock(),
            )
            self._sessions[name] = entry
            self._write_locked()
            return entry

    def reset(self, name: str) -> SessionEntry | None:
        """Forget a personality's session (``/new``); returns the old entry."""
        with self._lock:
            previous = self._sessions.pop(name, None)
            if previous is not None:
                self._write_locked()
            return previous

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "Session map %s is unreadable (%s); starting empty", self.path, exc,
            )
            return
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            logger.warning(
                "Session map %s has no sessions table; starting empty", self.path,
            )
            return
        loaded: dict[str, SessionEntry] = {}
        for name, value in sessions.items():
            if not isinstance(name, str):
                continue
            entry = SessionEntry.from_json(value)
            if entry is not None:
                loaded[name] = entry
        self._sessions = loaded

    def _write_locked(self) -> None:
        if self.path is None:
            return
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:  # pragma: no cover - best effort on odd filesystems
            pass
        payload = json.dumps(
            {"version": STATE_VERSION, "sessions": self.as_dict()},
            sort_keys=True,
            separators=(",", ":"),
        )
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:  # pragma: no cover - directory fsync is best effort
                pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


__all__ = ["SessionEntry", "SessionMap", "DEFAULT_STATE_DIR"]
