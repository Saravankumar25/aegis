"""The kill switch: durable, immediate halt of ALL autonomous action (FR-5.3).

State lives in the ``system_flags`` table so it survives every process restart and is
shared by every worker. Checked immediately before any execution — engaged means nothing
executes anywhere, including actions already approved. Engaging is allowed for
on_call_engineer/admin (route-enforced); disengaging is admin-only.
"""

from __future__ import annotations

import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from db.models import SystemFlag

KILL_SWITCH_KEY = "kill_switch"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def set_kill_switch(session: AsyncSession, *, engaged: bool, actor: str) -> dict:
    """Engage or disengage; upsert so the very first engage works with no seed row."""
    value = {"engaged": engaged, "by": actor, "at": _now().isoformat()}
    stmt = (
        pg_insert(SystemFlag)
        .values(key=KILL_SWITCH_KEY, value=value, updated_at=_now())
        .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": _now()})
    )
    await session.execute(stmt)
    log = get_logger(component="kill_switch")
    if engaged:
        log.error("kill_switch_ENGAGED", by=actor)
    else:
        log.warning("kill_switch_disengaged", by=actor)
    return value


async def is_kill_switch_engaged(session: AsyncSession) -> bool:
    flag = await session.get(SystemFlag, KILL_SWITCH_KEY)
    return bool(flag and flag.value.get("engaged"))
