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
A power-off is a gap in the cats' record, and this project cannot notice its own absence.

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

`deploy/install-service.sh` installs a systemd unit that runs the detector at boot and restarts
it after a crash. It builds the unit from `deploy/catdetector.service.in` for this checkout — the
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
```

The Review tab of the web UI (`http://<pi-ip>:5000/review`) scans the captures directory into
a local database and serves a keyboard-driven labeler — `c` cat, `n` not a cat, `u` unsure, `z`
undo, click a frame strip to zoom, "watch" to play the clip. With `CAT_NAMES` set, each cat gets
a digit key in the configured order and `m` marks an event with more than one cat; `c` then means
"a cat, which one not decided", and `/review?filter=cat` walks those to name them. `main.py` serves it beside the live
feed, so it is available whenever the detector is running.
Requires `ffmpeg` (`sudo apt install ffmpeg`).

## Test, lint, format, type-check

```
bash scripts/check.sh        # everything below, stopping at the first failure
uv run pytest                # unit tests (no hardware, and no picamera2, needed)
uv run ruff check .          # lint
uv run ruff check --fix .    # lint + autofix
uv run ruff format .         # format
uv run mypy .                # type-check (strict)
npm run lint                 # the page's JavaScript (needs node; run npm install once)
uv run shellcheck scripts/*.sh deploy/*.sh    # shell scripts
git ls-files -z | xargs -0 uv run codespell   # spelling, in every tracked file
```

`check.sh` also runs the privacy check first (`scripts/check_private.sh`; see CLAUDE.md).

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

When set, every frame appends a row of detection metrics (foreground pixel count, largest
contour area, bounding box) to that file. Use a local path, not the network share.

## License

MIT — see [LICENSE](LICENSE).
