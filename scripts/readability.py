"""Prose measurements for Markdown documents: what a reader meets, in numbers.

Usage: python3 readability.py SUMMARY.md [...] [--detail EVIDENCE.md ...]

For each file: words, sentences, the average sentence and the long ones, the
paragraphs over budget with their line numbers, headings and pictures. Fenced
blocks and table rows are not prose and are left out. A list item is a paragraph
of its own; a task item (`- [ ]`), which carries a spec and its done-means, has a
budget of its own. The budgets bind the summary tiers; files after --detail are
evidence, measured for information and marked so. A measuring tool, not a gate:
it always exits 0. A project copies it unchanged and imports measure() in a test
to make the budgets a gate.
"""

import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

SENTENCE_BUDGET = 22
LONG_SENTENCE = 40
PARAGRAPH_BUDGET = 150
TASK_BUDGET = 250
SHORTEST_SENTENCE = 3

LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
TASK_ITEM = re.compile(r"^\s*[-*+]\s\[[ xX]\]")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*_(\[\"'])")


@dataclass
class Prose:
    """A document's prose blocks, each with its first line, and what is not prose."""

    blocks: list[tuple[int, str]] = field(default_factory=list)
    headings: int = 0
    pictures: int = 0
    current: list[str] = field(default_factory=list)
    start: int = 0
    fenced: bool = False

    def add(self, number: int, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("```"):
            self.fence(stripped)
        elif not self.fenced:
            self.text(number, line, stripped)

    def fence(self, stripped: str) -> None:
        if not self.fenced and stripped.startswith("```mermaid"):
            self.pictures += 1
        self.fenced = not self.fenced
        self.close()

    def text(self, number: int, line: str, stripped: str) -> None:
        self.pictures += line.count("![")
        if not stripped or stripped.startswith(("|", "#")):
            self.headings += stripped.startswith("#")
            self.close()
            return
        if LIST_ITEM.match(line) or not self.current:
            self.close()
            self.start = number
        self.current.append(stripped.lstrip("> "))

    def close(self) -> None:
        if self.current:
            self.blocks.append((self.start, " ".join(self.current)))
            self.current = []


@dataclass(frozen=True)
class Measure:
    """What one document measures."""

    words: int
    sentences: list[int]
    over: list[tuple[int, str]]
    headings: int
    pictures: int

    @property
    def average(self) -> float:
        return statistics.mean(self.sentences) if self.sentences else 0.0

    @property
    def long(self) -> int:
        return sum(length > LONG_SENTENCE for length in self.sentences)


def budget(block: str) -> int:
    return TASK_BUDGET if TASK_ITEM.match(block) else PARAGRAPH_BUDGET


def measure(text: str) -> Measure:
    """The prose measurements of one Markdown document."""
    prose = Prose()
    for number, line in enumerate(text.splitlines(), 1):
        prose.add(number, line)
    prose.close()
    sentences = [
        len(sentence.split())
        for _, block in prose.blocks
        for sentence in SENTENCE_END.split(block)
        if len(sentence.split()) >= SHORTEST_SENTENCE
    ]
    return Measure(
        words=sum(len(block.split()) for _, block in prose.blocks),
        sentences=sentences,
        over=[(n, b) for n, b in prose.blocks if len(b.split()) > budget(b)],
        headings=prose.headings,
        pictures=prose.pictures,
    )


def report(path: Path, detail: bool = False) -> list[str]:
    found = measure(path.read_text())
    counts = (
        f"{path}: {found.words:,} words, {len(found.sentences)} sentences,"
        f" {found.average:.1f} words a sentence"
    )
    if detail:
        return [
            (
                f"{counts}, {found.long} over {LONG_SENTENCE}; {len(found.over)}"
                " paragraphs over the summary budget; evidence tier, budgets advisory"
            )
        ]
    lines = [
        (
            f"{counts} (budget {SENTENCE_BUDGET}), {found.long} over {LONG_SENTENCE};"
            f" {len(found.over)} paragraphs over budget; {found.headings} headings,"
            f" {found.pictures} pictures"
        )
    ]
    lines += [f"  line {n}: {len(b.split())} words: {b[:70]}" for n, b in found.over]
    return lines


def main(args: list[str]) -> int:
    detail = False
    for arg in args:
        if arg == "--detail":
            detail = True
        else:
            print("\n".join(report(Path(arg), detail)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
