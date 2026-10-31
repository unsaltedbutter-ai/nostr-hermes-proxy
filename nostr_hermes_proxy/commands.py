"""Local command layer: parsed before anything is forwarded to Hermes.

Only leading ``/word`` messages are commands. Everything else returns None and
travels to Hermes verbatim. Reply builders are pure functions so they can be
tested without a bot, a socket, or a gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

COMMANDS = ("new", "model", "status", "help")

MAX_MODELS_SHOWN = 30


@dataclass(frozen=True)
class Command:
    name: str
    args: str = ""

    @property
    def display(self) -> str:
        return f"/{self.name}" if self.name else "/"


@dataclass(frozen=True)
class ModelSelection:
    model: str
    provider: str | None = None


def parse_command(text: str) -> Command | None:
    """Return the leading command, or None for ordinary message text."""
    if not isinstance(text, str):
        return None
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    head, _, args = stripped[1:].partition(" ")
    name = head.strip().lower()
    return Command(name=name, args=args.strip())


def parse_model_selection(args: str) -> ModelSelection:
    """Parse ``provider/model`` (or ``provider model``) command arguments."""
    tokens = args.split()
    if not tokens:
        raise ValueError("missing model")
    if len(tokens) == 1:
        token = tokens[0]
        if "/" in token:
            provider, model = token.split("/", 1)
            if not provider or not model:
                raise ValueError("model must be provider/model")
            return ModelSelection(model=model, provider=provider)
        return ModelSelection(model=token)
    provider = tokens[0]
    model = " ".join(tokens[1:])
    if not model:
        raise ValueError("model must be provider/model")
    return ModelSelection(model=model, provider=provider)


def format_model_label(model: str | None, provider: str | None) -> str:
    if not model:
        return "gateway default"
    if provider and not model.startswith(f"{provider}/"):
        return f"{provider}/{model}"
    return model


def format_session_age(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "not started"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, remainder = divmod(total, 3600)
        return f"{hours}h {remainder // 60}m"
    days, remainder = divmod(total, 86400)
    return f"{days}d {remainder // 3600}h"


def help_text() -> str:
    return (
        "Commands:\n"
        "/new - start a fresh Hermes session\n"
        "/model - list models\n"
        "/model provider/model - switch this personality's model\n"
        "/status - show session, queue, relays, watchdog\n"
        "/help - show this message"
    )


def new_text() -> str:
    return "New conversation started."


def unknown_text(name: str) -> str:
    invoked = f"/{name}" if name else "/"
    return f"Unknown command {invoked}. Send /help for the command list."


def model_set_text(model: str, provider: str | None) -> str:
    return f"Model set to {format_model_label(model, provider)}."


def _model_parts(option: Any) -> tuple[str | None, str]:
    if isinstance(option, dict):
        provider = option.get("provider")
        model = option.get("model") or option.get("id")
    else:
        provider = getattr(option, "provider", None)
        model = getattr(option, "model", None)
    if model is None and isinstance(option, str):
        model = option
    provider = str(provider).strip() if provider else None
    model = str(model).strip() if model else ""
    return provider, model


def models_text(options: Sequence[Any]) -> str:
    lines: list[str] = []
    shown = 0
    total = 0
    for option in options:
        provider, model = _model_parts(option)
        if not model:
            continue
        total += 1
        if shown >= MAX_MODELS_SHOWN:
            continue
        lines.append(f"- {format_model_label(model, provider)}")
        shown += 1
    if total == 0:
        return "No models available."
    header = f"Models ({total}):"
    footer = "Use /model provider/model to switch."
    hidden = total - shown
    if hidden > 0:
        lines.append(f"- ... and {hidden} more")
    return "\n".join([header, *lines, footer])


def status_text(
    *,
    name: str,
    model: str | None,
    provider: str | None,
    session_age_seconds: float | None,
    queue_depth: int,
    relay_count: int,
    watchdog_state: str,
) -> str:
    return "\n".join([
        f"Personality: {name}",
        f"Model: {format_model_label(model, provider)}",
        f"Session: {format_session_age(session_age_seconds)}",
        f"Queue: {queue_depth} waiting",
        f"Relays: {relay_count}",
        f"Watchdog: {watchdog_state}",
    ])


def error_text(message: str = "") -> str:
    base = "Something went wrong talking to Hermes. Please try again."
    detail = message.strip()
    if detail:
        return f"{base}\n{detail}"
    return base


__all__ = [
    "COMMANDS",
    "Command",
    "MAX_MODELS_SHOWN",
    "ModelSelection",
    "error_text",
    "format_model_label",
    "format_session_age",
    "help_text",
    "model_set_text",
    "models_text",
    "new_text",
    "parse_command",
    "parse_model_selection",
    "status_text",
    "unknown_text",
]
