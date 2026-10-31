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
| **Watchdog** | nostrbot-sdk has **no hung-loop stall detection** (Incident 1). Port the watchdog from hermes-nostr `adapter.py` (stall detection → soft reconnect → escalation to full rebuild) or upstream it into the SDK |
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
  `.limit(0)` replay semantics on reconnect so a fresh daemon doesn't replay
  history (open question §9).

## 8. Workstream: nostrbot-sdk upgrade to nostr-sdk 0.45.x

PyPI `nostr-sdk` latest stable: **0.45.1** (2026-08-19; 0.45.0 final
2026-08-05). nostrbot-sdk 0.5.1 pins `==0.44.2`; hermes-nostr already runs
`>=0.45.0a7,<0.46`. Upgrade benefits:

1. **NIP-40 expiration on the *outside* of gift wraps** — on 0.44 the tag can
   only live inside the rumor, which relays can't see, so wraps are never
   garbage-collected. hermes-nostr's `dm_expiration_tag` +
   `nip59_make_gift_wrap_async(extra_tags=[exp_tag])` is the reference
   pattern. The Rust fork (`unsaltedbutter-ai/nostr`, branch
   `feat/nip17-expiration`, commit `c3a749a1`) already adds NIP-40 expiration
   to the gift-wrap/DM builders upstream of the bindings.
2. Moves both consumers onto the **supported stable binding generation**
   (wheels for macOS arm64/x86_64 and common Linux targets) instead of an
   alpha-era divergence.
3. One API generation across hermes-nostr + nostrbot-sdk — the incident
   debugging playbook (`UnwrappedGift.from_gift_wrap_async`) matches
   everywhere.

**Plan:** do it in the nostrbot-sdk repo as its own release (per the SDK
primer's ship process): bump pin, adapt `bot.py` call sites (sync
`UnwrappedGift.from_gift_wrap` → `_async` variant, signer-less `Client`,
`ReqTarget` subscriptions), run the 167-test battery + consumer tests
(orchestrator 510, tts_bot 275), tag, bump consumer pins.

**Decoupling rule:** the proxy targets the nostrbot-sdk public API
(`NostrBot` / `on_dm` / `send_dm` / `Dedup`), which is version-agnostic —
proxy work proceeds in parallel; `send_dm`'s expiration semantics improve for
free post-upgrade. Do not couple the proxy's delivery path to the SDK version
bump (the INCIDENTS.md lesson: version churn and delivery changes must not
land together).

## 9. Open questions (verify before/during implementation)

1. `Idempotency-Key` on `/api/sessions/{id}/chat` — undocumented; if
   unsupported, use `/v1/runs` (durable, documented) or
   `/v1/chat/completions` + `X-Hermes-Session-Id`.
2. Exact behavior of a second concurrent turn on one session (lease waits vs
   409) — proxy serializes per personality regardless; confirm the failure
   mode for safety.
3. `.limit(0)` subscription semantics on reconnect — confirm no history
   replay / no gap (SDK comment says "receive new ones only"; verify what a
   relay actually serves after a reconnect, and whether a fresh daemon with an
   empty dedup set could replay old DMs).
4. Hermes session durability across gateway restarts/updates (the session map
   assumes it); handle mapped-session-gone (`not_found`) by recreating.
5. Where the per-personality nsecs live (LaunchAgent env file with 0600 perms
   vs keychain); never in the repo.

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
