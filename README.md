# Cat Detector

Raspberry Pi camera monitor: motion-triggered recording plus a live MJPEG web stream.

## System dependencies

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

## Run

```
uv run python main.py                      # camera + web stream on :5000
uv run python test/manual_camera_test.py   # camera smoke test
```

`uv run` uses the project environment directly, so there is no need to activate the venv.

## Lint, format, type-check

```
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
