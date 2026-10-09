"""Paths and secrets. Secrets come only from the git-ignored .env and are never printed."""

import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "mailmate.db"


class ConfigError(RuntimeError):
    """.env is missing a value. The message names the key, never its value."""


def read_env_file(path: Path = PROJECT_ROOT / ".env") -> dict[str, str]:
    """KEY=VALUE lines from .env. Values are NOT put into os.environ, so secrets only reach the
    code that asks for them. Tolerates a BOM and Windows line endings (Notepad saves both)."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class N8nSettings:
    base_url: str
    token: str = field(repr=False)   # never shows up in logs, tracebacks or st.write


def n8n_settings(env: dict[str, str] | None = None) -> N8nSettings:
    env = read_env_file() if env is None else env
    base_url = env.get("N8N_BASE_URL", "").rstrip("/")
    token = env.get("N8N_WEBHOOK_TOKEN", "")
    if not base_url or "<name>" in base_url:
        raise ConfigError("N8N_BASE_URL is not set in .env (e.g. https://<name>.app.n8n.cloud)")
    if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ConfigError("N8N_BASE_URL must start with https:// (plain http only for a local n8n)")
    if not token:
        raise ConfigError("N8N_WEBHOOK_TOKEN is not set in .env")
    return N8nSettings(base_url=base_url, token=token)


RESUME_PATH = PROJECT_ROOT / "data" / "resume.pdf"


def test_address(env: dict[str, str] | None = None) -> str:
    """Rule 2: my own secondary inbox. Test batches may only go here (W2 checks it again)."""
    env = read_env_file() if env is None else env
    address = env.get("MAILMATE_TEST_ADDRESS", "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", address):
        raise ConfigError("MAILMATE_TEST_ADDRESS is not set in .env (your own secondary inbox for test emails)")
    return address
