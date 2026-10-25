"""The README's install commands, as a clean machine with no Raspberry Pi runs them.

Usage: python3 scripts/readme_install.py README.md > install.sh

Every command in the README's Install section up to its Settings step, in order,
less only what needs a Pi (the camera package) and the clone a CI checkout has
already made. CI runs the result on a clean Debian, so a README that has drifted
from a working install fails there.
"""

import sys
from pathlib import Path

PI_ONLY_PACKAGES = ("python3-picamera2",)
ALREADY_DONE = ("git clone ", "cd CatDetector")


def install_section(readme: str) -> str:
    """The Install section, up to the step that writes settings."""
    section = readme.split("\n## Install\n", 1)[1].split("\n## ", 1)[0]
    return section.split("**Settings.**", 1)[0]


def fenced_lines(text: str) -> list[str]:
    """The lines inside the text's fenced blocks, without their indent."""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        elif fenced:
            lines.append(line.strip())
    return lines


def without_the_pi(command: str) -> str:
    return " ".join(word for word in command.split() if word not in PI_ONLY_PACKAGES)


def install_commands(readme: str) -> list[str]:
    """The README's install commands, in order, as a machine with no Pi runs them."""
    return [
        without_the_pi(line)
        for line in fenced_lines(install_section(readme))
        if line and not line.startswith(("#", *ALREADY_DONE))
    ]


def main(args: list[str]) -> int:
    print("\n".join(install_commands(Path(args[0]).read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
