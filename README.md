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

Install [uv](https://docs.astral.sh/uv/) if you don't have it:

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
uv run python main.py                        # camera + web stream on :5000
uv run python tests/manual/camera_test.py    # camera smoke test (needs the camera)
```

`uv run` uses the project environment directly, so there is no need to activate the venv.

## Test, lint, format, type-check

```
uv run pytest                # unit tests (no hardware needed)
uv run ruff check .          # lint
uv run ruff check --fix .    # lint + autofix
uv run ruff format .         # format
uv run mypy .                # type-check (strict)
```

## Configuration

Copy the settings you need into a `.env` file at the repo root (gitignored):

```
NETWORK_SHARE_DIR=/mnt/nas
```

Recordings are written to `$NETWORK_SHARE_DIR/captures/`.
