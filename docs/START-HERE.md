# START HERE — agent onboarding for nostr-hermes-proxy

You are looking at a single-purpose daemon: it bridges **NIP-17 encrypted Nostr
DMs** to a **local Hermes gateway** (HTTP API) and back. Four bot identities
run inside it. The owner talks to any of them from their phone (Nostur);
commands are handled proxy-side, everything else becomes a Hermes turn and the
reply comes back as ordered, encrypted DM chunks. Hermes cron jobs push
messages out through a loopback control plane.

```
Nostur (owner) ⇄ relays (nos.lol + nip17.unsaltedbutter.ai)
        │ gift wraps (NIP-17/NIP-59)
        ▼
nostr-hermes-proxy (this repo, LaunchAgent)
  ├─ 4 personalities → nostrbot-sdk (NostrBot)
  ├─ command layer: /new /model /status /stop /help
  ├─ Hermes gateway client (127.0.0.1:8642, /v1/runs)
  ├─ outbound control plane (127.0.0.1:8787, POST /v1/dm, GET /v1/outbox/status)
  └─ outbox worker + watchdog
        ▲
        └── Hermes cron jobs ── nostr-send CLI ──┘
```

## Reading paths — start with the one that matches your goal

| Your goal | Read | Why |
|---|---|---|
| Run, restart, or configure the daemon; add an identity | [docs/OPERATIONS.md](OPERATIONS.md) | LaunchAgent, config/env files, control plane, `nostr-send`, healthy-startup log shapes |
| Something is broken — a DM didn't arrive, a bot went silent | [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptom-driven playbook with paste-ready probes |
| Add a feature, command, or config knob | [docs/ENHANCING.md](ENHANCING.md) | Module map, step-by-step recipes, testing and commit conventions |
| Understand *why* it was built this way | [DESIGN.md](../DESIGN.md) | The full design rationale, section by section |
| Alternatives that were rejected and why | [OPTIONS.md](../OPTIONS.md) | Platform trade-off analysis that produced this architecture |
| What has gone wrong before (read before "fixing" relays) | [INCIDENTS.md](../INCIDENTS.md) | Five post-mortems: silent-deaf bots, relay mismatches, black-holed gift wraps |
| Migrate/operate Hermes cron jobs | [docs/HERMES-CRON-SETUP.md](HERMES-CRON-SETUP.md) | The brief the Hermes CLI agent executed (migration is complete) |
| A cron DM seems missing | [docs/HERMES-DIAGNOSTICS.md](HERMES-DIAGNOSTICS.md) | Hermes-facing: read-only outbox status checks |
| Outbound subsystem spec | [docs/OUTGOING-MESSAGES.md](OUTGOING-MESSAGES.md) | Control plane + CLI design doc (provenance note inside) |
| Set up or verify the Hermes gateway itself | [docs/hermes-gateway-setup.md](hermes-gateway-setup.md) | Gateway-side API server brief |
| Relay behavior evidence (what nos.lol actually does) | [docs/relay-replay-probe.md](relay-replay-probe.md) and [docs/probe/](probe/) | Empirical probes: `limit:0` semantics, replay behavior, scripts + logs |

## Ground truths — assume these or you will be wrong

1. **NIP-59 timestamps are randomized up to 2 days into the past.** Clients
   order by `created_at`; a naive multi-chunk message arrives scrambled. The
   proxy defeats this with per-chunk timestamps sorted descending (chunk 1
   always newest) inside a configurable spread window
   (`outbound_spread_seconds`, default 3600). Do not "fix" timestamps back to
   now.
2. **The recipient's kind-10050 list decides DM delivery.** Senders send gift
   wraps to the relays in the *recipient's* NIP-17 inbox list. Every
   personality publishes kind 0/10002/10050 on every connect and reconnect
   (nostrbot-sdk v0.9.0+), and the daemon verifies the lists landed on
   nos.lol at startup — relay per-IP rate limits silently drop bursts.
3. **Only one sender is allowed.** `allow_npub` locks every personality to
   the owner's npub; non-allowlisted senders are dropped *silently* (no ack,
   no error) by design. The recipient of outbound mail is the same owner,
   never caller-supplied.
4. **`/v1/runs` returns 202 for everything.** Only the
   `Idempotency-Replayed: true` header/body flag means replay. Session titles
   must be unique on the gateway (`400 invalid_title` otherwise).
5. **This machine's relays**: nos.lol treats `limit:0` as "zero stored
   events, live only" — the SDK pairs `limit:0` with a 49-hour `.since`
   lookback guard. Read [docs/relay-replay-probe.md](relay-replay-probe.md)
   before touching subscription filters.
6. **Secrets never enter git, logs, or argv.** nsecs live in the 0600 env
   file, referenced from TOML as `${VAR}`; a guard test fails the suite if a
   literal key lands in a tracked file. See below.

## Non-negotiable conventions

- **Git identity**: `Info <info@unsaltedbutter.ai>`; remote uses the SSH host
  alias `git@github.unsaltedbutter`; commit messages are short imperatives;
  **never** add Co-Authored-By or other trailers.
- **Secrets**: nsec/API keys only in `~/.config/nostr-hermes-proxy/env`
  (mode 0600), interpolated as `${VAR}` in config.toml. Never echo them into
  shells, prompts, logs, or tests. `tests/test_secrets.py` enforces the repo
  half.
- **Testing**: run `venv/bin/python -m pytest -q` (full suite) before every
  commit; it must be green. The harness pattern is in
  [docs/ENHANCING.md](ENHANCING.md).
- **SDK boundary**: protocol/crypto primitives live in the sibling
  `nostrbot-sdk` repo (also owned; pinned here by git tag). Orchestration and
  policy stay in this repo. See [docs/ENHANCING.md](ENHANCING.md).
- **Docs update with behavior**: if you change behavior, update DESIGN.md and
  the relevant doc here in the same commit.
