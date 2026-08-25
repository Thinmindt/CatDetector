"""Config reads .env, but a real environment variable must win."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def restore_config() -> Iterator[None]:
    """Reloading config runs load_dotenv(), which leaks into os.environ.

    monkeypatch cannot undo that: delenv(raising=False) on an absent variable
    records nothing, so the value load_dotenv() then sets outlives the test.
    """
    before = os.environ.get("NETWORK_SHARE_DIR")
    yield
    if before is None:
        os.environ.pop("NETWORK_SHARE_DIR", None)
    else:
        os.environ["NETWORK_SHARE_DIR"] = before
    import config

    importlib.reload(config)


def test_env_var_overrides_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env sets NETWORK_SHARE_DIR=/mnt/nas; load_dotenv must not override it.

    This is what lets a test run point recordings somewhere other than the NAS.
    """
    monkeypatch.setenv("NETWORK_SHARE_DIR", "/tmp/somewhere-else")
    import config

    importlib.reload(config)
    assert config.Config.NETWORK_SHARE_DIR == "/tmp/somewhere-else"


def test_dotenv_is_actually_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserting on the value cannot prove this: .env holds the same string as
    config.py's hardcoded default, so both tests pass with load_dotenv removed.
    Assert the call instead."""
    calls: list[bool] = []

    def spy(*args: object, **kwargs: object) -> bool:
        calls.append(True)
        return True

    monkeypatch.setattr("dotenv.load_dotenv", spy)
    import config

    importlib.reload(config)
    assert calls


def test_falls_back_when_the_environment_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NETWORK_SHARE_DIR", raising=False)
    import config

    importlib.reload(config)
    assert config.Config.NETWORK_SHARE_DIR
