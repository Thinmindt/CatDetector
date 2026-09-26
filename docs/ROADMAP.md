# Roadmap: cat detection and identification

Current as of 2026-09-26. This file holds the plan as it stands: what is decided, what is next,
and what is still open. Finished items and the history behind decisions move to
`ROADMAP_ARCHIVE.md`, under the same section letters, so a citation such as "A.2" finds the plan
here and its history there.

**Goal.** Spot bowel or urinary trouble in a cat. That means reliably
recording every litter box visit and knowing **which cat** made it. Telling poop from pee might come
later, but it looks hard and unreliable, so nothing depends on it.

Two workstreams, in dependency order, and a third that runs alongside both:

- **Part A — trigger reliably.** A cat entering the frame always starts a recording.
  Recall is the priority: a missed visit is a failure, an extra clip is an annoyance.
- **Part B — identify the cat.** Collect and label enough images to train a per-cat
  classifier, which needs a labeling workflow before it needs a model.
- **Part C — any installation.** Someone else's cats, boxes, camera, Pi and storage should work
  as well as the ones this was built on, with the hardware behind interfaces and nothing about
  one home written into the code.

Part B depends on Part A's bounding box: the box that decides "this is cat-sized" is the
same box that crops training images. Do not build the crop pipeline twice.

**What every installation needs.** A fixed camera mounted overhead, pointing straight down at
the litter boxes, and **the boxes lit around the clock**: the camera sees nothing in the dark, and
constant light is what made nights record like days. Because the camera never moves and the boxes
are a fixed distance away, a cat's apparent size in pixels is roughly constant. That is the most
useful fact available, and the trigger does not use it yet (A.3). The number of boxes, where they
sit and how many cats use them are the installation's, not the project's (C.3).

**The installation this was built on:** a Raspberry Pi 5 with the IR-filtered Camera Module 3,
three boxes in one frame, three cats, and clips shipped to a NAS. Measurements in this roadmap
come from it; Part C is about not mistaking it for a requirement.

**Hardware split (decided 2026-08-24).** The **Pi stays the capture box** and the **Jetson does
training and inference**. The Pi's camera stack is the reason: `picamera2`/`libcamera` are
Raspberry Pi-specific (the `rpi/pisp` pipeline handler and `rp1-cfe` driver), and Jetson uses
NVIDIA's Argus stack instead — porting means rewriting `camera_manager.py` and the encoder path
in `motion_recorder.py`, i.e. every line that touches hardware, and likely a different camera
module too. Nothing is gained for capture. Everything downstream of a JPEG crop is portable, and
CUDA turns "fine-tune overnight" into minutes. So crops and labels move to the Jetson; frames
never do.

Consequence for B.5: training is off-device by design, and the crop/label store must be readable
from the Jetson — the NAS share is the natural handoff point.

**Platform.** Raspberry Pi 5 with no Hailo accelerator, so any *on-Pi* inference would be CPU.
Local disk is ext4 (204 GB free); the NAS share is **CIFS** (467 GB free), which is why SQLite
stays local (B.2). The Jetson's model and JetPack version are not yet recorded; JetPack pins its
own Python (3.10 on JetPack 6) and PyTorch comes from NVIDIA's wheels, so the training
environment is separate from this repo's `==3.13.*` pin and should not try to share it.

**Thermal headroom.** With the Active Cooler the SoC holds 51–55 °C under the detector with no
throttling; without it, 84–86 °C and throttled. Any extra on-Pi work, including inference for
B.5, has to fit in the headroom that leaves; measure it before assuming there is any.

---

# Part A — trigger reliably

## A.0 Where detection stands

`MotionDetector.detect` (`src/capture/motion_detector.py`) runs MOG2 on the 640x480 lores frame,
drops MOG2's shadow pixels, and calls the frame moving when the foreground pixel count exceeds
`MOTION_THRESHOLD`. It is gated off during MOG2's warmup and while the frame's mean brightness is
below `DARK_BRIGHTNESS`. Every frame is also measured for a cleaned blob (morphological open and
close, then the largest contour), which the metrics CSV logs and the sidecar records, but which
does not yet decide anything.

| setting | default | the service runs |
|---|---|---|
| `MOTION_THRESHOLD` | 300 | 150, for the data run (A.2) |
| `DARK_BRIGHTNESS` | 10 | 10 |
| `MOTION_TIMEOUT` | 10 s | 10 s |
| `MOG2_HISTORY` | 500 frames (~17 s) | 500 |

The boxes are lit around the clock, so nights record like days. The camera is IR-filtered and
sees nothing in the dark; if the light fails, the dark gate stops black clips but not the gap.

## A.1 What still stands between this and the goal

1. **No spatial coherence.** Scattered noise and one solid cat-sized blob are identical to a
   global count. A.3 fixes this.
2. **A stationary cat fades out.** MOG2 absorbs anything static in ~17 s, so recording can stop
   mid-visit. That is accepted: A.4 links the clips on either side into one event.
3. **Light changes trigger.** Evening sun crossing the floor is the main daytime false trigger,
   about one false clip an hour in daylight. A size band with an upper bound (A.3) is the fix.

## A.2 Measure before tuning

**The data run** has been running since 2026-09-14 as the `catdetector` service, with
`MOTION_THRESHOLD=150` and `METRICS_CSV` on, over-capturing rather than missing, so a visit below
the threshold still leaves a trace in the CSV. The trigger is tuned to the weakest visit, not the
average one: entry frames (325 px seen) and a cat sitting still (47–130 px) are the binding cases,
and both are rare, so the unit to collect is **visits, not hours**. Only visits under the light,
from 2026-09-16, count; earlier ones are a different regime. The run stops when every condition
below holds.

| condition | why | 2026-09-26 07:15 |
|---|---|---|
| **50+ labelled visits, 100 preferred** | zero misses in 50 only bounds the miss rate at ~6% (95%); at 100, ~3% | 70 |
| **10+ at night under the light** | more sensor gain, more noise: a different regime | 25 |
| **15+ per cat** | a dark cat on dark litter gives a weaker signal than a pale one | 37 / 18 / 15 |
| **all three boxes used** | the far box covers fewer pixels and may read weaker | left 44, middle 25, right 1 |
| **several days of varied weather, one overcast** | sunlight crossing the floor is the main daytime false trigger | not checked |
| **the smallest entry blob has stopped dropping over the last ~20 visits** | tuning follows the weakest case | not checked |

Boxes are placed by the trigger centroid's third of the lores frame. False positives are
measurable from hours rather than visits, and any candidate rule can be replayed against the
metrics CSV without re-recording. Storage is not the constraint (~130 MB/day of metrics plus
clips). **Labelling is**: ~10 clips a day has to be kept up with, or the backlog outlives the run.

- [ ] **Finish the data run** against the table above. The right box is the thin one.
- [ ] **Derive the size band.** From the labelled lit events, the distribution of cleaned
      largest-blob area with a cat present and absent: that is the band A.3 triggers on. Waits
      for the data run. Filter on `started_at` from 2026-09-16, and say in the result how many
      events were excluded.

## A.3 Detect a cat-sized thing, not "some pixels changed"

The cleaned blob is already measured on every frame (A.0); what is left is making it the trigger.

- [ ] Trigger on a **size band** (`min_cat_area < area < max_cat_area`) from A.2. The upper
      bound rejects lighting changes: a cat cannot be 60% of the frame. The band is only
      defensible because the overhead mount keeps apparent size constant.
- [ ] Require the blob to persist N consecutive frames (keep N at 2-3 — recall matters more
      than precision).
- [ ] **Keep the bounding box.** Part B needs it as the training crop. The sidecar already records
      the trigger and last blobs with their boxes; the crops need one per sampled frame, stored
      per event (B.1).

## A.4 One visit, one event, even across several clips

Recording may stop while a cat sits still, **as long as the clips before and after the pause are
linked to the same litter-box event**. Built: a sidecar of facts per clip, the grouping rule
(`src/review/event_grouping.py`), the clip/event schema with `--regroup`, and split and join in
the review page, stored as overrides a regroup respects. The design is in `DESIGN.md`; the plan
as agreed is in the archive.

- [ ] **Choose the thresholds from labelled events**, then `--regroup` and audit with
      `filter=multi`. `EVENT_GAP_SECONDS` (180) and `EVENT_BOX_DISTANCE_PX` (120) are starting
      guesses. Too small a gap splits a visit (double-counts frequency, shortens duration); too
      large merges two cats' visits (the labeler marks it `multiple`). The frequency signal is
      the point of the project, so lean toward merging. If two boxes prove too close for a
      centroid distance, replace it with three fixed rectangles in lores coordinates.

Known limit: two cats in different boxes at the same moment land in one clip, because detection
is a global count. Grouping cannot separate them; that is what the `multiple` label is for. One
clip can also cross from one box to another, and grouping places a clip by its trigger position.

## A.5 Verify against the goal

- [ ] Keep a small labeled set of clips (cat / no cat / ambiguous) as a regression set.
- [ ] Score on **recall first**, false-positive rate second. A change that trades recall for
      precision is a regression for this project.

---

# Part B — knowing which cat

## B.1 Capture the right data

A classifier trains on stills, not clips. Extend the recorder to also write, per event:

- [ ] A handful of **JPEG crops** from A.3's bounding box (the cat), not the whole frame.
      Several frames spread across the event, not one — pose varies, and variety is what
      makes the classifier generalize.
- [ ] One **full frame** per event for context, so a human reviewing an ambiguous crop can
      see what was actually going on.
- [ ] Metadata per event: start/end time, clip path, frame count, contour area, bbox.

**Model the data as events, not frames.** One visit = one event = one or more clips (A.4) and
many crops. This is the single most important structural decision in Part B, for two reasons:

- **Labeling cost.** A human labels the *event* once ("that's Cat A"), and the label
  propagates to every crop from it. Labeling frame-by-frame is 50x the work for the same
  information.
- **Honest evaluation.** Crops from one event are near-duplicates. Splitting train/val
  randomly *by crop* leaks the same cat-in-the-same-pose into both sides and inflates
  accuracy badly. **Split by event** (ideally by day). Decide this now; it is painful to
  retrofit once a model looks deceptively good.

Two cats in one event are labelled `multiple`, which excludes the event from single-label
training rather than silently mislabeling it.

## B.2 Storage

SQLite on local disk (`captures.db`), media on the NAS; never SQLite on the CIFS share, and never
image blobs in SQLite. The built tables are in `src/review/capture_db.py`: `event`, `clip`,
`label` (separate from `event`, so model predictions and human labels can coexist, which B.6's
review loop needs) and `poop_count`.

- [ ] A `crop` table for B.1: event, path on the NAS, when it was captured.
- [ ] **Prune `.review_cache`.** It keeps every strip, up to 48 full frames (~4 MB) per clip and
      a remuxed MP4 per watched clip, and nothing ever removes them; on a small SD card it
      eventually reaches the recorder's 1 GB free-space floor and recording stops. Give it a size
      budget (configurable, a sensible share of the disk by default) and evict least recently
      used entries past it, oldest first; everything in it can be rebuilt from the clip.
      **Done means** a test fills the cache past its budget and sees it shrink below it without
      touching the entries the page is showing, and the README says what the budget is.
- [ ] The DB is the only thing that is hard to recreate, so it is the thing to back up.
- [ ] Watch SD-card wear: many small writes. Batch inserts per event rather than per frame.

## B.3 Labeling UI

The Review tab labels at event level with one keystroke or one tap: the cats by name (a digit key
each, from `CAT_NAMES`), `cat` when which one is not decided, `multiple`, `not_cat`, `unsure` and
`clean`, with undo, live counts, `?filter=` walks for auditing, whole-clip tiles, split and join,
watch, and a phone layout. **The only requirement that really matters is speed:** hundreds of
events reviewed by one person means labeling must be a single action, or it will not happen and
the classifier never gets its data.

Next, in this order:

- [ ] **A reload loses your place.** The `?filter=` walks keep their position in page state, so
      reloading starts the walk over from its first event, and a backlog cannot be worked
      through in sittings. Put the position in the URL — the event id as the path, the filter as
      a query parameter — so a reload, the back button or a pasted link lands on the same event.
      This is the same route the notification deep links need (B.4), so build it once. On the
      API side the three walks (`/api/review/next`, `/multi/next`, `/labeled/next`) become
      one `/api/review/next?filter=&after=` — the page already has one `load()` that picks
      between them, and one route with the same two parameters as the URL is what a
      reload can replay. The staging table below joins the default walk in this step, so the
      queue has one source of truth from the start.
- [ ] **An explicit review queue, with staging.** Today the queue is
      implicit: the unlabelled events, in review order, and labelling one removes it. There is
      no way to put an event back without erasing its label, and no record of why it came back.
      The need is real now: 110 `not_cat` labels were made from the first eight seconds of each
      clip, before the whole-clip tiles, and `label.labeled_at` identifies them exactly, but
      nothing can walk them without losing the labels. A **staging table** fixes that: one row
      per staged event with a reason, when it was staged and by whom (`human`, `agent`, later
      `model`). The default walk becomes the unlabelled events **plus** the staged ones, in the
      same order, so a re-review is worked in the same sitting as fresh events. The event keeps
      its label and the page shows it with the reason, so confirming is one keystroke; any label
      press clears the staging row. Staging is done from the timeline (B.6, selection and a
      "stage everything shown" action, or one row's button) and from the API, so an agent that
      finds a doubtful label stages it rather than leaving a note; `main.py --stage` takes the
      same query and reason from the command line. B.6's human-in-the-loop item is this table
      with `source=model`: the same queue serves re-audits, agent findings and low-confidence
      predictions. Time of day is a **query** over `started_at` on the timeline, never stored
      state.
- [ ] **Four findings from the 2026-09-21 code review**, deferred until the unified route and
      the queue are in, because two of them are simpler once the route carries ids:
      - Media URLs are keyed by event and index while the cached file is keyed by clip, and
        the responses carry no cache headers, so after a split, join or `regroup` a browser
        can show the previous clip's tiles. Put the clip id in the media URL, or send
        `no-store`.
      - A join where either side is an undated clip (a recovered clip whose name did not
        parse) writes the override, but the grouping skips undated clips, so the page shows
        an unchanged event as merged. Refuse the join and say why.
      - `--regroup` while the service is up can lose a label posted between its read of the
        labels and its commit. Either wrap the regroup in one `BEGIN IMMEDIATE` transaction or
        make `--regroup` refuse to run while the service holds the database.
      - A persistent clip-open failure (directory gone, permissions) logs a full traceback on
        every motion frame, at 30 Hz, for as long as motion lasts. Log once per failure streak.
- [ ] **The tiles are slow to appear, worst on multi-clip events.** A strip
      is built on its first request: ffmpeg reads the whole clip off the share and decodes every
      keyframe, and an event with several clips fires several of those at once. Build them
      ahead instead: after every ingest (startup and `rescan`), a background thread walks the
      unlabeled events in review order and builds the strip for each clip that has none, so the
      page's request is a cache hit. One build at a time, so it does not starve a request or,
      under `main.py`, the encoder; the reviewer's own request for a strip still builds it on
      demand and only wastes work if the two collide (the staging names are unique and the
      publish atomic, so a collision is safe, just slow). Bound the walk to the next few dozen
      events, not the backlog, and never hold the `CaptureDB` lock while ffmpeg reads the
      share. The `?filter=` walks get no pre-caching: their events were already built when
      they were labelled.
- [ ] **Show the crop *and* the full frame** — a crop alone is often ambiguous. Needs A.3 and B.1.

Non-goals for v1: multi-user, accounts, editing bounding boxes by hand.

## B.4 Notifications

Purpose is to close the loop: a capture happens, you get a push, one tap lands on the
labeling page for that event. Labeling that rides on notifications actually gets done.

- [ ] **ntfy** is the recommended transport — a single HTTP POST, no account, free app, and
      self-hostable later if you want nothing leaving the LAN. Alternatives: Pushover (paid,
      polished), Telegram bot, SMTP, Home Assistant webhook.
- [ ] Include a thumbnail and a deep link to the event's review page (the URL route in B.3).
- [ ] **Send from a worker thread, never from the capture thread.** Every frame consumer runs
      synchronously on `CameraManager`'s capture loop, so a blocking HTTP call to a network
      service would stall frame distribution for every consumer. Queue and fire elsewhere.
- [ ] Rate-limit and batch. One push per visit, not per frame; a digest option for quiet
      review later.
- [ ] Later, once the classifier runs: notify *"Cat A used the litter box"*, and only ask for
      a label when the model is unsure.

## B.5 Train the classifier

- [ ] Baseline first: before any neural net, check whether mean coat color inside the mask
      separates the cats. Each cat has a distinct pattern and colour, which a top-down view
      preserves best; if colour alone separates them, that is a few lines of OpenCV and no
      training loop.
- [ ] Otherwise, transfer learning on a small backbone (MobileNetV3 / EfficientNet-lite)
      over the crops, **trained on the Jetson** (see the hardware split above). Decide where
      inference runs once there is a model to measure: on the Jetson for accuracy headroom, or
      exported (ONNX/TFLite) back to the Pi so a visit can be classified without a second box
      in the loop. A small classifier on one crop per event is cheap either way.
- [ ] Keep the training environment out of this repo. JetPack pins its own Python and PyTorch
      build; a separate project on the Jetson that reads crops from the NAS is cleaner than
      widening this repo's pins to straddle both machines.
- [ ] Rough target: several hundred to a low thousand crops per cat, spread across many
      distinct events, days, and lighting conditions. Event count matters more than crop
      count; 1000 crops from 5 visits is 5 examples.
- [ ] Expect **heavy class imbalance** — one cat already uses the boxes more than twice as often
      as another (A.2's table). Weight the loss or resample rather than letting the majority cat
      dominate.
- [ ] "Not a cat" negatives come free from false triggers; keep them, they are real data.
- [ ] Split by event/day, never by crop (see B.1).

## B.5b Ground truth at cleaning time: the hand signal

**At cleaning time, hold a hand over the box just cleaned, fingers extended for the number of
poops found.** The scoop-out is recorded anyway, so the count lands in the footage at the moment
and place of the observation, with no notebook, phone or app in the loop. The reviewer labels the
event `clean` and enters a count per box.

It is ground truth for the actual goal: combined with per-cat visit counts, a count per box per
cleaning gives the bowel signal statistically — a cat whose visits hold steady while the counts
fall is the case worth flagging. Counts cannot be reconstructed from old footage, so every
cleaning should carry one.

The convention, so the record stays readable:

- One signal **per box**, held **over that box**, palm toward the camera.
- **A closed fist means zero.** Absence of a signal must mean "forgot", not "none", or the data
  silently gains zeros.
- Hold for **about three seconds**. The review strip samples one frame a second, so a brief flash
  can fall between samples; three seconds guarantees a frame and lets the reviewer pick a clear one.
- Signal **while the scooping clip is still recording** (during or right after the scoop of that
  box), so it is inside a clip rather than in the gap after the motion timeout.
- More than five: hold up a hand twice, or use both hands.

- [ ] A view over time of counts per box, beside visit frequency per cat (B.6).

Not pursued: counting urine clumps the same way. They could not be validated with this setup, so
the urinary side of the goal rests on visit frequency and duration per cat (B.6).

## B.6 The payoff

- [ ] **A timeline of every event**, newest first, one row each with its time, label, box,
      duration and clip count, linking to the event's review page. One filter per label value
      (the same set the review walk uses, the cats' names included), so one cat's rows read as
      that cat's visit log and the `clean` rows as the cleaning log. Dates readable, relative
      time on hover (C.6). It is also the **selection surface for the review queue** (B.3):
      filters by label value, date range, time of day and labelled-before-a-timestamp, each row
      with a stage button, and "stage everything shown, with this reason" for the set. The first
      use is the 110 `not_cat` labels made before the whole-clip tiles.
- [ ] Litter box usage is a health signal, and catching bowel or urinary trouble is the goal.
      Alert on changes in how often a cat visits and how long it stays.
      That should shape what gets logged from the start (duration in box, time of day), because
      those are cheap now and unrecoverable later.
- [ ] Maybe later: tell poop from pee. It looks hard and unreliable, so no alert should depend on
      it.
- [ ] Human-in-the-loop: the model labels, low-confidence events are staged into the review
      queue (B.3) with `source=model`, corrections feed the next training round.

---

# Part C — any installation

The hardware, the layout and the numbers here are one installation's. Each item below removes a
place where that installation is written into the project, or says plainly what another one needs.

## C.1 Hardware behind interfaces

- [ ] **The camera becomes an interface with one implementation.** What the capture side needs
      from a camera is small: a display stream and an analysis stream delivered to consumers,
      and an H.264 encoder writing into a ring buffer that can be diverted to a file. Write that
      down as an interface, move everything picamera2- and sensor-specific into one module that
      implements it for a Pi 5 with the Camera Module 3 (the RGB888 analysis stream, the
      2304x1296 sensor mode, the software encoder), and narrow the `picamera2` lint fence from
      `src/capture/` to that module. A Pi 4 (which needs a YUV analysis stream and has a hardware
      encoder) or another camera then becomes a second module. A NoIR camera with an IR light
      would also arrive this way; it is not supported now, because it changes the night regime
      and the coat colours B.5 relies on, and nobody here can test it.
      **Done means** `picamera2` is importable only in the implementation module, the rest of
      `src/capture/` is tested against a fake of the interface, and the docs name the Pi 5 and
      Camera Module 3 as that implementation rather than as requirements.
- [ ] **Correct the encoder trap for the implementation that runs.** `CLAUDE.md` and `DESIGN.md`
      say clip bytes are written on `V4L2Encoder.thread_poll`. That is the hardware encoder; on a
      Pi 5 picamera2's `H264Encoder` is `LibavH264Encoder`, so that thread never runs. Find where
      output happens there, re-check that nothing can raise out of it, and state the trap per
      implementation. **Done means** the documented thread is the one a stack trace from this Pi
      shows.

## C.2 What every installation needs

- [x] **Light around the clock is a requirement**, stated in the README. Done 2026-09-26.
- [x] **pi-tools is suggested in the README** for what the project cannot do itself: logging
      power and heat, bringing a Pi 5 back after a power-off, and alerting when the Pi goes
      quiet. Done 2026-09-26.

## C.3 Any number of boxes, anywhere, and more than one camera

- [ ] **No fixed number or layout of boxes.** Three are hard-coded today: `BOXES` in
      `src/review/api.py`, the cleaning form in `app.html` and `app.js`, and the tests. Boxes
      become configuration, per camera, so the cleaning form shows the installation's boxes and
      a fourth one's counts can be recorded. Event grouping already places clips by centroid
      distance, so it needs no box map.
- [ ] **More than one camera.** Boxes may be in different rooms. Each camera is a capture node
      with an id, in its clips' names and sidecars; clips go to one share under that id; one
      review server ingests them all; grouping and box numbers are per camera; the review page
      says which camera an event came from. A second Pi is a second node, not a second project.
- **Done means** for both: a test installation with two cameras and a different number of boxes
  in each ingests, groups and records cleaning counts correctly, and no code, test or doc
  outside configuration says "three".

## C.4 Calibration per installation

- [ ] **The pixel numbers are this mount's.** `MOTION_THRESHOLD`, `EVENT_BOX_DISTANCE_PX`, the
      blob kernels and A.3's future size band all depend on mounting height, resolution and the
      size of the cats. Label every default as "from one installation", and turn A.2's derivation
      into a documented procedure anyone can run: a week at a low threshold with `METRICS_CSV`
      on, label the visits, run a script that proposes the values. **Done means** the README has
      a calibration section and the script works on a metrics CSV and a database other than this
      installation's.

## C.5 Storage anywhere

- [ ] **Clips go to any mounted filesystem, or stay local.** The share does not have to be a NAS:
      another computer's SMB or NFS share, a USB disk or cloud storage mounted with rclone all
      work if they are a real mountpoint (so an absent mount cannot fill the SD card), rename
      within them is atomic (so no reader sees a half-written clip), and the review side can read
      them. Cloud mounts make strip building slow, since ffmpeg reads each whole clip. Add an
      explicit local-only mode, so a single Pi can record and review with no second machine; today
      a plain directory is refused and nothing reaches the review page. The README's `soft` mount
      advice is CIFS-only: NFS mounts `hard` by default, which stalls the transfer and review
      threads. **Done means** the README lists the destinations and their requirements, and a
      test runs the whole path in local-only mode.

## C.6 Times stored in UTC, shown in local time

- [ ] Everything stored records UTC with an explicit `Z` or offset: clip names, sidecars, the
      database, the metrics CSV. Everything a person reads shows local time in a readable form
      (the review page takes the browser's zone and locale; logs take the Pi's). This removes the
      daylight-saving collisions the recorder works around today, makes time-of-day analysis
      (B.6) independent of how the Pi's zone was set, and absorbs B.3's "Dates are machine-shaped".
      Existing data was written in the Pi's local time: ingest reads an unmarked time as local
      and converts it, and names already on the share stay as they are.
      **Done means** every stored time carries its zone, a test crosses both daylight-saving
      changes, and no page or log shows a raw ISO string.

## C.7 Choices to make options, flagged for the next developer

Each works as it is for this installation and would bite someone else:

- [ ] The web port is fixed at 5000 (`src/web_app.py`); make it a setting.
- [ ] The service account needs the `video` group for the camera; say so in the README and in
      `install-service.sh`.
- [ ] Cat names that collide with the page's filters or counts (`total`, `labeled`, `multi`,
      `multi_clip`) are accepted; refuse them the way `cat` and `multiple` are refused.
- [ ] Digit keys stop at nine cats, and match `e.key`, so on AZERTY the digits need Shift. Match
      `e.code` for digits, and give a tenth cat a way in.
- [ ] Training hardware is a choice: the plan names the Jetson (B.5), but any machine with a GPU,
      a Hailo accelerator, or a CPU for a small model will do. Keep the handoff (crops and labels
      on the share) the only contract.
- [ ] `tests/manual/check_page.py` looks for `chromium` by that name; accept the other names
      Chromium and Chrome install under.
- [ ] The README's list of gates leaves out node and the JavaScript lint.

---

# Risks and open questions

**Security.** The Flask app binds `0.0.0.0` on the dev server with no authentication, which
already warns about production use. Adding a UI that browses stored images makes exposure
worse. Keep it LAN-only, and add auth and a real WSGI server (waitress/gunicorn) before it is
reachable from anywhere else. Do not port-forward it.

**Storage growth and retention.** 10 Mbps is ~75 MB per minute of recording, and crops add to
that. Clips took 31 GB from 2026-08-25 to 2026-09-26, with 467 GB free on the share.

- [ ] Set a retention policy before space runs short. Full clips are the bulk and are the least
      valuable once an event is labeled and cropped.

**Power.** The Pi has powered off without a shutdown four times (latest 2026-09-24), a PMIC
standby whose cause is not known; pi-wake brings it back within 10 minutes. Each one is a gap in
the cats' record, and a gap can look like the very change the project exists to flag. The
official 27 W supply is the next experiment.

**Coats and litter.** Detection has been tested on three cats' coats and one litter, and other
combinations may do worse. Untested so far: a dark cat on dark litter gives a weaker foreground
signal; a mid-dark cat on pale litter may be classed as shadow and not counted at all (MOG2 calls
a pixel shadow when it is darker than the background by up to half, by OpenCV's defaults; not
measured here); and the colour baseline in B.5 may not separate similar coats. None of this can
be settled without other cats. Leave the thresholds configurable and report what other
installations see.

Still open:

- Is the floor around the boxes plain, or patterned/reflective in ways that fight background
  subtraction?
