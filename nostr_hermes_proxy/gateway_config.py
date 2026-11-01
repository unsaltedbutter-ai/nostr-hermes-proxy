"""Live reader for the Hermes gateway's ``config.yaml``.

Every call re-reads and re-parses the file from disk — **no caching** — so
model and context-length changes made in the gateway take effect on the next
command without touching the proxy, and nothing needs to be kept in sync
between the two projects.

Only provider slugs, model ids, and context numbers are extracted; the file
contains secrets and its contents are never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class GatewayConfigError(Exception):
    """The gateway config could not be read or understood."""


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _slug(provider_ref: object) -> str | None:
    """Reduce a provider reference like ``custom:aspark`` to ``aspark``."""
    if not isinstance(provider_ref, str) or not provider_ref.strip():
        return None
    return provider_ref.strip().rsplit(":", 1)[-1]


@dataclass(frozen=True)
class GatewayModels:
    """The model menu and context totals from one fresh config read."""

    #: Ordered (provider slug, model id) pairs, document order, deduped.
    menu: list[tuple[str, str]]
    default_provider: str | None
    default_model: str | None
    default_context_length: int | None
    _providers: dict = field(repr=False, default_factory=dict)

    def context_length(
        self, provider: str | None, model: str | None,
    ) -> int | None:
        """Per-model context length, falling back to the gateway default.

        Lookup order: the named provider's ``models.<id>.context_length``,
        then any provider carrying that model id, then the top-level
        ``model.context_length`` when the model is the gateway default.
        """
        if not model:
            return None
        if provider:
            entry = self._providers.get(provider)
            if isinstance(entry, dict):
                models = entry.get("models")
                if isinstance(models, dict):
                    spec = models.get(model)
                    if isinstance(spec, dict) and _is_positive_int(
                        spec.get("context_length"),
                    ):
                        return spec["context_length"]
        for entry in self._providers.values():
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if isinstance(models, dict):
                spec = models.get(model)
                if isinstance(spec, dict) and _is_positive_int(
                    spec.get("context_length"),
                ):
                    return spec["context_length"]
        if model == self.default_model and self.default_context_length:
            return self.default_context_length
        return None


def read_gateway_models(path: str | Path) -> GatewayModels:
    """Read and parse ``path`` fresh; never cached.

    Raises :class:`GatewayConfigError` when the file is missing, unparsable,
    or yields no enabled providers with models — callers fall back to the
    gateway API inventory (menu) or "unknown" (context totals).
    """
    path = Path(path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GatewayConfigError(f"{path}: file not found") from exc
    except OSError as exc:
        raise GatewayConfigError(f"{path}: unreadable ({exc})") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GatewayConfigError(f"{path}: invalid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise GatewayConfigError(f"{path}: expected a top-level mapping")

    model_block = raw.get("model")
    model_block = model_block if isinstance(model_block, dict) else {}
    default_provider = _slug(model_block.get("provider"))
    default_model = model_block.get("default")
    default_model = default_model if isinstance(default_model, str) else None
    default_context_length = model_block.get("context_length")
    if not _is_positive_int(default_context_length):
        default_context_length = None

    providers_raw = raw.get("providers")
    providers_raw = providers_raw if isinstance(providers_raw, dict) else {}

    menu: list[tuple[str, str]] = []
    for slug, entry in providers_raw.items():
        if not isinstance(slug, str) or not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        for model_id in models:
            if (
                isinstance(model_id, str) and model_id
                and (slug, model_id) not in menu
            ):
                menu.append((slug, model_id))

    if (
        default_provider and default_model
        and (default_provider, default_model) not in menu
    ):
        menu.append((default_provider, default_model))

    if not menu:
        raise GatewayConfigError(f"{path}: no enabled providers with models")

    return GatewayModels(
        menu=menu,
        default_provider=default_provider,
        default_model=default_model,
        default_context_length=default_context_length,
        _providers=providers_raw,
    )


__all__ = [
    "GatewayConfigError",
    "GatewayModels",
    "read_gateway_models",
]
