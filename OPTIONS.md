> Copied from [`unsaltedbutter-ai/hermes-nostr`](https://github.com/unsaltedbutter-ai/hermes-nostr)@`2f1a5f8` (2026-10-31).
> This repo implements the "standalone nostr-bridge" proposed below; the API
> contract has since been verified against the live docs — see
> [DESIGN.md](DESIGN.md) §4.

# OPTIONS.md — Decoupling Nostr from the Hermes Gateway

Companion to `INCIDENTS.md` (the record of delivery failures). This document
captures a redesign option evaluated on 2026-10-31: moving the Nostr
integration out of the gateway so it survives Hermes updates untouched.

## Problem statement

The Nostr integration is an in-process Hermes plugin
(`~/.hermes/plugins/nostr/`, source `~/hermes-nostr/`). Consequences:

- Every `hermes update` can churn nostr-sdk / plugin internals, so the
  integration needs retesting after each update.
- A hung notification loop inside the plugin (INCIDENTS.md #1) makes the bot
  silently deaf, and recovery historically meant touching the whole gateway,
  which drops in-flight sessions on every platform.
- The plugin lives inside the gateway process; the failure modes of Nostr
  (relay sockets, NIP-59 unwrap, iOS clients) are coupled to everything else
  the gateway does.

Goal: a Nostr front door that is **fully standalone** — Hermes doesn't need
to know Nostr exists, and Hermes updates can't break Nostr delivery.

## Finding 1 — the webhook platform is NOT the tool

`platforms.webhook` (already enabled, port 8644) fires one-shot agent runs
from external events (GitHub PRs, pushes, etc.) and delivers the response to
log/Telegram/Discord. No conversation state, no session context. Right shape
for event triggers, wrong shape for a DM conversation.

## Finding 2 — the API Server platform IS the tool

The gateway exposes an authenticated HTTP API (OpenAI-compatible + Hermes
extensions) on port 8642. Docs:
https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server

Key surfaces:

- `POST /api/sessions/{id}/chat` — run one synchronous agent turn against a
  persistent session. The docs describe these session-chat endpoints as the
  ones that "carry cross-machine agent DMs". Context lives in the Hermes
  session; the caller never replays history.
- `POST /api/sessions/{id}/chat/stream` — SSE variant: `assistant.delta`,
  tool.started/completed/failed, terminal `run.completed`/`run.failed`/
  `run.cancelled`. Emits `: keepalive` lines every 10s so long tool calls
  don't trip idle timeouts.
- `POST /v1/chat/completions` with `X-Hermes-Session-Id` /
  `X-Hermes-Session-Key` headers — OpenAI-compatible, session-continuing.
- Sessions REST API (all gated by `API_SERVER_KEY`, under `/api/sessions/*`):
  `POST /api/sessions` (create empty session), `GET /api/sessions/{id}`
  (metadata), `GET /api/sessions/{id}/messages` (history),
  `POST /api/sessions/{id}/fork`, `DELETE /api/sessions/{id}`.
- `Idempotency-Key` header on `/v1/chat/completions` and `/v1/responses`:
  an exact retry replays the original answer instead of running twice; a
  conflicting retry is refused (409 `admission_conflict`). Directly useful
  against relay duplicate deliveries.
- Concurrent-run cap across run-starting endpoints
  (`gateway.api_server.max_concurrent_runs`, default 10); over-cap requests
  get HTTP 429 and should back off.

Enable with `API_SERVER_ENABLED=true` + `API_SERVER_KEY=...` in
`~/.hermes/.env`; the gateway logs
`[API Server] API server listening on http://127.0.0.1:8642`.

## Proposed architecture

```
Nostur ──NIP-17──> nos.lol (+ relay.damus.io)
                       │
        standalone nostr-bridge daemon (its own LaunchAgent)
        - subscribe to the user's npub gift wraps
        - unwrap NIP-17
        - map sender npub → Hermes session id
          (create on first contact via POST /api/sessions)
        - Idempotency-Key = gift wrap event id
        - POST /api/sessions/{id}/chat (or /chat/stream)
        - gift-wrap the reply back to the sender
                       │ HTTP (localhost:8642)
                   Hermes gateway (API server)
        - Nostr not installed, not known, not restarted
```

- Bridge runs as its own LaunchAgent (`ai.hermes.nostr-bridge`), separate
  from `ai.hermes.gateway` — per Hermes convention: one LaunchAgent per
  companion service, so a bridge crash never touches the gateway.
- The watchdog/reconnect/watchdog-stall logic moves into the bridge, where a
  hung loop means restarting one small daemon instead of the gateway.
- `nostr` can be removed from `plugins.enabled` in `~/.hermes/config.yaml`;
  Hermes genuinely never needs to know Nostr exists.
- The bridge depends only on the stable HTTP contract (bearer key + session
  endpoints), not on nostr-sdk versions or gateway plugin internals —
  `hermes update` churn becomes invisible to Nostr delivery.
- Estimated size: ~150–250 lines of Python on nostr-sdk (already proven
  working on this Mac) + stdlib/httpx. Local-only, no tunnels, no new
  toolchains.

## What this fixes (mapped to INCIDENTS.md)

| Incident | How the bridge changes it |
|---|---|
| #1 hung notification loop, silent deafness | Watchdog lives in the bridge; restart is a 1-process `launchctl kickstart`, gateway untouched |
| #4 Nostur never publishes | Unchanged (iOS-side), but the bridge can log "nothing received in N min" per relay, making the blind spot visible |
| #5 damus.io black-holing gift wraps | Bridge can probe every relay in its relay list (write + readback by event id) instead of the hardcoded-nos.lol canary blind spot |
| Retest after every Hermes update | Bridge depends on the HTTP contract only; plugin sync/retest steps disappear |

## Trade-offs (be honest)

- Turn latency: a turn with tools can run minutes. `/chat` blocks that long;
  the bridge should prefer `/chat/stream` with a generous timeout and/or send
  an ack ("working on it") first — the gateway's hold-on behavior would be
  reimplemented in the bridge.
- Feature loss: anything where the agent *initiates* to Nostr loses its
  delivery path — cross-platform `messaging` sends to nostr, cron deliveries
  targeting nostr. Current usage is conversational DMs, so this likely
  doesn't bite; if needed later, the bridge can expose a small outbound
  endpoint the agent could curl.
- Ownership: relay subscription, reconnect/backoff, watchdog, and canary
  logic are now bridge code to maintain (mostly a port of what the plugin
  already does, minus plugin glue).
- Migration: the bridge builds on nostr-sdk 0.45 (v2.1.0), inheriting that
  migration rather than avoiding it.

## Suggested rollout

1. Keep the current plugin running unchanged.
2. Enable the API server (`API_SERVER_ENABLED=true`, `API_SERVER_KEY=...`),
   restart gateway, verify with a curl to `/v1/chat/completions`.
3. Build the bridge in a standalone repo (suggested `~/nostr-bridge/`),
   wired to a TEST npub + test session, not production keys.
4. Live end-to-end test with Nostur against the test npub.
5. Cut over: bridge on the real npub, remove `nostr` from
   `plugins.enabled`, gateway restart.
6. Rollback path: re-enable the nostr plugin; the two can run side by side
   only if they subscribe different npubs (same npub twice = duplicate
   replies).
