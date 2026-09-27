# Cat Detector

Raspberry Pi camera monitor: motion-triggered recording plus a live MJPEG web stream.

## What you need

- **A camera fixed overhead**, pointing straight down at the litter boxes.
- **The boxes lit around the clock.** The camera cannot see in the dark, and a visit it cannot see
  is not recorded. Keep it constant: a light that switches on with motion leaves the moments
  before it dark, and switching on changes the whole frame at once.
- **Hardware the camera code supports.** Today that is one implementation: a Raspberry Pi 5 with
  the IR-filtered Camera Module 3. Other Pis and cameras are planned as further implementations
  (roadmap C.1).
- **Somewhere for the clips**: a mounted filesystem, such as a NAS, another computer's share or a
  USB disk (see Configuration).

Recommended: [pi-tools](https://github.com/Thinmindt/pi-tools), a Claude Code plugin that makes the
journal persistent, logs the Pi's supply voltage and temperature, brings a Pi 5 back on its own
after an unexpected power-off, and pings a dead man's switch so you hear when the Pi goes quiet.
A power-off is a gap in the cats' record, and this project cannot notice its own absence. What it
can report is its camera: see the capture heartbeat under Configuration.

## System dependencies

Runs on Raspberry Pi OS trixie (Debian 13), which ships Python 3.13. The Python version is set by
the OS rather than by this project, because the `libcamera` bindings are ABI-tagged apt packages —
hence the `requires-python = "==3.13.*"` pin.

`picamera2` and its `libcamera` bindings are **not installable from PyPI** — they ship as
compiled apt packages built against the system libcamera and the system Python:

```
sudo apt update
sudo apt install libcap-dev python3-dev python3-picamera2 ffmpeg
```

`ffmpeg` builds the review page's tiles and videos, and the tests use it too.

## Get the code

```
git clone https://github.com/Thinmindt/CatDetector
cd CatDetector
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

`deploy/install-service.sh` installs a systemd unit that runs the detector at boot and restarts
it after a crash, or when the camera has delivered no frame for 10 s (to be alerted when that
happens, see `CAPTURE_HEARTBEAT_URL` under Configuration). It builds the unit from `deploy/catdetector.service.in` for this checkout — the
directory's owner runs the service, and the share named by `NETWORK_SHARE_DIR` in `.env` is the
mount it waits for — and stops a copy started by hand, since the camera takes one process at a
time:

```
sudo bash deploy/install-service.sh
journalctl -u catdetector -f         # logs
sudo systemctl stop catdetector      # free the camera, e.g. for the smoke test
```

Settings for a particular run (`MOTION_THRESHOLD`, `METRICS_CSV`, ...) go in a drop-in rather
than in the tracked unit, so re-running the script never loses them:

```
sudo systemctl edit catdetector      # opens an override; add Environment= lines under [Service]
```

## Reviewing captures

```
uv run python main.py --regroup      # rebuild events from the current thresholds, then exit
uv run python main.py --stage --label not_cat --labeled-before 2026-09-20 --reason "recheck"
```

`--regroup` renumbers events, so it refuses to run while the detector has the database open: stop
the service first. `--stage` puts labelled events back in the review queue with a reason, keeping
their labels, and runs beside the service; `--label` and `--labeled-before` choose the events.

The Review tab of the web UI (`http://<pi-ip>:5000/review`) scans the captures directory into
a local database and serves a keyboard-driven labeler — `c` cat, `n` not a cat, `u` unsure, `z`
undo, `l` a litter cleaning (enter the poops found per box, `Enter` saves), click a tile to see
the full frame, "watch" to play the clip. With `CAT_NAMES` set, each cat gets a digit key in the
configured order and `m` marks an event with more than one cat; `c` then means "a cat, which one
not decided", and `/review?filter=cat` walks those to name them; `→` moves on while walking a
filter. The address names the event and the walk (`/review/<event>?filter=multi`), so a reload,
Back or a shared link returns to the same event. `main.py` serves it beside the live feed, so it is available whenever the detector is
running.

## Test, lint, format, type-check

```
npm install                  # once per checkout, for the JavaScript lint (needs node)
bash scripts/check.sh        # every gate, stopping at the first failure
uv run pytest                # unit tests (no camera or picamera2 needed; ffmpeg is)
uv run ruff check --fix .    # lint + autofix
uv run ruff format .         # format
```

`check.sh` runs, in order: the privacy check (`scripts/check_private.sh`; see CLAUDE.md),
shellcheck, codespell, ruff, the format check, mypy, eslint and pytest.

GitHub Actions runs the same script on every push (`.github/workflows/check.yml`).

## Contributing

`docs/ROADMAP.md` is the plan and the open questions, `docs/DESIGN.md` records why the code is
shaped the way it is, with the measurements behind each decision, and `docs/STYLE.md` is the
style guide; `CLAUDE.md` lists the traps. Before a commit, `scripts/check.sh` must pass.

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

`NETWORK_SHARE_DIR` must be a real mountpoint, so a missing mount cannot fill the SD card, and
renames within it must be atomic. A NAS, another computer's SMB or NFS share, a USB disk and cloud
storage mounted with rclone all qualify; a cloud mount makes the review page's tiles slow to
build, since each is decoded from the whole clip. A plain directory on local disk is refused for
now (roadmap C.5).

If the share is mounted from `/etc/fstab`, pin the `soft` option explicitly. It is the CIFS
default, so it is easy to lose by accident; NFS mounts `hard` unless told otherwise. On a `hard`
mount an unreachable server blocks writes indefinitely instead of failing with an error.

The cats, for labelling which one made a visit. Any number, comma-separated, in the order the
review page's digit keys should take:

```
CAT_NAMES=Ada,Bea
```

Optional, for tuning detection:

```
METRICS_CSV=/home/you/metrics.csv
```

When set, every frame appends a row of detection metrics to that file: the foreground pixel
count, the largest blob before and after cleanup (area, bounding box, centroid), whether a clip
is recording, and the frame's brightness; the header names each column. Use a local path, not
the network share.

Optional, to be told when the camera stops:

```
CAPTURE_HEARTBEAT_URL=https://hc-ping.com/your-check-uuid
```

When set, the detector sends an HTTP GET to that URL once a minute, **only while the camera is
delivering frames**. A dead man's switch watching the URL then alerts you when the pings stop: a
camera that has stalled, a detector that keeps restarting because its camera is dead, or a
detector that is not running. Without it the detector still restarts itself after a stall (see
Run), but nobody is told.

This project only sends the pings. The check that receives them, and the alert it sends, are
yours to set up outside it: for example a free check at [Healthchecks.io](https://healthchecks.io)
(or a self-hosted Healthchecks, or an Uptime Kuma push monitor; any service that alerts when a
URL stops being fetched will do). Give it a period of one minute and a grace time of at least
three, so a routine restart does not alert. If you use
[pi-tools](https://github.com/Thinmindt/pi-tools), its `pi-heartbeat` pings a check of its own
for the Pi; it does not create or share this one. Make a second check for the camera, since the
Pi can be up while the camera is not.

The URL is a secret: anyone who has it can keep your check quiet. Keep it in `.env`, which is
gitignored, and never in a tracked file; `scripts/check_private.sh` refuses a commit that
contains it. A URL that is not `http://` or `https://`, has no host, or contains a space turns
the heartbeat off with a warning, and so does a recorder that failed to start, since the camera
may then be delivering frames while nothing is recorded.

The other settings, with their defaults (`config.py` describes each):

| setting | default | what it sets |
|---|---|---|
| `MOTION_THRESHOLD` | 300 | foreground pixels in the 640x480 analysis frame that start a clip |
| `DARK_BRIGHTNESS` | 10 | mean grey level below which motion is ignored |
| `MOTION_TIMEOUT` | 10 | seconds without motion before a clip closes |
| `MOG2_HISTORY` | 500 | frames over which a still subject fades into the background |
| `EVENT_GAP_SECONDS` | 180 | how soon a clip must follow the last to join its visit |
| `EVENT_BOX_DISTANCE_PX` | 120 | how close two clips' centroids must be to count as one box |
| `DB_PATH` | `captures.db` | the review database; local disk, never the share |
| `REVIEW_CACHE_DIR` | `.review_cache` | cached tiles and videos |

The pixel thresholds depend on how far the camera is from the boxes; roadmap A.2 is how to
measure them for a mounting.

## License

MIT — see [LICENSE](LICENSE).
