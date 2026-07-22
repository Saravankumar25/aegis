"""Regression test: the rate-limit bucket keys on the route TEMPLATE, not the concrete path.

Found during the pre-launch security review. The middleware read ``request.scope["route"]``,
but ``@app.middleware("http")`` runs *outside* the router, so that key does not exist yet and
the lookup always returned ``None``. The bucket therefore fell back to the concrete path,
giving every distinct URL its own budget — so a caller varying the id in a parameterised route
was never limited. Measured against the running instance before the fix: 200 requests to
``/api/v1/incidents/<fresh uuid>`` produced **zero** 429s, while 140 requests to a single
fixed path produced 20.

The consequence is not merely load: the limiter is the only brake on unauthenticated
brute-forcing of the ingest webhook token and on scraping incident ids.
"""

from __future__ import annotations

import uuid

from starlette.requests import Request

from api.main import create_app, route_template


def _request(app, method: str, path: str) -> Request:
    """A Request carrying only what routing needs — no server, no I/O."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("1.2.3.4", 1234),
            "app": app,
        }
    )


def test_parameterised_paths_collapse_to_one_bucket() -> None:
    app = create_app()
    templates = {
        route_template(_request(app, "GET", f"/api/v1/incidents/{uuid.uuid4()}")) for _ in range(25)
    }
    assert len(templates) == 1, f"distinct ids produced distinct buckets: {templates}"
    assert "{incident_id}" in next(iter(templates))


def test_distinct_routes_keep_distinct_buckets() -> None:
    """Collapsing must not go too far: one route's traffic must not exhaust another's."""
    app = create_app()
    incident_id = uuid.uuid4()
    detail = route_template(_request(app, "GET", f"/api/v1/incidents/{incident_id}"))
    replay = route_template(_request(app, "GET", f"/api/v1/incidents/{incident_id}/replay"))
    listing = route_template(_request(app, "GET", "/api/v1/incidents"))
    assert len({detail, replay, listing}) == 3


def test_unmatched_paths_still_collapse_by_identifier_shape() -> None:
    """Otherwise spraying random 404 paths is itself a way around the limit.

    Nothing in the routing table matches these, so this exercises the framework-independent
    fallback rather than the route lookup.
    """
    app = create_app()
    numeric = {route_template(_request(app, "GET", f"/no/such/route/{i}")) for i in range(25)}
    assert len(numeric) == 1

    uuids = {
        route_template(_request(app, "GET", f"/no/such/route/{uuid.uuid4()}")) for _ in range(25)
    }
    assert len(uuids) == 1


def test_route_scope_key_is_not_read_from_an_unrouted_scope() -> None:
    """Guards the specific defect: ``scope['route']`` is absent this early in the stack.

    If a future refactor reintroduces ``request.scope.get("route")`` here it will silently
    return None again and the limiter will quietly stop working, which is exactly how this
    shipped. Asserting the key's absence documents *why* the helper exists.
    """
    app = create_app()
    request = _request(app, "GET", f"/api/v1/incidents/{uuid.uuid4()}")
    assert "route" not in request.scope
    assert route_template(request) != request.url.path
