"""Guards that must hold from day one: localhost only, secrets and personal data never committed."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_streamlit_serves_localhost_only():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["address"] == "localhost"
    assert config["client"]["toolbarMode"] == "minimal"     # no Deploy button


def test_secrets_and_personal_data_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/" in ignored
    assert ".env" in ignored


def test_env_example_has_no_secret_values():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("N8N_WEBHOOK_TOKEN="):
            assert line == "N8N_WEBHOOK_TOKEN="


def test_tests_cannot_reach_the_network():
    """Rule 9: the autouse guard in conftest.py blocks real connections."""
    import httpx
    import pytest
    with pytest.raises(RuntimeError, match="must not use the network"):
        httpx.get("https://example.com")
