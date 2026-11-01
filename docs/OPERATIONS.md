# OPERATIONS — running the daemon day to day

Everything here assumes you have shell access to this Mac as the user the
daemon runs under. For *why* things are the way they are, read
[DESIGN.md](../DESIGN.md); for symptom-driven debugging, read
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## The process

- LaunchAgent: `ai.unsaltedbutter.nostr-hermes-proxy` (KeepAlive — it
  restarts itself if it dies).
- Logs: `/tmp/nostr-hermes-proxy.err.log` (everything) and
  `/tmp/nostr-hermes-proxy.out.log`.
- Restart (picks up code + config + env changes):
  ```bash
  launchctl kickstart -k gui/$(id -u)/ai.unsaltedbutter.nostr-hermes-proxy
  ```
- Config check without starting:
  `venv/bin/python -m nostr_hermes_proxy --check` (uses the default config
  path; see `--help`).

### What a healthy startup looks like in the log

```
Starting 4 personality(ies) against http://127.0.0.1:8642
Personality Sam-I-Am started on 2 relay(s)          # ~6s apart, staggered
Personality unsaltedbutter-bot started on 2 relay(s)
Personality nostrmix-bot started on 2 relay(s)
Personality Notible started on 2 relay(s)
DM relay list verified on wss://nos.lol for <name>  # ×4, after ~5s settle
Control plane listening on http://127.0.0.1:8787
Outbox worker started (rate cap 10/min, 5 attempts max)
Watchdog polling 4 personality(ies) every 60s (threshold 3)
```

Starts are staggered (~6s) on purpose: relay per-IP rate limits silently
reject publish bursts, and each personality publishes kind 0/10002/10050 on
connect. The verification pass reads every identity's kind-10050 back from
nos.lol and republishes any that went missing
(`_verify_and_repair_dm_relay_lists` in `__main__.py`).

## Configuration files

| File | Purpose |
|---|---|
| `~/.config/nostr-hermes-proxy/config.toml` | Everything non-secret |
| `~/.config/nostr-hermes-proxy/env` | Secrets, mode 0600, `KEY=value` lines |
| `~/.config/nostr-hermes-proxy/control-token` | Control-plane bearer token (auto-generated 0600 on first start) |

Key config knobs (see `config.example.toml` for the annotated set):

- `hermes_url` — the gateway API (`http://127.0.0.1:8642`)
- `allow_npub` — the single allowlisted sender (the owner). This is also the
  locked recipient for all outbound mail.
- `[[personality]]` blocks — `name`, `nsec = "${VAR}"`, `relays`,
  optional `model`/`provider` overrides, optional `[personality.profile]`
  table (`name`, `about`, `picture`, `nip05`, `lud16`, `website`) published
  as kind-0 metadata on connect.
- `default_personality` — used when a control-plane call omits
  `personality` (set to `"Sam-I-Am"`). Must name a configured personality.
- `outbound_rate_limit_per_minute` (10), `outbound_expiry_max_seconds`
  (604800 = 7 days), `outbound_spread_seconds` (3600) — outbound caps.
- `control_port` (8787; 0 disables), `watchdog_*` knobs.

Secrets in the env file: `HERMES_API_KEY` (gateway bearer) and one
`NSEC_<NAME>` per personality.

## The four identities

| Internal name (config/control plane) | Published display name |
|---|---|
| `Sam-I-Am` (default) | Sam-I-Am |
| `nostrmix-bot` | nostrmix-bot |
| `unsaltedbutter-bot` | unsaltedbutter bot |
| `Notible` | Notible |

Internal names allow `[A-Za-z0-9_-]` only; a `[personality.profile]` `name`
key overrides the published display name (that's how the space in
"unsaltedbutter bot" works). All personalities allowlist and deliver to the
same owner.

### Adding an identity

1. Add the nsec to the env file as `NSEC_<SANITIZED_NAME>` (uppercase, `-`/space → `_`), keep mode 0600.
2. Append a `[[personality]]` block (copy an existing one; adjust name, env var, profile).
3. Restart the daemon; confirm the startup log shows the new personality and
   a `DM relay list verified on wss://nos.lol for <name>` line.
4. The new identity publishes its own kind 0/10002/10050; verify externally
   if you like (see the probe snippet in
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

## The control plane (outbound)

Loopback-only (`127.0.0.1:8787`), bearer-authed with the control token:

- `POST /v1/dm` — body `{"text": …, "personality": …, "expiry_seconds": …,
  "idempotency-key": …}` → `202` with `{"accepted": true, "chunks": N,
  "duplicate": bool, "message_id": …}`. Omitting `personality` uses
  `default_personality`. Same idempotency key within 24h → no second send.
- `GET /v1/outbox/status` — read-only queue snapshot (pending messages,
  attempts, backoff, previews). See
  [HERMES-DIAGNOSTICS.md](HERMES-DIAGNOSTICS.md) for the Hermes-facing how-to.
- `GET /v1/health` — per-personality health (watchdog state).

`nostr-send` CLI (installed in the proxy venv, symlinked at
`~/.local/bin/nostr-send`; cron briefs use the absolute venv path):

```bash
/Users/butter/nostr-hermes-proxy/venv/bin/nostr-send \
  --idempotency-key <key> --personality "Sam-I-Am" --file <(output)
```

Exit codes: `0` sent (or duplicate) · `1` HTTP error (body printed) · `2`
daemon unreachable. The daemon enforces the rate cap, retries delivery (5
attempts, exponential backoff), and locks the recipient to the owner.

## The gateway dependency

The daemon talks to the Hermes API server at `127.0.0.1:8642` (bearer
`HERMES_API_KEY` from the env file). Sessions are created via
`POST /api/sessions` with unique UTC-timestamped titles; turns go through
`POST /v1/runs` (202 always; only `Idempotency-Replayed: true` marks a
replay) and are consumed over `GET /v1/runs/{id}/events` with a poll
fallback. Gateway-side setup and probes: [hermes-gateway-setup.md](hermes-gateway-setup.md).

Cron integration is **complete** (2026-11-01): all four Hermes cron jobs
deliver via `nostr-send` — see
[HERMES-CRON-SETUP.md](HERMES-CRON-SETUP.md) for the exact wiring.

## The sibling SDK

`nostrbot-sdk` (git-tag pinned in `pyproject.toml`, currently v0.9.0)
provides `NostrBot` (relay lifecycle, profile publishing, gift-wrap
subscribe/dispatch, watchdog hooks). Protocol-level changes go there, not
here. After moving a tag, reinstall into this repo's venv:

```bash
venv/bin/pip install --no-cache-dir --force-reinstall --no-deps \
  "nostrbot-sdk @ git+https://github.com/unsaltedbutter-ai/nostrbot-sdk.git@v0.9.0"
```

(`--force-reinstall --no-deps` is required — the pip cache serves stale
tag builds otherwise, and `nostr-sdk` itself is not on PyPI.)

## Routine operations

- **Restart**: `launchctl kickstart -k …` (above); watch the log for the
  healthy-startup shape.
- **Health check**: `curl -s -H "Authorization: Bearer $TOKEN"
  http://127.0.0.1:8787/v1/health`.
- **Queue check**: same with `/v1/outbox/status`.
- **Test send**: any `nostr-send` with a fresh idempotency key.
- **Metadata repair** (rare): the startup pass self-heals; for a manual
  repair, restart the daemon — every connect republishes kind
  0/10002/10050 (nostrbot-sdk v0.9.0+).
