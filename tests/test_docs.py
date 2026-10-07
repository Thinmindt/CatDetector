"""What the documents promise, checked: links resolve, every setting, option and key
is in the README, prose spells words one way, and the summaries stay readable."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.readability import PARAGRAPH_BUDGET, SENTENCE_BUDGET, measure
from scripts.readme_install import install_commands

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
TRACKED = [
    README,
    ROOT / "CLAUDE.md",
    *(DOCS / name for name in ("DESIGN.md", "ROADMAP.md", "ROADMAP_ARCHIVE.md")),
    DOCS / "STYLE.md",
    *sorted((DOCS / "design").glob("*.md")),
]
# The archive is history, moved verbatim, so it keeps the spelling it had.
CURRENT = [doc for doc in TRACKED if doc.name != "ROADMAP_ARCHIVE.md"]

FENCED = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#+ (.+)$", re.MULTILINE)

# Prose spells these the British way; identifiers keep their own spelling.
AMERICAN = ("labeled", "labeling", "color", "colors", "behavior", "recognize")

# The summary tiers: (most words a sentence on average, paragraphs over budget).
# The roadmap's are its numbers today, until roadmap C.8 tiers it; lower, never raise.
SUMMARIES = {
    "README.md": (SENTENCE_BUDGET, 0),
    "DESIGN.md": (SENTENCE_BUDGET, 0),
    "CLAUDE.md": (SENTENCE_BUDGET, 0),
    "ROADMAP.md": (25.7, 1),
}


def name(doc: Path) -> str:
    return str(doc.relative_to(ROOT))


def without_code(text: str) -> str:
    return CODE_SPAN.sub("", FENCED.sub("", text))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(doc: Path) -> set[str]:
    seen: dict[str, int] = {}
    for heading in HEADING.findall(FENCED.sub("", doc.read_text())):
        base = slug(heading)
        seen[base] = seen.get(base, -1) + 1
        if seen[base]:
            seen[f"{base}-{seen[base]}"] = 0
    return set(seen)


def broken_links(doc: Path) -> list[str]:
    broken = []
    for target in LINK.findall(without_code(doc.read_text())):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        linked = (doc.parent / path).resolve() if path else doc
        if not linked.exists() or (anchor and anchor not in anchors(linked)):
            broken.append(target)
    return broken


@pytest.mark.parametrize("doc", TRACKED, ids=name)
def test_every_relative_link_and_anchor_resolves(doc: Path) -> None:
    assert broken_links(doc) == []


def test_every_setting_is_in_the_readme_table_and_nothing_else() -> None:
    read = set(re.findall(r'os\.getenv\("(\w+)"', (ROOT / "config.py").read_text()))
    table = README.read_text().split("\n## Settings\n", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", table, re.MULTILINE))
    assert read
    assert listed == read


def test_every_command_line_option_is_in_the_readme() -> None:
    main = (ROOT / "main.py").read_text()
    options = re.findall(r'add_argument\(\s*"(--[\w-]+)"', main)
    readme = README.read_text()
    assert options
    assert [o for o in options if not re.search(rf"{o}(?![\w-])", readme)] == []


def test_every_review_key_is_in_the_readme() -> None:
    script = (ROOT / "src" / "static" / "review.js").read_text()
    keys = re.findall(r"e\.key === '(\w+)'", script)
    shown = {"ArrowRight": "→", "Escape": "Esc"}
    readme = README.read_text()
    assert keys
    assert [k for k in keys if f"`{shown.get(k, k)}`" not in readme] == []
    assert "Number(e.key)" in script
    assert "`1`\u2013`9`" in readme  # an en dash, as the README writes it


@pytest.mark.parametrize("doc", CURRENT, ids=name)
def test_prose_spells_words_the_british_way(doc: Path) -> None:
    pattern = rf"\b({'|'.join(AMERICAN)})\b"
    assert re.findall(pattern, without_code(doc.read_text()), re.IGNORECASE) == []


@pytest.mark.parametrize("doc", [d for d in TRACKED if d.name in SUMMARIES], ids=name)
def test_summary_tiers_stay_within_their_budgets(doc: Path) -> None:
    most_words, paragraphs_over = SUMMARIES[doc.name]
    found = measure(doc.read_text())
    assert found.average <= most_words
    long_paragraphs = [(line, len(text.split())) for line, text in found.over]
    assert len(long_paragraphs) <= paragraphs_over, (
        f"paragraphs over {PARAGRAPH_BUDGET} words"
    )


def test_the_readme_install_runs_without_a_pi_as_written() -> None:
    """What CI's clean-machine job runs: the README's own commands, in order."""
    commands = install_commands(README.read_text())
    apt = "sudo apt install libcap-dev python3-dev ffmpeg"
    assert commands[:2] == ["sudo apt update", apt]
    assert "uv venv --system-site-packages" in commands
    assert commands[-1] == "uv sync"
    uv_on_path = next(i for i, c in enumerate(commands) if "/.local/bin/env" in c)
    assert uv_on_path < commands.index("uv venv --system-site-packages")
    pi_only = [c for c in commands if "picamera2" in c or c.startswith(("git ", "cd "))]
    assert pi_only == []
