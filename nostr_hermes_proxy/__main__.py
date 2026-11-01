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
from .hermes import HermesAuthError, HermesClient
from .personality import PersonalityRuntime
from .sessions import SessionMap
from .watchdog import Watchdog

logger = logging.getLogger("nostr_hermes_proxy")

DEFAULT_CONFIG_PATH = "~/.config/nostr-hermes-proxy/config.toml"


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
        )
        for personality in config.personalities
    ]
    return runtimes, hermes


async def run(config: Config) -> int:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    runtimes, hermes = _build_runtime(config)
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
        starts = [
            asyncio.create_task(runtime.start(), name=f"start_{runtime.name}")
            for runtime in runtimes
        ]
        results = await asyncio.gather(*starts, return_exceptions=True)
        for runtime, result in zip(runtimes, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Personality %s failed to start: %s",
                    runtime.name, type(result).__name__,
                )
                logger.debug("Start failure detail", exc_info=result)
        watchdog.start()
        await shutdown.wait()
    finally:
        logger.info("Shutting down")
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
    except HermesAuthError as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
