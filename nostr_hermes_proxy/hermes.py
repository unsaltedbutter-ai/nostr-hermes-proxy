"""Hermes gateway HTTP API client.

Talks only to the authenticated API server (DESIGN.md §4). The client owns the
session map: ``ensure_session`` creates/recreates sessions, ``run_turn`` streams
one SSE turn with per-personality session scope and idempotency headers.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from .sessions import SessionEntry, SessionMap

logger = logging.getLogger(__name__)

DEFAULT_HERMES_URL = "http://127.0.0.1:8642"
SESSION_KEY_PREFIX = "nostr-hermes-proxy"
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 600.0  # long tool turns
WRITE_TIMEOUT_SECONDS = 60.0

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INCOMPLETE = "incomplete"


class HermesError(RuntimeError):
    """Base class for Hermes API failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HermesAuthError(HermesError):
    """401: bad or missing HERMES_API_KEY."""


class HermesSessionGone(HermesError):
    """404: the mapped session no longer exists; recreate it."""


class HermesConflictError(HermesError):
    """409: conflicting idempotency key or session lease."""


class HermesRateLimitError(HermesError):
    """429: too many concurrent runs; back off."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int = 429,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ModelOption:
    provider: str | None
    model: str

    @property
    def display(self) -> str:
        if self.provider and not self.model.startswith(f"{self.provider}/"):
            return f"{self.provider}/{self.model}"
        return self.model


@dataclass
class ToolEvent:
    tool: str
    duration: float | None = None
    error: bool = False
    preview: str | None = None
    message: str | None = None


@dataclass
class TurnResult:
    final_text: str
    tool_summary: list[ToolEvent] = field(default_factory=list)
    status: str = STATUS_INCOMPLETE
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED and bool(self.final_text.strip())


class HermesClient:
    """Async Hermes API client bound to one :class:`SessionMap`."""

    def __init__(
        self,
        base_url: str = DEFAULT_HERMES_URL,
        sessions: SessionMap | None = None,
        *,
        api_key: str | None = None,
        session_key_prefix: str = SESSION_KEY_PREFIX,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("HERMES_API_KEY")
        if not key:
            raise HermesAuthError(
                "HERMES_API_KEY is not set (the Hermes API_SERVER_KEY value)"
            )
        self.base_url = base_url.rstrip("/")
        self.sessions = sessions if sessions is not None else SessionMap()
        self.session_key_prefix = session_key_prefix
        self._timeout = timeout or httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=WRITE_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        )
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=self._timeout,
            transport=transport,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> "HermesClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- session management ----------------------------------------------------

    def session_key(self, personality: str) -> str:
        return f"{self.session_key_prefix}:{personality}"

    async def ensure_session(self, personality: str) -> SessionEntry:
        """Return the mapped session, recreating it on 404/not_found."""
        entry = self.sessions.get(personality)
        if entry is not None and entry.hermes_session_id:
            try:
                await self._get_session(entry.hermes_session_id)
            except HermesSessionGone:
                logger.warning(
                    "Hermes session for %s disappeared; creating a new one",
                    personality,
                )
            else:
                return entry
        new_id = await self.create_session(personality)
        previous = self.sessions.get(personality)
        self.sessions.reset(personality)
        return self.sessions.set(
            personality,
            new_id,
            model=previous.model if previous else None,
            provider=previous.provider if previous else None,
        )

    async def create_session(self, personality: str) -> str:
        """POST /api/sessions; returns the new session id."""
        response = await self._client.post(
            "/api/sessions", json={"title": f"nostr:{personality}"},
        )
        await self._raise_for_status(response, "create session")
        data = self._json_body(response)
        session_id = self._extract_session_id(data)
        if not session_id:
            raise HermesError(
                "create session response did not contain a session id",
                status_code=response.status_code,
            )
        return session_id

    async def _get_session(self, session_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"/api/sessions/{quote(session_id, safe='')}",
        )
        await self._raise_for_status(response, "get session")
        return self._json_body(response)

    # -- turns -----------------------------------------------------------------

    async def run_turn(
        self,
        personality: str,
        text: str,
        event_id: str,
    ) -> TurnResult:
        """Run one session turn and consume its SSE stream."""
        entry = self.sessions.get(personality)
        if entry is None or not entry.hermes_session_id:
            raise HermesSessionGone(
                f"no Hermes session mapped for personality {personality!r}"
            )

        payload: dict[str, Any] = {"input": text}
        if entry.model:
            payload["model"] = entry.model
        if entry.provider:
            payload["provider"] = entry.provider

        headers = {
            "Accept": "text/event-stream",
            "Idempotency-Key": event_id,
            "X-Hermes-Session-Key": self.session_key(personality),
        }
        url = (
            f"/api/sessions/{quote(entry.hermes_session_id, safe='')}"
            "/chat/stream"
        )

        final_parts: list[str] = []
        tools: list[ToolEvent] = []
        pending_tools: dict[str, ToolEvent] = {}
        status = STATUS_INCOMPLETE
        error: str | None = None

        async with self._client.stream(
            "POST", url, json=payload, headers=headers,
        ) as response:
            await self._raise_for_status(
                response, f"chat stream for {personality}",
            )
            async for event_name, data in self._iter_sse(response):
                name, body = self._normalize_event(event_name, data)
                if name == "assistant.delta":
                    delta = self._extract_text(body)
                    if delta:
                        final_parts.append(delta)
                elif name == "assistant.commentary":
                    continue
                elif name == "tool.started":
                    tool = self._tool_event(body, started=True)
                    pending_tools[self._tool_key(body, tool, pending_tools)] = tool
                    tools.append(tool)
                elif name in ("tool.completed", "tool.failed"):
                    self._merge_tool_event(
                        body,
                        tools,
                        pending_tools,
                        failed=name == "tool.failed",
                    )
                elif name == "run.completed":
                    status = STATUS_COMPLETED
                    if not final_parts:
                        output = self._extract_text(body)
                        if output:
                            final_parts.append(output)
                elif name == "run.failed":
                    status = STATUS_FAILED
                    error = self._error_message(body) or "run failed"
                elif name == "run.cancelled":
                    status = STATUS_CANCELLED
                    error = self._error_message(body) or "run cancelled"
                elif name == "error":
                    status = STATUS_FAILED
                    error = self._error_message(body) or "gateway error"
                elif name in ("", "message", "ping", "keepalive"):
                    continue

        return TurnResult(
            final_text="".join(final_parts),
            tool_summary=tools,
            status=status,
            error=error,
        )

    # -- models ----------------------------------------------------------------

    async def list_models(self) -> list[ModelOption]:
        response = await self._client.get("/api/model/options")
        await self._raise_for_status(response, "list models")
        return self._normalize_models(self._json_body(response))

    # -- SSE parsing -----------------------------------------------------------

    async def _iter_sse(
        self, response: httpx.Response,
    ) -> AsyncIterator[tuple[str, str]]:
        event_name: str | None = None
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    yield event_name or "", "\n".join(data_lines)
                event_name = None
                data_lines = []
                continue
            if line.startswith(":"):
                # ": keepalive" comments; the proxy sends nothing for these.
                continue
            field_name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field_name == "event":
                event_name = value
            elif field_name == "data":
                data_lines.append(value)
        if data_lines:
            yield event_name or "", "\n".join(data_lines)

    def _normalize_event(self, event_name: str, payload: str) -> tuple[str, Any]:
        body = self._decode_data(payload)
        name = event_name
        if isinstance(body, dict):
            candidate = body.get("type") or body.get("event")
            if candidate and name in ("", "message"):
                name = str(candidate)
        elif payload.strip() == "[DONE]":
            name = "run.completed"
        return name, body

    @staticmethod
    def _decode_data(payload: str) -> Any:
        stripped = payload.strip()
        if not stripped:
            return ""
        try:
            return json.loads(stripped)
        except ValueError:
            return payload

    @staticmethod
    def _extract_text(body: Any) -> str:
        if isinstance(body, str):
            return body
        if not isinstance(body, dict):
            return ""
        for key in ("text", "delta", "content", "output", "response"):
            value = body.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = HermesClient._extract_text(value)
                if nested:
                    return nested
        nested_data = body.get("data")
        if isinstance(nested_data, (dict, str)):
            return HermesClient._extract_text(nested_data)
        return ""

    @staticmethod
    def _error_message(body: Any) -> str | None:
        if isinstance(body, str):
            return body.strip() or None
        if not isinstance(body, dict):
            return None
        for key in ("message", "error", "reason", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = HermesClient._error_message(value)
                if nested:
                    return nested
        nested_data = body.get("data")
        if isinstance(nested_data, (dict, str)):
            return HermesClient._error_message(nested_data)
        return None

    # -- tool events -----------------------------------------------------------

    @staticmethod
    def _tool_name(body: Any) -> str:
        if isinstance(body, dict):
            for key in ("tool", "name", "tool_name"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return "tool"

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_error(body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        if isinstance(error, bool):
            return error
        if isinstance(error, str):
            return bool(error.strip())
        return False

    def _tool_event(self, body: Any, *, started: bool) -> ToolEvent:
        data = body if isinstance(body, dict) else {}
        return ToolEvent(
            tool=self._tool_name(body),
            duration=self._as_float(
                data.get("duration", data.get("duration_ms"))
            ),
            error=self._is_error(body),
            preview=(
                data.get("preview")
                or data.get("result_preview")
                or data.get("result")
            ),
            message=self._error_message(body) if not started else None,
        )

    @staticmethod
    def _tool_key(
        body: Any, tool: ToolEvent, pending: dict[str, ToolEvent],
    ) -> str:
        if isinstance(body, dict):
            for key in ("id", "tool_call_id", "call_id"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        # No id: pair with the most recent unfinished event for this tool.
        for key in reversed(list(pending)):
            if pending[key].tool == tool.tool:
                return key
        return f"{tool.tool}:{len(pending)}"

    def _merge_tool_event(
        self,
        body: Any,
        tools: list[ToolEvent],
        pending: dict[str, ToolEvent],
        *,
        failed: bool,
    ) -> None:
        event = self._tool_event(body, started=False)
        key = self._tool_key(body, event, pending)
        existing = pending.pop(key, None)
        if existing is not None:
            existing.duration = event.duration or existing.duration
            existing.error = existing.error or event.error or failed
            existing.preview = event.preview or existing.preview
            existing.message = event.message or existing.message
            return
        event.error = event.error or failed
        tools.append(event)

    # -- models normalization --------------------------------------------------

    def _normalize_models(self, data: Any) -> list[ModelOption]:
        options: list[ModelOption] = []
        seen: set[tuple[str | None, str]] = set()

        def add(provider: Any, model: Any) -> None:
            if isinstance(model, dict):
                item = model
                model_name = (
                    item.get("id")
                    or item.get("model")
                    or item.get("name")
                    or item.get("slug")
                )
                item_provider = (
                    item.get("provider") or item.get("provider_id") or provider
                )
            else:
                model_name = model
                item_provider = provider
            if not isinstance(model_name, str) or not model_name.strip():
                return
            model_name = model_name.strip()
            provider_name = (
                str(item_provider).strip()
                if isinstance(item_provider, str) and item_provider.strip()
                else None
            )
            key = (provider_name, model_name)
            if key in seen:
                return
            seen.add(key)
            options.append(ModelOption(provider_name, model_name))

        if isinstance(data, list):
            for item in data:
                add(None, item)
        elif isinstance(data, dict):
            providers = data.get("providers")
            if isinstance(providers, list):
                for provider in providers:
                    if not isinstance(provider, dict):
                        add(None, provider)
                        continue
                    provider_name = (
                        provider.get("id")
                        or provider.get("provider")
                        or provider.get("name")
                    )
                    models = provider.get("models")
                    if isinstance(models, list):
                        for model in models:
                            add(provider_name, model)
                    elif models is not None:
                        add(provider_name, models)
            models = data.get("models")
            if isinstance(models, list):
                for model in models:
                    add(None, model)
            options_field = data.get("options")
            if isinstance(options_field, (list, dict)):
                for option in self._normalize_models(options_field):
                    add(option.provider, option.model)
        return options

    # -- HTTP helpers ----------------------------------------------------------

    async def _raise_for_status(
        self, response: httpx.Response, context: str,
    ) -> None:
        if response.status_code < 400:
            return
        body = ""
        try:
            raw = await response.aread()
            body = raw.decode("utf-8", errors="replace")[:500]
        except Exception:  # pragma: no cover - body read is best effort
            pass
        detail = f"HTTP {response.status_code}"
        if body:
            detail += f": {body}"
        message = f"{context} failed ({detail})"
        code = response.status_code
        if code == 401:
            raise HermesAuthError(message, status_code=code)
        if code == 404:
            raise HermesSessionGone(message, status_code=code)
        if code == 409:
            raise HermesConflictError(message, status_code=code)
        if code == 429:
            retry_after: float | None = None
            header = response.headers.get("Retry-After")
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            raise HermesRateLimitError(
                message, retry_after=retry_after, status_code=code,
            )
        raise HermesError(message, status_code=code)

    @staticmethod
    def _json_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HermesError(
                "Hermes returned a non-JSON response",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _extract_session_id(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("id", "session_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        for key in ("session", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                found = HermesClient._extract_session_id(nested)
                if found:
                    return found
        return None


__all__ = [
    "DEFAULT_HERMES_URL",
    "HermesAuthError",
    "HermesClient",
    "HermesConflictError",
    "HermesError",
    "HermesRateLimitError",
    "HermesSessionGone",
    "ModelOption",
    "ToolEvent",
    "TurnResult",
]
