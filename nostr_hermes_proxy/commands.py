"""Local command layer: parsed before anything is forwarded to Hermes.

Only leading ``/word`` messages are commands. Everything else returns None and
travels to Hermes verbatim. Reply builders are pure functions so they can be
tested without a bot, a socket, or a gateway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

COMMANDS = ("new", "model", "status", "help")

# Common typo aliases, normalized at parse time so they never reach Hermes.
_COMMAND_ALIASES = {"nee": "new"}

MODEL_MENU_PROMPT = "Reply /model # to switch."
MODEL_MENU_HINT = "Send /model first to list available models."
NO_MODELS_TEXT = (
    "No user-defined providers or models found on the gateway. "
    "Check the gateway config."
)
UNKNOWN_CONTEXT_HINT = (
    "Context: unknown for this model (the gateway config has no "
    "context_length for it)"
)


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
    name = _COMMAND_ALIASES.get(name, name)
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


def parse_model_index(args: str) -> int | None:
    """Parse a menu number (``/model 3``). None means "not a number"."""
    token = (args or "").strip()
    if not re.fullmatch(r"[+-]?\d+", token):
        return None
    return int(token)


def context_line(consumed: int | None, total: int | None) -> str:
    """One ``/status`` context line; degrades when totals are unknown."""
    if consumed is None:
        return "Context: no turns yet"
    if total is None:
        return f"Context: {consumed:,} tokens (total unknown)"
    if total <= 0:
        return f"Context: {consumed:,} tokens (total unknown)"
    percentage = consumed / total * 100
    return (
        f"Context: {consumed:,} / {total:,} tokens "
        f"({percentage:.0f}%)"
    )


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
        "/model - list user-defined models\n"
        "/model # - switch to a model from that list\n"
        "/model provider/model - switch this personality's model\n"
        "/status - show model, context, state, queue, relays, watchdog\n"
        "/help - show this message"
    )


def new_text(
    model: str | None = None,
    provider: str | None = None,
    context_total: int | None = None,
) -> str:
    if context_total is None:
        context = UNKNOWN_CONTEXT_HINT
    else:
        context = f"Context: {context_total:,} tokens"
    return "\n".join([
        "New conversation started.",
        f"Model: {format_model_label(model, provider)}.",
        context,
    ])


def unknown_text(name: str) -> str:
    invoked = f"/{name}" if name else "/"
    return f"Unknown command {invoked}. Send /help for the command list."


def model_set_text(
    model: str,
    provider: str | None = None,
    context_total: int | None = None,
) -> str:
    """Switch confirmation; includes the model's context length when known."""
    text = f"Model set to {format_model_label(model, provider)}."
    if context_total is not None:
        text += f" Context: {context_total:,} tokens."
    return text


def models_menu_text(
    items: Sequence[tuple[str | None, str]],
    *,
    current_model: str | None = None,
    current_provider: str | None = None,
) -> str:
    """Numbered ``/model`` menu: one ``provider/model`` line per entry."""
    if not items:
        return NO_MODELS_TEXT
    lines = [f"Models ({len(items)}):"]
    for index, (provider, model) in enumerate(items, start=1):
        marker = ""
        if model == current_model and (
            current_provider is None or provider == current_provider
        ):
            marker = " ← current"
        lines.append(
            f"{index}. {format_model_label(model, provider)}{marker}"
        )
    lines.append(MODEL_MENU_PROMPT)
    return "\n".join(lines)


def model_index_out_of_range_text(index: int) -> str:
    return (
        f"No model #{index} in the list. {MODEL_MENU_HINT}"
    )


def status_text(
    *,
    name: str,
    model: str | None,
    provider: str | None,
    session_age_seconds: float | None,
    queue_depth: int,
    relay_count: int,
    watchdog_state: str,
    consumed_tokens: int | None = None,
    context_total: int | None = None,
    busy: bool = False,
) -> str:
    return "\n".join([
        f"Personality: {name}",
        f"Model: {format_model_label(model, provider)}",
        f"Session: {format_session_age(session_age_seconds)}",
        f"State: {'busy (turn in flight)' if busy else 'idle'}",
        context_line(consumed_tokens, context_total),
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
    "MODEL_MENU_HINT",
    "MODEL_MENU_PROMPT",
    "ModelSelection",
    "NO_MODELS_TEXT",
    "UNKNOWN_CONTEXT_HINT",
    "context_line",
    "error_text",
    "format_model_label",
    "format_session_age",
    "help_text",
    "model_index_out_of_range_text",
    "model_set_text",
    "models_menu_text",
    "new_text",
    "parse_command",
    "parse_model_index",
    "parse_model_selection",
    "status_text",
    "unknown_text",
]
