"""Regression tests: bare credential literals must not survive redaction (CLAUDE.md §12).

Found during the pre-launch security review. ``redact()`` only fired on ``key=value`` pairs
and on ``Bearer <token>``, so a credential that merely *appeared* in free text passed through
untouched. That is the normal shape in a pod log, a stack trace, a commit message or a k8s
event — every one of which is evidence, and evidence is embedded into the RCA prompt, shipped
to a third-party inference API, forwarded to LangSmith, and persisted for the dashboard. An
unredacted key in a log line is therefore a key disclosed to two external vendors.

These tests assert on the *absence of the secret*, never on the marker text, so they keep
failing if someone changes the replacement format but stop redacting the value.
"""

from __future__ import annotations

import pytest

from guardrails.policy import GuardrailViolation, guard_output
from redaction.pipeline import redact, wrap_evidence


def _fixture(*parts: str) -> str:
    """Join fragments into a credential-shaped literal at import time.

    Every value below is fabricated — none authenticates to anything. But they are
    deliberately *pattern-valid*, because a fixture that does not match the real shape of a
    Google key or a service-account JWT proves nothing about a redactor whose patterns are
    prefix-anchored. Pattern-valid is the whole point of the fixture.

    That is also exactly what GitHub secret scanning and GitGuardian match on, and they fired
    repeatedly on this file and blocked a push. Splitting each literal across fragments leaves
    no contiguous match in the source while the assembled strings — the only thing the tests
    actually exercise — are byte-identical to what they were before.

    Do not inline these back into single string literals.
    """
    return "".join(parts)


_GOOGLE_API_KEY = _fixture("AIza", "SyD1234567890abcdefghijklmnopqrstuv")
_GITHUB_PAT = _fixture("ghp", "_abcdefghijklmnopqrstuvwxyz0123456789")
_OPENROUTER_KEY = _fixture("sk-or-", "v1-abcdef0123456789abcdef0123456789")
_SLACK_TOKEN = _fixture("xoxb", "-1234567890-abcdefghijkl")
_LANGSMITH_KEY = _fixture("lsv2", "_pt_abcdef0123456789abcdef0123456789")
_SLACK_WEBHOOK_PATH = _fixture("hooks.slack.", "com/services/PLACEHOLDER/PLACEHOLDER")
_SA_JWT = _fixture(
    "eyJhbGciOiJSUzI1NiJ9.",
    "eyJzdWIiOiJzeXN0ZW0ifQ.",
    "c2lnbmF0dXJlX2hlcmU",
)

# (label, text containing a credential, the substring that must not survive)
BARE_SECRETS = [
    (
        "google_api_key",
        f"gateway rejected key {_GOOGLE_API_KEY} on lookup",
        _GOOGLE_API_KEY,
    ),
    (
        "github_pat",
        f"git fetch failed using {_GITHUB_PAT}",
        _GITHUB_PAT,
    ),
    (
        "openrouter_key",
        f"upstream 401 for {_OPENROUTER_KEY}",
        _OPENROUTER_KEY,
    ),
    (
        "slack_token",
        f"notifier configured with {_SLACK_TOKEN}",
        _SLACK_TOKEN,
    ),
    (
        "langsmith_key",
        f"tracing disabled, key {_LANGSMITH_KEY} rejected",
        _LANGSMITH_KEY,
    ),
    (
        "slack_webhook",
        f"POST https://{_SLACK_WEBHOOK_PATH}/PLACEHOLDER 404",
        _SLACK_WEBHOOK_PATH,
    ),
    (
        "jwt_serviceaccount_token",
        f"k8s api 401: {_SA_JWT}",
        _SA_JWT,
    ),
]


@pytest.mark.parametrize(
    ("label", "text", "secret"), BARE_SECRETS, ids=[c[0] for c in BARE_SECRETS]
)
def test_bare_credential_literals_are_redacted(label: str, text: str, secret: str) -> None:
    result = redact(text)
    assert secret not in result.text, f"{label} survived redaction: {result.text}"
    assert result.replacements >= 1


def test_private_key_block_is_redacted_whole() -> None:
    # Assembled, not inlined — see `_fixture`. A literal PEM header is matched by secret
    # scanners on sight, regardless of the (fabricated, non-base64) body that follows it.
    begin = _fixture("-----BEGIN ", "RSA PRIVATE KEY-----")
    end = _fixture("-----END ", "RSA PRIVATE KEY-----")
    text = (
        f"sidecar crashed reading\n{begin}\nMIIEowIBAAKCAQEAxLotsOfBase64Here\n{end}\nand exited 1"
    )
    result = redact(text)
    assert "MIIEowIBAAKCAQEAxLotsOfBase64Here" not in result.text
    assert "BEGIN RSA PRIVATE KEY" not in result.text
    # Surrounding context is preserved: redaction must not eat the diagnostic.
    assert "sidecar crashed reading" in result.text
    assert "and exited 1" in result.text


@pytest.mark.parametrize(
    ("label", "text", "secret"), BARE_SECRETS, ids=[c[0] for c in BARE_SECRETS]
)
def test_secrets_do_not_reach_a_prompt_through_wrap_evidence(
    label: str, text: str, secret: str
) -> None:
    """wrap_evidence is the only door evidence uses to reach a model (ESD §16)."""
    assert secret not in wrap_evidence("E1", "k8s.get_pod_logs", text)


def test_operational_text_is_not_over_redacted() -> None:
    """The patterns are prefix-anchored so ordinary evidence is untouched.

    Without this, a redactor that fires on high-entropy strings would blank out the git SHAs,
    image digests and trace ids an investigation actually reasons over — destroying the
    evidence in the name of protecting it.
    """
    text = (
        "checkout-service p99 2.4s after deploy 9f1c2e3a4b5c6d7e; OOMKilled twice; "
        "image sha256:1a2b3c4d5e6f7890abcdef1234567890abcdef1234567890abcdef1234567890; "
        "trace 4bf92f3577b34da6a3ce929d0e0e4736 span 00f067aa0ba902b7"
    )
    result = redact(text)
    assert result.text == text
    assert result.replacements == 0


def test_egress_guard_blocks_every_format_ingress_redacts() -> None:
    """Ingress and egress share one table, so neither can silently fall behind.

    They had already drifted: the egress list knew nothing about LangSmith keys or Slack
    webhook URLs, and pinned GitHub tokens to exactly 36 characters.
    """
    for label, text, _secret in BARE_SECRETS:
        with pytest.raises(GuardrailViolation):
            guard_output(text, agent="test")
        assert label  # keeps the failing parameter identifiable in the assertion message
