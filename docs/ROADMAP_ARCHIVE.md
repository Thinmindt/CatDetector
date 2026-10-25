# Roadmap: done and superseded

What `ROADMAP.md` no longer needs to say: finished items, the history behind decisions, and
measurements that fed a decision now made. Entries sit under the same section letters as the
roadmap, so a citation such as "A.2" or "B.5b" in `DESIGN.md` or a commit message finds its
history here and its current plan there. Text is moved verbatim, with the date it moved; append
new moves at the end of their section.

---

# Front matter

Moved 2026-09-26.

**Hardware/platform facts** (measured 2026-08-24): Raspberry Pi 5, no Hailo accelerator, so any
*on-Pi* inference would be CPU. Local disk is ext4, and the share is **CIFS** — this matters for
storage decisions in B2 [B.2]. On a Jetson, JetPack pins its own Python (3.10 on JetPack 6) and
PyTorch comes from NVIDIA's wheels, so a training environment there is separate from this repo's
`==3.13.*` pin and should not try to share it.

Status: written 2026-08-24, after fixing clip saving and the startup false trigger.

Moved 2026-09-26: the reasoning behind the hardware split. The roadmap keeps the split as one
installation's choice; C.7 makes training hardware a choice for any installation.

**Hardware split (decided 2026-08-24).** The **Pi stays the capture box** and the **Jetson does
training and inference**. The Pi's camera stack is the reason: `picamera2`/`libcamera` are
Raspberry Pi-specific (the `rpi/pisp` pipeline handler and `rp1-cfe` driver), and Jetson uses
NVIDIA's Argus stack instead — porting means rewriting `camera_manager.py` and the encoder path
in `motion_recorder.py`, i.e. every line that touches hardware, and likely a different camera
module too. Nothing is gained for capture. Everything downstream of a JPEG crop is portable, and
CUDA turns "fine-tune overnight" into minutes. So crops and labels move to the Jetson; frames
never do.

Consequence for B.5: training is off-device by design, and the crop/label store must be readable
from the training machine — the share is the natural handoff point.

---

# Part A — trigger reliably

## A.0 Where detection stood

Moved 2026-09-26. The detector was split into `MotionDetector` on 2026-09-20; the current state
is A.0 in the roadmap.

`MotionRecorder.detect_motion` does:

```python
gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)  # 640x480 lores stream
fg_mask = self.background_subtractor.apply(gray)  # MOG2
return cv2.countNonZero(fg_mask) > self.motion_threshold  # default 300
```

One number — how many pixels changed anywhere in the frame — against one threshold.

Measured MOG2 defaults on this camera:

| parameter        | value | consequence                                          |
|------------------|-------|------------------------------------------------------|
| `detectShadows`  | True  | shadow pixels are written as **127**                  |
| `shadowValue`    | 127   | nonzero, so `countNonZero` counts shadows as motion   |
| `history`        | 500   | ~16.7 s at 30 fps before something becomes background |
| `varThreshold`   | 16.0  | sensitivity of the per-pixel model                    |

## A.1 Why the first detector would not hit the goal

Moved 2026-09-26. Items 1 and 2 are fixed; 3–5 carry on in the roadmap's A.1.

1. **Shadows count as motion.** `detectShadows=True` marks shadows 127 and `countNonZero`
   counts every nonzero pixel, so the cat's own shadow and any light change inflate the
   count. Fix with `detectShadows=False`, or threshold the mask to 255 first
   (`cv2.threshold(mask, 254, 255, cv2.THRESH_BINARY)`). This is a bug, not a tuning knob,
   and it pollutes every measurement below — fix it first.
2. **The default threshold may be larger than a cat.** 5000 px is 1.63% of the 640x480
   lores frame. If the camera covers roughly 3 m x 2.25 m, a cat from above is on the order
   of 1.5% — *below* the trigger. That estimate rests on a guessed mounting height and may
   be well off, which is the point: **nobody has measured what a cat is worth in pixels
   here**, so every threshold is a guess.
   **Confirmed 2026-09-11:** a walking cat measured about 400–1,400 px in the full-sensor view,
   so the default was lowered to 300. See A.2's first look.
3. **No spatial coherence.** 5000 px of scattered noise and one solid 5000 px cat are
   identical to `countNonZero`.
4. **A stationary cat disappears.** MOG2 absorbs anything static over `history` (~17 s). A
   cat that settles in the box stops being motion and recording stops mid-visit — which for
   a litter box is precisely the interesting part.
5. **Global illumination changes trigger everything.**

## A.2 Measure before tuning

Moved 2026-09-26: the two finished items, and the data-run entry as it stood that day. Its stop
rule carries on in the roadmap.

- [x] **Fix the shadow bug first, so measurements are clean.** Done 2026-08-26. Note the
      obvious fix is a no-op: `detectShadows=False` relabels shadows 255 rather than removing
      them, so `countNonZero` is unchanged. Shadow detection stays on and the mask is
      thresholded so only 255 counts.
- [x] **Instrumentation mode.** Done 2026-08-26. `METRICS_CSV=<path>` enables it; rows are
      timestamp, foreground px, largest contour area, bbox, centroid, recording flag. Written
      off the capture thread via a bounded queue, dropping rather than blocking. Measured cost
      on the Pi: none — 30.1 fps with and without.
- [ ] **NEXT: run a day or two of real traffic** with the threshold deliberately low,
      over-capturing rather than missing. Nothing below can be chosen until this data exists.
      **Running since 2026-09-14 17:46**, as the `catdetector` service. Settings are
      `MOTION_THRESHOLD=150` (the smallest cat so far was ~400 px walking in; daytime idle noise
      passes 150 on 0.017% of frames and never passed 228) and `METRICS_CSV` set, so a visit
      below the threshold still leaves a trace.
      **The camera is blind at night (measured 2026-09-14).** It is the standard IR-filtered
      Camera Module 3: libcamera reports `imx708`, not `imx708_noir`, and daylight colour is
      neutral. A light was switched off at 20:15 (brightness 45.6 to 3.5 in one frame); after that
      mean brightness sat at ~1.2/255 and sensor speckle alone gave 200–295 foreground px on every
      frame. From 21:24, at a threshold of 150, it recorded continuously as back-to-back 300 s black
      clips, about 2 GB/h. No threshold recovers a
      cat that the camera cannot see: night recall needs light, either a constant dim visible
      light (keeps colour for the coat-colour baseline in B.5) or a NoIR module plus an IR
      illuminator (monochrome at night). A light that switches on with motion would be worse,
      since the pre-roll stays dark and the switch-on changes the whole frame. Motion is ignored
      while mean brightness is below `DARK_BRIGHTNESS` (default 10; see DESIGN.md), which stops the
      black clips but not the gap. **Lighting the boxes around the clock (from 2026-09-16) closed
      it:** nights since 09-18 averaged ~125/255 in the metrics CSV and recorded like days, so the
      gap stays closed as long as the light stays on. The loop was
      confirmed at 30.0 fps on hardware (2026-09-12) with `METRICS_CSV` on and recording, all 16
      columns written, so the cleaned-blob and brightness work carries no measurable cost.

      First cat of the threshold-150 run (06:11 on 2026-09-15, seconds after the room light came
      on): recorded in full across two clips 1 s apart, which A.4 grouping should join. While
      moving it reached 1,000–11,000 foreground px and cleaned blobs of 500–9,000 px, far above the
      400–1,400 px scaled from the old cropped view. Sitting still in the box it fell to 47–130 px,
      under the threshold, so the first clip timed out with the cat still there and the second began
      when it moved. So the still phase, not the entrance, is what dips below 150.

      **Day 1 of the run (2026-09-15, lit hours only).** 22 clips: 4 cat visits (06:11 across two
      clips, 15:14, 15:38, 15:48), 2 of a person scooping, the rest speckle or moving light — about
      one false clip an hour in daylight, five of them in the 17:00 hour as the evening sun crossed
      the floor. Every visit was recorded from its first frame, and no lit frame over the threshold
      failed to start a clip. The 15:48 visit crossed from one box to another inside a *single*
      clip — an instance of A.4's known limit, since grouping places a clip by one trigger
      position and cannot split one clip between boxes. 30.0 fps all day, no restarts.

      **That evening and overnight.** 11 more clips between 21:37 and 23:17, most of them one cat
      in stays of one to three minutes at a box no daytime visit had used, so every box was now
      attested. Two cats are clearly distinguishable by coat, which is the B.5 premise holding
      up. **The dark suppression got its first real test:** the room light went off during the 23:16:53 clip, the recorder logged "Scene is dark;
      ignoring motion" at 23:16:55 and recorded nothing until morning. The share grew ~1 GB
      overnight, against 4.6 GB in the two hours of black clips the night before the change.

      **When this run has enough data** (decided 2026-09-15). The trigger is tuned to
      the weakest visit, not the average one. The two visits on 2026-09-15 ran 3,000–11,000 px while
      moving — twenty to seventy times the threshold — but 325 px on the entry frame (15:14) and
      47–130 px while the cat stood still (06:11). Entry and stillness are therefore the binding
      cases, and both are rare events, so the unit to collect is **visits, not hours**. At the
      5–10 visits a day expected then, that is roughly a week of the lit setup.
      Stop when all of these hold:
      - **50+ labelled visits, 100 preferred.** Zero misses in 50 only bounds the true miss rate at
        ~6% (95%); at 100 it is ~3%. Recall is the whole point, so prefer 100.
      - **10+ of them at night under the LED.** More sensor gain, more noise, less contrast: a
        different regime, not a sample of the same one.
      - **15+ per cat.** A dark cat on dark litter gives a weaker signal than a pale one; the two
        cats seen on 2026-09-15 already look different.
      - **Every box used.** A far box covers fewer pixels and may read weaker.
      - **Several days of varied weather, including one overcast.** Sunlight crossing the floor is
        the main daytime false trigger.
      - **The smallest entry blob has stopped dropping over the last ~20 visits.** The real
        convergence test: tuning follows the weakest case, so keep going while it keeps getting
        weaker.

      Two things are not the bottleneck. False positives are measurable from hours rather than
      visits, and any candidate rule can be replayed against the metrics CSV without re-recording
      anything. Storage is ~130 MB/day of metrics plus clips, small against the free disk. The
      bottleneck is **labelling**, which is one person's keystrokes: ~10 clips a day has to be kept
      up with, or the backlog outlives the run.

      First look (2026-09-11). This is one cat visit, so it's a sanity check, not a threshold:
      - The one visit (08:04) peaked at 3,264 foreground px and a 2,624 px blob in the old view,
        which is about 1,400 and 1,150 px in today's wide view. **The code's default threshold of
        5000 would miss every visit in the wide view, and 2000 would have missed this one.** The
        default is now 300, but data runs should still set `MOTION_THRESHOLD` explicitly.
      - At 2000 the visit split into two clips 20 s apart. Recording began 8 s after the cat
        appeared; the 5 s pre-roll covers most of that. The cat's last ~6 s of movement, at
        around 1,200 px, went unrecorded. It is a live example of A.4.
      - Idle noise in the wide view is tiny: 99.9% of idle frames are under 100 px, and no idle
        blob in the last run was larger than 1 px. That run's three non-cat clips were pure speckle
        (largest blob 0 px), which the new cleaned-blob measure removes.
      - Lighting can outsize a cat: the 10:44 clip's blob reached 2,791 px in the wide view. Blob
        size alone won't separate them, but brightness should help.
      - No motion went unrecorded for want of a trigger. Every frame over its run's threshold
        either started a clip or was already in one.


Moved 2026-10-03: done.

- [x] **Calibrate with one command, `main.py --calibrate`.** Every new mounting needs its own
      data run, so the method above is a command anyone reruns, not a script for one Pi. It reads
      `captures.db` and the metrics CSV, from the run's start (`--since`), and prints the table
      above with the counts so far (the weather is the reviewer's call) and how many events it
      left out. Once every measured condition holds it prints the settings, each replayed over
      the CSV with the visits it would miss (none, or it says so) and its false triggers per lit
      hour: `MOTION_THRESHOLD` for today's global-count trigger, below every visit's largest
      foreground count, and A.3's band (`min_cat_area`, `max_cat_area`), its lower edge below the
      weakest visit's largest blob and its upper edge above the largest, which a cat leaping in
      or out reaches. It stages the events that need a person's eye, each once and with the
      question: the same cat seconds apart (one visit split in two?), the weakest visits (a
      peek?), and `not_cat` events holding a visit-sized blob (a cat after all?), so calibrating
      happens in the review queue like any labelling. The README's "Calibrating an installation"
      walks a new installation through it: the data run, labelling, `--calibrate` until it
      reports done, then the drop-in.
      **Done means** the command prints the table, the settings and their replay, stages each
      doubt once, CI runs it on a small synthetic database and CSV whose answer is known, and the
      README section exists.
      Done 2026-10-03: as specified, plus `--boxes` for fewer than three boxes. Two weeks of CSV
      (47 million rows) take about six minutes on a Pi 5.

- [x] **Measure boxes by where the cat stays, and visits by how much is recorded.** A visit's box
      was the third holding its first clip's trigger, which is mostly the cat's head entering at
      the frame's top edge: on one run (2026-10-03) that gave the right box 4 visits, while the
      blob stayed 10 s or more in the right third in 38, and 66 of 126 visits stayed in more than
      one box. `--calibrate` counts boxes by that dwell and reports each box's weakest visit. Its
      replay also reports how much of each visit a rule records (each clip's span with the 5 s
      pre-roll), since a threshold that starts and holds clips alike lets them close while a cat
      squats: at 2876 the median visit was 84 % recorded, against 100 % at the run's 150. It
      replays a sweep of thresholds up from the run's and proposes the highest that misses no visit
      and keeps the median visit 98 % recorded, and replays A.3's start-and-hold pair beside it.
      **Done means** the report shows boxes by dwell with each box's weakest visit, coverage per
      rule, the sweep and the proposal by coverage, and tests cover a visit in two boxes, a dwell
      too short to count, and a threshold whose coverage falls below the line.
      Done 2026-10-04: as specified. A stay in a box runs from the blob's first sighting there to
      its last before it shows in another box, since a still cat fades from view; a visit counts as missed when no trigger fell inside it, not
      when no clip overlapped it; the replay ends a clip at a restart and at the recorder's
      300 s limit; and the proposal also keeps the visits recorded under 80 % within 5 % of the
      run's own count, or says that no threshold qualifies.

## A.3 Detect a cat-sized thing

Moved 2026-09-26. The split landed as `src/capture/motion_detector.py` (5adcc87, 2026-09-20). The
cleanup and the largest contour have been measured on every frame since 2026-09-11
(`src/capture/motion_metrics.py`, the `clean_*` columns of the metrics CSV), but do not yet decide
the trigger; that is the size band, still open.

- [x] **First, split `MotionRecorder`.** Its `# --- detection ---` and `# --- clip lifecycle ---`
      halves share nothing but the trigger boolean, and every step below changes only the first
      half. A `MotionDetector` that turns a lores frame into "motion or not, and the largest
      blob" leaves the recorder owning only the encoder and the open clip. Doing this as the
      first step means the detection changes land in one small class with its own tests, not
      inside a 440-line file that also knows how to rename a `.part`.
- [x] Morphological open to erase speckle, then close/dilate to merge head/body/tail into
      one blob.
- [x] `cv2.findContours`, take the largest contour's area instead of a global count.

Moved 2026-09-26: folded into B.1's crops, which carry one box per crop.

- [ ] **Keep the bounding box.** Part B needs it as the training crop. The sidecar already records
      the trigger and last blobs with their boxes; the crops need one per sampled frame, stored
      per event (B.1).

## A.4 One visit, one event, even across several clips

Moved 2026-09-26: the plan as agreed on 2026-09-12. Steps 1–4 are built; step 5 carries on in
the roadmap.

Decided 2026-09-11: recording may stop while a cat sits still, **as long as the clips before and
after the pause are linked to the same litter-box event**. A visit does not have to be one
continuous clip, so holding recording open on presence is no longer required.

**Plan agreed 2026-09-12.** Record the facts at capture time; decide the grouping later, with a
rule that can be re-run. The gap threshold comes from A.2 data that does not exist yet, so nothing
may bake an event id into a clip.

1. **Sidecar per clip** (done 2026-09-12). When a clip closes, the recorder writes
   `<clip>.h264.json` beside it:
   `started_at`, `ended_at`, `trigger_blob` (centroid and bbox that started the clip),
   `last_blob` (blob on the last motion frame), `close_reason` (`timeout`, `max_length`,
   `shutdown`). A clip recovered after a crash has no sidecar; ingest gives it
   `close_reason = recovered` and an end estimated from size at 1.25 MB/s. `ClipTransfer` ships
   the sidecar *before* its clip, so a clip never reaches the share without its facts. Raw `.h264`
   has no duration and the copy drops mtime, so the sidecar is the only source of the end time.
2. **Grouping rule.** Clips are chained per box, oldest first; any number of clips can form one
   event. Clip B joins the open event when it starts within `EVENT_GAP_SECONDS` of that event's
   last clip ending **and** B's trigger centroid is within `EVENT_BOX_DISTANCE_PX` of the event's
   last centroid. Start the gap at 180 s (a still cat fades from MOG2 in ~17 s, so gaps of minutes
   are normal) and let the data set it. A centroid distance needs no box map; if two boxes prove
   too close, replace it with three fixed rectangles in lores coordinates. Trade-off: too small a
   gap splits a visit (double-counts frequency, shortens duration); too large merges two cats'
   visits (the labeler marks it `multiple`). The frequency signal is the point of the project,
   so lean toward merging.
3. **Schema** (done 2026-09-12, with the grouping rule and `--regroup`). `event` stops
   being the clip. New `clip` table: path, `started_at`, `ended_at`,
   trigger and last centroids, `close_reason`, `event_id`. `event` holds the span of its clips,
   the box centroid and the clip count. `label` stays on `event_id` (B.1). `regroup` clears every
   `event_id` and reruns the rule; labels follow the clips (kept when a new event's clips agree,
   otherwise the event returns to the unlabeled queue). Manual splits and joins from the UI are
   stored as overrides that `regroup` treats as hard boundaries and links, so a threshold change
   never undoes a human correction. Clips already in the DB predate sidecars; re-ingest them as
   one-clip events.
4. **Review UI** (done 2026-09-12; details in B.3): the grouping is always visible and always
   correctable —
   every clip of an event is shown, with a split action at each boundary, a join-with-next
   action, and the full clip watchable on the page.
5. After the data run: choose the two thresholds from labeled events, `regroup`, audit with
   `filter=multi`.

Steps 1–3 need neither the camera nor cat data. Only the thresholds wait.

Known limit: two cats in different boxes at the same moment land in one clip, because detection
is a global count. Grouping cannot separate them; that is what the `multiple` label is for. Not
seen as of 2026-09-12, but possible.

`MOTION_TIMEOUT` (10 s) and `MOG2_HISTORY` (500 frames, ~17 s) still decide where clips split.
They are environment settings now, so a data run can try other values without code changes.

## A.6 Keep capturing

Moved 2026-09-26. Built as `src/capture/capture_heartbeat.py`, under the name
`CAPTURE_HEARTBEAT_URL`, a plain GET so that no one service is required; the README says the
check is set up outside this project.

- [x] **Alert when capture stops.** The Pi's heartbeat proves the Pi is up, not that the camera
      is: the 3 h 46 min stall pinged every minute. The detector pings its own dead man's switch
      (an optional `HEALTHCHECK_URL`), from a worker thread, and only while frames are arriving,
      so a stall, a restart loop or a stopped service each goes quiet.
      **Done means** a test sees the ping stop when frames stop and resume when they resume,
      the ping never runs on the capture thread, and the README's Configuration section
      describes the setting.

Moved 2026-09-27: done.

- [x] **Restart on a capture stall.** When no frame has arrived for 10 s, the detector logs an
      error, shuts down through the usual cleanup and exits non-zero, so systemd restarts it. A
      camera that stays dead restarts every ~35 s indefinitely, which is the right failure for
      recall: it keeps trying and the journal shows every attempt.
      **Done means** a test with a camera whose capture blocks sees the process exit non-zero
      through the same cleanup as SIGTERM, a test sees no exit while frames arrive, and the
      service runs on the Pi for 24 h with no stall restart.
      Done 2026-09-27: the tests are in `tests/test_main.py`. The service ran this code on the Pi
      from 2026-09-26 16:50:58 with no restart: in the 30 h to 2026-09-27 22:53 the metrics CSV
      holds 3.24 million frames at 30.00 fps, the longest gap between them 0.68 s, and the
      journal has no stall line.

---

# Part B — knowing which cat

## B.1 Capture the right data

Moved 2026-09-26. The metadata was already kept: the sidecar and the `clip` table hold each clip's
times, path, and trigger and last blobs with their boxes; per-crop boxes go with the crops. The
full frame was dropped: the review side already takes full frames from the clip, so capturing one
at record time would duplicate it.

- [x] Metadata per event: start/end time, clip path, frame count, contour area, bbox.
- [ ] One **full frame** per event for context, so a human reviewing an ambiguous crop can
      see what was actually going on. (Dropped.)

## B.2 Storage

Moved 2026-09-26: the finished items and the schema sketch. The built schema is in
`src/review/capture_db.py`; the `crop` table carries on in the roadmap.

- [x] **SQLite on local disk, media on the share.** SQLite's locking is unreliable over CIFS,
      and the share is CIFS — a database file there risks corruption under concurrent
      access. `captures.db` lives on the local ext4 root and stores *paths* on the share.
- [x] Images and clips stay on the share. Do not put image blobs in SQLite.

Schema sketch:

```sql
CREATE TABLE event (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,       -- ISO8601, first clip's start
    ended_at     TEXT,                -- last clip's end
    box_cx       INTEGER,             -- centroid that places it in a box
    box_cy       INTEGER,
    frame_path   TEXT,                -- full-frame context still
    max_area     INTEGER              -- largest contour area seen
);

CREATE TABLE clip (                   -- one visit = one or more clips (A.4)
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER REFERENCES event(id),   -- NULL until grouped
    path         TEXT NOT NULL UNIQUE,           -- on the share
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    close_reason TEXT,                -- timeout | max_length | shutdown | recovered
    trigger_cx   INTEGER, trigger_cy INTEGER,
    last_cx      INTEGER, last_cy    INTEGER,
    boundary     TEXT                 -- manual override: 'split' | 'join' | NULL
);

CREATE TABLE crop (
    id        INTEGER PRIMARY KEY,
    event_id  INTEGER NOT NULL REFERENCES event(id),
    path      TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE label (
    event_id   INTEGER PRIMARY KEY REFERENCES event(id),
    value      TEXT NOT NULL,    -- 'cat_a' | 'cat_b' | 'not_cat' | 'multiple' | 'unsure'
    labeled_at TEXT NOT NULL,
    source     TEXT NOT NULL     -- 'human' | 'model'
);
```

Keeping `label` separate from `event` means model predictions and human labels can coexist
without one overwriting the other — which is what makes B.6's review-the-model loop possible.

Since 2026-09-11 a visit may span several clips, so the clip is its own table and `event` is the
grouping; the full plan is in A.4.

Moved 2026-09-26: folded into B.1's crops, with the box per crop added.

- [ ] A `crop` table for B.1: event, path on the share, when it was captured.

## B.3 Labeling UI

Moved 2026-09-26: everything built up to then.

**Started 2026-08-26.** `src/review/api.py` is a Flask blueprint served as the Review tab of the one
web app on :5000, by `main.py` beside the live feed (merged 2026-09-12; the camera-less `review.py`
was dropped 2026-09-20). It labels at event level (`cat` / `not_cat` / `unsure` / `clean`) with single-key
shortcuts, auto-advance, undo, live counts, and a tile grid per clip that zooms to the full
frame. Storage follows B.2: SQLite on local disk (`DB_PATH`, default `captures.db`), label table
separate from event, media paths only. In daily use since 2026-09-17.

**The only requirement that really matters is speed.** Hundreds of events reviewed by one
person means labeling must be a single keystroke, or it will not happen and the classifier
never gets its data. Design to that:

- [x] `/review` — serves the oldest unlabeled event, with every clip's tiles and a link to
      watch it. The crop grid waits on A.3's bounding boxes.
- [x] One **keyboard shortcut per label**, auto-advancing to the next event. No mouse, no
      confirm dialog. `c`, `n`, `u`, `l`, a digit per cat and `m` (since 2026-09-21).
- [x] **Undo** (`z`). A mislabel poisons training data, and speed guarantees mislabels.
- [x] A counter of labeled/remaining per class, so imbalance is visible while labeling.
- [x] `/review?filter=...` to revisit a class, for auditing labels later. Done 2026-09-17:
      `filter=labeled` or `filter=<label>`, linked from the counts line; `→` walks, keys relabel.

Event grouping in the UI (agreed and built 2026-09-12, see A.4). The grouping must be reviewable
at the moment of labeling and never hidden:

- [x] The event page shows **every** clip of the event: a frame strip each, start and end time,
      why it closed, and the gap before it. The counter shows how many events are multi-clip, and
      `/review?filter=multi` walks them (labeled or not, `→` for next) for auditing the grouping.
- [x] **Split**: at any clip boundary, "a new visit starts here" moves that clip and the ones after
      it into a new, unlabeled event; the first half keeps its id and label. **Join**: "same visit
      as the next event" merges them. Both are stored as overrides that `regroup` respects (A.4
      step 3), so a later threshold change does not undo a human correction.
- [x] **Watch the full clip** from the page, for when the strip is ambiguous. Browsers cannot play
      a raw `.h264` stream, so the review server remuxes on demand with `ffmpeg -c copy` (no
      re-encode, ~0.7 s for an 18 MB clip) into `.review_cache` as `clip<id>.mp4`, and serves it
      to a `<video>` tag with byte ranges, so it seeks. The frame rate is pinned to `CAMERA_FPS`
      with `-r` as an input option; measured 2026-09-12, ffmpeg already read 30 fps from these
      clips (the Pi's encoder embeds timing), so the pin is belt and braces, not a fix.

**Next: label which cat** (asked for 2026-09-20, ahead of everything else on this page). Every
`cat` label so far says only that a cat was there. Part B needs the name, A.2's stop rule wants
15+ visits per cat and cannot be checked without it, and every visit labelled `cat` in the
meantime has to be revisited, so this goes before the usability items.

- [x] **The label values are the cats' names** (done 2026-09-21), read from config (`CAT_NAMES`, a comma-separated
      list of any length; the names and their number are the installation's, never the code's).
      The review page gives each cat a **digit key** in the configured order, shown beside the
      name, so labelling stays one keystroke; the counts line and the `?filter=` walks grow one
      entry per cat, which is the per-cat count A.2 asks for. The `label.value` column already
      takes any text, so the schema does not change; the page and the API validate against the
      configured list, and an empty list leaves the page as it is today.
- [x] **`cat` stays, meaning "a cat, which one not decided".** It is the value of every visit
      labelled before this lands, and it remains the honest answer when the tiles do not show
      the coat. `/review?filter=cat` is then the per-cat backlog: walk it
      and press a digit, and the visit leaves the filter. Nothing is migrated.
- [x] **`multiple`** (`m`) for two cats in one event, the B.1 edge case: excluded from
      single-label training, kept as a visit.

**From use, 2026-09-19.** Three things make the page hard to work with now that it is used
daily, one from 2026-09-20, and the review queue agreed 2026-09-21. They follow the per-cat
labels, in the order listed:

- [x] **It is used from a phone, and the buttons are too small to hit** (done 2026-09-22, merged as
      PR #4; the screenshot check is `tests/manual/check_page.py`). The target is a phone of
      412 x 915 CSS px in portrait. The page has no `viewport` meta tag, so the phone renders it
      at desktop width and shrinks it; that alone is most of "too small", and adding the tag is
      the first step, after which every size below means what it says. The layout, agreed
      2026-09-22 after comparing the labelling apps that solve the same problem (Zooniverse's
      mobile classifier, which puts the subject on top and the answers in a fixed bar at the
      bottom; iNaturalist's Identify; Material 3's docked toolbar and snackbar):
      - **The label buttons live in a bar fixed to the bottom of the screen**, in the thumb zone,
        so they are reachable from anywhere on the page without scrolling. Two rows: the cats
        by name plus `multiple` on the first, `not a cat`, `unsure`, `cleaning` and a `more`
        button on the second. Every target is at least 48 px tall, with the row's width shared
        equally, and the page gets bottom padding equal to the bar's height so the last tiles
        are not hidden under it. The bar is the same on the desk: the keyboard shortcuts stay,
        with their `kbd` hints shown only on a device with a hover pointer
        (`@media (hover: hover)`), so the phone's buttons carry the name alone.
      - **`more` opens a sheet** from the bottom with the actions used a few times a sitting,
        not once per event: plain `cat` (which one not decided), `same visit as the next event`,
        `next` on the `?filter=` walks, and `rescan`. The sheet closes on any action or a tap
        outside it.
      - **Undo is a snackbar.** After a label, a strip above the bar says what was recorded
        (the cat's name) with an `undo` button for a few seconds; `z` still works. That removes undo
        from the bar without hiding it, and the confirmation is the feedback a tap otherwise
        lacks. While a label request is in flight the bar is disabled, so a double tap cannot
        label two events.
      - **Split stays where it is**, on the boundary between two clips, since it is about that
        boundary; it just becomes a 48 px target. The cleaning form's three fields become
        `inputmode=numeric` with large fields side by side, and `save counts` joins the bar
        while the form is open.
      - **Tiles reflow to two columns** at phone width (about 200 px each at 16:9, wide enough
        to see a cat), with the event header on two lines and the counts line wrapped below the
        content. A tap on a tile opens it full width, as the zoom cursor promises today.
      - **Done means**: a screenshot from `chromium --headless --window-size=412,915` shows the
        bar, two tile columns and no horizontal scroll, and a sitting on a phone that size labels
        a backlog without a mis-tap. The screenshot is the agent's check; the sitting is a
        person's.

Moved 2026-09-26: absorbed into C.6, times stored in UTC and shown in local time.

- [ ] **Dates are machine-shaped.** Show times in a readable local form (`Tue 15 Sep, 06:11`),
      with the relative time (`3 days ago`) on hover, here and on the timeline below (B.6).

Moved 2026-09-26: done.

- [x] **A reload loses your place.** The `?filter=` walks keep their position in page state, so
      reloading starts the walk over from its first event, and a backlog cannot be worked
      through in sittings. Put the position in the URL — the event id as the path, the filter as
      a query parameter — so a reload, the back button or a pasted link lands on the same event.
      This is the same route the notification deep links need (B.4), so build it once. On the
      API side the three walks (`/api/review/next`, `/multi/next`, `/labeled/next`) become
      one `/api/review/next?filter=&after=` — the page already has one `load()` that picks
      between them, and one route with the same two parameters as the URL is what a
      reload can replay. The staging table below joins the default walk in this step, so the
      queue has one source of truth from the start.
      **Done means** reloading, pressing Back or pasting the URL lands on the same event in the
      same walk for every filter, a test drives `/api/review/next?filter=&after=` for each filter
      and gets the sequence the old routes gave, the old three routes are gone, and
      `check_page.py` shows the event id in the address.
      Done 2026-09-26: `/review/<event id>?filter=<walk>` and one `next` route, as specified;
      the staging table does not exist yet, so it joins the default walk with the staging item.
      Driving it in headless Chromium showed Back on a 14-clip event waiting 60 s behind that
      event's strip builds, so leaving an event now cancels its pending strips (DESIGN.md,
      Review UI).

Moved 2026-09-27: done.

- [x] **An explicit review queue, with staging.** Today the queue is
      implicit: the unlabelled events, in review order, and labelling one removes it. There is
      no way to put an event back without erasing its label, and no record of why it came back.
      The need is real now: the `not_cat` labels made before 2026-09-20 (`label.labeled_at`)
      include those made from the first eight seconds of each clip, before the whole-clip tiles
      landed on 2026-09-17, but nothing can walk them without losing the labels. A **staging
      table** fixes that: one row
      per staged event with a reason, when it was staged and by whom (`human`, `agent`, later
      `model`), stored against the event's first clip as poop counts are, so a `regroup` keeps
      it with its clips. The default walk becomes the unlabelled events **plus** the staged ones, in the
      same order, so a re-review is worked in the same sitting as fresh events. The event keeps
      its label and the page shows it with the reason, so confirming is one keystroke; any label
      press clears the staging row. Staging is done from the timeline (B.6, selection and a
      "stage everything shown" action, or one row's button) and from the API, so an agent that
      finds a doubtful label stages it rather than leaving a note; `main.py --stage` takes the
      same query and reason from the command line (`--label`, `--labeled-before`, `--reason`),
      and runs beside the service, since staging renumbers nothing. B.6's human-in-the-loop item is this table
      with `source=model`: the same queue serves re-audits, agent findings and low-confidence
      predictions. Time of day is a **query** over `started_at` on the timeline, never stored
      state. Staging from the timeline lands with B.6; this item is done without it.
      **Done means** the default walk serves the unlabelled events and the staged ones in one
      order, a staged event shows its label and reason, one label press clears its staging row,
      the API and `main.py --stage` both stage, a test stages a labelled event, walks to it and
      confirms it without losing the label, and the early `not_cat` labels can be staged in
      one command.
      Done 2026-09-27: a `staged` table keyed by clip, as specified; `main.py --stage --label
      not_cat --labeled-before 2026-09-20` staged every one of those events in a copy of the live
      database, and a regroup of that copy kept them all. The page shows the reason beside the
      label. The API route is `POST /api/review/<event>/stage` with `reason` and `source`.

- [x] **Four findings from the 2026-09-21 code review**, deferred until the unified route and
      the queue are in, because two of them are simpler once the route carries ids:
      - Media URLs are keyed by event and index while the cached file is keyed by clip, and
        the responses carry no cache headers, so after a split, join or `regroup` a browser
        can show the previous clip's tiles. Put the clip id in the media URL: a clip's
        files never change, so a URL that names the clip can never serve another's.
      - A join where either side is an undated clip (a recovered clip whose name did not
        parse) writes the override, but the grouping skips undated clips, so the page shows
        an unchanged event as merged. Refuse the join and say why.
      - `--regroup` while the service is up can lose a label posted between its read of the
        labels and its commit. A transaction is not enough: the page
        holds event ids, and a label posted after the regroup commits lands on a renumbered
        event. So `--regroup` refuses to run while another process holds the database: every
        `CaptureDB` takes a shared lock on a file beside it, and `regroup` needs it exclusive.
      - A persistent clip-open failure (directory gone, permissions) logs a full traceback on
        every motion frame, at 30 Hz, for as long as motion lasts. Log once per failure streak.
        This one is on the capture side and does not wait for the route.
      **Done means** each has a regression test that fails with the bug put back: a split followed
      by a strip request serves the new clip's tiles; a join onto an undated clip is refused with a
      message; a label posted during `--regroup` survives, or `--regroup` refuses to run while the
      service holds the database; and a persistent clip-open failure logs one traceback per streak.
      Done 2026-09-27: media URLs are `/review/clip/<clip id>/...`; a join touching an undated
      clip answers 409 with the reason; `--regroup` refuses, and exits 1, while any other
      `CaptureDB` holds the lock file beside the database; clip-open failures log a traceback at
      most once a minute, not once per streak, since a streak that lasted days would have hidden
      every later missed visit. Each test was mutation-checked.

- [x] **The tiles are slow to appear, worst on multi-clip events.** A strip
      is built on its first request: ffmpeg reads the whole clip off the share and decodes every
      keyframe, and an event with several clips fires several of those at once. Build them
      ahead instead: after every ingest (startup and `rescan`) and every label, a background
      thread walks the default walk's events (unlabelled and staged) in review order and builds the strip for each clip that has none, so the
      page's request is a cache hit. One build at a time, so it does not starve a request or,
      under `main.py`, the encoder; the reviewer's own request for a strip still builds it on
      demand and only wastes work if the two collide (the staging names are unique and the
      publish atomic, so a collision is safe, just slow). Bound the walk to the next few dozen
      events, not the backlog, and never hold the `CaptureDB` lock while ffmpeg reads the
      share. The `?filter=` walks get no pre-caching: their events were already built when
      they were labelled.
      **Done means** after an ingest the next few dozen unlabelled events have their strips before
      they are asked for, a test shows the builder skipping built clips and never holding the
      `CaptureDB` lock while it runs, and on the Pi the first tile of a fresh multi-clip event
      appears in under a second.
      Done 2026-09-27: `StripsAhead`, one thread, 30 events ahead, woken by every ingest,
      label, stage, split, join and rescan and every ten minutes, and reniced to 10 so its
      ffmpeg is too; a page request for a strip being built waits for it. On the Pi, against an
      empty scratch cache, it built the 10 clips of the next 8 queued events in 17.7 s while
      the running service held 30.00 fps (longest frame gap 62 ms); the page's requests for a
      3-clip event's strips then took 16 ms together, served by the app on the Pi from that
      cache.

Moved 2026-10-03: done.

- [x] **Mark a peek: a cat that looks in and uses no box.** A peek keeps the cat's name, since it
      still shows which cat it is (B.5), but it is not a visit: A.2's counts and B.4's rule leave
      it out. One key (`p`), or a button in the sheet, toggles the mark without moving on, so a
      peek is two keys: `p`, then the cat's digit. The mark is stored on each of the event's
      clips, which keep their ids through a regroup, and an event is a peek only while every one
      of its clips carries it, so joining it to a visit makes the whole a visit.
      **Done means** the mark survives a `regroup`, a join with an unmarked event clears it, a
      split keeps it on both halves, the review page shows it beside the label and the timeline
      in the row, tests cover each, and `check_page.py` screenshots at both sizes show the mark
      and the button.
      Done 2026-10-03: as specified, with `POST /api/review/<event>/peek` (`{"peek": true}`)
      for agents. The timeline puts "peek" under the label, so a phone row is no wider.

## B.4 Notifications

Moved 2026-09-28: not pursued. Visits are labelled in sittings, and a push per visit is not
wanted; B.4 is now alerts on a change in a cat's habits, and the alert rule moved there
from B.6.

Purpose is to close the loop: a capture happens, you get a push, one tap lands on the
labeling page for that event. Labeling that rides on notifications actually gets done.

**ntfy** is the recommended transport — a single HTTP POST, no account, free app, and
self-hostable later if you want nothing leaving the LAN. Alternatives: Pushover (paid, polished),
Telegram bot, SMTP, Home Assistant webhook.

**Send from a worker thread, never from the capture thread.** Every frame consumer runs
synchronously on `CameraManager`'s capture loop, so a blocking HTTP call to a network service
would stall frame distribution for every consumer. Queue and fire elsewhere.

One push per visit, not per clip or per frame. A visit is an event, and an event is only closed
`EVENT_GAP_SECONDS` after its last clip, so the push waits that long. A digest option for quiet
review can come later.

- [ ] **One push per visit, with a thumbnail and a deep link** to the event's review page (the
      URL route in B.3).
      **Done means** a visit sends one push to a configurable ntfy topic within a minute of its
      event closing, with a thumbnail and a link that opens that event's review page; a test with
      a send that hangs leaves the frame loop at full rate; a burst of clips from one visit sends
      one push; and the README says how to subscribe.

## B.5b Ground truth at cleaning time

Moved 2026-09-26: the finished UI items and the feasibility measurement.

Measured feasibility (2026-09-15 06:55 clip, full-resolution frames): a hand at box height spans
about 120–150 px across the 1280x720 frame, so spread fingers are 20–25 px apart and countable by
eye. Raising the hand toward the camera makes it larger. Two limits: the room was dim at 06:55 and
contrast was low, so a daytime or lit cleaning reads better; and a cleaning in the dark is invisible
like everything else until the boxes are lit.

- [x] A `clean` label value in the review UI (B.3), alongside cat / not_cat / multiple / unsure.
      Done 2026-09-16: key `l` labels the event and opens the count form.
- [x] A per-box count on that event: three small number fields, `Enter` saves and moves on. Done
      2026-09-16; stored against a clip, so a regroup cannot strand them (see DESIGN.md).

## B.6 The payoff

Moved 2026-09-26: not a task. The goal paragraph and B.5b already say it.

- [ ] Maybe later: tell poop from pee. It looks hard and unreliable, so no alert should depend on
      it.

Moved 2026-09-28: to B.4, reworded, as its one item.

- [ ] Litter box usage is a health signal, and catching bowel or urinary trouble is the goal.
      Alert on changes in how often a cat visits and how long it stays.
      That should shape what gets logged from the start (duration in box, time of day), because
      those are cheap now and unrecoverable later.
      **Done means** a stated rule (for example, a cat's visits per day or median duration outside
      its trailing four-week range two days running) runs daily over `captures.db`, a test over a
      seeded history fires on an injected change and not on normal variation, and a firing reaches
      a person through B.4's transport.

---

# Part C — any installation

## C.2 What every installation needs

Moved 2026-09-26.

- [x] **Light around the clock is a requirement**, stated in the README. Done 2026-09-26.
- [x] **pi-tools is suggested in the README** for what the project cannot do itself: logging
      power and heat, bringing a Pi 5 back after a power-off, and alerting when the Pi goes
      quiet. Done 2026-09-26.

## C.7 Choices to make options, flagged for the next developer

Moved 2026-09-26.

- [x] The README's list of gates leaves out node and the JavaScript lint. Done 2026-09-26.

Moved 2026-09-26: not a task; it is now prose in B.5.

- [ ] Training hardware is a choice: the plan names the Jetson (B.5), but any machine with a GPU,
      a Hailo accelerator, or a CPU for a small model will do. Keep the handoff (crops and labels
      on the share) the only contract.

---

# Risks and open questions

Moved 2026-09-26: the resolved questions, and two open ones the data run answered or made moot.
A.2 measures cats in pixels directly, so the mounting height no longer gates any estimate, and
Day 1 of the run showed daylight crossing the floor (A.2 above).

**Can the cats actually be told apart from directly overhead? Resolved 2026-08-26: yes.** Each
cat has a distinct pattern and colour, which is what a top-down view preserves
best. Part B is viable as designed; no second camera or RFID fallback is needed. Coat colour
also makes the B.5 baseline worth trying first — mean colour inside the mask may separate the
cats without any neural network.

- Camera mounting height and floor coverage — every pixel-area estimate depends on it.
- ~~How many cats, and do they need telling apart individually?~~ Resolved 2026-09-20: each cat
  is identified by name, and the number is not the app's to know. `CAT_NAMES` is a list of any
  length, set per installation (B.3); the code never assumes a count.
- Is the floor around the box plain, or patterned/reflective in ways that fight background
  subtraction? (still open, see the roadmap)
- Consistent lighting, or does daylight sweep across it?

---

# When this became a service

Moved 2026-09-26: done 2026-09-14. Retention carries on under the roadmap's storage risk.

**Done 2026-09-14:** `deploy/catdetector.service.in`, installed with `deploy/install-service.sh`. The
reasoning for each setting is in DESIGN.md (Process). Two items below changed on the way in: the
clock wait is bounded instead of enabling `systemd-time-wait-sync.service`, and the share is
wanted rather than required.

- **Start at boot and restart on failure** (deferred 2026-09-10, done 2026-09-14). That day the Pi
  powered itself off at 12:57 and was switched back on at 17:23, but main.py was started by
  hand, so nothing was recorded until someone restarted it. The unit has
  `WantedBy=multi-user.target` and `Restart=on-failure`.
  **`RestartSec` must be at least ~15 s.** Measured 2026-09-12: after an unclean kill (`kill -9`,
  a crash, a power cut) the camera stays busy for 10-15 s, and a restart during that window dies
  with "Camera __init__ sequence did not complete". Too short a `RestartSec` would crash-loop
  instead of recovering. Use `RestartSec=20`.
- **Start after the clock is set.** At boot, fake-hwclock rewinds the clock to its last hourly
  save, and NTP only corrects it about 35 s later. A unit that started earlier would put wrong
  times on clips and metrics rows. **Changed:** `systemd-time-wait-sync.service` waits with no
  timeout, so enabling it would keep the detector from starting at all on a boot without internet.
  The unit runs the same helper as an `ExecStartPre` under `timeout 90` and starts anyway.
- Logging is handled: the app moved off bare `print()` to `logging` on 2026-08-25, so the
  block-buffering trap that lost lines when a unit was killed is gone, and records carry
  timestamps and levels. `PYTHONUNBUFFERED=1` is no longer needed. `configure_logging()` in
  main.py sets the root to WARNING and only this application's loggers to INFO; keep it that
  way, since a blanket INFO root makes picamera2 narrate every state change.
  For the unit, consider `StandardOutput=journal` and dropping the timestamp from the format,
  since journald adds its own. (carried to the roadmap 2026-09-26)
- **SIGTERM runs the same cleanup as Ctrl-C** (done 2026-09-11, verified on hardware 2026-09-12).
  `kill -TERM` mid-clip logged "Received SIGTERM", stopped saving, promoted the clip to its final
  name and flushed the metrics.
- **Clips cut off by a crash or power loss are recovered at startup** (done 2026-09-11, commit
  3ebf28e, verified on hardware 2026-09-12). `kill -9` mid-clip left a 15 MB `.part`; the restart
  logged "Recovered ... cut off by an unclean shutdown", `ClipTransfer` shipped it, and the
  recovered clip decoded to 365 frames with no ffmpeg errors.
- ~~`RequiresMountsFor=/mnt/nas` — otherwise clips vanish into an empty mountpoint.~~ Obsolete
  since clips are staged locally and `ClipTransfer` refuses a non-mountpoint. Requiring the mount
  would only stop recording when the share is down at boot, so the unit wants it instead.
- Clips stay raw `.h264` on the share. Playback in the review UI remuxes on demand (B.3), so the
  capture path never runs ffmpeg; revisit only if something else needs seekable files.
- Retention/pruning, per the storage note above.
