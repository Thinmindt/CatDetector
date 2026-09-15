# Cat Detector

Raspberry Pi camera monitor: motion-triggered recording plus a live MJPEG web stream.

## System dependencies

Runs on Raspberry Pi OS trixie (Debian 13), which ships Python 3.13. The Python version is set by
the OS rather than by this project, because the `libcamera` bindings are ABI-tagged apt packages —
hence the `requires-python = "==3.13.*"` pin.

`picamera2` and its `libcamera` bindings are **not installable from PyPI** — they ship as
compiled apt packages built against the system libcamera and the system Python:

```
sudo apt update
sudo apt install libcap-dev python3-dev python3-picamera2
```

## Python environment (uv)

Install [uv](https://docs.astral.sh/uv/):

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Create the virtualenv. The `--system-site-packages` flag is **required** so the venv can
see the apt-installed `picamera2`/`libcamera`:

```
uv venv --system-site-packages
uv sync
```

> **Do not delete `.venv` and run a bare `uv sync`.** uv will recreate it *without*
> system site packages and `import picamera2` will fail with `ModuleNotFoundError`.
> If that happens, re-run the `uv venv --system-site-packages` line above, then `uv sync`.
>
> The same rebuild is required after an **OS upgrade**. A venv built against the old interpreter
> keeps pointing at a `lib/python3.X/site-packages` that the new one never looks in, so its packages
> silently vanish from `sys.path` without any error.

## Run

```
uv run python main.py                        # camera, live feed and review on :5000
uv run python tests/manual/check_camera.py    # camera smoke test (needs the camera)
```

`uv run` uses the project environment directly, so there is no need to activate the venv.

### As a service

`deploy/catdetector.service` runs the detector at boot and restarts it after a crash. Install or
refresh it with the script below, which also stops a copy started by hand, since the camera takes
one process at a time:

```
sudo bash deploy/install-service.sh
journalctl -u catdetector -f         # logs
sudo systemctl stop catdetector      # free the camera, e.g. for the smoke test
```

The unit hard-codes the checkout path and user, and carries the detection settings of the current
data run in its `Environment=` lines. Edit them to suit, then re-run the script.

## Reviewing captures

```
uv run python review.py      # the same web UI without the camera, on :5000
```

The Review tab of the web UI (`http://<pi-ip>:5000/review`) scans the captures directory into
a local database and serves a keyboard-driven labeler — `c` cat, `n` not a cat, `u` unsure, `z`
undo, click a frame strip to zoom, "watch" to play the clip. `main.py` serves it beside the live
feed; when the detector is not running, `review.py` serves the same page on the same port.
Requires `ffmpeg` (`sudo apt install ffmpeg`).

## Test, lint, format, type-check

```
uv run pytest                # unit tests (no hardware needed)
uv run ruff check .          # lint
uv run ruff check --fix .    # lint + autofix
uv run ruff format .         # format
uv run mypy .                # type-check (strict)
```

## Security

**The web UI has no authentication.** The live feed, every recorded clip and the review tools
are served to anyone who can reach port 5000, over plain HTTP, by Flask's development server.
Anyone on the same network can watch the camera and relabel events.

Run this only on a network you trust, and **do not forward port 5000 through your router** or
let UPnP open it. If you ever need it reachable from outside, put authentication and a real
WSGI server (waitress, gunicorn) in front of it first — and note that the development server
prints its own warning about this on every start.

## Configuration

Copy the settings you need into a `.env` file at the repo root (gitignored):

```
NETWORK_SHARE_DIR=/mnt/nas
LOCAL_CLIP_DIR=.clip_cache
```

Clips are recorded to `LOCAL_CLIP_DIR` on local disk, then moved to
`$NETWORK_SHARE_DIR/captures/` once each clip is complete. Recording therefore does not depend
on the share being reachable: if it goes away, clips queue locally and are shipped when it comes
back. A clip only appears on the share under its final `.h264` name once every byte has arrived,
so the review UI never samples a half-written file.

If the share is mounted from `/etc/fstab`, pin the `soft` option explicitly. It is the CIFS
default, so it is easy to lose by accident — and on a `hard` mount an unreachable NAS blocks
writes indefinitely instead of failing with an error.

Optional, for tuning detection:

```
METRICS_CSV=/home/you/metrics.csv
```

When set, every frame appends a row of detection metrics (foreground pixel count, largest
contour area, bounding box) to that file. Use a local path, not the network share.
