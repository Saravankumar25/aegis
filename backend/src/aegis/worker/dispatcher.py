"""CLI process that drains the durable workflow dispatch outbox."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import time

from aegis.container import build_container
from aegis.core.config import get_settings
from aegis.core.logging import configure_logging, get_logger
from aegis.persistence.dispatch_outbox import DispatchOutbox
from aegis.persistence.migrate import run_migrations
from aegis.worker.dispatch import WorkflowDispatcher, build_dispatch_adapter

log = get_logger(__name__)


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    container = build_container(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)

    await container.connect()
    try:
        await run_migrations(container.db)
        dispatcher = WorkflowDispatcher(
            DispatchOutbox(container.db), build_dispatch_adapter(settings)
        )
        deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
        while not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                log.info("workflow dispatcher reached time limit")
                return
            count = await dispatcher.dispatch_batch(args.dispatcher_id, limit=args.max_items)
            if args.once:
                return
            if count == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=args.poll_interval)
    finally:
        await container.aclose()
        log.info("workflow dispatcher stopped", dispatcher_id=args.dispatcher_id)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Aegis workflow outbox dispatcher")
    result.add_argument("--once", action="store_true", help="drain one bounded batch and exit")
    result.add_argument("--max-items", type=int, default=25)
    result.add_argument("--max-seconds", type=float, help="stop dispatching after this time budget")
    result.add_argument("--poll-interval", type=float, default=2.0)
    result.add_argument(
        "--dispatcher-id", default=os.getenv("HOSTNAME", f"dispatcher-{os.getpid()}")
    )
    return result


def main() -> None:
    args = parser().parse_args()
    args.max_items = max(1, min(args.max_items, 100))
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise SystemExit("--max-seconds must be greater than 0")
    args.poll_interval = max(0.1, min(args.poll_interval, 60.0))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
