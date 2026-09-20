# Design decisions

Why the code is shaped the way it is. Kept here so the source can stay terse.

Entries cite `ROADMAP.md` by section (A.2, A.4, B.5b, …).

## Goal

**The point of the project is catching bowel or urinary trouble in a cat.** Knowing which cat
made a visit is how we get there, not the goal itself. Three things follow from it:

- **Recall comes first.** A missed visit leaves a gap in a cat's record. A gap can look like the very
  change the project exists to flag, so losing a visit can lead to wrongly flagging a healthy cat. An
  extra clip only costs review time.
- **A visit may span several clips.** It is fine to stop recording while nothing is truly moving, for
  example while a cat sits still in the box, as long as the clips on either side of the pause are
  linked to the same litter-box event. One clip does not have to stay open through the stillness.
  Clips are linked into events at ingest; see Events below and ROADMAP A.4.
- **Poop versus pee is a possible later step.** Telling them apart looks hard and error-prone, so
  nothing should depend on it.

## Camera

**One `Picamera2` per process.** picamera2 allows only one instance, so `CameraManager` owns it
and everything else borrows via `get_camera()`.

**`capture_arrays(["main", "lores"])`, never two `capture_array()` calls.** Each `capture_array`
waits for and consumes its own libcamera request. Two calls therefore cost two frame periods and
return frames from different exposures ~33 ms apart. Measured on the imx708: 15.01 fps for two
calls, 30.00 fps for one `capture_arrays`. It returns `(arrays, metadata)`, not a bare list.

**No sleep in the capture loop.** `capture_arrays` blocks until the next frame, which paces the
loop at the sensor rate. An added sleep is pure loss: a `sleep(0.033)` on top of two blocking
captures runs the loop at ~10 fps.

**Consumers run outside `_frame_lock`.** The lock covers capture only. Consumers run
synchronously on the capture thread, so a slow one throttles everything; a lock held across them
as well would let one blocked consumer stop capture itself. Exceptions are caught per consumer so
one bad consumer cannot stop the loop.

**Joins are bounded.** A wedged consumer must not make shutdown hang. Five seconds, then log and
move on.

**The sensor mode is pinned to the full array.** picamera2 picks a sensor mode by output size.
For these streams it chose 1536x864, a 2x2 binning of a 3072x1728 crop, which keeps only the
central two thirds of the imx708's width. Asking for 2304x1296, the whole array binned 2x2, widens
the view enough to keep all three litter boxes in frame (2026-09-10). The detector held 30 fps
in this mode.

Pixel measurements changed with the mode. An object now spans about two thirds as many pixels in
each direction, so roughly 0.44 of the area. Metrics recorded before the switch cannot be compared
with later ones.

## Recording

**Two independent video paths.** The consumer fan-out feeds detection and the web view. The
recorded bytes go through picamera2's own encoder pipeline: `H264Encoder` into a
`CircularOutput`. Changing frame distribution does not change what is recorded, and vice versa.

**Clips are written to local disk, then shipped.** `MotionRecorder` writes to
`LOCAL_CLIP_DIR`; `ClipTransfer` moves finished clips to `$NETWORK_SHARE_DIR/captures/` on its
own thread. The share is therefore never touched by the capture or encoder threads, which is
what removes the whole class of network-stall freezes described below. It also means recording
survives a NAS outage entirely: clips accumulate locally and ship when it returns, where an
in-memory buffer would have filled and started dropping footage.

Sizing: at `H264_BITRATE` (10 Mbit/s) clip data is 1.25 MB/s, so the 300 s `max_clip_seconds`
ceiling is 375 MB. The SD card in use holds ~200 GB free — roughly 11,000 clips — so `_has_room`'s 1 GB
floor is a guard against a months-long outage, not a routine constraint. SD wear is not a
concern at this volume: ~20 clips/day is ~134 GB/year, well under one full-card write.

**Two names, and only one means "done".** The recorder writes `<clip>.h264.part` and renames to
`<clip>.h264` once the file is closed; `ClipTransfer` copies to `<clip>.h264.part` on the share
and renames within the share. Both renames are same-filesystem, so they are atomic. A reader
therefore never sees a final-named clip that is still growing. This is what stops the review
server ingesting a half-written clip and caching a thumbnail strip from footage that does not
show the visit. A cross-filesystem `shutil.move` would *not* do this — it degrades to copy then
delete, exposing the final name immediately.

**Every finished clip ships with a sidecar.** Raw H.264 carries no duration and `ClipTransfer`'s
copy drops the mtime, so once a clip reaches the share nothing says when it ended or where in the
frame the cat was. `<clip>.h264.json` records the start and end wall times, the cleaned blob that
triggered the clip, the blob on its last motion frame, and why it closed (`timeout`, `max_length`,
`shutdown`). Those are the inputs to event grouping (ROADMAP A.4). Grouping is decided later from
the sidecars rather than at capture time, because the gap threshold is set from data, and a rule
over recorded facts can be re-run when it changes. The recorder writes the sidecar *before*
promoting the clip and `ClipTransfer` copies it *before* the clip, so neither the local scan nor
the review server can see a finished clip without its facts. A clip recovered after a crash has no
sidecar; ingest treats that as `recovered` and estimates the end from size. The blob is computed
on motion frames only, so an idle scene pays nothing for it.

**A clip abandoned mid-write says so** (2026-09-16). When `_ResilientCircularOutput` gives up on a
clip, `_finish_clip` still writes the sidecar and still promotes the clip — a truncated clip beats no
clip — but the `close_reason` it records becomes `abandoned` rather than the `timeout`,
`max_length` or `shutdown` the caller asked for. The sidecar is the only record of how a clip ended,
so a truncated one that claimed `timeout` read as an ordinary visit in review, and grouping trusted
its `ended_at`, which is the moment the write failed rather than the moment the cat left.

**Clips cut off mid-write are recovered at startup.** A crash, a `kill -9` or a power cut while a
clip is open leaves `<clip>.h264.part`. Only final names ship, so that visit would be lost. The raw
stream decodes up to the cut: a copy of a real clip truncated at half its size gave 252 of its 486
frames, and ffmpeg's only complaints were two errors about the last, partial frame.

So when the recorder is constructed, before it opens a clip of its own, it renames every non-empty
`.part` in its directory to its final name, and `ClipTransfer` ships them on its next pass. Empty
ones are discarded like any empty clip. A final name that already exists is never overwritten;
the `.part` is left for a person to look at.

**After an unclean stop the camera stays busy ~10-15 s.** A `kill -9`, a crash or a power cut
gives libcamera no chance to release the sensor, so the next `Picamera2()` fails with "Camera
__init__ sequence did not complete" until the kernel reclaims it. Constructing the camera is the
first thing `main.py` does, so this is a startup failure, not a recording one. The service's
`RestartSec` waits out that window; see Process. Measured on the Pi 5, 2026-09-12.

**`_ResilientCircularOutput`.** Clip bytes are written on the **encoder's poll thread**
(`V4L2Encoder.thread_poll`), not the camera event-loop thread. That distinction matters: the
poll thread is the only place that calls `queue_item.release()` to return camera buffers and
that refills `buf_available`. Kill it and `_encode` blocks forever on the camera event loop, so
`capture_arrays` never returns — a silent, total freeze. `FileOutput._write` catches only
`ConnectionResetError`, `ConnectionRefusedError`, `BrokenPipeError` and `ValueError`, so the
subclass swallows `OSError` too and marks the clip abandoned.

It also guards `outputframe`. `CircularOutput.outputframe` appends under `_lock`, releases, then
re-acquires to `popleft`; `stop()` can drain the whole ring in that window, making `popleft`
raise `IndexError` on the poll thread. Rare per clip end, but every clip end rolls the dice.

**The share is mounted `soft`, and that is load-bearing.** `soft` is the CIFS default — it is
*not* in `/etc/fstab`. On a soft mount an unreachable server makes writes fail with an error
after ~1–2 minutes (governed by `echo_interval=60`) instead of blocking forever, which is what
lets the `OSError` handling above work at all. Writing clips locally first means a `hard` mount
would now only stall `ClipTransfer`'s thread rather than the camera, but pin `soft` explicitly
anyway: switching it looks like a robustness improvement and is the opposite.

**Draining happens off-thread.** `CircularOutput.stop()` writes the entire ring buffer with a
flush per frame, holding the output's lock throughout. Inline on the capture thread that stalls
frame distribution and picamera2's event loop for the length of the write. `_finish_clip` runs
on its own thread; handoff measures ~0.3 ms. With the destination on local disk that write is
milliseconds; to the share it is a multi-megabyte network round trip.

**`buffer_seconds = 5`.** `stop()` empties the ring buffer, so pre-motion footage is bounded by
the gap since the last clip ended regardless of buffer size. A 30 s buffer bought nothing and
made every drain ~37 MB. `.start()` flushes from the *oldest* keyframe in the buffer, not the
most recent.

**Clips are `.h264`.** `CircularOutput` writes a raw elementary stream. Naming it `.mp4` claimed
a container that was never there. They decode fine but carry no duration and cannot seek; remux
with `ffmpeg -i clip.h264 -c copy clip.mp4` if that matters.

**Bounded clip length and a free-space check.** Nothing else closes a clip except the motion
timeout, and anything that keeps triggering — a sunbeam, a moving shadow — would record without
bound at 10 Mbit/s (4.5 GB/hour). ENOSPC is not merely a failed recording; it is the uncaught
`OSError` above. `_has_room` now guards *local* disk, and is throttled to one check every 5 s:
it runs on the capture thread for every motion frame, so an unthrottled `statvfs` plus its
warning meant 30 syscalls and 30 log lines a second for as long as the disk stayed full.

**UTC is not used for clip names.** Local time is friendlier for browsing, so collisions across
the DST fall-back are handled by never returning a path that already exists.

## Detection

**MOG2 warmup.** MOG2 has no background model on its first frame and reports the whole frame as
foreground, which triggered a clip on every startup. Measured on this camera: frame 0 is 100%
foreground, frame 1 ~3.8% (still far over any threshold), frame 2 onward under 25
pixels. `warmup_frames = 30` is ~1 s of margin.

**Monotonic clock for durations.** The Pi has no RTC backup cell and timesyncd steps the clock
at boot. With `time.time()` a backward step made the elapsed difference negative and held a clip
open for the size of the jump; a forward step truncated one early.

**Known weakness: a global pixel count.** Detection compares the number of foreground pixels
anywhere in the frame against one threshold, with no spatial coherence, so scattered noise and
one cat-sized blob look the same. Shadows do not count; see Instrumentation. See ROADMAP.md
part A.

**Threshold, timeout and MOG2 history are environment settings** (`MOTION_THRESHOLD`,
`MOTION_TIMEOUT`, `MOG2_HISTORY`). A.2's data runs deliberately use a low threshold, catching too
much rather than missing a visit, and trying different values should not need a code change. The
timeout and history keep their old hard-coded values (10 s and 500 frames).

**The default threshold is 300 px** (2026-09-11, from one visit). A walking cat measures about
400–1,400 foreground px in the full-sensor view, and about 3,300 at peak in the cropped mode, so a
threshold of 5000 records no visits at all and 2000 loses the end of a visit. Idle noise in the
same view stays under 100 px in 99.9% of frames and peaks at 228, so 300 sits between the two.
It is a safety net for a run that forgets to set the variable, not a tuned value; A.2's data sets
the real one. The recorder's own default matches.

History sets how long a still cat takes to fade into the background: roughly history ÷ 30
seconds. The timeout then closes the clip. The goal allows a visit to span several clips, so
neither setting has to be stretched to cover a cat sitting still.

**A dark scene cannot trigger** (2026-09-14). The camera is the IR-filtered imx708 and, until a
light was fitted on 2026-09-16, the room was unlit at night. After a light went off, the mean grey level sat at 1.1–2.7 and sensor speckle alone
gave 200–295 foreground px on every frame. At a threshold of 150 that recorded back-to-back 300 s
clips of black, about 2 GB/h. Raising the threshold would cost daytime recall and gain nothing,
because no cat is visible in that footage anyway. So `detect_motion` ignores motion while the
analysis frame's mean grey level is below `DARK_BRIGHTNESS`. Its default of 10 sits between the
brightest frame that night (2.7) and the dimmest lit frame that evening (46.1), leaving room for a
dim night-light. MOG2 keeps learning in the dark, so its background is current when the light
returns; the light coming on is itself a whole-frame change and records one clip. Each change
between dark and lit is logged. This removes the waste, not the gap: visits in the dark go
unrecorded. The gap closed when the boxes were lit around the clock on 2026-09-16 (ROADMAP A.2);
since then nights average ~125/255 and the gate only matters if the light fails.

## Layout

**`src/` is split by which side of the share a module lives on** (2026-09-20). `src/capture`
touches the camera; `src/review` reads the share and serves the labeler; `src/clips` is what both
agree a clip is: its names, its sidecar, the `Blob` and `Frame` types. The sides share no objects;
the clip and its sidecar on the share are the whole contract, so either side can change without
the other noticing. The rule that the review side imports without `picamera2` had been a sentence
in CLAUDE.md; it is now ruff's `banned-api` (`TID251`) with a per-file allowance for
`src/capture`, so a stray import fails the lint gate rather than a machine without a camera.
`Blob` moved out of `motion_metrics` for this: the sidecar needed it, and importing it dragged the
CSV writer and the morphology kernels into the review side.

**Detection is its own class** (2026-09-20). `MotionDetector` holds MOG2, the warmup, the dark
gate and the last blob, and answers a `Detection` per frame; `MotionRecorder` takes one and turns
its answer into a clip lifecycle. Before the split every test of a threshold or a warmup count had
to fake the camera, the encoder and the ring buffer to reach the detector. The split was made
ahead of A.3, which grows exactly that half.

**Nothing under `src/` reads `Config`** (2026-09-20). `main.py` reads it and passes settings down,
for both sides; `CaptureDB` requires its grouping thresholds and the review routes take the
captures directory in a `Review` dataclass. The capture side had always worked this way, and the
review side's `Config` fallbacks were the only hidden inputs left.

**Domain types inside, ISO text at the edges** (2026-09-20). `Clip`, `Event` and `FoundClip` carry
`datetime`s, a `CloseReason` and a `Boundary` enum; SQLite and the JSON API see ISO 8601 text to
the millisecond, formatted by the one `iso()` in the sidecar module. Before this the scan
flattened the sidecar's datetimes to strings and the database parsed them back for the grouping
rule. `RECOVERED` joined `CloseReason` so a clip's reason is one type whether the recorder or
ingest assigned it.

## Process

**`main.py` degrades rather than dying.** If the share is not mounted, `mkdir` would create
`captures/` on the SD card and clips would fill the boot media until the share mounted over them.
If the recorder cannot be constructed, the web stream should still come up — it is often how you
find out something is wrong.

**Shutdown stops the producer first.** Tearing down the recorder while the capture thread is
still calling into it races `_start_saving` and can leave a zero-byte clip plus a recorder stuck
reporting "recording".

**SIGTERM takes the same path as Ctrl-C.** systemd and `kill` stop a process with SIGTERM, whose
default action ends it on the spot. Cleanup never ran, so a clip being written stayed a `.part`
file that `ClipTransfer` never ships, and that visit was lost. The handler raises `SystemExit` on
the main thread, so `monitor()`'s `finally` runs the usual shutdown. It also ignores any further
SIGTERM, so a second one cannot interrupt that cleanup.

**It runs as a systemd service** (`deploy/catdetector.service.in`, 2026-09-14), so it starts at boot
and comes back after a crash. A detector started by hand stays down after a power cut until a
person notices; one such gap cost four and a half hours of recording. Most of the unit's settings
favour recall over tidiness:

- **It wants the share's mount but does not require it.** `RequiresMountsFor=/mnt/nas` would stop
  the detector from starting whenever the NAS is down at boot. Recording does not need the share:
  clips are staged on local disk, and `ClipTransfer` refuses a path that is not a mountpoint. The
  unit is still ordered after the mount, so a share that is coming up is there for the first ingest.
- **It waits for NTP, for at most 90 s.** After a power cut, fake-hwclock rewinds the clock to its
  last hourly save and NTP corrects it about 35 s later, so a detector that started straight away
  would stamp clips and metrics with the wrong time. `systemd-time-wait-sync.service` would do the
  waiting, but its `TimeoutStartSec` is `infinity`, so with the internet down the detector would
  never start. The unit runs the same helper as an `ExecStartPre` under `timeout 90` and ignores
  its failure. The helper works unprivileged, which was checked on the Pi.
- **`RestartSec=20`**, because the camera stays busy for 10–15 s after an unclean exit, and a faster
  restart would crash-loop on "Camera __init__ sequence did not complete".
- **`Restart=on-failure`.** SIGTERM takes the clean-shutdown path and exits 0, so a
  `systemctl stop` is not undone.
- **`TimeoutStopSec=60`** covers the 30 s final clip flush and the metrics writer's 10 s join.
- **It runs the venv's Python, not `uv run`**, which can sync the environment at startup, and so
  change or fail it at boot.

The detection settings of the current data run live in a systemd drop-in
(`systemctl edit catdetector`), not in `.env` and not in the tracked unit. Putting `METRICS_CSV`
in `.env` would make the test suite's `build_recorder` calls open the real metrics file, and a
run's settings in the tracked unit would be one machine's experiment published as the default.
The tracked file is a template: `install-service.sh` fills in the user, the checkout path and the
share's mount unit, so the unit is not tied to one account or directory.

**Logging root at WARNING, application loggers at INFO.** A blanket INFO root turns on every
third-party logger; picamera2 narrates every state change and buries our output.

**The web stream binds `0.0.0.0` with no authentication.** Deliberate — it is meant to be
reachable from other devices on the LAN. Do not forward the port. See the README's Security
section.

## Instrumentation

**Shadows are excluded by thresholding, not by disabling shadow detection.** MOG2 marks shadow
pixels 127 and real foreground 255. Turning `detectShadows=False` off is a no-op for this
purpose: it relabels those pixels 255 instead of removing them, and `countNonZero` returns the
same number either way. Verified against real MOG2 — with a synthetic shadow, both settings
counted 3400 pixels, of which 1600 were shadow. Shadow detection stays on and the mask is
thresholded at 127 so only true foreground counts.

**Metrics are written off the capture thread.** `MetricsLog.record()` puts a row on a bounded
queue; a daemon thread writes and periodically flushes. When the queue fills, rows are dropped
and counted rather than blocking — the capture thread must never wait on a disk. The CSV lives
on local disk, not the share: one row per frame at 30 fps is exactly the small-write pattern
that CIFS handles worst.

**Every frame is measured once, whether or not metrics are on** (2026-09-19). `detect_motion`
decides from the same `FrameMetrics` the log receives, so the trigger and the CSV can never
disagree, and A.3's size band reads its blob from the same place. The cost of measuring on
every frame, on the Pi 5 with a synthetic 640x480 mask: 2.4 ms for a cat-sized blob or an empty
mask, 9 ms for a mask that is 1 % speckle (raw-contour extraction on the noise), against a 33 ms
frame budget. The metrics-on path already paid that and measured motion frames a second time,
and held 30.0 fps on the camera (2026-09-18), so the metrics-off path now costs at most what the
data run has been costing.

**Blobs are logged both raw and cleaned, along with frame brightness.** A.3 will trigger on the
largest blob after cleanup: a morphological open with a 3x3 ellipse drops speckle, then a close
with a 9x9 ellipse joins the pieces MOG2 splits one cat into. Logging that measurement during the
A.2 run means the size band comes from the same numbers the trigger will see. The raw blob is
still logged, so other kernel sizes can be judged later. Brightness is the mean grey level of the
analysis frame. It makes lighting changes easy to pick out, and they are the main source of
non-cat clips.

Measured on the Pi 5's CPU with a synthetic mask and no camera: cleanup plus contours costs about
1.7 ms a frame, and brightness 0.03 ms, against a 33 ms frame budget. With the camera running and
metrics on, the service holds 30.0 fps (from the metrics timestamps, 2026-09-18).

**A file with a different column layout is never appended to.** Mixing layouts would corrupt the
CSV, so `MetricsLog` leaves the old file alone, writes to a timestamped sibling and logs a warning.

## Events

**Clips are grouped into events at ingest, by one rule, from the sidecars.** `event_grouping`
holds the rule as a pure function over `ClipRow`s so it can be tested against synthetic timelines
with no database. `CaptureDB` calls it from two places: `ingest`, for every rescan, and `regroup`,
on demand. The thresholds (`EVENT_GAP_SECONDS`, `EVENT_BOX_DISTANCE_PX`) are read when the
database opens, not when the module loads, so `main.py --regroup` sees a changed setting.

**Ingest reads only the sidecars of clips it has not seen** (2026-09-16). Reading every sidecar
and letting `INSERT OR IGNORE` discard the known ones costs one CIFS round trip per clip in the
archive, at every start of either entry point — and the service restarts on its own, so that cost
lands where it hurts most: startup is time the camera is not recording. `ingest` selects the known
paths once and skips them.

**Event ids are stable across rescans.** Deep links (B.4 notifications), the UI's undo history and
the frame cache all hold ids, so `ingest` may extend an event or create one but never renumbers.
Each group keeps the lowest event id already among its clips. A clip that arrives late, held back
by a NAS outage, can bridge two existing events; they merge under the lower id. Only `regroup`
rebuilds from scratch, and then ids do change, which is why it is a deliberate command and not part
of a rescan.

**Labels follow clips, not events.** Before either placement pass, every clip remembers its
event's label. Afterwards an event keeps a label its clips agree on and drops one they contest,
returning to the review queue with a warning in the log. That is what lets `regroup` change the
gap threshold without discarding a day of labeling, and what stops a bridge merge from silently
attributing a not-cat clip to a cat.

**A clip with no sidecar is `recovered`.** It was cut off by a crash before the recorder could write
one. Its start comes from the filename and its end is estimated from size at the encoder's bit rate
(10 Mbit/s, so 1.25 MB per second). It has no centroids, and the rule lets an unknown position
match on time alone rather than strand the clip in its own event.

**The frame cache is keyed by clip id, not event id.** Clip ids never change; event ids do on a
`regroup`, and one event has several clips. Cache files are named `clip<id>_...`.

**A database from before grouping is refused, not migrated.** The only database that predated
grouping held nine test events and was carried across once by hand (2026-09-12). Migration code
for one machine's nine rows would outlive its purpose; a clear error at startup is enough.

## Review UI

**ffmpeg, not cv2.VideoCapture.** VideoCapture cannot handle the raw elementary streams the
recorder writes: on a real clip it reported a garbage frame count, failed its first read, and
could not seek. ffmpeg decodes the same files cleanly (~0.6 s for an 8-thumb strip, ~0.4 s for
one full frame on the Pi 5), so extraction shells out. Results are cached on local disk keyed by
clip id — clips are immutable once closed, so the cache never needs invalidating.

**Cache entries are published by rename** (2026-09-16). Flask serves the review UI threaded, and
ffmpeg creates and truncates its output before it writes anything, so a check-then-write on the
final cache path has two failure modes: a second request for the same clip can see the file exist
and be served a truncated MP4 or JPEG, and two requests can run ffmpeg against one path and leave
a corrupt entry cached for good, since nothing invalidates the cache. Each artifact is written
to a unique `<name>.<hex>.part.<ext>` beside it and renamed into place; the rename is atomic within
the cache directory, and the real extension stays last because ffmpeg and cv2 choose the format
from it.

**Tiles span the whole clip** (2026-09-17). A strip of the first eight seconds — pre-roll plus
the trigger — is not enough for cat/not-cat: in one 96 s clip a cat used the left box for eighty
seconds, and eight early tiles showed one paw at the top of the last frame, so the reviewer
labelled it `not_cat`. Cut that way, every visit also looks like "a cat enters and never leaves",
because the exit is minutes past the last tile. The strip covers the file: one tile per second up
to `MAX_TILES` (48), then every 2, 3, … seconds so the count stays bounded (`tile_seconds`).

**The stride comes from the footage, not the sidecar.** The sidecar's `started_at → ended_at` is
the trigger-to-close span, not the file's: the file starts up to `buffer_seconds` earlier, because
`CircularOutput.start()` flushes from the oldest keyframe in the ring (that 96 s clip has 101
keyframes for a 95.9 s sidecar), so a stride sized from the sidecar caps the strip short of the
exit. The build dumps every keyframe, counts them, and thins in Python; nothing about the clip's
length is needed up front.
Consequences: the stride is only known once the clip is decoded, so the cache name carries it
(`clip<id>_strip<stride>s.jpg`, found by glob) and the caption is drawn **into** each tile rather
than sent to the page. A caption is the offset into the file, the same clock the player shows,
not seconds since the trigger.

**One decode serves both the strip and the full frames.** The keyframes are dumped at full size
(`-q:v 3`), the thumbs are resized from those, and the full frames behind the tiles are kept as
`clip<id>_t<second>s.jpg` — ≤ 48 per clip, ~4 MB — so a click on a tile is a cache hit rather than
a second read of a 300 MB clip over CIFS (measured 12 s for a late tile of the largest clip). The
frames are published before the strip, so the strip's existence means the set is complete, and
`frame()` answers from the cache alone: a slot with no file is out of range.

**Extraction decodes keyframes only.** Decoding all of a 96 s clip takes 9.5 s on the Pi 5
(~320 fps); with `-skip_frame nokey` it takes 1.7 s, and a five-minute clip fits comfortably inside
the 60 s ffmpeg timeout even when read from the share. That works because the recorder's encoder
writes a keyframe every 30 frames — libav's default on the Pi 5, now pinned with
`iperiod=CAMERA_FPS` so a picamera2 upgrade cannot silently change the tile spacing. ffmpeg
renumbers the surviving frames, so `n` counts keyframes, i.e. seconds; a raw stream has no
timestamps to select on instead, which is why the tests build their fixture with libx264 at
`keyint=30` rather than through cv2, whose writer sets its own GOP.

**The share is read outside the database lock** (2026-09-18). `serialized` exists to stop
concurrent reads corrupting SQLite's cursors. Under it, `ingest` — which globs the share and reads
every new sidecar over CIFS, up to a couple of minutes per failing operation on a soft mount —
would hold every reader of the review UI behind it. So `ingest` collects paths, sizes and sidecar
facts with no lock held and takes it only to insert, place and commit;
`INSERT OR IGNORE` covers a clip that another ingest registered in between. The share-reading
half is its own module, `clip_scan.py`, with no lock in it (2026-09-19): the rule is then a
property of the import graph rather than of one method's care.

**Single-page UI.** Labeling navigates no pages — next event and images are fetched and swapped
in place — so the undo history can live in a JS array rather than sessionStorage, and labeling
is one keypress with no page load between events.

**The label table is separate from event** (per the roadmap): model predictions and human labels
can later coexist via the `source` column without overwriting each other.

**Poop counts hang off a clip, not an event** (2026-09-16, ROADMAP B.5b). Whoever scoops signals
the count by hand over the box; the reviewer reads it out of the cleaning clip and types one
number per box. Events are renumbered by
`regroup`, so counts keyed by event id would be stranded by a threshold change; clip ids never
change. They are written against the event's first clip and read back across all of its clips, the
same shape that lets labels follow clips. The box is a **position in frame, 1 to 3 left to right**,
not a box identity: nothing in the pipeline knows one box from another, and the signal is made over
the box concerned.

**One app, one port, two tabs** (2026-09-12). `web_app.create_app` takes the streamer and the
review parts as optionals and serves whatever it was given, and a review database that fails to
open leaves the Live tab up rather than taking the detector down, in the same spirit as a broken
recorder leaving the stream up. Until 2026-09-20 a second entry point, `review.py`, passed no
streamer and served the labeler on the same port while the detector was off; it was dropped once
the detector ran as a service around the clock, because the tab was always up anyway and two
scripts that could not run together were one more thing to explain. `main.py --regroup` took
over the one job it still had. The cost is that a machine with no camera cannot run the labeler;
the review package still imports without the camera stack, so that could be restored. The page is a Jinja template
rather than a string in a Python module: ruff's line limit applies inside strings, and JavaScript
wrapped to satisfy it is unreadable.

**The MJPEG stream is torn down when its tab is hidden.** Each viewer of `/video_feed` costs a
JPEG encode of every frame on the server, so the Live tab sets the image source when it is shown
and removes it when it is not, which closes the connection and ends the generator.

**The page never hides the grouping** (2026-09-12). Every clip of an event
is rendered with its own strip, times, close reason and the gap since the previous clip; a
multi-clip event says so in the header; the counter reports `multi_clip`; and `?filter=multi`
walks every multi-clip event, labeled or not, so the grouping rule's output can be audited as a
whole rather than noticed one event at a time.

**The multi-clip audit walks time order, not the multi-clip list** (2026-09-16). `next_multi` took
the position of `after_id` within the multi-clip events, so an id that had stopped being multi-clip
— exactly what a split does to the event the reviewer is looking at — fell through to the first
entry, and the audit silently restarted at the oldest event instead of advancing. It now takes the
position in the full time order, which a split leaves intact, and returns the next event from there
that still has several clips.

**Split and join are overrides on clips, not edits to events.** A split marks the clip that begins
the new visit (`boundary = 'split'`); a join marks the first clip of the later event with the path
of the clip it continues (`boundary = 'join'`, `joins = <path>`). Both then re-run the ordinary
placement, so the rule and the overrides always agree and `regroup` reproduces the reviewer's
decisions. The split's later half starts unlabeled, because it is a visit the reviewer has not
judged; the first half keeps its id and label. A join names its target clip rather than "the
previous event" so that interleaved boxes cannot make it land on the wrong visit.

**Video is remuxed on demand, in the review server, never on the capture path.** `-c copy` into
MP4 costs ~0.7 s for an 18 MB clip on the Pi 5 and no CPU worth mentioning; the result is cached
like the strips. Doing it at clip close would put ffmpeg on the Pi's capture side for every clip
whether or not anyone ever watches it. The `<video>` element uses `preload="none"` so opening an
event does not fetch every clip. Flask's `send_file` honours `Range`, which is what lets the
player seek; the test asserts a 206, since `Accept-Ranges` is advertised either way.
