# Running as a service: decisions

The decisions behind [Running as a service](../DESIGN.md#running-as-a-service) in
[DESIGN.md](../DESIGN.md), each with the measurement, the failure or the constraint that settled it,
and when. Start from the summary there; read the decision here before changing what it covers. Words
with a meaning of their own are in the [glossary](../DESIGN.md#glossary).

## Contents

- [Running as a service](#running-as-a-service)
  - [`main.py` degrades rather than dying](#mainpy-degrades-rather-than-dying)
  - [Shutdown stops the producer first](#shutdown-stops-the-producer-first)
  - [SIGTERM takes the same path as Ctrl-C](#sigterm-takes-the-same-path-as-ctrl-c)
  - [It runs as a systemd service (`deploy/catdetector.service.in`, 2026-09-14)](#it-runs-as-a-systemd-service-deploycatdetectorservicein-2026-09-14)
  - [After an unclean stop the camera stays busy ~10-15 s](#after-an-unclean-stop-the-camera-stays-busy-10-15-s)
  - [A capture stall ends the process (roadmap A.6)](#a-capture-stall-ends-the-process-roadmap-a6)
  - [The detector has a heartbeat of its own (roadmap A.6)](#the-detector-has-a-heartbeat-of-its-own-roadmap-a6)
  - [Logging root at WARNING, application loggers at INFO](#logging-root-at-warning-application-loggers-at-info)
  - [The web stream binds `0.0.0.0` with no authentication](#the-web-stream-binds-0000-with-no-authentication)

## Running as a service

### `main.py` degrades rather than dying

If the recorder cannot be constructed, the web stream
still comes up — it is often how you find out something is wrong — and a review database that
fails to open leaves the Live tab up. A share that is not mounted stops neither: recording goes
to local disk.

### Shutdown stops the producer first

Tearing down the recorder while the capture thread is
still calling into it races `_start_saving` and can leave a zero-byte clip plus a recorder stuck
reporting "recording".

### SIGTERM takes the same path as Ctrl-C

systemd and `kill` stop a process with SIGTERM, whose
default action ends it on the spot. Without a handler cleanup never runs, so a clip being written
stays a `.part` file until the next start recovers it, and its sidecar is never written. The
handler raises `SystemExit` on
the main thread, so `monitor()`'s `finally` runs the usual shutdown. It also ignores any further
SIGTERM, so a second one cannot interrupt that cleanup.

### It runs as a systemd service (`deploy/catdetector.service.in`, 2026-09-14)

It runs as a systemd service (`deploy/catdetector.service.in`, 2026-09-14), so it starts at boot
and comes back after a crash. A detector started by hand stays down after a power cut until a
person notices, and every hour of that is a gap in the record. Most of the unit's settings favour
recall over tidiness:

- **It wants the share's mount but does not require it.** `RequiresMountsFor=/mnt/nas` would stop
  the detector from starting whenever the share is down at boot. Recording does not need the share:
  clips are staged on local disk, and `ClipTransfer` refuses a path that is not a mountpoint. The
  unit is still ordered after the mount, so a share that is coming up is there for the first ingest.
- **It waits for NTP, for at most 90 s.** On a Pi with no RTC backup cell,
  fake-hwclock rewinds the clock after a power cut to its last hourly save and NTP corrects it
  about 35 s later (2026-09-14), so a detector that started straight away would stamp clips and
  metrics with the wrong time. `systemd-time-wait-sync.service` would do the waiting, but its
  `TimeoutStartSec` is `infinity`, so with the internet down the detector would never start. The
  unit runs the same helper as an `ExecStartPre` under `timeout 90` and ignores its failure. The
  helper works unprivileged (checked on a Pi 5, 2026-09-14).
- **`RestartSec=20`**, because the camera stays busy for 10–15 s after an unclean exit (below), and a faster restart would crash-loop on "Camera __init__ sequence did not complete".
- **`Restart=on-failure`.** SIGTERM takes the clean-shutdown path and exits 0, so a
  `systemctl stop` is not undone.
- **`TimeoutStopSec=60`** covers the 30 s final clip flush and the metrics writer's 10 s join.
- **It runs the venv's Python, not `uv run`**, which can sync the environment at startup, and so
  change or fail it at boot.

Per-run detection settings live in a systemd drop-in
(`systemctl edit catdetector`), not in `.env` and not in the tracked unit. Putting `METRICS_CSV`
in `.env` would make the test suite's `build_recorder` calls open the real metrics file, and a
run's settings in the tracked unit would be one machine's experiment published as the default.
The tracked file is a template: `install-service.sh` fills in the user, the checkout path and the
share's mount unit, so the unit is not tied to one account or directory.

### After an unclean stop the camera stays busy ~10-15 s

A `kill -9`, a crash or a power cut
gives libcamera no chance to release the sensor, so the next `Picamera2()` fails with "Camera
__init__ sequence did not complete" until the kernel reclaims it. Constructing the camera is the
first thing `main.py` does, so this is a startup failure, not a recording one. The service's
`RestartSec` waits out that window; see `RestartSec` above. Measured on a Pi 5, 2026-09-12.

### A capture stall ends the process (roadmap A.6)

Once (2026-09-26) libcamera logged "Camera
frontend has timed out" and `capture_arrays()` blocked for hours without raising, while the
process and its web server looked healthy and nothing alerted; only a restart brought frames
back. So `monitor()` checks, once a second, how long ago the capture loop last got a frame, and
past `CAPTURE_STALL_SECONDS` it leaves through the same cleanup as SIGTERM and exits 1, which
`Restart=on-failure` turns into a restart. The limit is 10 s because, across twelve days of the
metrics CSV, the longest gap between frames that was not a restart or a power-off was 1.95 s.
The cleanup did not need a working camera in that stall: the restart stopped the wedged process
in 5 s, the frame thread's bounded join included. The gap a stall now costs is the limit, that
cleanup, `RestartSec` and startup, about 35 s.

That evidence covers one kind of wedge, the frontend timeout, and other kinds can hang the exit
itself: picamera2 registers `Picamera2.close` with `atexit`, and its `NullPreview.stop` joins the
event-loop thread with no timeout, while `stop_encoder` takes the encoder's lock and flushes. A
SIGTERM exit that hangs is killed by systemd after `TimeoutStopSec`, but a process that exits on
its own is not being stopped, so nothing would kill it and the outage would return. So a stall
arms a timer, `STALL_EXIT_DEADLINE_SECONDS` (60 s, as `TimeoutStopSec`, which covers the 30 s
final flush and the metrics writer's 10 s join), that calls `os._exit(1)`, skipping whatever is
still waiting; a clip cut off that way is recovered at the next start like any other. From the
moment of the stall SIGTERM is ignored, as on the SIGTERM path, so a `systemctl stop` cannot
raise mid-cleanup and turn the exit code into 0. The frame clock starts with distribution, not
when the camera opens, so a slow startup is not mistaken for a stall. The check measures the
capture loop, capture and consumers together, so a consumer that blocks the loop also restarts
the process, and the log says "from the capture loop" rather than blaming the camera. The alternative, reopening the camera inside the
process, would need a second `Picamera2` while the first may be stuck in libcamera, and the
single-instance rule and the recorder's hold on the encoder make that the less tested path.

### The detector has a heartbeat of its own (roadmap A.6)

A restart ends a stall but tells nobody,
and a camera that stays dead restarts every ~35 s forever. A heartbeat from the Pi itself keeps
pinging through a stall, because it proves the Pi is up, not the camera. So `CaptureHeartbeat`
GETs `CAPTURE_HEARTBEAT_URL` once a minute, but only when the last frame is newer than
`CAPTURE_STALL_SECONDS`: a stall, a restart loop and a stopped service all go quiet the same way,
and the watcher alerts on silence.

- **The interface is a URL and a GET**, nothing more. Healthchecks.io, a self-hosted Healthchecks,
  an Uptime Kuma push monitor and most dead man's switches accept exactly that, so no service is
  named in the code and none is required: unset, the heartbeat does not exist. Creating the check
  and choosing where its alert goes happen outside this project.
- **The ping runs on its own thread.** A network call on the capture thread would stall every
  consumer for as long as the network hangs; the GET has a 10 s timeout and the thread's stop is
  a bounded join.
- **The class takes a frame-age function and a send function**, not a camera and a URL. The camera
  stays in `CameraManager`, the HTTP in `http_ping`, and the tests drive the logic with neither.
- **The URL is a secret**, since whoever has it can keep the check quiet. It is never logged: a
  failure is described by its status or its cause's type, never by the exception's text, since
  `http.client` puts the path in some messages. A URL with no host, a space or a control
  character is refused at startup, also without being printed. `scripts/private_terms.sh` adds
  it to the privacy check, with every path segment or query value that looks like a token,
  since services put the token in different places (a Healthchecks ping key is followed by a
  public slug; an Uptime Kuma token is followed by a query).
- **No recorder, no heartbeat.** If the recorder cannot be built, the live feed still gets
  frames but no visit is recorded; a heartbeat then would report health during exactly the gap
  it exists to reveal, so it is off, with a warning.
- **The first ping comes 15 s after start**, not a full interval, so a restart does not add a
  minute of silence to its own downtime; 15 s is past the stall limit, so frames have had their
  chance to prove themselves.
- **Failures are logged once per streak**, with a line when pings get through again, so a
  watcher that is down for a day costs two log lines rather than 1,440.

### Logging root at WARNING, application loggers at INFO

A blanket INFO root turns on every
third-party logger; picamera2 narrates every state change and buries the application's own.

### The web stream binds `0.0.0.0` with no authentication

Deliberate — it is meant to be
reachable from other devices on the LAN. Do not forward the port. See the README's Security
section.
