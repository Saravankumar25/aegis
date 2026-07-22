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

# (label, text containing a credential, the substring that must not survive)
BARE_SECRETS = [
    (
        "google_api_key",
        "gateway rejected key AIzaSyD1234567890abcdefghijklmnopqrstuv on lookup",
        "AIzaSyD1234567890abcdefghijklmnopqrstuv",
    ),
    (
        "github_pat",
        "git fetch failed using ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ),
    (
        "openrouter_key",
        "upstream 401 for sk-or-v1-abcdef0123456789abcdef0123456789",
        "sk-or-v1-abcdef0123456789abcdef0123456789",
    ),
    (
        "slack_token",
        "notifier configured with xoxb-1234567890-abcdefghijkl",
        "xoxb-1234567890-abcdefghijkl",
    ),
    (
        "langsmith_key",
        "tracing disabled, key lsv2_pt_abcdef0123456789abcdef0123456789 rejected",
        "lsv2_pt_abcdef0123456789abcdef0123456789",
    ),
    (
        "slack_webhook",
        "POST https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX 404",
        "hooks.slack.com/services/T00000000/B00000000",
    ),
    (
        "jwt_serviceaccount_token",
        "k8s api 401: eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJlX2hlcmU",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJlX2hlcmU",
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
    text = (
        "sidecar crashed reading\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxLotsOfBase64Here\n"
        "-----END RSA PRIVATE KEY-----\n"
        "and exited 1"
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
