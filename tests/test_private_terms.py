"""private_terms.sh: each URL shape the README names yields its secret, not a word."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "private_terms.sh"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts")
    return tmp_path


def terms(repo: Path, env: str | None) -> list[str]:
    """The terms as check_private.sh reads them: trimmed, blank lines dropped."""
    if env is not None:
        (repo / ".env").write_text(env)
    result = subprocess.run(  # noqa: S603 -- fixed argv
        ["/bin/bash", str(repo / "scripts" / "private_terms.sh")],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_no_env_means_no_terms(repo: Path) -> None:
    assert terms(repo, None) == []


def test_cat_names_are_terms(repo: Path) -> None:
    assert terms(repo, 'CAT_NAMES="Ada,Bea"\n') == ["Ada", "Bea"]


def test_uuid_url_yields_the_url_and_its_uuid(repo: Path) -> None:
    uuid = "5b1e8a3c-0000-4000-8000-00000000abcd"
    got = terms(repo, f"CAPTURE_HEARTBEAT_URL=https://hc-ping.com/{uuid}\n")
    assert got == [f"https://hc-ping.com/{uuid}", uuid]


def test_ping_key_url_yields_the_key_not_the_slug(repo: Path) -> None:
    key = "Xk3pQ9rT2vLm8nB4wY6zAa"
    got = terms(repo, f"CAPTURE_HEARTBEAT_URL=https://hc-ping.com/{key}/camera-feed\n")
    assert key in got
    assert "camera-feed" not in got


def test_push_url_yields_its_id_without_the_query(repo: Path) -> None:
    push_id = "Ab12Cd34Ef"
    url = f"https://kuma.example.org/api/push/{push_id}"
    got = terms(repo, f"CAPTURE_HEARTBEAT_URL='{url}?status=up&msg=OK&ping='\n")
    assert got == [url, push_id]


def test_a_quoted_url_with_a_comment_is_cut_at_the_comment(repo: Path) -> None:
    uuid = "5b1e8a3c-0000-4000-8000-00000000abcd"
    got = terms(repo, f'CAPTURE_HEARTBEAT_URL="https://hc-ping.com/{uuid}"  # camera\n')
    assert got == [f"https://hc-ping.com/{uuid}", uuid]
