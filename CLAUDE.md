# CLAUDE.md

Guidance for Claude Code working in this repository. Written agent-to-agent: it assumes you have
no prior context and are about to change something on real hardware.

## Read this first: the docs

| file | what it holds |
|---|---|
| `docs/ROADMAP.md` | where the project is going, phase by phase, and the open questions |
| `docs/DESIGN.md` | why the code is shaped the way it is — rationale, measurements, rejected alternatives |
| `docs/STYLE.md` | how to write code here; a general Python style guide, portable to other repos |

Read `docs/STYLE.md` before writing code and `docs/DESIGN.md` before changing behaviour. This
file overlaps them deliberately: CLAUDE.md carries the traps you must not fall into, and the
design doc carries the full reasoning behind each one. The roadmap's section letters (A.2, A.4,
B.5b, …) are cited by the design doc and the commit messages.

Anything else in `docs/` is **gitignored** (`docs/TODO.md` is the owner's to-do list), as is
`CLAUDE.local.md`, which holds facts about the one Pi this runs on — its account, its mounts,
what needs the owner's own terminal. A fresh clone has neither file.

## What this actually is

A Raspberry Pi camera pointed **down at the litter boxes**; three are in frame. It records
motion-triggered clips and serves a live MJPEG stream. The end goal is **catching bowel or urinary
trouble in a particular cat**. That needs every visit recorded and attributed to **which cat**
made it, which is why the roadmap is split into triggering reliably (part A) and building a
labelled dataset plus classifier (part B). Telling poop from pee may be tried later; nothing
should depend on it.

Three consequences worth holding onto:

- **Recall beats precision.** A missed visit is a failure; an extra clip is an annoyance. A gap
  in a cat's record can look like the very change the project exists to flag. Do not "improve"
  detection by making it stricter without checking that against the roadmap.
- **A visit may span several clips.** Recording may stop while a cat sits still, as long as the
  clips on either side end up linked to one litter-box event (roadmap A.4; this linking is not
  built yet). Do not "fix" a clip ending mid-visit by holding recording open.
- The camera is fixed and overhead, so a cat's apparent size in pixels is roughly constant. That
  fact is the basis of the planned detection work — do not design around a moving camera.

## Platform

This only runs on a Pi with an attached camera. `picamera2` binds to real hardware when a
`Picamera2` object is **constructed** (not when the module is imported), so nothing in `src/` can
be exercised on a dev machine; the unit tests fake that layer instead. The suite itself runs
anywhere: on a machine with no `picamera2`, `tests/conftest.py` registers empty stand-in modules
before `src/` is imported, and the two tests that exercise the real `CircularOutput` skip.

The recordings directory is a **CIFS** network share mounted at `/mnt/nas`. That matters more
than it sounds: writes can fail with a plain `OSError`, the mount can be absent at boot, and
SQLite must never live there (see the roadmap's storage section).

**Clips are no longer written to the share directly.** `MotionRecorder` writes to
`LOCAL_CLIP_DIR` (`.clip_cache`) and `ClipTransfer` ships finished clips across on its own
thread. So the capture and encoder threads never touch the network, recording keeps working
through a NAS outage, and a clip only appears on the share once it is complete.

**The share is mounted `soft`, and nothing in fstab says so** — it is the CIFS default. On a
soft mount an unreachable server fails writes after ~1–2 minutes instead of blocking forever.
Since clips are staged locally, a `hard` mount would now only stall the transfer thread rather
than freeze the camera, but pin `soft` explicitly if you touch that fstab line: switching it
reads like a robustness improvement and is the exact opposite.

## Commands

```bash
uv run python main.py                        # run the detector (camera, live feed and review on :5000)

sudo bash deploy/install-service.sh          # install/refresh the catdetector service and (re)start it
sudo systemctl stop catdetector              # free the camera for anything else
journalctl -u catdetector -f                 # the service's logs

bash scripts/check.sh                        # all four gates, in the order that fails fastest
uv run pytest                                # unit tests (no hardware needed)
uv run ruff check .                          # lint
uv run ruff format .                         # format
uv run mypy .                                # type-check (strict, must stay clean)

uv run python tests/manual/check_camera.py   # hardware smoke test; writes test_images/test_image.jpg

METRICS_CSV=$HOME/metrics.csv uv run python main.py          # + per-frame detection metrics

uv run python review.py                      # the same web UI without the camera, on :5000
```

The web UI is **one Flask app on one port** ([src/web_app.py](src/web_app.py)): a page with a
Live tab and a Review tab, built from `src/templates/app.html`. `main.py` serves it with the
camera; `review.py` serves the same app without one, for when the detector is off. Only one of
them can hold port 5000, which is the point. The review half ingests
`$NETWORK_SHARE_DIR/captures/*.h264` into a local SQLite DB (`DB_PATH`, default `captures.db` —
local disk, never the share) and serves a keyboard-driven cat/not-cat labeler; frame extraction
shells out to ffmpeg because cv2.VideoCapture cannot seek raw elementary streams.

Ingest reads each clip's sidecar and groups clips into **events** (one visit, one or more clips)
with the rule in `src/event_grouping.py`, tuned by `EVENT_GAP_SECONDS` and
`EVENT_BOX_DISTANCE_PX`. Event ids are stable across rescans. `uv run python review.py --regroup`
rebuilds every event from the current thresholds: ids change, labels follow their clips. A
`captures.db` from before grouping is refused at startup; move it aside and re-ingest.

The review page shows every clip of an event and lets the reviewer split an event at a clip or
join it with the next; those are stored as overrides on the clips (`boundary`, `joins`) and
survive a `regroup`. `/review?filter=multi` walks the multi-clip events; `?filter=labeled` or
`?filter=<label>` walks the events already labelled, for auditing and relabelling. Each clip is
shown as a grid of tiles spanning the whole file, cut client-side from its cached strip image:
one per second up to `MAX_TILES`, sparser beyond that (`tile_seconds`), the offset drawn into
each tile. The stride is derived from the clip's keyframe count, **not** from the sidecar: the
file starts up to `buffer_seconds` before the sidecar's `started_at` (the ring-buffer pre-roll).
Extraction decodes **keyframes only**, and the recorder pins one keyframe per second
(`iperiod=CAMERA_FPS`): change either and the tiles land on the wrong seconds. One decode
yields the strip and the full frames behind its tiles; the strip is published last, so its
presence means the set is complete. "Watch" remuxes the clip to MP4 on demand into
`.review_cache`; that cache is keyed by **clip** id (`clip<id>_...`), never by event id, because
event ids change on a regroup.

`METRICS_CSV` turns on the instrumentation for roadmap A.2. Point it at **local disk**, never at
the share: one row per frame at 30 fps is the small-write pattern CIFS handles worst. Rows are
written off the capture thread and dropped rather than queued without bound, so the count of
dropped rows is reported at shutdown.

`CaptureDB` is one SQLite connection shared by Flask's request threads, so its public methods run
under an `RLock` (`@serialized`). Anything that touches the share — `ingest`'s glob and sidecar
reads — must stay **outside** that lock: a stalled CIFS mount would otherwise freeze every review
request behind it.

**All four gates must pass before every commit** — tests, lint, format check, type check.
`scripts/check.sh` runs them, and `.github/workflows/check.yml` runs the same script on every
push. Run it and report the result.

`uv run` targets the project venv directly, so activating it is unnecessary.

## Working on the Pi

**The camera is exclusive.** One process at a time. If `main.py` is running, the smoke test and
any hardware script will fail to open the camera, and vice versa. Check with
`ps aux | grep main.py` before wondering why initialisation failed.

**The detector normally runs as the `catdetector` systemd service** (built from
`deploy/catdetector.service.in` by `deploy/install-service.sh`), so the camera is usually taken.
Anything else that needs it, including a hand-run `main.py`, needs `sudo systemctl stop
catdetector` first and a `start` afterwards. Every minute it is stopped is a gap in the cats'
record, so keep that window short and say when it is open. Per-run settings such as
`MOTION_THRESHOLD` and `METRICS_CSV` live in a systemd drop-in (`systemctl edit catdetector`),
not in the tracked unit. The service's output goes to the journal (`journalctl -u catdetector`).
When matching its process with `pgrep -f`, anchor the pattern to the venv's interpreter path, as
`install-service.sh` does: an unanchored one also matches the shell running your own command,
and a `kill` then takes that shell down with it.

**Testing the recorder without polluting the NAS.** `config.py` calls `load_dotenv()`, which does
*not* override variables already in the environment, so pointing both directories at scratch
exercises the whole path:

```bash
LOCAL_CLIP_DIR=/some/scratch/clips NETWORK_SHARE_DIR=/some/scratch/share uv run python main.py
```

`ClipTransfer` refuses to write to a path that is not a real mountpoint, so a scratch share
leaves the clips on local disk rather than scattering them across the SD card. Do not leave test
clips on the share.

**Verify on hardware, not just in the test suite.** The suite fakes the entire camera layer, so
it cannot catch a picamera2 API misuse. Things worth measuring after a change to the capture or
recording path:

- capture loop rate — should be **~30 fps**; 15 fps means something reintroduced two
  `capture_array` calls
- a clip decodes cleanly — `ffmpeg -v error -i clip.h264 -f null -` should print nothing
- a static scene records **nothing** — if it writes a clip, the MOG2 warmup has regressed
- no threads left behind after `cleanup()`

**Logs go to stderr via `logging`.** `PYTHONUNBUFFERED` is no longer needed; if you see advice
saying otherwise, it predates the switch. The root logger sits at `WARNING` and only this
application is raised to `INFO`, because picamera2 narrates every state change at INFO and buries
everything else.

## Conventions

Source comments are **spartan and describe current behaviour**. No rationale, no measurements, no
rejected alternatives, no history — those go in `docs/DESIGN.md`. If you are about to write a
comment beginning "deliberately", "we used to", or "this would otherwise", write it there
instead. Docstrings: one line if one line does it.

This file is the exception. It is written for agents, and being thorough here is the point.

Lint is configured to enforce what it can: `C90` complexity is a **ratchet** set at the current
worst function, so new complexity must be extracted rather than absorbed. `PLR2004` and several
others are disabled inside `tests/**` for reasons recorded in `docs/STYLE.md` — read that before
"fixing" the config.

## Repo hygiene

**This repo is public.** Treat tracked files and commit messages as world-readable. Never put LAN
addresses, share names, hostnames, or credentials in anything tracked — they belong in
`CLAUDE.local.md`, which is gitignored. `.env` is ignored and has never been
committed; keep it that way. The same test applies to prose: tracked files are written for a
stranger with a Pi and a cat. Opinions about the code belong in; notes about the owner, their
machine or their workflow go in `CLAUDE.local.md`.

Commit messages carry no `Claude-Session:` trailers. `Co-Authored-By:` is fine.

If something sensitive lands in a commit that has **not** been pushed, remove it from history
then, not later — rewriting unpushed commits is free and rewriting pushed ones is not.

## Tests

`tests/` holds pytest unit tests that never touch the camera. `tests/conftest.py` fakes
`Picamera2`, `H264Encoder` and `_ResilientCircularOutput`, and `StubSubtractor` replaces MOG2 so
a test can dictate the foreground pixel count exactly. A test that needs the real picamera2
classes takes the `needs_real_picamera2` marker, so it skips where the package is absent.

The fixture patches `_ResilientCircularOutput`, the subclass the recorder actually constructs —
patching `CircularOutput` does nothing, because the subclass bound the real base class at import
time.

`tests/manual/` holds hardware scripts that grab the real camera. They are named `check_*.py` so
they match none of pytest's collection globs. That is structural on purpose: `norecursedirs` was
*not* enough, because it only suppresses directory walking and does not stop an explicitly named
path (`pytest tests/manual`).

Traps when adding tests here:

- **Never fetch `/video_feed` with the Flask test client.** It buffers the whole response and
  `generate_frames()` is an infinite generator, so the suite hangs with no failure at all. Build
  the response by calling `streamer.video_feed()` inside `app.test_request_context(...)`.
- `pythonpath = ["."]` in `pyproject.toml` is load-bearing: `src/` and `config.py` are top-level
  modules of an application, not an installed package, so without it every import fails.
- **Join with a timeout and assert something that can fail.** `assert not thread.is_alive()`
  after an unbounded `join()` is a tautology, and a regression hangs the suite instead of failing
  it. The suite runs with `--timeout=60` for the same reason.
- **Assert on behaviour, not on "it did not raise."** Several tests here once asserted only that
  a frame was stored, and would have passed with the feature deleted.
- **When a fake grows a method in production, grow it in the fake.** A diverged fake surfaces as
  an exception inside a worker thread, not as a clean failure.

**Mutation-check new regression tests.** Reintroduce the bug once and confirm the test fails. A
regression test that has never failed has not been shown to work. Several tests in this repo were
verified that way, and one turned out to be redundant rather than wrong.

For a comment-only or docstring-only refactor, you can prove you changed no behaviour by parsing
each file before and after, stripping docstrings, and comparing `ast.dump`.

## Environment: the constraint that governs dependencies

Package management is uv (`pyproject.toml` + committed `uv.lock`). One non-obvious rule applies,
and it has already caused breakage:

**The venv must have system site packages.** `picamera2` and `libcamera` are apt packages —
libcamera ships compiled Python bindings and is not on PyPI — so they can only be reached through
`include-system-site-packages = true`. `uv sync` *preserves* that flag on an existing venv, but if
`.venv` is deleted, `uv sync` silently recreates it with the flag off and `import picamera2`
starts failing. The recovery, and the correct first-time setup, is:

```bash
uv venv --system-site-packages && uv sync
```

`[tool.uv] python-preference = "only-system"` exists for the same reason: a uv-managed CPython
download would not see the apt packages at all.

**The Python version is not ours to choose.** `python3-libcamera` ships an ABI-tagged
`_libcamera.cpython-313-aarch64-linux-gnu.so`, so the interpreter is whatever the OS ships, and
libcamera is not on PyPI to escape it. The Pi runs Raspberry Pi OS trixie (Debian 13): Python
3.13, `python3-libcamera` 0.7.2, `picamera2` 0.3.37, system numpy 2.2.4. `requires-python` is
pinned to `==3.13.*`. Moving Python means moving the OS.

**An OS upgrade invalidates the venv silently.** `.venv/pyvenv.cfg` keeps the *old* `version_info`
and the old `lib/python3.X/site-packages`, while `.venv/bin/python` is a symlink to
`/usr/bin/python3` that now resolves to the *new* interpreter. Nothing raises — the venv's own
packages just disappear from `sys.path`. System site packages still resolve, so `picamera2`
imports fine and only the PyPI deps like `cv2` go missing, which makes it look like a dependency
problem rather than a venv problem. Rebuild with the two-line recovery above.

## Architecture

One camera, several consumers. `picamera2` allows a single `Picamera2` instance per process, so
[src/camera_manager.py](src/camera_manager.py) owns it and everything else borrows from it. It
also defines the `FrameConsumer` alias. The `Frame` array alias lives in
[src/frame.py](src/frame.py), and what a clip is on disk — its suffixes, `partial_name()`,
`CAMERA_FPS`, `H264_BITRATE` — in [src/clip_format.py](src/clip_format.py), so that the review
side (`capture_db`, `clip_frames`, `clip_transfer`, `review`) imports nothing from the modules
that import `picamera2`. `review.py` runs on a machine without the camera stack for that reason;
keep it so.

**Frame fan-out.** `CameraManager` configures two streams — `main` at 1280x720 for
display/recording and `lores` at 640x480 for cheap analysis. Components call `add_consumer(fn)` to
register a `fn(main_frame, lores_frame)` callback; a single background thread
(`_distribute_frames`) takes **one** `capture_arrays(["main", "lores"])` under `_frame_lock`, then
invokes every consumer **synchronously, in order** outside the lock. The lock covers capture only,
and there is no sleep — `capture_arrays` blocks until the next frame, pacing the loop at ~30 fps.

Use `capture_arrays`, never two `capture_array` calls: those consume two *separate* libcamera
requests, so the frames would be different exposures ~33 ms apart and the loop would run at half
rate (measured 15 fps vs 30 fps). It returns `(arrays, metadata)`, not a bare list.

Consumers run on the capture thread, so a slow or blocking consumer throttles the loop and every
other consumer. **Copy anything you retain, return fast, hand real work to your own thread.**
Consumer exceptions are caught and logged per frame, so a broken consumer fails loudly without
stopping the loop.

**Two independent video paths.** The consumer fan-out is *not* how video gets recorded.
[src/motion_recorder.py](src/motion_recorder.py) reaches through `camera_manager.get_camera()` and
drives picamera2's own encoder pipeline: an `H264Encoder` writes continuously into a
`CircularOutput`. Recording is diverted to a file when motion starts
(`circular_output.fileoutput = <local .part path>` then `.start()`, which flushes from the
buffer's **oldest** keyframe) and back to buffering only when it stops (`.stop()`).

So the recorder uses the consumer callback only for *detection* (MOG2 on the `lores` frame,
thresholded on `countNonZero`), while the bytes flow through picamera2's encoder. Changing frame
distribution does not change what is recorded, and vice versa.

**Clips reach the share in two stages.** The recorder writes `<clip>.h264.part` to local disk
and renames it to `<clip>.h264` when the file closes. [src/clip_transfer.py](src/clip_transfer.py)
scans for those finished names every 60 s, copies each to `<clip>.h264.part` on the share, then
renames *within* the share. Both renames are same-filesystem and therefore atomic, so no reader
ever sees a final-named clip that is still growing — which is what keeps the review server from
ingesting a half-written file. Do not replace either step with `shutil.move`: across filesystems
it degrades to copy-then-delete and exposes the final name immediately. The scan is stateless,
so finished clips stranded by a crash ship on the next pass. A clip cut off mid-write, by a crash
or a power cut, is left as `<clip>.h264.part`. The recorder gives it its final name at startup so
it ships too. It decodes up to the cut, and ffmpeg's error about its last, partial frame is
expected.

**Every finished clip has a sidecar**, `<clip>.h264.json`, holding what the raw stream cannot:
start and end time, the blob that triggered it, the blob on its last motion frame, and why it
closed. Event grouping (roadmap A.4) is computed from sidecars, so two orderings are load-bearing:
the recorder writes the sidecar *before* the clip takes its final name, and `ClipTransfer` copies
the sidecar *before* the clip. Do not reorder either. A clip with no sidecar means it was cut off
by a crash; that is expected, not a bug.

**Web stream.** [src/web_streamer.py](src/web_streamer.py) is another consumer: it keeps the
newest `main` frame under a lock, optionally annotates it with recorder status via OpenCV, and
serves it as MJPEG at `/video_feed` with JSON status at `/api/status`, as a blueprint on the one
Flask app from [src/web_app.py](src/web_app.py). The page at `/` and `/review` is the same
template with a different tab active; leaving the Live tab drops the `<img>` source so a hidden
tab does not hold an MJPEG connection open. [main.py](main.py) runs the app in a daemon thread;
`monitor()` on the main thread starts frame distribution and blocks until
Ctrl-C. `WebStreamer` takes `motion_recorder` as an optional dependency and degrades to a bare
feed when it is `None`.

## Traps

Each of these was a real bug. The reasoning is in `docs/DESIGN.md`.

- **The camera is shared, so the recorder must not stop it.** `Picamera2.stop_recording()` calls
  `stop()` on the camera itself, cutting off every other consumer. `MotionRecorder.cleanup()`
  calls `stop_encoder(self.encoder)` — only the encoder is the recorder's to stop.
- **Nothing may raise out of the encoder's poll thread.** Clip bytes are written on
  `V4L2Encoder.thread_poll`, **not** the camera event-loop thread — earlier revisions of this
  file said otherwise. That thread is the only one that returns camera buffers and refills
  `buf_available`, so killing it makes `_encode` block forever on the camera event loop and
  every `capture_arrays()` with it — a silent, total freeze. `_ResilientCircularOutput` contains
  both known ways out: `OSError` from `_write`, and the `IndexError` from `outputframe` when
  `stop()` drains the ring between a frame's append and its `popleft`.
- **Never yield while holding a lock the capture thread needs.** A suspended generator keeps its
  context manager, so one slow MJPEG viewer used to block frame distribution — and therefore
  motion detection — indefinitely.
- **`stop()` empties the ring buffer**, so pre-motion footage is bounded by the gap since the last
  clip ended, not by `buffer_seconds`. Draining also writes the whole buffer to disk, which is why
  it runs on its own thread.
- **Durations use `time.monotonic()`.** This Pi has no RTC backup cell; an NTP step on a wall
  clock would hold a clip open by the size of the jump.
- **Shut down the producer first.** `stop_frame_distribution()` before `recorder.cleanup()`;
  the other order races the capture thread.
- **MOG2 calls the whole first frame foreground**, which triggered a clip on every startup.
  `warmup_frames` suppresses detection while the model trains.
- Clips are raw `.h264` elementary streams, not MP4. They decode fine but have no duration and
  cannot seek. Remux with `ffmpeg -i clip.h264 -c copy clip.mp4` if that matters.

## Known weaknesses

Not bugs, but do not mistake them for correct:

- Detection is a **global pixel count**, so the shadow fix does not make it good — only honest.
- No spatial coherence: scattered noise and one solid cat-sized blob are indistinguishable.
  The largest blob is logged, both raw and after cleanup, but nothing yet *triggers* on it —
  that is A.3.
- A cat that settles is absorbed into the background in roughly `history` frames (~17 s at the
  default), so recording can stop mid-visit. That is acceptable once clips are linked into one
  event (roadmap A.4). The linking is not built yet, so for now one visit can be several separate
  clips.
- **The camera cannot see in the dark.** It is the IR-filtered Camera Module 3 (libcamera reports
  `imx708`, not `imx708_noir`) and the room is unlit at night. Motion is ignored while the analysis
  frame's mean grey level is below `DARK_BRIGHTNESS` (default 10), because sensor speckle alone
  otherwise records black video nonstop. That stops the waste, not the gap: visits in the dark go
  unrecorded until the boxes are lit. Do not "fix" night recall by tuning the threshold.
- The web stream has **no authentication** and runs on Flask's dev server. LAN-only by design;
  see the README's Security section.

Fixing these is roadmap part A, and it starts with instrumentation rather than tuning — nobody
has yet measured what a cat is worth in pixels in this mounting.
