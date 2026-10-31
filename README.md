# nostr-hermes-proxy

A standalone **NIP-17 ↔ Hermes proxy**: DM a Nostr identity from your phone,
get your local [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s
answer back — end-to-end encrypted, with the gateway never knowing Nostr
exists.

**Status: design phase.** No implementation yet — see
[DESIGN.md](DESIGN.md) for the architecture and plan.

## What it will do

- Run as its own daemon (LaunchAgent), fully decoupled from the Hermes gateway
  — gateway updates can't break Nostr delivery
- Host one or more Nostr identities ("personalities"), each configured with
  its **own `nsec`** and its own conversation
- Accept DMs from exactly **one allowlisted npub** per personality; deny
  everyone else
- Forward each DM to the local Hermes gateway over its authenticated HTTP API,
  keeping one persistent Hermes session per personality
- Reply over NIP-17 gift wraps: **ack** on receipt, **tool-call noise omitted
  or summarized**, long replies chunked
- Intercept special commands before Hermes: `/new` (fresh conversation),
  `/model` (switch models), `/status`

## Architecture

```
Mobile Nostr client (your npub)
    │ NIP-17 DM
    ▼
Relays (nos.lol first) ── one subscription per personality
    │
nostr-hermes-proxy (own LaunchAgent)
    ├─ Personality A (nsec A) ── NostrBot (nostrbot-sdk)
    ├─ Personality B (nsec B) ── NostrBot
    └─ …                         each: ack → Hermes turn → summarize → chunk → DM back
    │ HTTP 127.0.0.1:8642 (bearer key)
    ▼
Hermes gateway API server (one persistent session per personality)
```

## Foundation

Built on [`nostrbot-sdk`](https://github.com/unsaltedbutter-ai/nostrbot-sdk),
which wraps the `nostr-sdk` bindings and already handles the NIP-17 footguns
(kind-1059 subscriptions, dedup, per-user locks, protocol-matched DMs,
NIP-40 expiration, kind-10050 inbox publication). The proxy is policy and
glue: Hermes HTTP client, session mapping, commands, ack/verbosity policy,
relay hygiene, and a hung-loop watchdog.

## Docs

| Doc | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | Architecture, feature specs, verified API contract, open questions, rollout |
| [INCIDENTS.md](INCIDENTS.md) | Delivery-failure incident log (copied from hermes-nostr) |
| [OPTIONS.md](OPTIONS.md) | The decoupling proposal that motivated this repo (copied from hermes-nostr) |

Sibling project:
[hermes-nostr](https://github.com/unsaltedbutter-ai/hermes-nostr) — the
in-gateway plugin this proxy replaces. The plugin stays runnable during the
transition (side-by-side on different npubs); see the rollout section of
DESIGN.md.

## License

[MIT](LICENSE) © unsaltedbutter
