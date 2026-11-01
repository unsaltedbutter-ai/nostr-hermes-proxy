"""``python -m nostr_hermes_proxy`` entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config
from .control import ControlPlane, ControlTokenError, ensure_control_token
from .hermes import HermesAuthError, HermesClient
from .outbox import Outbox
from .personality import PersonalityRuntime
from .sessions import SessionMap
from .watchdog import Watchdog

logger = logging.getLogger("nostr_hermes_proxy")

DEFAULT_CONFIG_PATH = "~/.config/nostr-hermes-proxy/config.toml"

#: Seconds between personality starts. Relay per-IP rate limits silently
#: reject publish bursts; staggering keeps each identity's metadata intact.
STARTUP_STAGGER_SECONDS = 6.0

#: Read-back delay before verifying the published DM relay lists.
METADATA_VERIFY_DELAY_SECONDS = 5.0


async def _verify_and_repair_dm_relay_lists(
    runtimes: list[PersonalityRuntime],
) -> None:
    """Verify each identity's kind-10050 landed; republish any that didn't.

    Relays can silently drop publishes under per-IP rate limits, and a
    missing DM relay list makes senders strand the identity (NIP-17 senders
    read the recipient's kind-10050 to choose delivery relays). Best-effort:
    any probe failure only logs.
    """
    import datetime

    from nostr_sdk import (
        ClientBuilder,
        Filter,
        Keys,
        Kind,
        PublicKey,
        RelayUrl,
        ReqTarget,
    )

    await asyncio.sleep(METADATA_VERIFY_DELAY_SECONDS)
    relay = "wss://nos.lol"
    try:
        client = ClientBuilder().build()
        await client.add_relay(RelayUrl.parse(relay))
        await client.connect()
        try:
            for runtime in runtimes:
                author = PublicKey.parse(
                    Keys.parse(runtime.config.nsec).public_key().to_hex(),
                )
                deadline = asyncio.get_event_loop().time() + 6.0
                found = False
                while True:
                    events = await client.fetch_events(
                        ReqTarget.auto([
                            Filter().author(author).kind(Kind(10050)),
                        ]),
                        datetime.timedelta(seconds=6),
                    )
                    if events:
                        found = True
                        break
                    if asyncio.get_event_loop().time() > deadline:
                        break
                    await asyncio.sleep(1.0)
                if found:
                    logger.info(
                        "DM relay list verified on %s for %s",
                        relay, runtime.name,
                    )
                    continue
                logger.warning(
                    "DM relay list for %s missing on %s — republishing",
                    runtime.name, relay,
                )
                await runtime.bot.republish_metadata()
                await asyncio.sleep(3.0)
        finally:
            await client.disconnect()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "DM relay list verification failed (non-fatal)",
            exc_info=True,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nostr_hermes_proxy",
        description="Proxy NIP-17 Nostr DMs to a local Hermes gateway.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "NOSTR_HERMES_PROXY_CONFIG", DEFAULT_CONFIG_PATH,
        ),
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("NOSTR_HERMES_PROXY_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_args(argv)


def _build_runtime(config: Config) -> tuple[list[PersonalityRuntime], HermesClient]:
    sessions = SessionMap(config.state_dir / "sessions.json")
    hermes = HermesClient(config.hermes_url, sessions)
    runtimes = [
        PersonalityRuntime(
            personality,
            allow_hex=config.allow_hex,
            hermes=hermes,
            sessions=sessions,
            ack_text=config.ack_text,
            ack_ttl_seconds=config.ack_ttl_seconds,
            gateway_config_path=config.hermes_config_path,
            watchdog_stall_seconds=config.watchdog_stall_seconds,
            outbound_expiry_max_seconds=config.outbound_expiry_max_seconds,
            outbound_spread_seconds=config.outbound_spread_seconds,
        )
        for personality in config.personalities
    ]
    return runtimes, hermes


async def run(config: Config) -> int:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    runtimes, hermes = _build_runtime(config)
    runtimes_by_name = {runtime.name: runtime for runtime in runtimes}
    outbox = Outbox(
        config.state_dir / "outbox.json",
        rate_limit_per_minute=config.outbound_rate_limit_per_minute,
    )

    async def deliver(
        personality: str,
        text: str,
        expiry_seconds: int | None,
    ) -> None:
        runtime = runtimes_by_name[personality]
        await runtime.deliver_dm(
            runtime.allow_hex, text, expiry_seconds, protocol="nip17",
        )

    control: ControlPlane | None = None
    watchdog = Watchdog(
        runtimes,
        interval_seconds=config.watchdog_interval_seconds,
        failure_threshold=config.watchdog_failure_threshold,
        stall_seconds=config.watchdog_stall_seconds,
    )
    for runtime in runtimes:
        runtime.set_watchdog_status_provider(
            lambda name=runtime.name: watchdog.state_for(name)
        )

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass

    logger.info(
        "Starting %d personality(ies) against %s",
        len(runtimes), config.hermes_url,
    )
    try:
        # Stagger the starts: each personality publishes kind 0/10002/10050
        # on connect, and relays enforce per-IP rate limits that silently
        # reject bursts (Notible's DM relay list was lost this way on
        # 2026-11-01 — nos.lol accepted three identities' metadata and
        # dropped the fourth without any error surfacing).
        for index, runtime in enumerate(runtimes):
            if index:
                await asyncio.sleep(STARTUP_STAGGER_SECONDS)
            try:
                await runtime.start()
            except Exception as exc:
                logger.error(
                    "Personality %s failed to start: %s",
                    runtime.name, type(exc).__name__,
                )
                logger.debug("Start failure detail", exc_info=exc)
        verify_task = asyncio.create_task(
            _verify_and_repair_dm_relay_lists(runtimes),
            name="verify_dm_relay_lists",
        )
        if config.control_port:
            token, created = ensure_control_token(config.control_token_path)
            token_path = Path(config.control_token_path).expanduser()
            if created:
                logger.info(
                    "Generated control token at %s (mode 0600)", token_path,
                )
            else:
                logger.info("Using control token at %s", token_path)
            control = ControlPlane(
                config=config,
                outbox=outbox,
                runtimes=runtimes_by_name,
                token=token,
            )
            await control.start()
        else:
            logger.info("Control plane disabled (control_port = 0)")
        outbox.start_worker(deliver)
        watchdog.start()
        await shutdown.wait()
    finally:
        logger.info("Shutting down")
        verify_task.cancel()
        if control is not None:
            await control.stop()
        await outbox.stop_worker()
        await watchdog.stop()
        await asyncio.gather(
            *(runtime.stop() for runtime in runtimes),
            return_exceptions=True,
        )
        await hermes.close()
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(config))
    except (ControlTokenError, OSError) as exc:
        print(f"control plane error: {exc}", file=sys.stderr)
        return 2
    except HermesAuthError as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
