# Capture decisions: frames, motion and clips

The decisions behind [Capturing frames](../DESIGN.md#capturing-frames), [Detecting
motion](../DESIGN.md#detecting-motion) and [Recording and shipping a
clip](../DESIGN.md#recording-and-shipping-a-clip) in [DESIGN.md](../DESIGN.md), each with the
measurement, the failure or the constraint that settled it, and when. Start from the summary there;
read the decision here before changing what it covers. Words with a meaning of their own are in the
[glossary](../DESIGN.md#glossary).

## Contents

- [Capturing frames](#capturing-frames)
  - [One `Picamera2` per process](#one-picamera2-per-process)
  - [`capture_arrays(["main", "lores"])`, never two `capture_array()` calls](#capture_arraysmain-lores-never-two-capture_array-calls)
  - [No sleep in the capture loop](#no-sleep-in-the-capture-loop)
  - [The sensor mode is pinned to the full array](#the-sensor-mode-is-pinned-to-the-full-array)
- [Detecting motion](#detecting-motion)
  - [MOG2 warmup](#mog2-warmup)
  - [Shadows are excluded by thresholding, not by disabling shadow detection](#shadows-are-excluded-by-thresholding-not-by-disabling-shadow-detection)
  - [Every frame is measured once, whether or not metrics are on (2026-09-19)](#every-frame-is-measured-once-whether-or-not-metrics-are-on-2026-09-19)
  - [Blobs are logged both raw and cleaned, along with frame brightness](#blobs-are-logged-both-raw-and-cleaned-along-with-frame-brightness)
  - [Known weakness: a global pixel count](#known-weakness-a-global-pixel-count)
  - [A data run uses a low threshold (ROADMAP A.2)](#a-data-run-uses-a-low-threshold-roadmap-a2)
  - [The default threshold is 300 px (2026-09-11, from one visit)](#the-default-threshold-is-300-px-2026-09-11-from-one-visit)
  - [A dark scene cannot trigger (2026-09-14)](#a-dark-scene-cannot-trigger-2026-09-14)
  - [A file with a different column layout is never appended to](#a-file-with-a-different-column-layout-is-never-appended-to)
- [Recording and shipping a clip](#recording-and-shipping-a-clip)
  - [Two names, and only one means "done"](#two-names-and-only-one-means-done)
  - [Every finished clip ships with a sidecar](#every-finished-clip-ships-with-a-sidecar)
  - [A clip abandoned mid-write says so (2026-09-16)](#a-clip-abandoned-mid-write-says-so-2026-09-16)
  - [Clips cut off mid-write are recovered at startup](#clips-cut-off-mid-write-are-recovered-at-startup)
  - [`buffer_seconds = 5`](#buffer_seconds--5)
  - [Clips are `.h264`](#clips-are-h264)
  - [Bounded clip length and a free-space check](#bounded-clip-length-and-a-free-space-check)
  - [A clip that cannot open is logged at most once a minute (2026-09-27)](#a-clip-that-cannot-open-is-logged-at-most-once-a-minute-2026-09-27)
  - [Monotonic clock for durations](#monotonic-clock-for-durations)
  - [UTC is not used for clip names](#utc-is-not-used-for-clip-names)
  - [Mount the share `soft`, and say so](#mount-the-share-soft-and-say-so)
  - [`ClipTransfer` writes only to a mounted share](#cliptransfer-writes-only-to-a-mounted-share)

## Capturing frames

### One `Picamera2` per process

picamera2 allows only one instance, so `CameraManager` owns it
and everything else borrows via `get_camera()`.

### `capture_arrays(["main", "lores"])`, never two `capture_array()` calls

Each `capture_array`
waits for and consumes its own libcamera request. Two calls therefore cost two frame periods and
return frames from different exposures ~33 ms apart. Measured on the imx708 (2026-08-25): 15.01
fps for two calls, 30.00 fps for one `capture_arrays`. It returns `(arrays, metadata)`, not a bare
list.

### No sleep in the capture loop

`capture_arrays` blocks until the next frame, which paces the
loop at the sensor rate. An added sleep is pure loss: a `sleep(0.033)` on top of two blocking
captures runs the loop at ~10 fps (2026-08-25).

### The sensor mode is pinned to the full array

picamera2 picks a sensor mode by output size.
For these streams it chose 1536x864, a 2x2 binning of a 3072x1728 crop, which keeps only the
central two thirds of the imx708's width. Asking for 2304x1296, the whole array binned 2x2, gives
the widest view; an installation may widen or crop it to keep its boxes in frame. Tested at
2304x1296 (2026-09-10): the detector held 30 fps.

Pixel measurements depend on the mode. In the full-array mode an object spans about two thirds as
many pixels in each direction as in the cropped one, so roughly 0.44 of the area. Metrics from one
mode cannot be compared with another's, so settle the mode and the framing before recording the
metrics a threshold is tuned from.

## Detecting motion

With `METRICS_CSV` set, each frame becomes a row: here an empty scene, then the frame where the
cat's head came in. `blob_*` and `x`…`h` are the raw blob, `clean_*` the cleaned one, `cx`, `cy`
the raw blob's centre, and an empty cell means no blob:

```csv
timestamp,foreground_px,blob_area,x,y,w,h,cx,cy,recording,clean_area,clean_x,clean_y,clean_w,clean_h,brightness
2026-09-12T08:04:12.988,0,0,,,,,,,0,0,,,,,118.4
2026-09-12T08:04:13.021,1653,1102,304,0,52,31,330,15,0,1416,301,0,59,36,118.2
```

### MOG2 warmup

MOG2 has no background model on its first frame and reports the whole frame as
foreground, which triggered a clip on every startup. Measured on the imx708 (2026-08-24, in the
cropped sensor mode): frame 0 is 100% foreground, frame 1 ~3.8% (still far over any threshold),
frame 2 onward under 25 pixels. `warmup_frames = 30` is ~1 s of margin.

### Shadows are excluded by thresholding, not by disabling shadow detection

MOG2 marks shadow
pixels 127 and real foreground 255. Turning `detectShadows=False` off is a no-op for this
purpose: it relabels those pixels 255 instead of removing them, and `countNonZero` returns the
same number either way. Verified against real MOG2 (2026-08-26) — with a synthetic shadow, both
settings counted 3400 pixels, of which 1600 were shadow. Shadow detection stays on and the mask is
thresholded at 127 so only true foreground counts.

### Every frame is measured once, whether or not metrics are on (2026-09-19)

`detect_motion`
decides from the same `FrameMetrics` the log receives, so the trigger and the CSV can never
disagree, and A.3's size band reads its blob from the same place. The whole measurement, on the
Pi 5 with a synthetic 640x480 mask (2026-09-19): 2.4 ms for a cat-sized blob or an empty mask,
9 ms for a mask that is 1 % speckle (raw-contour extraction on the noise), against a 33 ms frame
budget. With the camera running and metrics on, the service held 30.0 fps (from the metrics
timestamps, 2026-09-18), while that path measured motion frames a second time, so measuring every
frame costs no more than a metrics-on run.

### Blobs are logged both raw and cleaned, along with frame brightness

Cleanup is a
morphological open with a 3x3 ellipse, which drops speckle, then a close with a 9x9 ellipse,
which joins the pieces MOG2 splits one cat into. A.3's trigger is planned on the largest blob
after cleanup, so logging it during the A.2 run gives the size band the same numbers the trigger
sees. The raw blob is logged too, so other kernel sizes can be judged from the same data.
Brightness is the mean grey level of the analysis frame. It makes lighting changes easy to pick
out, and they are the main source of non-cat clips. Measured alone on a Pi 5's CPU with a
synthetic mask (2026-09-11), cleanup plus contours cost about 1.7 ms a frame and brightness
0.03 ms; the 2.4 ms above is the whole per-frame measurement that contains them.

### Known weakness: a global pixel count

Detection compares the number of foreground pixels
anywhere in the frame against one threshold, with no spatial coherence, so scattered noise and
one cat-sized blob look the same. Shadows do not count; see Shadows above. See ROADMAP.md
part A.

### A data run uses a low threshold (ROADMAP A.2)

A data run uses a low threshold (ROADMAP A.2), catching too much rather than missing a
visit, and trying another value needs no code change. `MOTION_TIMEOUT` defaults to 10 s and
`MOG2_HISTORY` to 500 frames.

### The default threshold is 300 px (2026-09-11, from one visit)

A walking
cat measures about 400–1,400 foreground px in the full-sensor view, and about 3,300 at peak in the
cropped mode, so a threshold of 5000 records no visits at all and 2000 loses the end of a visit.
Idle noise in the same view stays under 100 px in 99.9% of frames and peaks at 228, so 300 sits
between the two. It is a safety net for a run that forgets to set the variable, not a tuned value;
the tuned value is A.2's to derive (ROADMAP C.4 covers other installations). The default lives in
`config.py` alone: `MotionDetector` requires `motion_threshold` as an argument.

History sets how long a still cat takes to fade into the background: roughly history ÷ 30
seconds. The timeout then closes the clip. The goal allows a visit to span several clips, so
neither setting has to be stretched to cover a cat sitting still.

### A dark scene cannot trigger (2026-09-14)

The camera is the IR-filtered imx708, so an unlit
room is black to it. Unlit, after a light went off, the mean grey
level sat at 1.1–2.7 and sensor speckle alone gave 200–295 foreground px on every frame. At a
threshold of 150 that recorded back-to-back 300 s clips of black, about 2 GB/h. Raising the threshold would cost daytime recall and gain nothing,
because no cat is visible in that footage anyway. So `detect_motion` ignores motion while the
analysis frame's mean grey level is below `DARK_BRIGHTNESS`. Its default of 10 sits between the
brightest frame that night (2.7) and the dimmest lit frame that evening (46.1), leaving room for a
dim night-light. MOG2 keeps learning in the dark, so its background is current when the light
returns; the light coming on is itself a whole-frame change and records one clip. Each change
between dark and lit is logged. This removes the waste, not the gap: visits in the dark go
unrecorded, which is why light around the clock is a requirement (ROADMAP C.2). A lit night
averaged ~125/255 (2026-09-20), so the gate matters only if the light fails.

### A file with a different column layout is never appended to

Mixing layouts would corrupt the
CSV, so `MetricsLog` leaves the old file alone, writes to a timestamped sibling and logs a warning.

## Recording and shipping a clip

A sidecar, as written for a clip that a cat's head started at the top edge of the frame. Blob
coordinates are in the 640x480 analysis frame, and `cx`, `cy` is the box's centre:

```json
{
 "started_at": "2026-09-12T08:04:13.120",
 "ended_at": "2026-09-12T08:05:02.480",
 "close_reason": "timeout",
 "trigger_blob": {
  "area": 1416,
  "x": 301,
  "y": 0,
  "w": 59,
  "h": 36,
  "cx": 330,
  "cy": 18
 },
 "last_blob": {
  "area": 2930,
  "x": 286,
  "y": 208,
  "w": 71,
  "h": 58,
  "cx": 321,
  "cy": 237
 }
}
```

### Two names, and only one means "done"

The recorder writes `<clip>.h264.part` and renames to
`<clip>.h264` once the file is closed; `ClipTransfer` copies to `<clip>.h264.part` on the share
and renames within the share. Both renames are same-filesystem, so they are atomic. A reader
therefore never sees a final-named clip that is still growing. This is what stops the review
server ingesting a half-written clip and caching a thumbnail strip from footage that does not
show the visit. A cross-filesystem `shutil.move` would *not* do this — it degrades to copy then
delete, exposing the final name immediately.

### Every finished clip ships with a sidecar

Raw H.264 carries no duration and `ClipTransfer`'s
copy drops the mtime, so once a clip reaches the share nothing says when it ended or where in the
frame the cat was. `<clip>.h264.json` records the start and end wall times, the cleaned blob that
triggered the clip, the blob on its last motion frame, and why it closed (`timeout`, `max_length`,
`shutdown`). Those are the inputs to event grouping (ROADMAP A.4). Grouping is decided later from
the sidecars rather than at capture time, because the gap threshold is set from data, and a rule
over recorded facts can be re-run when it changes. The recorder writes the sidecar *before*
promoting the clip and `ClipTransfer` copies it *before* the clip, so neither the local scan nor
the review server can see a finished clip without its facts. A clip recovered after a crash has no
sidecar; ingest treats that as `recovered` and estimates the end from size. The blobs come from
the per-frame measurement described under Detecting motion, so recording them costs nothing extra.

### A clip abandoned mid-write says so (2026-09-16)

When `_ResilientCircularOutput` gives up on a
clip, `_finish_clip` still writes the sidecar and still promotes the clip — a truncated clip beats no
clip — but the `close_reason` it records becomes `abandoned` rather than the `timeout`,
`max_length` or `shutdown` the caller asked for. The sidecar is the only record of how a clip ended,
so a truncated one that claimed `timeout` read as an ordinary visit in review, and grouping trusted
its `ended_at`, which is the moment the write failed rather than the moment the cat left.

### Clips cut off mid-write are recovered at startup

A crash, a `kill -9` or a power cut while a
clip is open leaves `<clip>.h264.part`. Only final names ship, so that visit would be lost. The raw
stream decodes up to the cut: a copy of a real clip truncated at half its size gave 252 of its 486
frames, and ffmpeg's only complaints were two errors about the last, partial frame (2026-09-11).

So when the recorder is constructed, before it opens a clip of its own, it renames every non-empty
`.part` in its directory to its final name, and `ClipTransfer` ships them on its next pass. Empty
ones are discarded like any empty clip. A final name that already exists is never overwritten;
the `.part` is left for a person to look at.

### `buffer_seconds = 5`

`stop()` empties the ring buffer, so pre-motion footage is bounded by
the gap since the last clip ended regardless of buffer size. A 30 s buffer buys nothing and
makes every drain ~37 MB (2026-08-25). `.start()` flushes from the *oldest* keyframe in the
buffer, not the most recent.

### Clips are `.h264`

`CircularOutput` writes a raw elementary stream. Naming it `.mp4` claimed
a container that was never there. They decode fine but carry no duration and cannot seek; remux
with `ffmpeg -i clip.h264 -c copy clip.mp4` if that matters.

### Bounded clip length and a free-space check

Nothing else closes a clip except the motion
timeout, and anything that keeps triggering — a sunbeam, a moving shadow — would record without
bound at `H264_BITRATE` (4.5 GB/hour; see [the clip cache's sizing](system.md#the-local-clip-cache-is-sized-for-a-long-outage-of-the-share)). ENOSPC is not merely a failed
recording; it is the `OSError` of [the thread that writes clip bytes](system.md#nothing-may-raise-out-of-the-thread-that-writes-clip-bytes-2026-09-26). `_has_room` guards the *local* clip directory, throttled to
one check every 5 s: it runs on the capture thread for every motion frame, so an unthrottled
`statvfs` plus its warning means 30 syscalls and 30 log lines a second for as long as the disk
stays full.

### A clip that cannot open is logged at most once a minute (2026-09-27)

Motion retries
`_start_saving` on every frame, so an open that keeps failing (a clip directory that is not
writable) logged a full traceback 30 times a second for as long as the motion lasted. Now a failure
logs its traceback only if none was logged in the last minute, and the first open that succeeds
says so. Once per failure streak is not enough: a streak ends only when an open succeeds, so a
failure that lasted days would have left every missed visit after the first unlogged. A missing
directory is caught earlier, by `_has_room`, which is already throttled.

### Monotonic clock for durations

A Pi without an RTC backup cell boots with a stale clock that
timesyncd then steps. With `time.time()` a backward step makes the
elapsed difference negative and holds a clip open for the size of the jump; a forward step
truncates one early.

### UTC is not used for clip names

Local time is friendlier for browsing, so collisions across
the DST fall-back are handled by never returning a path that already exists.

### Mount the share `soft`, and say so

`soft` is the CIFS default, so an fstab line that does
not name it gets it anyway. On a soft mount an unreachable server makes writes fail with an error
after ~1–2 minutes (governed by `echo_interval=60`) instead of blocking forever, which is what
lets `ClipTransfer`'s `OSError` handling work at all. With clips written locally first, a `hard`
mount stalls only `ClipTransfer`'s thread rather than the camera, but pin `soft` explicitly
anyway: switching it looks like a robustness improvement and is the opposite.

### `ClipTransfer` writes only to a mounted share

`_share_is_ready` checks
`os.path.ismount` before every pass. Writing under an absent mountpoint would put `captures/` on
the SD card, filling the boot media, and the share would hide those clips the moment it mounted
over them. Clips wait on local disk instead and ship when the mount returns.
