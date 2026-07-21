"""Firebase ID-token verification and allowlist role resolution (ESD §8).

This module is the *only* place a Google identity enters Aegis. Its job is narrow and
deliberately so: turn an opaque client-supplied string into either a verified identity or
nothing at all. It never issues a session — that remains ``api.security``'s httpOnly-cookie
path, so the browser's durable credential is still one JavaScript cannot read (CLAUDE.md §12).

Two properties matter more than anything else here:

* **Nothing the client says is believed.** The email, uid and name are read from claims that
  Google signed, never from the request body. A caller cannot assert who they are.
* **It fails closed.** Every failure mode — malformed token, wrong project, expired, revoked,
  unverified email, Firebase unreachable — returns ``None``. There is no branch that returns
  an identity on an error path, so a verification outage denies logins rather than admitting
  unauthenticated ones.

``firebase_admin``'s verification is synchronous and may perform a network fetch for Google's
rotating signing certificates, so it is dispatched to a worker thread: holding the event loop
during someone else's TLS handshake would stall every concurrent incident (CLAUDE.md §3).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import anyio
import firebase_admin
from firebase_admin import auth as firebase_auth_sdk
from firebase_admin import credentials

from core.config import get_settings
from core.logging import get_logger
from db.enums import UserRole

_log = get_logger(component="firebase_auth")

# firebase_admin keeps a process-global app registry that is not itself initialisation-safe
# under concurrency; two simultaneous first requests would otherwise race to initialize_app.
_init_lock = threading.Lock()
_app: firebase_admin.App | None = None


class FirebaseConfigError(RuntimeError):
    """Firebase is not configured or its credential file is unusable.

    Raised at initialisation, never during verification: a misconfigured deployment should
    fail loudly at the first login attempt rather than quietly rejecting valid users in a way
    that looks like a credential problem on their end.
    """


@dataclass(frozen=True, slots=True)
class FirebaseIdentity:
    """A verified Google identity. Every field originates from a Google-signed claim."""

    uid: str
    email: str
    email_verified: bool
    display_name: str | None
    photo_url: str | None


def _get_app() -> firebase_admin.App:
    """Initialise (once) and return the Firebase Admin app."""
    global _app
    if _app is not None:
        return _app
    with _init_lock:
        if _app is not None:  # another thread won the race while we waited
            return _app
        settings = get_settings()
        if not settings.firebase_project_id:
            raise FirebaseConfigError("FIREBASE_PROJECT_ID is not set")
        key_path = Path(settings.firebase_service_account_file)
        if not key_path.is_absolute():
            # Settings paths are relative to the backend/ working directory, matching the
            # existing k8s token-file convention.
            key_path = (Path(__file__).resolve().parent.parent / key_path).resolve()
        if not key_path.is_file():
            raise FirebaseConfigError(
                f"Firebase service-account file not found at {key_path}. "
                "Generate one in Firebase console → Project settings → Service accounts."
            )
        # The path is logged; the key contents never are (CLAUDE.md §12).
        _log.info("firebase_admin_init", project_id=settings.firebase_project_id)
        _app = firebase_admin.initialize_app(
            credentials.Certificate(str(key_path)),
            {"projectId": settings.firebase_project_id},
        )
        return _app


def _verify_blocking(id_token: str) -> FirebaseIdentity | None:
    """Verify an ID token. Runs in a worker thread; returns None on any rejection."""
    app = _get_app()
    try:
        # check_revoked forces a lookup against the user record so that a session disabled
        # or revoked in the Firebase console stops working immediately, rather than
        # surviving until the token's own expiry.
        claims = firebase_auth_sdk.verify_id_token(id_token, app=app, check_revoked=True)
    except (
        firebase_auth_sdk.ExpiredIdTokenError,
        firebase_auth_sdk.RevokedIdTokenError,
        firebase_auth_sdk.InvalidIdTokenError,
        firebase_auth_sdk.UserDisabledError,
        firebase_auth_sdk.CertificateFetchError,
    ) as exc:
        # Deliberately coarse at the caller: the client learns only "rejected". The reason is
        # recorded here for operators. The token itself is never logged.
        _log.warning("firebase_token_rejected", reason=type(exc).__name__)
        return None

    email = claims.get("email")
    if not email:
        # A provider that yields no email cannot be matched against the role allowlist, so
        # it could never be authorized for anything. Reject rather than admit as a viewer.
        _log.warning("firebase_token_rejected", reason="no_email_claim", uid=claims.get("uid"))
        return None
    if not claims.get("email_verified", False):
        # Google always verifies; another provider on this shared project might not, and an
        # unverified address would let someone claim an allowlisted email they do not own.
        _log.warning("firebase_token_rejected", reason="email_not_verified", uid=claims.get("uid"))
        return None

    return FirebaseIdentity(
        uid=claims["uid"],
        email=email.strip().lower(),
        email_verified=True,
        display_name=claims.get("name"),
        photo_url=claims.get("picture"),
    )


async def verify_google_id_token(id_token: str) -> FirebaseIdentity | None:
    """Verify a Firebase ID token off the event loop. ``None`` means rejected, always."""
    if not id_token or not id_token.strip():
        return None
    return await anyio.to_thread.run_sync(_verify_blocking, id_token)


def resolve_role(email: str) -> UserRole:
    """Map a verified email to an RBAC role from the operator-controlled allowlists.

    Fails closed: an email in neither list becomes ``viewer``, which cannot approve a
    remediation. This is the single control that makes signing in with *any* Google account
    acceptable — authentication is open, authorization is not.
    """
    settings = get_settings()
    normalized = email.strip().lower()
    if normalized in settings.admin_email_set:
        return UserRole.admin
    if normalized in settings.approver_email_set:
        return UserRole.on_call_engineer
    return UserRole.viewer
