# nostr-hermes-proxy

A standalone **NIP-17 ↔ Hermes proxy**: DM a Nostr identity from your phone,
get your local [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s
answer back — end-to-end encrypted, with the gateway never knowing Nostr
exists.

**Status: MVP implemented.** See [DESIGN.md](DESIGN.md) for architecture and
[Running it](#running-it) for setup.

## What it will do

- Run as its own daemon (LaunchAgent), fully decoupled from the Hermes gateway
  — gateway updates can't break Nostr delivery
- Host one or more Nostr identities ("personalities"), each configured with
  its **own `nsec`** and its own conversation
- Accept DMs from exactly **one allowlisted npub** per personality; deny
  everyone else
- Forward each DM to the local Hermes gateway over its authenticated HTTP API,
  keeping one persistent Hermes session per personality
- **Steer** a follow-up DM into the turn already running instead of queueing it
- Reply over NIP-17 gift wraps with an **instant 👀 ack** (its own DM, 20-minute
  NIP-40 expiry), **tool-call noise omitted or summarized**, long replies chunked
- Drop replayed duplicate deliveries (the gateway's durable `Idempotency-Key`
  replay) without sending a second reply
- Intercept special commands before Hermes: `/new` (fresh conversation,
  echoes model + context size), `/model` (numbered menu of your user-defined
  providers/models, switch by number or `provider/model`), `/status`
  (conversation position, busy/idle, session, queue, relays, watchdog)

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

### Message flow

Each ordinary DM is submitted as a durable Hermes **run**:

1. `POST /v1/runs` with `{"input": text, "session_id": …}` and
   `Idempotency-Key: <gift-wrap event id>`. The key is retained by the gateway
   for 24 h, so an exact redelivery returns the original `run_id` with
   `Idempotency-Replayed: true` — the proxy logs it and sends **no reply**.
2. `GET /v1/runs/{run_id}/events` is streamed for the reply. `message.delta`
   tokens accumulate, `run.completed` carries the authoritative final output,
   `tool.completed`/`tool.failed` feed the optional one-line tool summary, and
   `: keepalive` comment lines are skipped. If the stream ends before a
   terminal frame, one `GET /v1/runs/{run_id}` poll provides `{status, output}`.
3. Every accepted non-command DM is acked immediately with a 👀 DM carrying a
   20-minute NIP-40 expiry, sent **before** the turn lock is acquired (so a DM
   that will be steered still gets exactly one ack). While a run is in flight,
   another DM from the owner is **steered** with
   `POST /v1/runs/{run_id}/steer` rather than queued; the running turn's reply
   covers both messages. If the steer is refused because the turn just
   finished, the proxy falls back to submitting a new turn (serialized by the
   gateway's session lease).
4. `run.failed` / `run.cancelled` / `run.interrupted` produce an error DM;
   successful replies are chunked at ~4096 characters with fence-aware
   `(1/N)` markers.

## Foundation

Built on [`nostrbot-sdk`](https://github.com/unsaltedbutter-ai/nostrbot-sdk) ≥
v0.6.0 (pinned), which wraps the `nostr-sdk` stable bindings (≥ 0.45.1) and
already handles the NIP-17 footguns
(kind-1059 subscriptions, dedup, per-user locks, protocol-matched DMs,
NIP-40 expiration, kind-10050 inbox publication, hung-loop
`health_check()`/`restart()` hooks, and a bounded gift-wrap replay window).
The proxy is policy and
glue: Hermes HTTP client, session mapping, commands, ack/verbosity policy,
relay hygiene, and the process-level watchdog that escalates to a LaunchAgent
restart.

## Docs

| Doc | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | Architecture, feature specs, verified API contract, open questions, rollout |
| [INCIDENTS.md](INCIDENTS.md) | Delivery-failure incident log (copied from hermes-nostr) |
| [OPTIONS.md](OPTIONS.md) | The decoupling proposal that motivated this repo (copied from hermes-nostr) |

## Running it

### 1. Install

Python 3.12 or 3.13 is required. (Python 3.14 currently has no wheel for a
transitive dependency of `nostr-sdk` and attempts a source build that fails.)

```sh
git clone git@github.unsaltedbutter:unsaltedbutter-ai/nostr-hermes-proxy.git
cd nostr-hermes-proxy
python3.12 -m venv venv
venv/bin/pip install -e ".[dev]"
```

### 2. Configure

```sh
mkdir -p ~/.config/nostr-hermes-proxy
cp config.example.toml ~/.config/nostr-hermes-proxy/config.toml
chmod 600 ~/.config/nostr-hermes-proxy/config.toml
```

Edit `config.toml`:

- `hermes_url` — the Hermes API server (normally `http://127.0.0.1:8642`).
- `allow_npub` — the one owner pubkey allowed to DM any personality.
- `[[personality]]` — one table per Nostr identity. Each `nsec` must be an
  `${ENV_VAR}` placeholder; a literal `nsec1...` is rejected at startup.
- Optional per-personality `relays`, `model`, `provider`, and a global `relays`
  fallback.
- `ack_text` (default `"👀"`) and `ack_ttl_seconds` (default `1200`, minimum
  60) for the immediate ack.
- `hermes_config_path` (default `~/.hermes/config.yaml`) — the gateway's
  config file, **re-read live on every `/model`, `/new`, and `/status`** (no
  caching): the `/model` menu lists the enabled providers and models defined
  there, and context totals come from each model's `context_length`
  (falling back to the top-level `model.context_length`). Change models in
  the gateway and the proxy follows on the next command — nothing to keep
  in sync. If the file is unreadable, `/model` falls back to the gateway
  API's user-defined inventory and context shows as unknown. Models without
  a context length show `Context: unknown for this model`; `/status` still
  reports consumed tokens.

Secrets come from the environment:

| Variable | Purpose |
|---|---|
| `HERMES_API_KEY` | Hermes `API_SERVER_KEY` value |
| `NSEC_<NAME>` | One key per personality, e.g. `NSEC_ATLAS` for `name = "atlas"` |

A 0600 env file is the recommended place for them:

```sh
install -m 600 /dev/null ~/.config/nostr-hermes-proxy/env
$EDITOR ~/.config/nostr-hermes-proxy/env
# HERMES_API_KEY=...
# NSEC_ATLAS=nsec1...
```

Alternatively export the variables in the shell / LaunchAgent env.

### 3. Run

```sh
venv/bin/python -m nostr_hermes_proxy --config ~/.config/nostr-hermes-proxy/config.toml
```

`--config` defaults to `$NOSTR_HERMES_PROXY_CONFIG` or
`~/.config/nostr-hermes-proxy/config.toml`. `--log-level` accepts
`DEBUG|INFO|WARNING|ERROR|CRITICAL`.

### 4. Install as a LaunchAgent

`deploy/ai.unsaltedbutter.nostr-hermes-proxy.plist` is a template: replace
`/Users/YOUR_USER`, copy it to `~/Library/LaunchAgents/`, and load it.

```sh
cp deploy/ai.unsaltedbutter.nostr-hermes-proxy.plist ~/Library/LaunchAgents/
# edit the WorkingDirectory path and the /Users/YOUR_USER references first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.unsaltedbutter.nostr-hermes-proxy.plist
launchctl kickstart -k gui/$(id -u)/ai.unsaltedbutter.nostr-hermes-proxy
```

Logs go to `/tmp/nostr-hermes-proxy.{out,err}.log`. To unload:

```sh
launchctl bootout gui/$(id -u)/ai.unsaltedbutter.nostr-hermes-proxy
```

The plist's wrapper command sources `~/.config/nostr-hermes-proxy/env` (0600)
before starting the proxy. The `EnvironmentVariables` block is only a fallback;
remove the placeholder values once the env file is in place.

### 5. Chat commands

| Command | Behavior |
|---|---|
| `/new` | Start a fresh Hermes session (keeps the model preference) and echo the model plus configured context size |
| `/model` | Numbered menu of the gateway's **user-defined** `provider/model` entries, with `← current` |
| `/model #` | Switch to entry `#` from the last menu |
| `/model provider/model` | Switch this personality's model directly |
| `/status` | Personality, model, context (consumed/total), busy/idle, session age, queue, relays, watchdog state |
| `/help` | Command list |

Anything else is forwarded to Hermes verbatim as a `/v1/runs` run, after an
immediate 👀 ack (20-minute expiry). A DM sent while a turn is running steers
that turn and still gets its own ack; an exact duplicate delivery is dropped
(the first delivery was already acked). Long replies are split at roughly 4096
characters with fence-aware `(1/N)` markers.

## Development

```sh
python3.12 -m venv venv
venv/bin/pip install -e ".[dev]"
venv/bin/python -m pytest -q
```

Tests use `httpx.MockTransport`, fake bots, and fake clocks: no network, no
relays, no real keys.

Sibling project:
[hermes-nostr](https://github.com/unsaltedbutter-ai/hermes-nostr) — the
in-gateway plugin this proxy replaces. The plugin stays runnable during the
transition (side-by-side on different npubs); see the rollout section of
DESIGN.md.

## License

[MIT](LICENSE) © unsaltedbutter
