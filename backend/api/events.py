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

    async def start(self) -> None:
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        self._conn = await asyncpg.connect(dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)
        self._log.info("event_hub_listening", channel=CHANNEL)

    async def stop(self) -> None:
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
            # Non-blocking put: a slow SSE consumer drops events rather than backing up the hub.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

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
