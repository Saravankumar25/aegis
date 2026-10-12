"""Create a private Docker-secret file from the root .env Firebase value."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
SECRET_DIR = ROOT / ".secrets"
SECRET_FILE = SECRET_DIR / "firebase-service-account.json"


def _env_value(text: str, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip().strip("\"'") if match else ""


def _restrict_path(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return

    user = subprocess.run(  # noqa: S603 - fixed Windows system executable
        [str(Path(os.environ["WINDIR"]) / "System32" / "whoami.exe")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    permissions = "(OI)(CI)F" if directory else "F"
    subprocess.run(  # noqa: S603 - fixed system executable; arguments are not shell-expanded
        [
            str(Path(os.environ["WINDIR"]) / "System32" / "icacls.exe"),
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{user}:{permissions}",
            f"NT AUTHORITY\\SYSTEM:{permissions}",
            f"BUILTIN\\Administrators:{permissions}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def materialize() -> None:
    if not ENV_FILE.is_file():
        raise SystemExit("Root .env is missing; copy .env.example and configure it.")

    _restrict_path(ENV_FILE, directory=False)
    env_text = ENV_FILE.read_text(encoding="utf-8-sig")
    encoded = _env_value(env_text, "FIREBASE_SERVICE_ACCOUNT_JSON_BASE64")
    project = _env_value(env_text, "FIREBASE_PROJECT_ID")
    if not encoded or not project:
        raise SystemExit(
            "FIREBASE_SERVICE_ACCOUNT_JSON_BASE64 and FIREBASE_PROJECT_ID "
            "must be set in root .env."
        )

    try:
        account_text = base64.b64decode(encoded, validate=True).decode("utf-8")
        account = json.loads(account_text)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("Firebase service-account value in .env is invalid.") from exc

    if (
        not isinstance(account, dict)
        or account.get("project_id") != project
        or not account.get("private_key")
        or not account.get("client_email")
    ):
        raise SystemExit(
            "Firebase service-account project or required fields do not match .env."
        )

    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _restrict_path(SECRET_DIR, directory=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SECRET_DIR,
            prefix=".firebase-service-account-",
            delete=False,
        ) as temporary:
            temporary.write(account_text)
            temporary_path = Path(temporary.name)

        _restrict_path(temporary_path, directory=False)
        os.replace(temporary_path, SECRET_FILE)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


if __name__ == "__main__":
    materialize()
