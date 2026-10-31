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
| `/new` | Start a fresh Hermes session (keeps the model preference) |
| `/model` | List available models |
| `/model provider/model` | Switch this personality's model |
| `/status` | Personality, model, session age, queue, relays, watchdog state |
| `/help` | Command list |

Anything else is forwarded to Hermes verbatim. Long replies are split at
roughly 4096 characters with fence-aware `(1/N)` markers.

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
