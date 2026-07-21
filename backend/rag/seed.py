"""Ingest the seed runbook corpus from eval/runbooks/ (FR-3.3).

Idempotent: unchanged files (same content hash) are no-ops. Run:  python -m rag.seed
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.db import session_scope
from core.logging import configure_logging, get_logger
from db.enums import RunbookSource
from rag.store import upsert_runbook

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "eval" / "runbooks"

# filename -> service tags (which services this runbook is most relevant to)
_TAGS = {
    "oom-crashloop.md": ["checkout-service", "payment-service", "catalog-service"],
    "error-rate-after-deploy.md": ["checkout-service", "payment-service", "catalog-service"],
    "latency-degradation.md": ["checkout-service", "catalog-service"],
    "pod-unavailable.md": ["checkout-service", "payment-service", "catalog-service"],
}


async def seed_runbooks() -> int:
    log = get_logger(component="rag_seed")
    count = 0
    async with session_scope() as session:
        for path in sorted(RUNBOOK_DIR.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            title = content.splitlines()[0].lstrip("# ").strip()
            _, changed = await upsert_runbook(
                session,
                title=title,
                content=content,
                source=RunbookSource.internal,
                service_tags=_TAGS.get(path.name, []),
            )
            count += 1
            log.info("runbook_seeded", title=title, changed=changed)
    return count


if __name__ == "__main__":
    configure_logging()
    asyncio.run(seed_runbooks())
