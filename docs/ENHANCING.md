# ENHANCING — adding features the way this repo expects them

Read [START-HERE.md](START-HERE.md) first (ground truths + conventions),
then this. The golden rule: **the full test suite stays green, secrets stay
out of tracked files, and behavior changes update docs in the same commit.**

## Module map

| File | Responsibility |
|---|---|
| `nostr_hermes_proxy/__main__.py` | Wiring: config load, runtimes, control plane, outbox, watchdog, shutdown. Also the staggered startup and DM-relay-list verify/repair pass. |
| `nostr_hermes_proxy/config.py` | TOML parsing + validation (`ConfigError` messages name the problem). Unknown top-level keys are tolerated; personality names must match `[A-Za-z0-9_-]+`. |
| `nostr_hermes_proxy/personality.py` | The runtime: DM handling (allowlist → ack → command dispatch → turn lock → steering/queueing), `deliver_dm` (the single ordered delivery path used by acks, replies, and the outbox), Hermes turn lifecycle, run-id tracking. |
| `nostr_hermes_proxy/hermes.py` | Gateway client: sessions, `/v1/runs` submit (idempotency), SSE stream + poll fallback, `steer`, `stop_run`, model options. |
| `nostr_hermes_proxy/commands.py` | Command registry (`COMMANDS`), aliases (`nee`→`new`), reply text builders (`help_text`, `new_text`, status/context lines). |
| `nostr_hermes_proxy/control.py` | Loopback HTTP API: auth (bearer, `hmac.compare_digest`), `POST /v1/dm`, `GET /v1/outbox/status`, `GET /v1/health`. |
| `nostr_hermes_proxy/outbox.py` | Persisted idempotency ledger (24h) + per-personality queues, rate cap, retry/backoff, `status_snapshot()`. |
| `nostr_hermes_proxy/send_cli.py` | `nostr-send` CLI (thin client over the control plane). |
| `nostr_hermes_proxy/sessions.py` | Persisted session map (atomic tmp+replace writes). |
| `nostr_hermes_proxy/watchdog.py` | Polls SDK health + gateway; triggers soft-reconnect. |
| `tests/` | `fakes.py` (harness + fakes), per-area test files, `test_secrets.py` (literal-key guard). |
| `~/Documents/nostrbot-sdk/` | Sibling repo (v0.9.0): `NostrBot` — relay lifecycle, profile/10002/10050 publishing, gift-wrap subscription (limit:0 + 49h since guard), `make_gift_wrap_async` (ordered timestamps), `stop_run`-friendly client wrapping. |

## Recipe: add a user-facing command

1. `commands.py`: add the name to `COMMANDS` (keep the tuple ordered as the
   help listing), add reply-text constants/builders there (pure functions —
   easy to test).
2. `personality.py`: dispatch in `_handle_command` (the `if name == …` chain)
   and implement `_command_<name>`. Commands run *before* the turn lock —
   they must never block on it (`/stop` is the reference implementation).
3. `commands.py::help_text()`: list the command.
4. Tests: mirror `tests/test_stop.py` / the command tests in
   `tests/test_personality.py`. Reply text is asserted by unwrapping the
   outgoing gift wrap with the owner key (see testing conventions below).
5. Docs: DESIGN.md command references + this file's neighbors in the same
   commit.

## Recipe: add a config knob

1. `config.py`: field on `Config` (or `PersonalityConfig`), parse + validate
   in `parse_config`/`_parse_personality` — raise `ConfigError` with
   `f"{source}: <knob> must be …"` wording; validate cross-references (see
   `default_personality` for the pattern).
2. `config.example.toml`: annotated entry.
3. Wire it where it's consumed (`__main__.py` constructs the runtime/
   control-plane with explicit kwargs).
4. Tests in `tests/test_config.py` (default, custom, invalid).
5. DESIGN.md row/mention.

## Recipe: change delivery behavior

All outbound DMs flow through `PersonalityRuntime.deliver_dm(recipient_hex,
text, expiry_seconds=None, protocol="nip17") -> int` — acks, turn replies
(`_send_chunks` is a thin best-effort wrapper), and the outbox worker all
use it. Invariants to preserve (tests assert them):

- **Chunk ordering**: `truncate_message` splits (>4096 bytes → chunks with
  `(1/N)` markers); each chunk gets a random backdate within
  `_outbound_spread_seconds`, **sorted ascending** so chunk 1 carries the
  newest `created_at` (clients show reading order; wraps still look
  individually random to relays).
- **Expiry clamping**: requests clamp into `[MIN_OUTBOUND_EXPIRY_SECONDS,
  _outbound_expiry_max_seconds]`; the NIP-40 tag is stamped inside the rumor
  *and* on the gift wrap.
- **Protocol split**: `nip17` = manual rumor + `make_gift_wrap_async`
  (ordered timestamps); `nip04` fallback = legacy encrypted kind 4 (real
  timestamps).
- **Never raise into the turn path**: `_send_chunks` swallows and logs;
  `deliver_dm` raises only for the outbox worker to retry.

## Recipe: decide SDK vs proxy

Put it in **nostrbot-sdk** if it's a protocol primitive or reusable bot
infrastructure (crypto helpers, publishing policy, subscription filters,
watchdog hooks). Keep it in **this repo** if it's policy/orchestration
(allowlist, sessions, Hermes specifics, cron wiring). SDK changes ship as:

1. Code + tests in `~/Documents/nostrbot-sdk` (suite green).
2. Version bump (`pyproject.toml`, `__init__.py`, `tests/test_version.py`) —
   new public API → minor bump; behavior change → minor bump at least (see
   v0.9.0's republish-policy change).
3. README.md "Versions" entry, commit `Info <info@unsaltedbutter.ai>`, tag
   `vX.Y.Z`, push both.
4. This repo: pin bump + reinstall
   `venv/bin/pip install --no-cache-dir --force-reinstall --no-deps "nostrbot-sdk @ git+…@vX.Y.Z"`
   (never without `--no-deps` — `nostr-sdk` isn't on PyPI; never without
   `--no-cache-dir` — stale tag wheels).

## Testing conventions

- `tests/fakes.py::make_harness()` builds a full runtime with fakes: FakeBot
  (records `sent` decryptable DMs + raw `sent_events`), FakeHermes
  (scriptable results/submissions/models/steer/stop), FakeNostrClient.
- Owner-decryptable assertions: by default the harness generates a real owner
  keypair (`harness.owner`); outgoing gift wraps the runtime publishes are
  decrypted into `bot.sent` so tests assert plaintext naturally. Pass
  `decrypt_keys=[owner]` when constructing manually.
- Activity markers: `"send"` per publish, `"submit"`/`"steer"` per gateway
  call — used to assert ordering (e.g. ack before turn).
- No test touches the network or real relays; crypto happens for real (SDK
  primitives) — that's intentional, it exercises the actual NIP-44/NIP-59
  path.
- Run `venv/bin/python -m pytest -q` (the whole suite) before committing;
  the count is currently ~262 and only grows.

## Security invariants to preserve

- One allowlisted sender per personality (`allow_npub`); everyone else is
  **silently dropped** — no acks/errors to strangers.
- Outbound recipient is the configured owner, never caller-supplied.
- Control plane binds `127.0.0.1` only; bearer token required
  (`hmac.compare_digest`); token file mode 0600.
- Secrets only via `${VAR}` interpolation from the 0600 env file;
  `tests/test_secrets.py` fails the suite on literal bech32 keys in tracked
  files.
- NIP-59 freshness: every message encrypts under a fresh ephemeral key
  (known-plaintext is harmless — see DESIGN.md ack notes); never "optimize"
  keys/nonces into reuse.
- The ack pattern is metadata-visible by accepted trade-off; don't add
  content that makes acks leak more.

## Git / docs conventions

- Identity `Info <info@unsaltedbutter.ai>`; remote host alias
  `git@github.unsaltedbutter`; short imperative messages; **no trailers**.
- Same-commit doc updates: DESIGN.md (why), README.md (what's new at a
  glance), and the relevant file under `docs/` (operations/troubleshooting
  impact).
- Push to `origin main` after a green suite.
