"""nostr-hermes-proxy: NIP-17 Nostr DMs for a local Hermes gateway.

The proxy is policy and glue: it owns one Nostr identity per configured
personality, maps each identity to a persistent Hermes API session, intercepts
a small command set, and chunks long replies back over NIP-17.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
