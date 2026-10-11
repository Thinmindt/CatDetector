# CLAUDE.md

Guidance for Claude Code working in this repository. Written agent-to-agent: it
assumes you have no prior context and are about to change something on real
hardware.

## Read this first: the docs

| file | what it holds |
|---|---|
| `README.md` | what the project is for, how it works in a minute, and how to install, run, label and calibrate it |
| `docs/DESIGN.md` | how the system works: the goal, an overview with diagrams and the thread map, a summary of each mechanism, and a glossary; read in a few minutes |
| `docs/design/*.md` | the decisions behind each domain, linked from its summary in DESIGN.md: what was decided, why, when, and the measurements and failures that settled it |
| `docs/ROADMAP.md` | where the project is going, phase by phase, and the open questions — the plan as it stands today |
| `docs/ROADMAP_ARCHIVE.md` | finished roadmap items and the history behind decisions, under the same section letters |
| `docs/STYLE.md` | how to write code here; a general Python style guide, portable to other repos |

Each fact has one home, where a reader would look for it: how the system works
and why is in DESIGN.md and the decisions under `docs/design/`, the plan in the
roadmap, installing and using it in the README. This file holds what is its own:
how to work here, and the traps, each in a line or two with a pointer to the
document under `docs/design/` that holds its reasoning. Read DESIGN.md before
changing anything, `docs/STYLE.md` before writing code, and the decisions for
the part you are changing before changing its behavior. Event, visit, sidecar,
tile, peek and the project's other words mean what DESIGN.md's glossary says.

The roadmap's section letters (A.2, A.4, B.5b, …) are cited by the design doc
and the commit messages, and never change: when an item is finished, or a
passage turns into history, it moves verbatim to the archive under the same
letter, so the roadmap only ever describes the present plan.

Anything else in `docs/`, outside `docs/design/`, is **gitignored**
(`docs/TODO.md`, if present, is a maintainer's own to-do list, open items only,
and `docs/TODO_ARCHIVE.md` holds the finished ones), as is `CLAUDE.local.md`,
which holds facts about the one Pi a checkout runs on and the person who runs it
— its account, its mounts, what needs that person's own terminal. A fresh clone
has none of these files.

## What this actually is

A Raspberry Pi camera pointed **down at the litter boxes**, which must be lit
around the clock. It records motion-triggered clips and serves a live MJPEG
stream. The end goal is **catching bowel or urinary trouble in a particular
cat**. That needs every visit recorded and attributed to **which cat** made it,
which is why the roadmap is split into triggering reliably (part A) and building
a labeled dataset plus classifier (part B). Part C keeps it working for other
installations: any one installation's Pi, camera, box count and thresholds are
one implementation and one calibration, not requirements. Telling poop from pee
may be tried later; nothing should depend on it.

Three consequences worth holding onto:

- **Recall beats precision.** A missed visit is a failure; an extra clip is an
  annoyance. A gap in a cat's record can look like the very change the project
  exists to flag. Do not "improve" detection by making it stricter without
  checking that against the roadmap.
- **A visit may span several clips.** Recording may stop while a cat sits still,
  as long as the clips on either side end up linked to one litter-box event
  (roadmap A.4; clips are grouped into events at ingest). Do not "fix" a clip
  ending mid-visit by holding recording open.
- The camera is fixed and overhead, so a cat's apparent size in pixels is
  roughly constant. That fact is the basis of the planned detection work — do
  not design around a moving camera.

## Platform

This only runs on a Pi with an attached camera. `picamera2` binds to real
hardware when a `Picamera2` object is **constructed** (not when the module is
imported), so nothing in `src/capture/` can be exercised on a dev machine; the
unit tests fake that layer instead. The review half imports anywhere;
`tests/manual/check_page.py` serves it with no camera attached, but it builds
the whole web app, so it needs `picamera2` installed. The suite itself runs
anywhere: on a machine with no `picamera2`, `tests/conftest.py` registers empty
stand-in modules before `src/` is imported, and the two tests that exercise the
real `CircularOutput` skip.

The recordings directory, `NETWORK_SHARE_DIR` (default `/mnt/nas`), is typically
a **CIFS** network share. That matters more than it sounds: writes can fail with
a plain `OSError`, the mount can be absent at boot, and SQLite must never live
there (roadmap B.2).

**A CIFS share should be mounted `soft`**, which is the CIFS default and so
often not written in fstab (the README's section on the share has the details).
Since clips are staged locally, a `hard` mount stalls the transfer and review
threads rather than the camera, but do not switch a share to `hard`: it reads
like a robustness improvement and is the exact opposite.

## Commands

```bash
uv run python main.py                        # run the detector (camera, live feed and review on :5000)

sudo bash deploy/install-service.sh          # install/refresh the catdetector service and (re)start it
sudo systemctl stop catdetector              # free the camera for anything else
journalctl -u catdetector -f                 # the service's logs

bash scripts/check.sh                        # every gate, fastest first
uv run pytest                                # unit tests (no hardware needed)
uv run ruff check .                          # lint
uv run ruff format .                         # format
uv run mypy .                                # type-check (strict, must stay clean)
npm run lint                                 # lint the page's JavaScript (npm install first)

uv run python tests/manual/check_camera.py   # hardware smoke test; writes test_images/test_image.jpg
uv run python tests/manual/check_page.py     # screenshot the review page, phone and desk sizes, no camera
                                             # (--path '/timeline?hours=22-6' for any other address)

METRICS_CSV=$HOME/metrics.csv uv run python main.py          # + per-frame detection metrics

uv run python main.py --regroup              # rebuild every event from the current thresholds, then exit
                                             # (refused while the service has the database: stop it first)
uv run python main.py --stage --label not_cat --labeled-before 2026-09-20 --reason "..."
                                             # put labeled events back in the review queue; runs beside the service
uv run python main.py --calibrate --since 2026-09-16
                                             # a data run's progress, doubts staged, proposed settings (A.2)
```

`uv run` targets the project venv directly, so activating it is unnecessary.

To put an event in front of the maintainer for a second look, stage it: `main.py
--stage` from the command line, or `POST /api/review/<event id>/stage` with a
`reason` and `"source": "agent"`. It keeps its label and waits in the review
queue with the reason beside it.

## Gates

**Every gate must pass before every commit**: the privacy check, shellcheck,
codespell, lint, format check, type check, the JavaScript lint, the vendored-d3
check, tests. `scripts/check.sh` runs them; run it and report the result, and
commit only when it exits 0 (piped into `tail`, its failure is lost).
`.github/workflows/check.yml` runs the same script on every push, and
`install.yml` runs the README's install steps on a clean Debian as an ordinary
user, then the tests, so a README that no longer installs fails CI.

The tests include the documents' own promises (`tests/test_docs.py`). Links,
anchors and citations resolve, in a comment or a unit file as much as in a
document, and so does every roadmap section cited. Every setting, option and key
is in the README, with its default. Prose spells the American way, and the
summary tiers stay within their readability budgets, measured by
`scripts/readability.py`, the conventions plugin's script copied unchanged like
`check_private.sh`. `tests/test_doc_figures.py` builds each figure the documents
quote (the stall's 10 s, the 48 tiles, the calibration targets) from the
constant behind it, so a constant that moves without its sentence fails; a
figure added to a summary gets a line there.

shellcheck and codespell are pinned dev dependencies, so `uv sync` installs
them. The JavaScript gate is eslint, pinned by `package.json` and
`package-lock.json` and configured in `eslint.config.mjs`; run `npm install`
once per checkout (CI runs `npm ci`). It needs node, which CI installs and a
contributor installs once. JavaScript dependencies go in `package.json` like
Python ones go in `pyproject.toml`.

Before the gates, `scripts/check.sh` runs `scripts/check_private.sh`, which
fails if any private term appears in a file git tracks or would add, or in the
author, committer or message of a commit no remote has yet. The terms are each
line of the gitignored `.private-terms` and whatever `scripts/private_terms.sh`
prints, which here is the names in `.env`'s `CAT_NAMES` and the
`CAPTURE_HEARTBEAT_URL` there: the URL without its query, and each part of it
that looks like a token (8 or more characters, a digit among them). Without
`.env` and `.private-terms`, as on CI and in a fresh clone, it reports that
nothing is configured and passes. When a new private fact turns up (an address,
a hostname, a token), add it to `.private-terms`. `check_private.sh` is the
template from the [conventions
plugin](https://github.com/Thinmindt/conventions), copied unchanged; this
project's own rule lives in `private_terms.sh`, so keep changes there.

**A page change is verified by looking at it.** The test suite cannot see a
layout, and the one bug the phone layout shipped with (a sheet that showed on
load, because a `display: flex` rule beat the `hidden` attribute) was invisible
to every gate. `tests/manual/check_page.py` serves the app from a copy of the
review database, with no camera, and screenshots `/review` at a phone portrait
(412 x 915) and a laptop size into `test_images/`; `--filter multi` and the
other walks work too, `--event <id>` opens one event as a link would, and
`--path` takes any address, the timeline's included. Run it and read the images
before calling a page change done. Flask caches templates once rendered, so a
server started before an edit serves the old page: restart it.

## Working on the Pi

**The camera is exclusive.** One process at a time. If `main.py` is running, the
smoke test and any hardware script will fail to open the camera, and vice versa.
Check with `systemctl is-active catdetector` (and an anchored `pgrep`, below)
before wondering why initialization failed.

**The detector normally runs as the `catdetector` systemd service** (built from
`deploy/catdetector.service.in` by `deploy/install-service.sh`), so the camera
is usually taken. Anything else that needs it, including a hand-run `main.py`,
needs `sudo systemctl stop catdetector` first and a `start` afterwards. Every
minute it is stopped is a gap in the cats' record, so keep that window short and
say when it is open. Per-run settings such as `MOTION_THRESHOLD` and
`METRICS_CSV` live in a systemd drop-in (`systemctl edit catdetector`), not in
the tracked unit. The service's output goes to the journal (`journalctl -u
catdetector`). When matching its process with `pgrep -f`, anchor the pattern to
the venv's interpreter path, as `install-service.sh` does: an unanchored one
also matches the shell running your own command, and a `kill` then takes that
shell down with it. The same applies to `pkill -f`.

**The service runs from this checkout**, whatever branch it is on: a `git
checkout` plus `sudo systemctl restart catdetector` is how a branch is tried on
the device before it is merged (a restart costs about two seconds before motion
is armed again, plus the ring's pre-roll). So check `git branch --show-current`
before assuming the service runs `main`, say which branch it is on when you
restart it, and after a merge pull `main` and restart so the two agree again.

**Testing the recorder without polluting the share.** `config.py` calls
`load_dotenv()`, which does *not* override variables already in the environment,
so scratch paths set on the command line win. Point the database and the review
cache at scratch too, or the run ingests into the live ones:

```bash
LOCAL_CLIP_DIR=/some/scratch/clips NETWORK_SHARE_DIR=/some/scratch/share \
  DB_PATH=/some/scratch/captures.db REVIEW_CACHE_DIR=/some/scratch/cache uv run python main.py
```

`ClipTransfer` refuses to write to a path that is not a real mountpoint, so a
scratch share leaves the clips in the scratch clip directory: this exercises
recording, not transfer. Do not leave test clips on the share.

**Verify on hardware, not just in the test suite.** The suite fakes the entire
camera layer, so it cannot catch a picamera2 API misuse. Things worth measuring
after a change to the capture or recording path:

- capture loop rate — should be **~30 fps**; 15 fps means something reintroduced
  two `capture_array` calls
- a clip decodes cleanly — `ffmpeg -v error -i clip.h264 -f null -` should print
  nothing
- a static scene records **nothing** — if it writes a clip, the MOG2 warmup has
  regressed
- no threads left behind after `cleanup()`

**Logs go to stderr via `logging`**, so `PYTHONUNBUFFERED` is not needed. The
root logger sits at `WARNING` and only this application is raised to `INFO`,
because picamera2 narrates every state change at INFO and buries everything
else.

## Conventions

Source comments are **spartan and describe current behavior**. No rationale, no
measurements, no rejected alternatives, no history — those go in the design
documents (`docs/DESIGN.md`, `docs/design/`). If you are about to write a
comment beginning "deliberately", "we used to", or "this would otherwise", write
it there instead. Docstrings: one line if one line does it.

This file is the exception. It is written for agents, and being thorough here is
the point.

**A feature starts as a roadmap entry with a "done means" line.** The phone
layout was specified in `docs/ROADMAP.md` first, with the sizes, the pattern and
what done looks like, and that last line is what produced the screenshot check.
Write the spec and the code on one branch, spec commit first, so the roadmap
checkbox and the code land together; two branches from `main` left the code
unable to tick the box. A branch is reviewed before it is pushed.

**Check a branch before handing it to the maintainer, by what it changes.** Code
gets `/code-review high`; documentation gets the doc-audit checks (the
conventions plugin's `doc-audit` skill: true, current, in one place, safe to
publish, general, shaped, readable), not a code review. Fix each finding or say
why not. The gates check what they were written to check; a review reads the
change against the libraries it calls. On the capture-stall branch it found,
after every gate had passed, a self-exit that picamera2's `atexit` close could
hang forever and a secret URL reaching the log through exception text. When a
document states an invariant ("never logged", "never on the capture thread"),
write the test that would fail if it broke (`docs/STYLE.md`, Tests).

Lint is configured to enforce what it can: `C90` complexity is a **ratchet** set
at the current worst function, so new complexity must be extracted rather than
absorbed. `PLR2004` and several others are disabled inside `tests/**` for
reasons recorded in `docs/STYLE.md` — read that before "fixing" the config.

## Repo hygiene

**This repo is public.** Treat tracked files and commit messages as
world-readable. Never put LAN addresses, share names, hostnames, credentials, or
anything personal about whoever runs it — their email, their cats' names — in
anything tracked; they belong in `CLAUDE.local.md` or `.env`, which are
gitignored. A pet's name is a common answer to account-recovery questions, and
knowing it lends a stranger false familiarity, so treat the cats' names like a
password. It is also a generality rule: another installation has other cats, so
code, tests and docs use made-up names (`Ada`, `Bea`) or "Cat A", never the ones
in `.env`. `.env` is ignored and has never been committed; keep it that way. The
same test applies to prose: tracked files are written for a stranger with a Pi
and a cat. Opinions about the code belong in; notes about one person, their
machine or their workflow go in `CLAUDE.local.md`.

**Tracked docs describe the project, not one installation.** A dated measurement
may justify a default, but one setup's counts, progress, incidents and hardware
go in a gitignored local note (`CLAUDE.local.md`, or another gitignored file
under `docs/`, such as a data-run note). When in doubt, share less.

Commit messages carry no `Claude-Session:` trailers. `Co-Authored-By:` is fine.
Author and committer lines are public too: list any address you would not
publish in `.private-terms`, and the privacy check fails a commit that carries
it before it is pushed. Commits already on a remote are not rechecked.

If something sensitive lands in a commit that has **not** been pushed, remove it
from history then, not later — rewriting unpushed commits is free and rewriting
pushed ones is not.

## Tests

`tests/` holds pytest unit tests that never touch the camera.
`tests/conftest.py` fakes `Picamera2`, `H264Encoder` and
`_ResilientCircularOutput`, and `StubSubtractor` replaces MOG2 so a test can
dictate the foreground pixel count exactly. A test that needs the real picamera2
classes takes the `needs_real_picamera2` marker, so it skips where the package
is absent.

The fixture patches `_ResilientCircularOutput`, the subclass the recorder
actually constructs — patching `CircularOutput` does nothing, because the
subclass bound the real base class at import time.

`tests/manual/` holds scripts run by hand: the ones that grab the real camera,
and `check_page.py`, which needs a database and chromium. They are named
`check_*.py` so they match none of pytest's collection globs. That is structural
on purpose: `norecursedirs` was *not* enough, because it only suppresses
directory walking and does not stop an explicitly named path (`pytest
tests/manual`).

Traps when adding tests here:

- **Never fetch `/video_feed` with the Flask test client.** It buffers the whole
  response and `generate_frames()` is an infinite generator, so the suite hangs
  with no failure at all. Build the response by calling `streamer.video_feed()`
  inside `app.test_request_context(...)`.
- `pythonpath = ["."]` in `pyproject.toml` is load-bearing: `src/` and
  `config.py` are top-level modules of an application, not an installed package,
  so without it every import fails.
- **Join with a timeout and assert something that can fail.** `assert not
  thread.is_alive()` after an unbounded `join()` is a tautology, and a
  regression hangs the suite instead of failing it. The suite runs with
  `--timeout=60` for the same reason.
- **Assert on behavior, not on "it did not raise."** Several tests here once
  asserted only that a frame was stored, and would have passed with the feature
  deleted.
- **When a fake grows a method in production, grow it in the fake.** A diverged
  fake surfaces as an exception inside a worker thread, not as a clean failure.

**Mutation-check new regression tests.** Reintroduce the bug once and confirm
the test fails. A regression test that has never failed has not been shown to
work. Several tests in this repo were verified that way, and one turned out to
be redundant rather than wrong.

For a comment-only or docstring-only refactor, you can prove you changed no
behavior by parsing each file before and after, stripping docstrings, and
comparing `ast.dump`.

## Environment: the constraint that governs dependencies

Package management is uv (`pyproject.toml` + committed `uv.lock`). One
non-obvious rule applies, and it has already caused breakage:

**The venv must have system site packages.** `picamera2` and `libcamera` are apt
packages — libcamera ships compiled Python bindings and is not on PyPI — so they
can only be reached through `include-system-site-packages = true`. `uv sync`
*preserves* that flag on an existing venv, but if `.venv` is deleted, `uv sync`
silently recreates it with the flag off and `import picamera2` starts failing.
The recovery, and the correct first-time setup, is:

```bash
uv venv --system-site-packages && uv sync
```

`[tool.uv] python-preference = "only-system"` exists for the same reason: a
uv-managed CPython download would not see the apt packages at all.

**The Python version is not ours to choose.** `python3-libcamera` ships an
ABI-tagged `_libcamera.cpython-313-aarch64-linux-gnu.so`, so the interpreter is
whatever the OS ships, and libcamera is not on PyPI to escape it. Tested on
Raspberry Pi OS trixie (Debian 13): Python 3.13, `python3-libcamera` 0.7.2,
`picamera2` 0.3.37, system numpy 2.2.4. `requires-python` is pinned to
`==3.13.*`. Moving Python means moving the OS.

**An OS upgrade invalidates the venv silently.** `.venv/pyvenv.cfg` keeps the
*old* `version_info` and the old `lib/python3.X/site-packages`, while
`.venv/bin/python` is a symlink to `/usr/bin/python3` that now resolves to the
*new* interpreter. Nothing raises — the venv's own packages just disappear from
`sys.path`. System site packages still resolve, so `picamera2` imports fine and
only the PyPI deps like `cv2` go missing, which makes it look like a dependency
problem rather than a venv problem. Rebuild with the two-line recovery above.

## Traps

Each of these was a real bug or a near miss. The line here is the rule; the
reasoning, the measurement and the failure are in the document under
`docs/design/` named after it.

**Capture** (`docs/design/`: `system.md`, `capture.md`, `service.md`):

- **The camera is shared, so the recorder must not stop it.**
  `MotionRecorder.cleanup()` calls `stop_encoder(self.encoder)`;
  `Picamera2.stop_recording()` would stop the camera under every other consumer.
- **Nothing may raise out of the thread that writes clip bytes**: picamera2's
  camera event loop on a Pi 5 (`LibavH264Encoder`), `V4L2Encoder.thread_poll` on
  a Pi 4. Either way an exception freezes capture silently and for good.
  `_ResilientCircularOutput` contains the two known ways out, `OSError` from
  `_write` and the `IndexError` from `outputframe` racing `stop()`; anything new
  that writes clip bytes needs the same care. Roadmap C.1 still wants the Pi 5
  path confirmed by a stack trace.
- **One `capture_arrays(["main", "lores"])` per frame, never two `capture_array`
  calls**: two calls take two libcamera requests, frames ~33 ms apart, and halve
  the rate to 15 fps. It returns `(arrays, metadata)`, not a bare list. There is
  no sleep in the loop; the capture paces it.
- **Consumers run on the capture thread.** Copy anything you keep, return fast,
  hand real work to a thread of your own. A slow consumer throttles every other
  one, and one that blocks for 10 s restarts the process.
- **Never yield while holding a lock the capture thread needs.** A suspended
  generator keeps its context manager: one slow MJPEG viewer used to stop motion
  detection.
- **The ring's drain goes to local disk.** `stop()` writes out the whole ring,
  on its own thread but holding the output's lock, which a Pi 5's event loop
  needs for the next frame. It also empties the ring, so pre-roll is bounded by
  the gap since the last clip, not `buffer_seconds`.
- **Shut down the producer first**: `stop_frame_distribution()` before
  `recorder.cleanup()`; the other order races the capture thread.
- **A self-exit needs its own deadline.** The stall exit arms an `os._exit`
  after 60 s, because picamera2's `atexit` close can wait on a wedged camera
  forever, and systemd only kills a process it is stopping.
- **The heartbeat URL is a secret.** Never log it, and never log an exception's
  text where it could appear: describe a failure by its status or its type.
- **Durations use `time.monotonic()`.** A Pi with no RTC battery steps its clock
  after boot.
- **MOG2 calls the whole first frame foreground**; `warmup_frames` is what stops
  a clip at every start.

**Clips and the share** (`docs/design/capture.md`):

- **Nothing on the capture side writes to the share.** Clips, the ring's drain
  and the metrics CSV go to local disk; `ClipTransfer` ships clips from its own
  thread. One row per frame at 30 fps is the small-write pattern CIFS handles
  worst.
- **A clip reaches the share by two renames**: `.part` to its final name on
  local disk, then a copy to `.part` on the share and a rename there. Never
  `shutil.move` across filesystems: it degrades to copy-then-delete and shows
  the final name before the bytes are all there.
- **The sidecar goes first**: written before the clip takes its final name,
  copied before the clip. Do not reorder either. A clip with no sidecar was cut
  off by a crash, or its sidecar write failed and was logged; that is expected.
- Clips are raw `.h264` elementary streams, not MP4: no duration, no seeking.
  Remux with `ffmpeg -i clip.h264 -c copy clip.mp4` if that matters.

**The review side** (`docs/design/`: `system.md`, `events.md`, `review.md`):

- **Share I/O stays outside `CaptureDB`'s lock**: the scan in
  `src/review/clip_scan.py`, which holds no lock, and the media `ClipFrames`
  builds, after the lock is let go. Under the lock, a stalled CIFS mount would
  freeze every review request.
- **Never open the database file itself for a lock**: closing any descriptor of
  it drops SQLite's own POSIX locks. The cross-process lock is a `flock` on
  `<database>.lock`, shared by every open `CaptureDB`, which is why `--regroup`
  refuses to run beside the service.
- **Event ids change on a regroup; clip ids never do.** Anything that must
  survive a regroup is keyed by clip: the cache files (`clip<id>*`), the media
  URLs (`/review/clip/<clip id>/...`), staging rows, poop counts, peeks and the
  questions already asked.
- **The tiles assume one keyframe a second.** The recorder pins
  `iperiod=CAMERA_FPS`, extraction decodes keyframes only, and the stride comes
  from the keyframe count, not the sidecar (the file starts up to
  `buffer_seconds` before the sidecar's `started_at`). Change any of these and
  the tiles land on the wrong seconds.
- **A strip's presence means its frames are complete**: the frames are published
  first and the strip last, each by rename.
- **The page's modules expose no globals.** Settings arrive in the JSON block
  (`id="config"`), and buttons carry `data-action`, never `onclick`. d3 is
  committed under `src/static/vendor/` from the version `package.json` pins:
  after a bump, run `npm run vendor`.

**The code's shape** (`docs/design/system.md`):

- **`picamera2` is banned outside `src/capture/`** by ruff's `banned-api` rule
  (`TID251`), so the review side imports on a machine with no camera stack. Do
  not add a per-file ignore to get round it.
- **Nothing under `src/` reads `Config`**: `main.py` reads it and passes
  settings down.

## Known weaknesses

Not bugs, but do not mistake them for correct (`docs/design/capture.md`):

- **Detection is a global pixel count**, with no spatial coherence: scattered
  noise and one solid cat-sized blob look the same. Excluding shadows makes it
  honest, not good. The cleaned blob is measured and logged on every frame, but
  nothing triggers on it yet; that is A.3.
- **A cat that settles fades into the background** in roughly `MOG2_HISTORY`
  frames (~17 s at the default), so recording can stop mid-visit. That is
  acceptable because clips are grouped into one event (A.4), though the grouping
  thresholds are still starting guesses.
- **The camera needs light.** The supported camera is the IR-filtered Camera
  Module 3 (libcamera reports `imx708`, not `imx708_noir`; roadmap C.1), so the
  boxes must be lit around the clock. The dark gate (`DARK_BRIGHTNESS`) stops
  black clips when the light fails; restoring the light, not tuning the
  threshold, is what closes the gap.
- The web page has **no authentication** and runs on Flask's dev server.
  LAN-only by design; see the README's Security section.

Fixing these is roadmap part A, and it starts with instrumentation rather than
tuning: A.2 measures what a cat is worth in pixels for one installation before
any threshold moves.
