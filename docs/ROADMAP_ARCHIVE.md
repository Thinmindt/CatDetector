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
*on-Pi* inference would be CPU. Local disk is ext4 with ~210 GB free. `/mnt/nas` is a **CIFS**
share (~497 GB free) — this matters for storage decisions in B2. Jetson model and JetPack version are not yet recorded; JetPack pins its own Python (3.10 on
JetPack 6) and PyTorch comes from NVIDIA's wheels, so the training environment is separate from
this repo's `==3.13.*` pin and should not try to share it.

Status: written 2026-08-24, after fixing clip saving and the startup false trigger.

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
      passes 150 on 0.017% of frames and never passed 228) and `METRICS_CSV=~/metrics_eval_20260914.csv`,
      so a visit below the threshold still leaves a trace.
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
      black clips but not the gap. **The boxes have been lit around the clock since 2026-09-16:**
      nights since 09-18 average ~125/255 in the metrics CSV and record like days, so the gap is
      closed as long as the light stays on. The loop was
      confirmed at 30.0 fps on hardware (2026-09-12) with `METRICS_CSV` on and recording, all 16
      columns written, so the cleaned-blob and brightness work carries no measurable cost.
      Collected so far (2026-09-10):
      - About 5.5 h of per-frame metrics in `~/metrics*.csv`, split into one file per setting.
        The first 2.6 h, in `metrics_narrowfov_20260910.csv`, used the old cropped view and
        can't be compared with the rest.
      - 9 labelled clips, 2 of them cats.

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
      failed to start a clip. Boxes: left in all four visits, right in the 15:48 visit, middle not
      at all. That visit crossed from the left box to the right inside a *single* clip — an instance
      of A.4's known limit, since grouping places a clip by one trigger position and cannot split
      one clip between boxes. 30.0 fps all day, no restarts, 51–55 °C, no throttling.

      **That evening and overnight.** 11 more clips between 21:37 and 23:17, most of them the dark
      cat at the **middle box** (trigger centroids x≈350–400), in stays of one to three minutes, plus
      one at the left box at 22:10. All three boxes are now attested: left (four visits), right
      (15:48), middle (21:37 onward). Two cats are clearly distinguishable by coat — a calico and a
      dark one — which is the B.5 premise holding up. **The dark suppression got its first real
      test:** the room light went off during the 23:16:53 clip, the recorder logged "Scene is dark;
      ignoring motion" at 23:16:55 and recorded nothing until morning. The share grew ~1 GB
      overnight, against 4.6 GB in the two hours of black clips the night before the change.

      **When this run has enough data** (decided 2026-09-15). The trigger is tuned to
      the weakest visit, not the average one. The two visits on 2026-09-15 ran 3,000–11,000 px while
      moving — twenty to seventy times the threshold — but 325 px on the entry frame (15:14) and
      47–130 px while the cat stood still (06:11). Entry and stillness are therefore the binding
      cases, and both are rare events, so the unit to collect is **visits, not hours**. At an
      expected 5–10 visits a day from 2 cats and 3 boxes, that is roughly a week of the lit setup.
      Stop when all of these hold:
      - **50+ labelled visits, 100 preferred.** Zero misses in 50 only bounds the true miss rate at
        ~6% (95%); at 100 it is ~3%. Recall is the whole point, so prefer 100.
      - **10+ of them at night under the LED.** More sensor gain, more noise, less contrast: a
        different regime, not a sample of the same one.
      - **15+ per cat.** A dark cat on dark litter gives a weaker signal than a pale one; the two
        cats seen on 2026-09-15 already look different.
      - **All three boxes used.** The far box covers fewer pixels and may read weaker.
      - **Several days of varied weather, including one overcast.** Sunlight crossing the floor is
        the main daytime false trigger.
      - **The smallest entry blob has stopped dropping over the last ~20 visits.** The real
        convergence test: tuning follows the weakest case, so keep going while it keeps getting
        weaker.

      Two things are not the bottleneck. False positives are measurable from hours rather than
      visits, and any candidate rule can be replayed against the metrics CSV without re-recording
      anything. Storage is ~130 MB/day of metrics plus clips against 209 GB free. The bottleneck is
      **labelling**, which is one person's keystrokes: ~10 clips a day has to be kept up with, or
      the backlog outlives the run.

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
   never undoes a human correction. The 9 clips already in the DB predate sidecars; re-ingest
   them as one-clip events.
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

---

# Part B — knowing which cat

## B.2 Storage

Moved 2026-09-26: the finished items and the schema sketch. The built schema is in
`src/review/capture_db.py`; the `crop` table carries on in the roadmap.

- [x] **SQLite on local disk, media on the NAS.** SQLite's locking is unreliable over CIFS,
      and `/mnt/nas` is CIFS — a database file there risks corruption under concurrent
      access. `captures.db` lives on the ext4 root (210 GB free) and stores NAS *paths*.
- [x] Images and clips stay on the NAS. Do not put image blobs in SQLite.

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
    path         TEXT NOT NULL UNIQUE,           -- on the NAS
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
      labelled before this lands (31 events on 2026-09-20), and it remains the honest answer when
      the tiles do not show the coat. `/review?filter=cat` is then the per-cat backlog: walk it
      and press a digit, and the visit leaves the filter. Nothing is migrated.
- [x] **`multiple`** (`m`) for two cats in one event, the B.1 edge case: excluded from
      single-label training, kept as a visit.

**From use, 2026-09-19.** Three things make the page hard to work with now that it is used
daily, one from 2026-09-20, and the review queue agreed 2026-09-21. They follow the per-cat
labels, in the order listed:

- [x] **It is used from a phone, and the buttons are too small to hit** (done 2026-09-22, merged as
      PR #4; the screenshot check is `tests/manual/check_page.py`). The phone is a Pixel 9
      (412 x 915 CSS px in portrait). The page has no `viewport` meta tag, so the phone renders it
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
        bar, two tile columns and no horizontal scroll, and a sitting on the Pixel 9 labels a
        backlog without a mis-tap. The screenshot is the agent's check; the sitting is the
        owner's.

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
  subtraction?
- Consistent lighting, or does daylight sweep across it?

---

# When this became a service

Moved 2026-09-26: done 2026-09-14. Retention carries on under the roadmap's storage risk.

**Done 2026-09-14:** `deploy/catdetector.service.in`, installed with `deploy/install-service.sh`. The
reasoning for each setting is in DESIGN.md (Process). Two items below changed on the way in: the
clock wait is bounded instead of enabling `systemd-time-wait-sync.service`, and the share is
wanted rather than required.

- **Start at boot and restart on failure** (deferred 2026-09-10, done 2026-09-14). That day the Pi
  powered itself off at 12:57 (overheating suspected) and was switched back on at 17:23, but
  main.py was started by hand, so nothing was recorded until someone restarted it. The unit has
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
  since journald adds its own.
- **SIGTERM runs the same cleanup as Ctrl-C** (done 2026-09-11, verified on hardware 2026-09-12).
  `kill -TERM` mid-clip logged "Received SIGTERM", stopped saving, promoted the clip to its final
  name and flushed the metrics.
- **Clips cut off by a crash or power loss are recovered at startup** (done 2026-09-11, commit
  3ebf28e, verified on hardware 2026-09-12). `kill -9` mid-clip left a 15 MB `.part`; the restart
  logged "Recovered ... cut off by an unclean shutdown", `ClipTransfer` shipped it, and the
  recovered clip decoded to 365 frames with no ffmpeg errors.
- ~~`RequiresMountsFor=/mnt/nas` — otherwise clips vanish into an empty mountpoint.~~ Obsolete
  since clips are staged locally and `ClipTransfer` refuses a non-mountpoint. Requiring the mount
  would only stop recording when the NAS is down at boot, so the unit wants it instead.
- Clips stay raw `.h264` on the NAS. Playback in the review UI remuxes on demand (B.3), so the
  capture path never runs ffmpeg; revisit only if something else needs seekable files.
- Retention/pruning, per the storage note above.
