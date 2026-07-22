"""Live incident event fan-out: Postgres LISTEN/NOTIFY → per-incident SSE subscribers.

The worker (and the API itself, on ingestion) publishes small JSON events via ``pg_notify``
on one channel; this hub holds a single dedicated LISTEN connection and fans events out to
in-process subscriber queues keyed by incident id. SSE endpoints consume a queue each —
push-based end to end, no polling loop anywhere (CLAUDE.md §11). Postgres remains the only
coordination mechanism, so any number of API processes can run this hub concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.logging import get_logger

CHANNEL = "incident_events"
# Reconnect backoff bounds for the LISTEN connection. Capped rather than unbounded so a
# Postgres restart is picked up within a few seconds once it comes back, not minutes later.
_RECONNECT_MIN_DELAY = 0.5
_RECONNECT_MAX_DELAY = 5.0


async def publish_event(
    session: AsyncSession, incident_id: uuid.UUID, event: str, data: dict[str, Any] | None = None
) -> None:
    """Publish an incident event through Postgres NOTIFY (fires on commit)."""
    payload = json.dumps({"incident_id": str(incident_id), "event": event, "data": data or {}})
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)").bindparams(channel=CHANNEL, payload=payload)
    )


class EventHub:
    """One LISTEN connection, many per-incident subscriber queues."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)
        self._conn: asyncpg.Connection | None = None
        self._log = get_logger(component="event_hub")
        self._supervisor: asyncio.Task[None] | None = None
        self._closing = False
        # Counts events dropped because a subscriber's queue was full. Exposed so a slow
        # consumer is a measurable condition rather than an invisible one.
        self.dropped_events = 0

    async def start(self) -> None:
        self._closing = False
        await self._connect()
        self._supervisor = asyncio.create_task(self._supervise())

    async def _connect(self) -> None:
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        await conn.add_listener(CHANNEL, self._on_notify)
        self._conn = conn
        self._log.info("event_hub_listening", channel=CHANNEL)

    def is_connected(self) -> bool:
        """True while the LISTEN connection is actually usable.

        Exposed so a health check can distinguish a live event pipeline from a dead one.
        A dead hub is otherwise indistinguishable from a quiet system: every SSE stream
        stays open sending keepalives and the dashboard simply never updates again.
        """
        return self._conn is not None and not self._conn.is_closed()

    async def _supervise(self) -> None:
        """Re-establish the LISTEN connection whenever it dies.

        Without this, a Postgres restart, a failover, or an idle-connection reaper silently
        ended live updates for the lifetime of the API process — asyncpg does not reconnect
        on its own, and nothing here noticed the connection was gone.
        """
        delay = _RECONNECT_MIN_DELAY
        while not self._closing:
            await asyncio.sleep(delay if not self.is_connected() else _RECONNECT_MIN_DELAY)
            if self._closing:
                return
            if self.is_connected():
                delay = _RECONNECT_MIN_DELAY
                continue
            try:
                await self._connect()
                self._log.warning("event_hub_reconnected", channel=CHANNEL)
                delay = _RECONNECT_MIN_DELAY
            except (OSError, asyncpg.PostgresError) as exc:
                self._log.warning("event_hub_reconnect_failed", error=str(exc))
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def stop(self) -> None:
        self._closing = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.remove_listener(CHANNEL, self._on_notify)
                await self._conn.close()
            self._conn = None

    def _on_notify(
        self, connection: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            self._log.warning("event_hub_bad_payload")
            return
        incident_id = event.get("incident_id", "")
        for queue in self._subscribers.get(incident_id, set()) | self._subscribers.get("*", set()):
            # Non-blocking put: a slow SSE consumer drops events rather than backing up the
            # hub, because one stalled browser tab must not stop every other subscriber (and
            # the worker) from making progress.
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._note_gap(queue, incident_id)

    def _note_gap(self, queue: asyncio.Queue[dict], incident_id: str) -> None:
        """Tell a lagging subscriber it lost events instead of dropping them silently.

        Silently discarding leaves the client showing a stale incident it believes is
        current — the failure mode is a human reading a resolved incident as still open.
        The oldest queued event is evicted to make room for a marker the client can act on
        by re-fetching, so the gap becomes visible rather than merely absent.
        """
        self.dropped_events += 1
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(
                {
                    "incident_id": incident_id,
                    "event": "stream_gap",
                    "data": {"reason": "subscriber too slow; refetch to resynchronise"},
                }
            )
        self._log.warning("event_hub_subscriber_lagging", incident_id=incident_id)

    def subscribe(self, incident_id: str) -> asyncio.Queue[dict]:
        """Subscribe to one incident's events ('*' for all incidents)."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._subscribers[incident_id].add(queue)
        return queue

    def unsubscribe(self, incident_id: str, queue: asyncio.Queue[dict]) -> None:
        self._subscribers[incident_id].discard(queue)
        if not self._subscribers[incident_id]:
            self._subscribers.pop(incident_id, None)


hub = EventHub()
