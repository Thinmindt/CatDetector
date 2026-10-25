# The system and its threads: decisions

The decisions behind [The system at a glance](../DESIGN.md#the-system-at-a-glance) and [Threads and
locks](../DESIGN.md#threads-and-locks) in [DESIGN.md](../DESIGN.md), each with the measurement, the
failure or the constraint that settled it, and when. Start from the summary there; read the decision
here before changing what it covers. Words with a meaning of their own are in the
[glossary](../DESIGN.md#glossary).

## Contents

- [The system at a glance](#the-system-at-a-glance)
  - [The local clip cache is sized for a long outage of the share](#the-local-clip-cache-is-sized-for-a-long-outage-of-the-share)
  - [`src/` is three packages: `capture`, `review`, and `clips` for what both agree a clip is (2026-09-20)](#src-is-three-packages-capture-review-and-clips-for-what-both-agree-a-clip-is-2026-09-20)
  - [One app, one port (2026-09-12)](#one-app-one-port-2026-09-12)
  - [Detection is its own class (2026-09-20)](#detection-is-its-own-class-2026-09-20)
  - [Nothing under `src/` reads `Config` (2026-09-20)](#nothing-under-src-reads-config-2026-09-20)
  - [Domain types inside, ISO text at the edges (2026-09-20)](#domain-types-inside-iso-text-at-the-edges-2026-09-20)
- [Threads and locks](#threads-and-locks)
  - [Consumers run outside `_frame_lock`](#consumers-run-outside-_frame_lock)
  - [Joins are bounded](#joins-are-bounded)
  - [Nothing may raise out of the thread that writes clip bytes (2026-09-26)](#nothing-may-raise-out-of-the-thread-that-writes-clip-bytes-2026-09-26)
  - [Draining happens off-thread, and the destination must be local disk (2026-09-26)](#draining-happens-off-thread-and-the-destination-must-be-local-disk-2026-09-26)
  - [Metrics are written off the capture thread](#metrics-are-written-off-the-capture-thread)
  - [The share is read outside the database lock (2026-09-18)](#the-share-is-read-outside-the-database-lock-2026-09-18)
  - [The startup ingest runs on its own thread (2026-09-22)](#the-startup-ingest-runs-on-its-own-thread-2026-09-22)

## The system at a glance

### The local clip cache is sized for a long outage of the share

Clips accumulate on local
disk while the share is away and ship when it returns, where an in-memory buffer would fill
and drop footage. Clip data arrives at `H264_BITRATE / 8` bytes a second, 1.25 MB/s at the
constant's value of 10 Mbit/s, so the 300 s `max_clip_seconds` ceiling is 375 MB. Clips have
averaged ~18 MB, so every 100 GB of free local disk holds roughly 5,500 of them, and
`_has_room`'s 1 GB floor is a guard against a months-long outage, not a routine constraint.
SD wear is not a concern at this volume: 20 such clips a day is ~134 GB a year, a fraction of
a card's write endurance.

### `src/` is three packages: `capture`, `review`, and `clips` for what both agree a clip is (2026-09-20)

`src/` is three packages: `capture`, `review`, and `clips` for what both agree a clip is
(2026-09-20): its names, its sidecar, the `Blob` and `Frame` types. The rule that the review
side imports without the camera stack is ruff's `banned-api` (`TID251`) on `picamera2` and
`libcamera`, with a per-file allowance for `src/capture`, so a stray import fails the lint gate
rather than a machine without a camera; a rule written only as prose is broken without anyone
noticing. `Blob` lives in `src/clips` rather than `motion_metrics` because the sidecar needs it,
and importing it from `motion_metrics` drags the CSV writer and the morphology kernels into the
review side.

### One app, one port (2026-09-12)

`web_app.create_app` takes the streamer and the
review parts as optionals and serves whatever it was given, and a review database that fails to
open leaves the Live tab up rather than taking the detector down, in the same spirit as a broken
recorder leaving the stream up. `main.py` is the only entry point (2026-09-20): with the detector
running as a service around the clock the Review tab is always up, so a second, camera-less
script serving the labeler on the same port would be one more thing to explain and could never
run beside it. `main.py --regroup` covers regrouping without the web app. The cost is that a
machine with no camera cannot run the labeler as a service; the review package imports without
the camera stack (`TID251`), so a camera-less entry point would need no change to it. The page is a Jinja template
rather than a string in a Python module: ruff's line limit applies inside strings, and JavaScript
wrapped to satisfy it is unreadable.

### Detection is its own class (2026-09-20)

`MotionDetector` holds MOG2, the warmup, the dark
gate and the last blob, and answers a `Detection` per frame; `MotionRecorder` takes one and turns
its answer into a clip lifecycle. With the two in one class, every test of a threshold or a warmup
count has to fake the camera, the encoder and the ring buffer to reach the detector, and A.3 grows
exactly the detection half.

### Nothing under `src/` reads `Config` (2026-09-20)

`main.py` reads it and passes settings down,
for both sides; `CaptureDB` requires its grouping thresholds and the review routes take the
captures directory in a `Review` dataclass. A module that falls back to `Config` has a hidden
input that a test or a second caller cannot see.

### Domain types inside, ISO text at the edges (2026-09-20)

`Clip`, `Event` and `FoundClip` carry
`datetime`s, a `CloseReason` and a `Boundary` enum; SQLite and the JSON API see ISO 8601 text to
the millisecond, formatted by the one `iso()` in the sidecar module. Flattening the sidecar's
datetimes to strings in the scan means the database parses them back for the grouping rule.
`CloseReason` includes `RECOVERED`, so a clip's reason is one type whether the recorder or ingest
assigned it.

## Threads and locks

### Consumers run outside `_frame_lock`

The lock covers capture only. Consumers run
synchronously on the capture thread, so a slow one throttles everything; a lock held across them
as well would let one blocked consumer stop capture itself. Exceptions are caught per consumer so
one bad consumer cannot stop the loop.

### Joins are bounded

A wedged consumer must not make shutdown hang. Five seconds, then log and
move on.

### Nothing may raise out of the thread that writes clip bytes (2026-09-26)

Which thread that is
depends on the encoder picamera2 picks, and `H264Encoder` is a different class per platform
(`picamera2/encoders/__init__.py`):

- **Pi 5 (`Platform.PISP`), the tested platform.** `H264Encoder` is
  `LibavH264Encoder`, a software x264 encode. Clip bytes are written on picamera2's **camera
  event-loop thread**: `NullPreview.thread_func` → `handle_request` →
  `Picamera2.process_requests`, which calls `encoder.encode` for each request while holding
  `Picamera2.lock` → `Encoder.encode` (under the encoder's `_lock`) → `LibavH264Encoder._encode`
  → `outputframe` → `CircularOutput.outputframe` → `_write`. `handle_request` logs an exception
  and re-raises it, which ends the event loop: no request completes again, so every
  `capture_arrays` waits forever — a silent, total freeze. The x264 encode and each write run
  synchronously on that thread, so a slow write stalls capture directly, frame by frame. The
  final flush of frames still inside libav (`LibavH264Encoder._stop`) runs on whichever thread
  calls `stop_encoder`, which is the one in `MotionRecorder.cleanup()`.
- **Pi 4 (`Platform.VC4`).** `H264Encoder` is the V4L2 hardware encoder, and output runs on
  `V4L2Encoder.thread_poll`. That thread is the only one that calls `queue_item.release()` to
  return camera buffers and refill `buf_available`. Kill it and `_encode` blocks forever on the
  camera event loop, with the same freeze as a result.

`FileOutput._write` catches only `ConnectionResetError`, `ConnectionRefusedError`,
`BrokenPipeError` and `ValueError`, so `_ResilientCircularOutput` swallows `OSError` too and marks
the clip abandoned.

It also guards `outputframe`. `CircularOutput.outputframe` appends under `_lock`, releases, then
re-acquires to `popleft`; `stop()` can drain the whole ring in that window, making `popleft`
raise `IndexError` on the output thread. Rare per clip end, but every clip end rolls the dice.

### Draining happens off-thread, and the destination must be local disk (2026-09-26)

`CircularOutput.stop()` writes the entire ring buffer with a flush per frame, holding the
output's `_lock` throughout. `_finish_clip` runs it on its own thread, so the capture thread and
its consumers do not do the write; handoff measured ~0.3 ms (2026-08-25). That does not keep the
drain off the camera: `outputframe` takes the same `_lock`, so the thread that writes output
(the event loop on a Pi 5) waits for the drain whichever thread runs it. On local disk the wait
does not show: across 14 clip stops, the largest gap between consecutive frames in the 6 frames
after each stop was 0.037 s against a 0.033 s frame period (metrics CSV, 2026-09-26). Draining
several megabytes to a network share would hold the camera for the length of that write, which
is one more reason clips are staged locally.

### Metrics are written off the capture thread

`MetricsLog.record()` puts a row on a bounded
queue; a daemon thread writes and periodically flushes. When the queue fills, rows are dropped
and counted rather than blocking — the capture thread must never wait on a disk. The CSV lives
on local disk, not the share: one row per frame at 30 fps is exactly the small-write pattern
that CIFS handles worst.

### The share is read outside the database lock (2026-09-18)

`serialized` exists to stop
concurrent reads corrupting SQLite's cursors. Under it, `ingest` — which globs the share and reads
every new sidecar over CIFS, up to a couple of minutes per failing operation on a soft mount —
would hold every reader of the review UI behind it. So `ingest` collects paths, sizes and sidecar
facts with no lock held and takes it only to insert, place and commit;
`INSERT OR IGNORE` covers a clip that another ingest registered in between. The share-reading
half is its own module, `clip_scan.py`, with no lock in it (2026-09-19): the rule is then a
property of the import graph rather than of one method's care.

### The startup ingest runs on its own thread (2026-09-22)

It scans the share, and a soft CIFS
mount that is unreachable at boot fails each operation only after one to two minutes. Run before
`monitor()` starts frame distribution, the scan would delay the first recording by that timeout
while the share is down, with the camera and encoder already up. The review page can come up a few
seconds before the scan lands; `rescan` covers a reviewer who beats it.
