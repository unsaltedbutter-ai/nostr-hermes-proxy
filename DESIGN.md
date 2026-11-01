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
- **One conversation per personality, growing until the other party sends
  `/new`.** The Hermes session is long-lived and spans many Nostr messages;
  the SessionMap's persistent session id — never a message id — anchors it.
  Multiple nsec run in one process; separate proxy instances (separate
  config/state dirs) also work but are unnecessary.
- **A second DM mid-turn should *steer*, not queue** (decision 2026-10-31):
  forward the new text into the in-flight Hermes turn via
  `POST /v1/runs/{run_id}/steer` rather than holding it in line. That
  requires submitting turns via `/v1/runs` and capturing the `run_id` —
  gated on the live-gateway probes in `docs/hermes-gateway-setup.md`
  (Probe C). Until steering lands, the MVP queues the message per
  personality (asyncio lock) and runs it after the current turn completes.

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
| **Ack policy** | Instant `👀` DM on every accepted non-command DM (including steers), with a 20-minute absolute NIP-40 expiry (`ack_text`/`ack_ttl_seconds` knobs). Built proxy-side (rumor + gift wrap via the SDK primitives) so it doesn't inherit the 7-day default |
| **Ordered multi-chunk delivery** | NIP-59 randomizes wrap timestamps up to 2 days back, which scrambles multi-chunk ordering in clients. Every delivery (acks, replies, cron outbound) assigns each chunk a random backdate within `outbound_spread_seconds` (default 3600), **sorted ascending across chunks** — chunk 1 is always the newest, so clients show chunks in reading order while each wrap still looks individually random to relays. Implemented via nostrbot-sdk v0.8.0 `make_gift_wrap_async(..., wrap_created_at=…)` |
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

**Live-probe addendum (2026-10-31, gateway v0.22.5, PID 5324):** session-chat
**ignores** `Idempotency-Key` (Probe A — keyed retry ran a full new turn);
concurrent same-session chats **serialize** cleanly, no 409/429 (B);
**steering works**: `POST /v1/runs/{run_id}/steer` body `{"input": "…"}` →
`{"accepted": true}` and the final output reflected the steer (C); **sessions
and full history survive a gateway restart** (D — 13/13 messages intact);
`/v1/runs` idempotency is durable with 24 h retention; the session-chat
stream's terminal `done` frame carries a `run_id`.

## 5. Message flow

```
DM arrives on personality P (allowlisted sender)
  → dedup (SDK) → unwrap → allowlist check → persist seen
  → send ack (instant 👀, 20-min expiry)
  → command? → handle locally (reply, no Hermes turn)
  → turn in flight? steer it (POST /v1/runs/{id}/steer) — VERIFIED (Probe C)
    : submit turn via POST /v1/runs (Idempotency-Key = gift-wrap event id;
      durable 24h; a replayed key returns the original run_id → duplicate
      dropped, the first delivery already replied)
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
| `/new` | Create fresh session (`POST /api/sessions`), swap the map entry, echo "New conversation started. Model: <label>. Context: <total> tokens." Model/provider preferences are preserved across `/new` |
| `/model` | No args: numbered menu of the gateway's models, read **live from `~/.hermes/config.yaml`** (enabled providers → their models; falls back to the API's user-defined inventory when unreadable). `/model N` switches to menu entry N; `/model provider/model` switches literally. The preference is sent on every turn request — no conversation restart needed |
| `/status` | Personality, model, session age, **state (busy with an in-flight turn vs idle)**, **context consumed vs total with percentage** (consumed from the last turn's `usage.input_tokens`; total from the gateway config's `context_length`), queue depth, relay count, watchdog state |
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

1. `Idempotency-Key` on `/api/sessions/{id}/chat` — **ANSWERED (Probe A,
   2026-10-31): ignored** — a keyed retry ran a full new turn (4.1 s then
   1.7 s, no replay, no 409). Resolution per the original decision: the proxy
   pivots to `/v1/runs`, whose idempotency is durable with 24 h retention
   (`runs_idempotency: {supported: true, durable: true,
   retention_seconds: 86400}`). The gift-wrap event id stays the
   per-request key; session identity remains the SessionMap's persistent id
   spanning many messages.
2. Second DM mid-turn — **steering, VERIFIED (Probe C)**:
   `POST /v1/runs/{run_id}/steer` with `{"input": "…"}` → 200
   `{"accepted": true}`; final output visibly reflected the steer.
   Implementation: turns are submitted via `POST /v1/runs` (+ events
   stream), the in-flight `run_id` is tracked per personality, and a
   mid-turn DM is steered; on refusal (turn already terminal) the DM falls
   back to a new turn submission. Probe B (serialization) is the safety net.
   Steered DMs get no ack — the running turn's reply covers them.
3. `.limit(0)` subscription replay semantics — **ANSWERED** (empirical probe
   against `nos.lol`, 2026-10-31; scripts+logs under
   `docs/probe/`, write-up in `docs/relay-replay-probe.md`). Verdict: nos.lol
   serves **zero stored events** for `limit:0` (fresh subscribe and after
   reconnects) while live frames flow normally — 393 stored gift wraps for
   the bot's npub exist in retention and were served only on no-limit
   filters, so a cold-started daemon would NOT replay history on nos.lol.
   Caveat: NIP-01 does not define `limit:0`; relays that treat `0` as
   unlimited would replay their full retention. Shipped as nostrbot-sdk
   v0.6.0 `gift_wrap_lookback_seconds` (default 49 h): any replay is capped
   at the window and absorbed by dedup. Dedup design note: duplicate
   detection is already two layers deep on the **gift-wrap event id** —
   (a) the SDK's in-memory `Dedup` and (b) the Hermes `Idempotency-Key`.
   A text+time hash would be strictly weaker: NIP-59 deliberately fuzzes
   `created_at` up to 48 h, so "sent time" is not a trustworthy
   distinguisher, and the event id is cryptographically unique per
   redelivery. Rapid repeat `/new` is harmless — each creates a session and
   the map's last write wins.
4. Hermes session durability across gateway restarts — **ANSWERED (Probe
   D): yes** — sessions and full history survive a gateway restart (13/13
   messages intact post-restart, new PID). The SessionMap persists
   atomically across proxy restarts; the `not_found` → recreate-and-retry
   path remains as a safety net, not a requirement.
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

ack_text = "👀"                # instant ack content on every non-command DM
ack_ttl_seconds = 1200         # the ack expires 20 minutes after sending
# Gateway config.yaml is read LIVE on every /model, /new, /status (menu +
# context totals) — override the path only if it lives somewhere unusual:
# hermes_config_path = "~/.hermes/config.yaml"

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

- ~~Anything Hermes *initiates* to Nostr~~ — **implemented 2026-11-01**: a
  loopback control plane (`POST /v1/dm` + `nostr-send` CLI + persisted
  idempotency + outbox worker) lets Hermes cron jobs push messages to the
  owner — see `docs/HERMES-CRON-SETUP.md`. Cron jobs are the only outbound
  caller; proactive agent-initiated sends remain out of scope. The daemon's
  `default_personality` knob (set to Sam-I-Am) resolves calls that omit
  `--personality`; with several identities configured and no default, the
  control plane still rejects the call with HTTP 400.
- Group chats, media, reactions/read receipts (no NIP-17 equivalents without
  metadata leaks).
