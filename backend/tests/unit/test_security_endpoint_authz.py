"""Structural authz tests: no state-changing route may reach a handler unauthenticated.

Written during the pre-launch security review. Reading the routers and confirming each
decorator carries ``require_role`` proves today's code is correct and proves nothing about
tomorrow's — the realistic regression is a new endpoint added without the guard, which no
existing test would notice. These walk the live routing table instead, so a route added
without auth fails the build rather than shipping.

The ingestion webhook is the one deliberate exception: it is a machine-to-machine path
authenticated by a shared secret in middleware, not by a session cookie.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from api.deps import get_current_user
from api.main import _flatten_routes, create_app
from api.security import ACCESS_COOKIE, clear_auth_cookies, set_auth_cookies

STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that legitimately run without a session dependency. Enumerated rather than pattern-
# matched, so adding one is a deliberate edit to this set and shows up in review.
#
#   /incidents (POST)  — machine-to-machine ingestion, authenticated by shared secret in the
#                        ``webhook_guard`` middleware before routing ever happens.
#   /auth/session      — the route that *establishes* a session; its credential is the
#                        Firebase ID token in the body, verified against Google's keys.
#   /auth/refresh      — authenticates by presenting the refresh cookie, which the handler
#                        validates against ``refresh_sessions`` (rotation + reuse detection).
#   /auth/logout       — same, and must stay callable with an already-invalid session so a
#                        user can always clear cookies.
UNAUTHENTICATED_BY_DESIGN = {
    ("POST", "/api/v1/incidents"),
    ("POST", "/api/v1/auth/session"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
}


def _api_routes() -> list[tuple[str, str, APIRoute]]:
    triples = []
    for prefix, route in _flatten_routes(create_app().routes):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            triples.append((method, prefix + route.path, route))
    return triples


def _auth_dependency_names(route: APIRoute) -> set[str]:
    """Every dependency callable in the route's tree, by qualified name."""
    names: set[str] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__qualname__", getattr(call, "__name__", "")))
        stack.extend(dependant.dependencies)
    return names


def _is_guarded(route: APIRoute) -> bool:
    names = _auth_dependency_names(route)
    return get_current_user.__qualname__ in names or any(
        n.startswith("require_role") for n in names
    )


def test_every_state_changing_route_requires_authentication() -> None:
    unguarded = [
        f"{method} {path}"
        for method, path, route in _api_routes()
        if method in STATE_CHANGING
        and (method, path) not in UNAUTHENTICATED_BY_DESIGN
        and not _is_guarded(route)
    ]
    assert not unguarded, f"state-changing routes with no auth dependency: {unguarded}"


def test_the_unauthenticated_exemptions_still_exist() -> None:
    """A stale exemption is how a guard silently stops covering anything.

    If one of these routes is renamed or removed, the entry must go too — otherwise the set
    quietly grants a pass to a path that no longer means what it did when it was added.
    """
    live = {(m, p) for m, p, _ in _api_routes()}
    assert UNAUTHENTICATED_BY_DESIGN <= live, (
        f"exemptions for routes that no longer exist: {UNAUTHENTICATED_BY_DESIGN - live}"
    )


def test_every_read_route_requires_authentication() -> None:
    """Incident data is operational intelligence about production; none of it is public."""
    public = {"/health", "/api/v1/health", "/metrics", "/api/v1/metrics"}
    unguarded = [
        f"{method} {path}"
        for method, path, route in _api_routes()
        if method == "GET" and path not in public and not _is_guarded(route)
    ]
    assert not unguarded, f"GET routes with no auth dependency: {unguarded}"


def test_metrics_is_authenticated_even_though_convention_says_otherwise() -> None:
    """`/metrics` reports incident counts and spend; a scraper gets credentials instead."""
    for method, path, route in _api_routes():
        if method == "GET" and path in {"/metrics", "/api/v1/metrics"}:
            assert _is_guarded(route), f"{path} is unauthenticated"


def test_privileged_routes_demand_a_role_not_merely_a_session() -> None:
    """A viewer must not be able to cause an infrastructure action (CLAUDE.md §12).

    ``get_current_user`` alone is not sufficient for these: it authenticates but does not
    authorize, so a route carrying only it would let any signed-in Google account through.
    """
    privileged = {
        "/api/v1/kill-switch",
        "/api/v1/circuit-breaker/clear",
        "/api/v1/incidents/{incident_id}/approvals",
        "/api/v1/incidents/{incident_id}/resolve",
        "/api/v1/memory/{summary_id}/approve",
    }
    seen = set()
    for method, path, route in _api_routes():
        if method in STATE_CHANGING and path in privileged:
            seen.add(path)
            names = _auth_dependency_names(route)
            assert any(n.startswith("require_role") for n in names), (
                f"{method} {path} authenticates but does not check a role"
            )
    assert seen == privileged, f"privileged routes missing from the table: {privileged - seen}"


class _Recorder:
    """Minimal Response stand-in that records cookie kwargs."""

    def __init__(self) -> None:
        self.cookies: dict[str, dict] = {}

    def set_cookie(self, key, value, **kwargs):
        self.cookies[key] = kwargs

    def delete_cookie(self, key, **kwargs):
        self.cookies.pop(key, None)


def test_session_cookies_are_httponly_and_samesite_strict() -> None:
    """CLAUDE.md §12: no session token anywhere JavaScript can read.

    SameSite=Strict is also what carries CSRF defence for every state-changing endpoint —
    there is no separate CSRF token — so weakening it to Lax silently removes that protection
    and this test is the thing that should stop it.
    """
    recorder = _Recorder()
    set_auth_cookies(recorder, access_token="a.b.c", refresh_token="d.e.f")

    assert set(recorder.cookies) == {"aegis_access", "aegis_refresh"}
    for name, kwargs in recorder.cookies.items():
        assert kwargs["httponly"] is True, f"{name} is readable by JavaScript"
        assert kwargs["samesite"] == "strict", f"{name} does not defend against CSRF"

    # The refresh cookie is scoped to the auth routes so it is not attached to ordinary API
    # traffic, limiting where the longer-lived credential can be observed.
    assert recorder.cookies["aegis_refresh"]["path"] == "/api/v1/auth"


def test_logout_clears_both_cookies() -> None:
    recorder = _Recorder()
    set_auth_cookies(recorder, access_token="a.b.c", refresh_token="d.e.f")
    clear_auth_cookies(recorder)
    assert recorder.cookies == {}
    assert ACCESS_COOKIE == "aegis_access"
