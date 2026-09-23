"""Screenshot the review page at phone and desktop sizes, with no camera.

Run by hand -- `uv run python tests/manual/check_page.py [--filter multi]`. It
serves the app from a copy of the review database on a spare port, shoots it
with headless chromium, and writes test_images/page-<size>.png. It does not
touch the camera or the live database, so it runs beside the service. Named
check_*.py so pytest never collects it.

This one keeps print() rather than logging: it is a standalone script whose
output is the point, not a component of the running application.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import Config
from src.review.api import Review
from src.review.capture_db import CaptureDB
from src.review.clip_frames import ClipFrames
from src.web_app import create_app

PORT = 5055
SIZES = {"phone": "412,915", "desk": "1280,900"}  # a Pixel 9 portrait, and a laptop
OUTPUT_DIR = Path("test_images")
START_TIMEOUT = 30
RENDER_BUDGET_MS = 15000


def serve(db_copy: Path) -> None:
    db = CaptureDB(
        db_copy,
        gap_seconds=Config.EVENT_GAP_SECONDS,
        box_distance_px=Config.EVENT_BOX_DISTANCE_PX,
    )
    review = Review(
        db,
        ClipFrames(Path(Config.REVIEW_CACHE_DIR)),
        Path(Config.CAPTURES_DIR),
        Config.CAT_NAMES,
    )
    create_app(None, review).run(host="127.0.0.1", port=PORT, threaded=True)


def wait_for(url: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).close()  # noqa: S310 -- localhost
        except OSError:
            time.sleep(0.5)
        else:
            return
    raise SystemExit(f"the page did not come up at {url}")


def screenshot(url: str, size: str, output: Path) -> None:
    chromium = shutil.which("chromium")
    if chromium is None:
        raise SystemExit("chromium is not installed; it takes the screenshots")
    subprocess.run(  # noqa: S603 -- fixed command, local URL
        [
            chromium,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={size}",
            f"--virtual-time-budget={RENDER_BUDGET_MS}",
            f"--screenshot={output}",
            url,
        ],
        check=True,
        capture_output=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--filter", help="a ?filter= value, e.g. multi or labeled")
    args = parser.parse_args()
    query = f"?filter={args.filter}" if args.filter else ""

    with tempfile.TemporaryDirectory() as scratch:
        db_copy = Path(scratch) / "captures.db"
        shutil.copy(Config.DB_PATH, db_copy)
        threading.Thread(target=serve, args=(db_copy,), daemon=True).start()
        url = f"http://127.0.0.1:{PORT}/review{query}"
        wait_for(url)
        OUTPUT_DIR.mkdir(exist_ok=True)
        for name, size in SIZES.items():
            output = OUTPUT_DIR / f"page-{name}.png"
            screenshot(url, size, output)
            print(f"{name} ({size}): {output}")


if __name__ == "__main__":
    main()
