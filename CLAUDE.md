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

## Environment: the constraint that governs dependencies

Package management is uv (`pyproject.toml` + committed `uv.lock`). One non-obvious rule applies, and it
has already caused breakage:

**The venv must have system site packages.** `picamera2` and `libcamera` are apt packages — libcamera
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
`_libcamera.cpython-313-aarch64-linux-gnu.so`, so the interpreter is whatever the OS ships, and libcamera
is not on PyPI to escape it. The Pi runs Raspberry Pi OS trixie (Debian 13): Python 3.13,
`python3-libcamera` 0.7.2, `picamera2` 0.3.37, system numpy 2.2.4. `requires-python` is pinned to
`==3.13.*` to match. Moving Python means moving the OS.

**An OS upgrade invalidates the venv silently.** `.venv/pyvenv.cfg` keeps the *old* `version_info` and
the old `lib/python3.X/site-packages`, while `.venv/bin/python` is a symlink to `/usr/bin/python3` that
now resolves to the *new* interpreter. Nothing raises — the venv's own packages just disappear from
`sys.path` (system site packages still resolve, so `picamera2` imports and only the PyPI deps like `cv2`
go missing, which makes it look like a dependency problem rather than a venv problem). Rebuild with the
two-line recovery above.

## Architecture

One camera, several consumers. `picamera2` allows a single `Picamera2` instance per process, so
[src/camera_manager.py](src/camera_manager.py) owns it and everything else borrows from it. It also defines
the shared `Frame` (`NDArray[np.uint8]`) and `FrameConsumer` type aliases.

**Frame fan-out.** `CameraManager` configures two streams — `main` at 1280x720 for display/recording and
`lores` at 640x480 for cheap analysis. Components call `add_consumer(fn)` to register a
`fn(main_frame, lores_frame)` callback; a single background thread (`_distribute_frames`) captures both
arrays under `_frame_lock`, then invokes every consumer **synchronously, in order** outside the lock, then
sleeps ~33 ms. The lock covers capture only. Consumers still run on the capture thread, so a slow or
blocking consumer throttles the capture loop and every other consumer.
Consumers must copy anything they retain and return fast — hand work off to their own thread if it isn't
cheap. Consumer exceptions are caught and logged per-frame, so a broken consumer fails loudly but does not
stop the loop.

**Two independent video paths.** The consumer fan-out above is *not* how video gets recorded.
[src/motion_recorder.py](src/motion_recorder.py) reaches through `camera_manager.get_camera()` and drives
picamera2's own encoder pipeline: an `H264Encoder` writes continuously into a `CircularOutput` (a rolling
`buffer_seconds`-worth of pre-motion footage), and recording is diverted to a file when motion starts
(`circular_output.fileoutput = path` then `.start()`, which flushes the buffer from its most recent
keyframe so the clip opens with the pre-motion footage) and back to buffering only when it stops
(`.stop()`, which drains the remainder and closes the file). So the recorder uses the consumer callback only for
*detection* (MOG2 background subtraction on the `lores` frame, thresholded on `countNonZero`), while the
actual bytes flow through picamera2's encoder. Changing frame distribution does not change what is
recorded, and vice versa.

**Web stream.** [src/web_streamer.py](src/web_streamer.py) is another consumer: it keeps the newest `main`
frame under a lock, optionally annotates it with recorder status via OpenCV, and serves it as an MJPEG
`multipart/x-mixed-replace` response at `/video_feed`, with a status page at `/`. Flask runs in a daemon
thread started from [main.py](main.py); `monitor()` on the main thread just starts frame distribution and
blocks until Ctrl-C.

`WebStreamer` takes `motion_recorder` as an optional dependency and degrades gracefully to a bare feed when
it is `None`. `main.py` wires a real `MotionRecorder` in.

**The camera is shared, so the recorder must not stop it.** `Picamera2.stop_recording()` calls
`stop()` on the camera itself, which would cut off every other consumer. `MotionRecorder.cleanup()`
therefore calls `stop_encoder(self.encoder)` — only the encoder is the recorder's to stop.

## Notes

- Clips are raw H.264 elementary streams (`.h264`), which is what `CircularOutput` writes — not MP4.
  They decode fine, but carry no container metadata, so players report no duration and cannot seek.
  Remux with `ffmpeg -i clip.h264 -c copy clip.mp4` if that matters.
- MOG2 has no background model on its first frame and reports the whole frame as foreground, which used
  to trigger a clip on every startup. `MotionRecorder` suppresses detection for its first `warmup_frames`
  (default 30, ~1s) while still feeding the subtractor so the model trains. Measured on this camera:
  frame 0 is 100% foreground, frame 1 ~3.8% (still over the default threshold), frame 2 onward under 25
  pixels — so the default carries about a second of margin.
