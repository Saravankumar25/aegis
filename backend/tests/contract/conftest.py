"""Shared helpers for MCP server contract tests (ESD §22: fixtures, no live upstream)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture() -> Callable[[str], object]:
    """Load a JSON fixture by relative path, e.g. ``k8s/pod_list.json``."""

    def _load(relative: str) -> object:
        return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def mock_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient]:
    """Build an ``httpx.AsyncClient`` whose transport is the given handler function."""

    def _build(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://fixture.test", transport=httpx.MockTransport(handler)
        )

    return _build
