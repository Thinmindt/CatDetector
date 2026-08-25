"""Config reads .env, but a real environment variable must win."""

from __future__ import annotations

import importlib

import pytest


def test_env_var_overrides_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env sets NETWORK_SHARE_DIR=/mnt/nas; load_dotenv must not override the env.

    This is what lets a test run point recordings somewhere other than the NAS.
    """
    monkeypatch.setenv("NETWORK_SHARE_DIR", "/tmp/somewhere-else")
    import config

    importlib.reload(config)
    assert config.Config.NETWORK_SHARE_DIR == "/tmp/somewhere-else"


def test_falls_back_to_dotenv_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NETWORK_SHARE_DIR", raising=False)
    import config

    importlib.reload(config)
    # Either the .env value or the hardcoded default, but never empty.
    assert config.Config.NETWORK_SHARE_DIR
