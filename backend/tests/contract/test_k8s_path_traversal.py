"""Contract tests: the k8s client refuses to build a traversing request path (ESD §16).

Found in the pre-launch security review and rated a deployment blocker. Every k8s request
path is an f-string interpolating a namespace and object name — and those arguments are
chosen by the Correlation agent, which chooses them by reading pod logs and Kubernetes
events. That is attacker-influenceable text. `httpx` normalises dot-segments before the
request leaves the process, so a pod name of
``../../../../../api/v1/namespaces/kube-system/secrets`` resolves to exactly that path: an
injected log line aiming a read-only ServiceAccount at cluster secrets.

RBAC denies it today, and that is verified separately. But the tool layer contributed no
defence of its own, which left the whole protection resting on a single binding staying
correct forever. These tests pin the tool layer's half.

The guard is an **allowlist of the legal RFC 1123 shape**, not a denylist of `../`.
A denylist has to anticipate percent-encoding, double-encoding, unicode lookalikes,
backslashes and absolute paths; an allowlist of `[a-z0-9-]` has none of those edges.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_servers.k8s.client import K8sClient, UnsafeResourceName

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def recording_client() -> tuple[K8sClient, list[str]]:
    """A client whose transport records every path it is asked to fetch.

    Recording rather than asserting-on-exception alone: the property that matters is that
    the dangerous request is never *issued*, which a raised exception implies but does not
    prove on its own.
    """
    requested: list[str] = []

    # Minimal but structurally valid bodies. The point of these tests is which path gets
    # requested, so the payloads only need to survive parsing.
    pod_body = {
        "metadata": {"name": "p", "namespace": "meridian"},
        "status": {"phase": "Running", "containerStatuses": []},
        "spec": {"containers": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if "/pods/" in request.url.path:
            return httpx.Response(200, json=pod_body)
        return httpx.Response(200, json={"items": []})

    http = httpx.AsyncClient(base_url="https://k8s.test", transport=httpx.MockTransport(handler))
    return K8sClient(http=http), requested


# The exact payload from the security review, plus the encodings a denylist would miss.
TRAVERSAL_NAMES = [
    "../../../../../api/v1/namespaces/kube-system/secrets",
    "..%2F..%2Fsecrets",
    "....//....//secrets",
    "/api/v1/namespaces/kube-system/secrets",
    "pod/../../../secrets",
    "..",
    ".",
    "pod%00",
    "pod name with spaces",
    "UPPERCASE-pod",  # k8s names are lowercase; anything else is not a real object
    "pod\\..\\secrets",
    "a" * 254,  # longer than any legal k8s name
    "",
]


@pytest.mark.parametrize("evil", TRAVERSAL_NAMES)
async def test_get_pod_refuses_illegal_names(recording_client, evil):
    client, requested = recording_client
    with pytest.raises(UnsafeResourceName):
        await client.get_pod(evil, "meridian")
    assert requested == [], f"a request was issued for {evil!r}: {requested}"


@pytest.mark.parametrize("evil", TRAVERSAL_NAMES)
async def test_get_pod_logs_refuses_illegal_names(recording_client, evil):
    """The highest-value target: pod logs return raw text straight into an agent prompt."""
    client, requested = recording_client
    with pytest.raises(UnsafeResourceName):
        await client.get_pod_logs(evil, "meridian")
    assert requested == []


@pytest.mark.parametrize("evil", TRAVERSAL_NAMES)
async def test_namespace_scoping_cannot_be_escaped(recording_client, evil):
    """Namespace is the scoping control; escaping it defeats least-privilege entirely."""
    client, requested = recording_client
    with pytest.raises(UnsafeResourceName):
        await client.list_pods(evil)
    assert requested == []


async def test_write_paths_are_guarded_too(recording_client):
    """Writes matter more, not less — these mutate cluster state."""
    client, requested = recording_client
    with pytest.raises(UnsafeResourceName):
        await client.restart_pod("../../../../etc/passwd", "meridian")
    with pytest.raises(UnsafeResourceName):
        await client.scale_deployment("../../kube-system/deployments/coredns", 3, "meridian")
    assert requested == []


async def test_events_and_deployments_are_guarded(recording_client):
    client, requested = recording_client
    for call in (client.list_events, client.list_deployments):
        with pytest.raises(UnsafeResourceName):
            await call("../../../secrets")
    assert requested == []


# --- the guard must not break legitimate use ----------------------------------------------


@pytest.mark.parametrize(
    "legal",
    [
        "checkout-service-5fb95cc87f-k2rjt",  # a real pod name from this cluster
        "meridian",
        "kube-system",
        "a",
        "0",
        "svc-123-abc",
        "a" * 253,  # exactly at the limit
    ],
)
async def test_legal_kubernetes_names_are_accepted(recording_client, legal):
    """A guard that blocks real pod names would break every investigation."""
    client, requested = recording_client
    await client.get_pod(legal, "meridian")
    assert requested == [f"/api/v1/namespaces/meridian/pods/{legal}"]


async def test_refusal_names_the_offending_value_without_echoing_it_whole(recording_client):
    """The error must be diagnosable but must not paste an unbounded attacker string into
    logs that a human will read."""
    client, _ = recording_client
    with pytest.raises(UnsafeResourceName) as exc:
        await client.get_pod("x" * 5000, "meridian")
    assert len(str(exc.value)) < 200
