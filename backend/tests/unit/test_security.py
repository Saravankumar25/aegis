"""Authentication configuration tests that need no Firebase service."""

from __future__ import annotations

import base64
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import SecretStr

from aegis.api.security import FirebaseVerifier
from aegis.core.config import Settings


def _fake_firebase(monkeypatch):
    certificate = Mock(side_effect=lambda value: value)
    firebase_admin = SimpleNamespace(
        _apps=[],
        credentials=SimpleNamespace(Certificate=certificate),
        get_app=Mock(),
        initialize_app=Mock(return_value=object()),
    )
    monkeypatch.setitem(sys.modules, "firebase_admin", firebase_admin)
    return firebase_admin, certificate


def test_firebase_verifier_uses_base64_service_account_from_settings(monkeypatch):
    firebase_admin, certificate = _fake_firebase(monkeypatch)
    account = {"type": "service_account", "project_id": "test-project"}
    encoded = base64.b64encode(json.dumps(account).encode()).decode()
    settings = Settings(
        _env_file=None,
        firebase_project_id="test-project",
        firebase_service_account_json_base64=SecretStr(encoded),
    )

    verifier = FirebaseVerifier(settings)

    assert verifier.initialise() is True
    certificate.assert_called_once_with(account)
    firebase_admin.initialize_app.assert_called_once_with(
        account, {"projectId": "test-project"}
    )


def test_firebase_verifier_keeps_support_for_mounted_service_account(monkeypatch):
    firebase_admin, certificate = _fake_firebase(monkeypatch)
    settings = Settings(
        _env_file=None,
        firebase_project_id="test-project",
        firebase_service_account_path="/run/secrets/firebase.json",
    )

    verifier = FirebaseVerifier(settings)

    assert verifier.initialise() is True
    certificate.assert_called_once_with("/run/secrets/firebase.json")
    firebase_admin.initialize_app.assert_called_once()


def test_invalid_base64_service_account_fails_closed(monkeypatch):
    firebase_admin, certificate = _fake_firebase(monkeypatch)
    settings = Settings(
        _env_file=None,
        firebase_project_id="test-project",
        firebase_service_account_json_base64=SecretStr("not-base64"),
        firebase_service_account_path="/run/secrets/firebase.json",
    )

    verifier = FirebaseVerifier(settings)

    assert verifier.initialise() is False
    certificate.assert_not_called()
    firebase_admin.initialize_app.assert_not_called()
