# DESIGN.md — nostr-hermes-proxy

Status: **planning** (2026-10-31). No implementation yet. This document is the
charter: architecture, verified contracts, feature specs, open questions, and
rollout.

Companion docs (copied from
[`hermes-nostr`](https://github.com/unsaltedbutter-ai/hermes-nostr)@`2f1a5f8`):
[INCIDENTS.md](INCIDENTS.md) — the delivery-failure record that motivates the
reliability requirements below; [OPTIONS.md](OPTIONS.md) — the decoupling
proposal this repo implements.

---

## 1. What this is

A standalone daemon that proxies NIP-17 Nostr DMs to a locally-running Hermes
gateway and proxies the responses back. Hermes never learns Nostr exists; the
proxy depends only on Hermes's authenticated HTTP API, so `hermes update`
churn cannot break Nostr delivery (the failure record in INCIDENTS.md drove
this design).

**Deployment model:** one process, its own LaunchAgent (`ai.unsaltedbutter.nostr-hermes-proxy`
or similar), never restarted by Hermes operations.

## 2. Multi-personality model

The proxy hosts **N simultaneous conversations, each with a unique `nsec`**:

- Config lists personalities: `{ name, nsec, relays?, model?, … }`.
- Each personality is an independent Nostr identity (its own `NostrBot`
  instance, subscription, dedup set, locks, kind-10050 list).
- Every personality allows exactly **one sender npub** (the owner's mobile
  npub). Deny-by-default; any other sender is dropped and logged.
- **No concurrency within a personality by design** (one human DMs one
  identity) — but the proxy still serializes turns per personality: a
  multi-minute Hermes turn must not block the notification loop or corrupt
  state if a second DM arrives meanwhile (queue it, don't run it).

Personality = identity + Hermes session. Personalities are a clean way to get
"channels" — e.g. one nsec per project or per agent persona — with the
session mapping keeping each conversation separate.

## 3. Responsibilities split

### Provided by nostrbot-sdk (library, tested — 167 tests, 3 consumers)

- Client lifecycle, relay pool management, persistent inbox relays
- Kind-1059 gift-wrap subscription (`.limit(0)` — the silent-drop footgun)
- NIP-17 unwrap (`UnwrappedGift.sender()`, never `rumor.author()`)
- Dedup, per-user locks, NIP-04/NIP-17 protocol-matched `send_dm`
- NIP-40 expiration on outbound DMs (`dm_expiry_seconds`)
- Kind-10050 inbox publication, kind-0 profile
- `@bot.on_dm` dispatch with `SenderContext`

### Written new in this repo (proxy policy + glue)

| Concern | Notes |
|---|---|
| **Multi-personality fan-out** | N × `NostrBot` (`start()` per personality as an asyncio task; not `run()`, which installs signal handlers). Shared process, per-identity state only |
| **Allowlist** | Single npub check in the `on_dm` handler (`ctx.sender_hex == ALLOWED`) |
| **Hermes client** | `httpx` against `127.0.0.1:8642`, bearer `API_SERVER_KEY`. Session create / chat-stream / model-options / runs |
| **Session map** | personality → Hermes session id, **persisted atomically** (tmp+replace, the hermes-nostr seen-IDs pattern). Create on first contact via `POST /api/sessions`. `/new` swaps it |
| **Command layer** | `/new`, `/model`, `/status` parsed *before* forwarding. See §6 |
| **Ack policy** | Immediate ack DM on accept, or delayed ack past a threshold (avoid ack+reply double-message on fast turns). Configurable |
| **Tool verbosity** | From the turn's SSE stream: omit entirely, or one-line summary (tools used, duration, failures). Precedent: hermes-nostr's `format_tool_event` returns None — on Nostr every tool event would be its own gift wrap |
| **Reply chunking** | ~4096 chars, code-fence-aware `(1/N)` splitting — port from gateway core's `truncate_message` (NOT in hermes-nostr or nostrbot-sdk) |
| **Relay hygiene** | See §7 — the INCIDENTS.md remediations |
| **Watchdog** | ✅ SDK v0.6.0 ships the Incident-1 hooks (`health_check()`, `restart()`, `watchdog_stall_seconds`); the proxy `Watchdog` polls per personality (60 s), restarts unhealthy bots, and after N failures escalates to process exit so the LaunchAgent restarts us. The proxy's `watchdog_stall_seconds` is plumbed into each bot's SDK config so the SDK's `health_check()` is the single source of truth |
| **Error paths** | `run.failed` / `run.cancelled` / `tool.failed`-aborted turns → send an error DM rather than silence |

## 4. Verified Hermes API contract (checked against live docs 2026-10-31)

Auth: `Authorization: Bearer $API_SERVER_KEY` on `http://127.0.0.1:8642`.

- `POST /api/sessions` — create empty session; `GET/DELETE /api/sessions/{id}`;
  `GET /api/sessions/{id}/messages`; `POST /api/sessions/{id}/fork`.
- `POST /api/sessions/{id}/chat/stream` — one synchronous turn as SSE:
  `assistant.delta`, `assistant.commentary`, `tool.started`,
  `tool.completed` (carries tool name, duration, error flag, redacted 500-char
  result preview), `tool.failed`, terminal `run.completed` / `run.failed` /
  `run.cancelled`. `: keepalive` comment lines every 10 s (parsers must skip
  `:` lines).
- **Per-request model selection is supported** on session-chat endpoints:
  `model`, `provider`, `model_options` — always honored (the opt-in flag only
  applies to bare `model` on the OpenAI-compat endpoints). `GET
  /api/model/options` returns the curated provider/model inventory for a
  `/model` menu.
- `Idempotency-Key` is documented on `/v1/chat/completions` and `/v1/responses`
  (exact retry replays; conflicting retry → 409) and on `/v1/runs` (durable,
  24 h, survives gateway restarts, replay returns original `run_id`). **Not
  documented on `/api/sessions/{id}/chat`** — open question §9.
- **`/v1/runs` alternative:** submit with `session_id` + `Idempotency-Key`,
  subscribe `GET /v1/runs/{id}/events` (same tool lifecycle events +
  `subagent.*`), poll `GET /v1/runs/{id}` (returns `status`, `output`,
  `usage`, `runtime`). Also `POST /v1/runs/{id}/stop` (→ future `/stop`
  command) and the approval/clarify controls. Durable idempotency here is
  attractive: gift-wrap event id as key.
- `X-Hermes-Session-Key` — stable per-personality long-term-memory scope
  (e.g. `nostr-hermes-proxy:<personality-name>`) that survives `/new`.
- Caveat: API-server sessions are their own sessions — they cannot bind
  sessions owned by other surfaces (CLI/TUI/messaging). Exactly what we want.
- Over-cap concurrent runs → 429; a session turn lease serializes concurrent
  writers. The proxy serializes per personality regardless and backs off on
  429/409.

## 5. Message flow

```
DM arrives on personality P (allowlisted sender)
  → dedup (SDK) → unwrap → allowlist check → persist seen
  → send ack (per policy)
  → command? → handle locally (reply, no Hermes turn)
  → enqueue turn (per-personality serial queue)
  → Hermes: ensure session (map hit, else POST /api/sessions)
  → POST /api/sessions/{sid}/chat/stream {input, model?, provider?}
  → collect SSE: final text from assistant deltas; tool events → summary or omit
  → chunk → send_dm() back to the sender npub
  → terminal run.failed/cancelled → error DM
```

## 6. Command layer (draft)

Intercepted before Hermes, matched on the raw DM text:

| Command | Behavior |
|---|---|
| `/new` | Create fresh session (`POST /api/sessions`), swap the map entry, ack "new conversation started". Optionally `DELETE` the old session or `POST .../fork` to keep it |
| `/model` | List models (`GET /api/model/options`) or switch: store per-personality `{model, provider}` preference, sent on every turn request (per-request selection is always honored on session-chat endpoints) |
| `/status` | Personality name, current model, session age, queue depth, relay statuses, gateway health (`GET /health/detailed`) |
| `/stop` | (future) `POST /v1/runs/{run_id}/stop` for the in-flight turn — requires the `/v1/runs` flow |
| `/help` | Command list |

Non-command text always goes to Hermes verbatim.

## 7. Relay hygiene (from INCIDENTS.md — requirements, not options)

- **Order the kind-10050 inbox list with `nos.lol` first.** Incident 5:
  damus.io black-holes gift wraps (accepts writes, never serves reads), and a
  client that publishes only to the first advertised relay loses every DM
  silently. Consider dropping verified-black relays from the subscription set
  entirely.
- **Per-relay canary**: timer that publishes a kind-1059 gift wrap (test key →
  own npub) per relay and reads it back **by event id** from the same relay.
  Write-success without readback = black-holed relay → alert. This closes the
  blind spot of the old hardcoded-nos.lol canary (Incident 5).
- **Per-relay silence logging**: "nothing received in N min" per relay, so a
  dead subscription is visible (OPTIONS.md trade-off note).
- Lookback/dedup on reconnect per the SDK's existing behavior; verify the
  `.limit(0)` replay semantics so a fresh daemon doesn't replay history
  (open question §9). **Privacy note (probe finding):** NIP-59 protects the
  payload only — gift-wrap *metadata* (recipient `#p`, volume, timing) is
  world-readable; a throwaway key could observe all 393 of the bot's gift
  wraps in nos.lol's retention. Inherent to NIP-59; assume DM metadata is
  public when choosing relays.

## 8. Workstream: nostrbot-sdk upgrade to nostr-sdk 0.45.x — ✅ DONE

Shipped as **nostrbot-sdk v0.6.0** (2026-10-31, tag `v0.6.0`, branch merged to
`main`). The 0.44→0.45 migration had already landed upstream at v0.5.2; this
release aligned the pin to stable `>=0.45.1,<0.46`, fixed the residual
alpha→stable API drift (`Tag.to_vec()`, list-returning `Event.tags()`, sync
`nip04_encrypt`), and added:

1. **NIP-40 expiration on the *outside* of gift wraps** — already in v0.5.2.
2. **Watchdog hooks**: `NostrBotConfig.watchdog_stall_seconds`,
   `NostrBot.health_check()`, `NostrBot.restart()` (soft reconnect, no
   identity-event republish), `NostrBot.last_activity_monotonic` — the
   Incident-1 failure class is now detectable at library level.
3. **`SenderContext.event_id`** — the outer gift-wrap id, used by the proxy
   as the HTTP `Idempotency-Key`.
4. **Bounded gift-wrap replay window** (`gift_wrap_lookback_seconds`, default
   49 h) — see open question 3 below.

Test battery: **234 passing** in the SDK repo. Consumer pins (orchestrator /
tts_bot on the other machine, `nostrmix-bot` locally) still need bumping to
v0.6.0 — noted as follow-up for their maintainers.

## 9. Open questions (status)

1. `Idempotency-Key` on `/api/sessions/{id}/chat` — **still unverified**;
   blocked on enabling the gateway's API server (operator action:
   `API_SERVER_ENABLED=true` + `API_SERVER_KEY=…` in `~/.hermes/.env`, then a
   gateway restart). The proxy already sends the header unconditionally (key
   = `ctx.event_id`, the outer gift-wrap id), so the answer changes behavior,
   not code. If session-chat ignores it, pivot to `/v1/runs` (durable,
   documented) or `/v1/chat/completions` + `X-Hermes-Session-Id`.
2. Exact behavior of a second concurrent turn on one session (lease waits vs
   409) — same blocker. The proxy serializes per personality regardless;
   confirm the failure mode for safety.
3. `.limit(0)` subscription replay semantics — **ANSWERED** (empirical probe
   against `nos.lol`, 2026-10-31; scripts+logs under
   `/var/tmp/opencode/nostr-probe/`). Verdict: nos.lol serves **zero stored
   events** for `limit:0` (fresh subscribe and after reconnects) while live
   frames flow normally — 393 stored gift wraps for the bot's npub exist in
   retention and were served only on no-limit filters, so a cold-started
   daemon would NOT replay history on nos.lol. Caveat: NIP-01 does not
   define `limit:0`; relays that treat `0` as unlimited would replay their
   full retention. Shipped as nostrbot-sdk v0.6.0
   `gift_wrap_lookback_seconds` (default 49 h): any replay is capped at the
   window and absorbed by dedup.
4. Hermes session durability across gateway restarts/updates (the session map
   assumes it) — deferred; the proxy already handles mapped-session-gone
   (`not_found`) by recreating and retrying the same `Idempotency-Key`.
5. Where the per-personality nsecs live — **decided**: `${ENV}`-interpolated
   into `config.toml`, values provided via the LaunchAgent (0600-perm env
   file); `config.example.toml` carries placeholders only, and a test asserts
   no literal `nsec1` values are committed.

## 10. Configuration sketch (draft)

```toml
# config.toml (secrets via env / LaunchAgent env file, not here)
hermes_url = "http://127.0.0.1:8642"
# hermes_key from env: HERMES_API_KEY (API_SERVER_KEY value)
allow_npub = "npub1… (owner's mobile npub)"

ack_delay_seconds = 3          # ack only if no reply within N s (0 = always ack)

[[personality]]
name   = "atlas"
nsec   = "${NSEC_ATLAS}"       # env-interpolated, never in the file
model  = "…/…"                 # optional per-personality default
relays = ["wss://nos.lol", "wss://relay.damus.io"]
```

## 11. Rollout (from OPTIONS.md, adapted)

1. Keep the current hermes-nostr plugin running unchanged.
2. Enable the Hermes API server (`API_SERVER_ENABLED`/`API_SERVER_KEY`),
   restart gateway, verify with a curl to `/v1/chat/completions`.
3. Build the proxy wired to a **test nsec + test session**, not production keys.
4. Live end-to-end test from the mobile client against the test personality.
5. Cut over personalities one at a time: the plugin and the proxy can run
   side-by-side **only on different npubs** (same npub twice = duplicate
   replies).
6. Rollback: re-enable the nostr plugin for that npub; stop the proxy
   personality.

## 12. Out of scope (for now)

- Anything Hermes *initiates* to Nostr (cron deliveries, proactive sends) —
  conversational DMs only, per OPTIONS.md's trade-off note. A tiny outbound
  endpoint can be added later if needed.
- Group chats, media, reactions/read receipts (no NIP-17 equivalents without
  metadata leaks).
