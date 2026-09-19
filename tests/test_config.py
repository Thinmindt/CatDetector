"""Config reads .env, but a real environment variable must win."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from types import ModuleType

import pytest

DETECTION_SETTINGS = (
    "MOTION_THRESHOLD",
    "DARK_BRIGHTNESS",
    "MOTION_TIMEOUT",
    "MOG2_HISTORY",
)

# Foreground pixels of a cat walking into the full-sensor view; see DESIGN.md.
WALKING_CAT_PX = 400

# Mean grey level of the brightest frame in the unlit room at night, and of the
# dimmest frame with the room lit; see DESIGN.md.
BRIGHTEST_NIGHT_FRAME = 2.7
DIMMEST_LIT_FRAME = 46.1


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


def reload_with_default_detection(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reload config with no detection settings in the environment or .env."""

    def skip_dotenv(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr("dotenv.load_dotenv", skip_dotenv)
    for name in DETECTION_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    import config

    return importlib.reload(config)


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
    """With neither the environment nor .env naming the share, the default holds."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("NETWORK_SHARE_DIR", raising=False)
    import config

    importlib.reload(config)
    assert config.Config.NETWORK_SHARE_DIR == "/mnt/nas"


def test_detection_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOTION_THRESHOLD", "200")
    monkeypatch.setenv("DARK_BRIGHTNESS", "12.5")
    monkeypatch.setenv("MOTION_TIMEOUT", "30.5")
    monkeypatch.setenv("MOG2_HISTORY", "1500")
    import config

    importlib.reload(config)
    assert config.Config.MOTION_THRESHOLD == 200
    assert config.Config.DARK_BRIGHTNESS == 12.5
    assert config.Config.MOTION_TIMEOUT == 30.5
    assert config.Config.MOG2_HISTORY == 1500


def test_default_threshold_is_below_a_walking_cat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that forgets MOTION_THRESHOLD must still record a visit."""
    config = reload_with_default_detection(monkeypatch)
    assert config.Config.MOTION_THRESHOLD < WALKING_CAT_PX


def test_default_dark_cutoff_separates_night_from_a_lit_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = reload_with_default_detection(monkeypatch)
    assert BRIGHTEST_NIGHT_FRAME < config.Config.DARK_BRIGHTNESS < DIMMEST_LIT_FRAME


def test_timeout_and_history_keep_their_old_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = reload_with_default_detection(monkeypatch)
    assert config.Config.MOTION_TIMEOUT == 10
    assert config.Config.MOG2_HISTORY == 500
