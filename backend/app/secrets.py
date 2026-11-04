"""Secret resolution from environment variables or secret files."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr

from app.logging_conf import get_logger

log = get_logger("secrets")


def read_secret(
    env_value: str | SecretStr | None,
    file_path: str | None,
    name: str,
    *,
    app_env: str | None = None,
) -> SecretStr | None:
    """Resolve a secret from an environment value first, falling back to a file path."""
    raw_env = env_value.get_secret_value() if isinstance(env_value, SecretStr) else env_value
    if raw_env is not None and raw_env.strip():
        return SecretStr(raw_env.strip())

    if file_path is None or not file_path.strip():
        return None

    display_path = file_path.strip()
    path = Path(display_path).expanduser()
    try:
        st = path.stat()
        raw_file = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise RuntimeError(f"Failed to read {name} file: {display_path}") from None

    effective_env = app_env if app_env is not None else os.environ.get("APP_ENV", "dev")
    if effective_env == "prod" and (st.st_mode & 0o077):
        log.warning(
            "secret_file_permissions_too_open",
            name=name,
            file_path=display_path,
            mode=oct(st.st_mode & 0o777),
        )

    cleaned = raw_file.strip()
    if not cleaned:
        raise RuntimeError(f"{name} file is empty: {display_path}")
    if "\n" in cleaned or "\r" in cleaned:
        raise RuntimeError(f"{name} file must contain a single line: {display_path}")

    return SecretStr(cleaned)


__all__ = ["read_secret"]
