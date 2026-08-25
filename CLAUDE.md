# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Platform

This is a Raspberry Pi project. It only runs on a Pi with an attached camera — `picamera2` binds to real
hardware at import/construction time, so nothing in `src/` can be exercised on a dev machine. Assume the
working directory *is* the Pi.

## Commands

```bash
uv run python main.py                      # run the detector (camera + web stream on :5000)
uv run python test/manual_camera_test.py   # camera smoke test; writes test_images/test_image.jpg

uv run ruff check .                        # lint
uv run ruff format .                       # format
uv run mypy .                              # type-check (strict, must stay clean)
```

There is no test runner. `test/` holds standalone scripts run directly with `python`, not pytest files.
`uv run` targets the project venv directly, so activating it is unnecessary.

## Environment: the two constraints that govern dependencies

Package management is uv (`pyproject.toml` + committed `uv.lock`). Two non-obvious rules apply, and both
have already caused breakage:

**1. The venv must have system site packages.** `picamera2` and `libcamera` are apt packages — libcamera
ships compiled Python bindings and is not on PyPI — so they can only be reached through
`include-system-site-packages = true`. `uv sync` *preserves* that flag on an existing venv, but if `.venv`
is deleted, `uv sync` silently recreates it with the flag off and `import picamera2` starts failing. The
recovery, and the correct first-time setup, is:

```bash
uv venv --system-site-packages && uv sync
```

`[tool.uv] python-preference = "only-system"` exists for the same reason: a uv-managed CPython download
would not see the apt packages at all.

**The Python version is not ours to choose.** `python3-libcamera` ships an ABI-tagged
`_libcamera.cpython-311-aarch64-linux-gnu.so`, so the interpreter is whatever the OS ships — 3.11 on
bookworm. `libcamera` is not on PyPI, so there is no pip escape hatch. Upgrading Python means upgrading
the OS: Raspberry Pi OS trixie ships Python 3.13 with `python3-libcamera` 0.7.2 and `picamera2` 0.3.37.
`requires-python` is deliberately widened to `>=3.11,<3.14` so the repo works either side of that move.

**2. `av`, `pillow`, and `simplejpeg` are listed as project dependencies for ABI reasons, not because this
code imports them.** They are picamera2's runtime deps. The system numpy is 1.24, so the *apt* builds of
those three are compiled against the numpy 1.x C ABI — but `opencv-python` requires numpy >= 2, and that
numpy shadows the system one inside the venv. The result is `import picamera2` dying with
`ValueError: numpy.dtype size changed, may indicate binary incompatibility`. Pinning numpy-2-built wheels
of all three in `pyproject.toml` shadows the apt copies and keeps the import working. **Do not remove
them** because "nothing imports them" — that is exactly the trap.

## Architecture

One camera, several consumers. `picamera2` allows a single `Picamera2` instance per process, so
[src/camera_manager.py](src/camera_manager.py) owns it and everything else borrows from it. It also defines
the shared `Frame` (`NDArray[np.uint8]`) and `FrameConsumer` type aliases.

**Frame fan-out.** `CameraManager` configures two streams — `main` at 1280x720 for display/recording and
`lores` at 640x480 for cheap analysis. Components call `add_consumer(fn)` to register a
`fn(main_frame, lores_frame)` callback; a single background thread (`_distribute_frames`) captures both
arrays and invokes every consumer **synchronously, in order, while holding `_frame_lock`**, then sleeps
~33 ms. Consequence: a slow or blocking consumer throttles the capture loop and every other consumer.
Consumers must copy anything they retain and return fast — hand work off to their own thread if it isn't
cheap. Consumer exceptions are caught and logged per-frame, so a broken consumer fails loudly but does not
stop the loop.

**Two independent video paths.** The consumer fan-out above is *not* how video gets recorded.
[src/motion_recorder.py](src/motion_recorder.py) reaches through `camera_manager.get_camera()` and drives
picamera2's own encoder pipeline: an `H264Encoder` writes continuously into a `CircularOutput` (a rolling
`buffer_seconds`-worth of pre-motion footage), and recording is meant to be diverted to a file when motion
starts and back to the circular buffer when it stops. So the recorder uses the consumer callback only for
*detection* (MOG2 background subtraction on the `lores` frame, thresholded on `countNonZero`), while the
actual bytes flow through picamera2's encoder. Changing frame distribution does not change what is
recorded, and vice versa.

**Web stream.** [src/web_streamer.py](src/web_streamer.py) is another consumer: it keeps the newest `main`
frame under a lock, optionally annotates it with recorder status via OpenCV, and serves it as an MJPEG
`multipart/x-mixed-replace` response at `/video_feed`, with a status page at `/`. Flask runs in a daemon
thread started from [main.py](main.py); `monitor()` on the main thread just starts frame distribution and
blocks until Ctrl-C.

`WebStreamer` takes `motion_recorder` as an optional dependency and degrades gracefully to a bare feed when
it is `None` — which is the current state of `main.py`, where the `MotionRecorder` construction is commented
out. Re-enabling it means uncommenting both the recorder and the `motion_recorder=` argument.

## Known bugs

- **`MotionRecorder` cannot currently save a clip.** `_start_saving`/`_stop_saving` call
  `self.picam2.split_recording(...)`, which does not exist on `Picamera2` 0.3.27 — that name is from the
  legacy `picamera` library. Every save attempt is swallowed by the `except Exception` and logged as
  `Failed to start saving: 'Picamera2' object has no attribute 'split_recording'`, so no file is ever
  written. The picamera2 equivalent is to set `circular_output.fileoutput = path` and call
  `circular_output.start()` / `.stop()`. This is almost certainly why the recorder is commented out in
  `main.py`.
- `MotionRecorder.detect_motion` prints a line for *every* frame (~30/sec), which floods stdout.
- `CameraManager` holds `_frame_lock` across the entire consumer loop *and* the sleep, so the lock protects
  far more than frame capture.
