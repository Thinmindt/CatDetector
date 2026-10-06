# Showing clips and the review page: decisions

The decisions behind [Showing a clip](../DESIGN.md#showing-a-clip) and [The review
page](../DESIGN.md#the-review-page) in [DESIGN.md](../DESIGN.md), each with the measurement, the
failure or the constraint that settled it, and when. Start from the summary there; read the decision
here before changing what it covers. Words with a meaning of their own are in the
[glossary](../DESIGN.md#glossary).

## Contents

- [Showing a clip](#showing-a-clip)
  - [ffmpeg, not cv2.VideoCapture](#ffmpeg-not-cv2videocapture)
  - [Cache entries are published by rename (2026-09-16)](#cache-entries-are-published-by-rename-2026-09-16)
  - [Tiles span the whole clip (2026-09-17)](#tiles-span-the-whole-clip-2026-09-17)
  - [The stride comes from the footage, not the sidecar](#the-stride-comes-from-the-footage-not-the-sidecar)
  - [One decode serves both the strip and the full frames](#one-decode-serves-both-the-strip-and-the-full-frames)
  - [Extraction decodes keyframes only (2026-09-17)](#extraction-decodes-keyframes-only-2026-09-17)
  - [Strips are built before the page asks (2026-09-27, ROADMAP B.3)](#strips-are-built-before-the-page-asks-2026-09-27-roadmap-b3)
  - [Leaving an event cancels its strips (2026-09-26)](#leaving-an-event-cancels-its-strips-2026-09-26)
  - [Video is remuxed on demand, in the review server, never on the capture path](#video-is-remuxed-on-demand-in-the-review-server-never-on-the-capture-path)
- [The review page](#the-review-page)
  - [Single-page UI](#single-page-ui)
  - [The address is the position (2026-09-26, ROADMAP B.3)](#the-address-is-the-position-2026-09-26-roadmap-b3)
  - [An empty walk names itself and points at the queue (2026-09-28)](#an-empty-walk-names-itself-and-points-at-the-queue-2026-09-28)
  - [The page never hides the grouping (2026-09-12)](#the-page-never-hides-the-grouping-2026-09-12)
  - [The multi-clip audit walks time order, not the multi-clip list (2026-09-16)](#the-multi-clip-audit-walks-time-order-not-the-multi-clip-list-2026-09-16)
  - [Label shortcuts ignore modified and repeated keys (2026-09-22)](#label-shortcuts-ignore-modified-and-repeated-keys-2026-09-22)
  - [The label buttons are a bar fixed to the bottom of the screen (2026-09-22, ROADMAP B.3)](#the-label-buttons-are-a-bar-fixed-to-the-bottom-of-the-screen-2026-09-22-roadmap-b3)
  - [The cats' names are configuration, not code (2026-09-21, ROADMAP B.3)](#the-cats-names-are-configuration-not-code-2026-09-21-roadmap-b3)
  - [Times are shown in the browser's locale (2026-09-28)](#times-are-shown-in-the-browsers-locale-2026-09-28)
  - [The timeline filters once, on the server (2026-09-28, ROADMAP B.6)](#the-timeline-filters-once-on-the-server-2026-09-28-roadmap-b6)
  - [The timeline query uses an index on `clip(event_id)` (2026-09-28)](#the-timeline-query-uses-an-index-on-clipevent_id-2026-09-28)
  - [The strips are drawn for a thumb (2026-09-28)](#the-strips-are-drawn-for-a-thumb-2026-09-28)
  - [The MJPEG stream is torn down when its tab is hidden](#the-mjpeg-stream-is-torn-down-when-its-tab-is-hidden)
  - [The page is modules with no build step (2026-09-28, ROADMAP B.6)](#the-page-is-modules-with-no-build-step-2026-09-28-roadmap-b6)
  - [d3 is vendored from a pinned package (2026-09-28)](#d3-is-vendored-from-a-pinned-package-2026-09-28)
  - [Page changes are checked by screenshot (2026-09-22)](#page-changes-are-checked-by-screenshot-2026-09-22)

## Showing a clip

### ffmpeg, not cv2.VideoCapture

VideoCapture cannot handle the raw elementary streams the
recorder writes: on a real clip it reported a garbage frame count, failed its first read, and
could not seek. ffmpeg decodes the same files cleanly (~0.4 s for one full frame on a Pi 5,
2026-08-26; strip costs are under keyframes below), so extraction shells out. Results are cached on local disk keyed by
clip id — clips are immutable once closed, so the cache never needs invalidating.

### Cache entries are published by rename (2026-09-16)

Flask serves the review UI threaded, and
ffmpeg creates and truncates its output before it writes anything, so a check-then-write on the
final cache path has two failure modes: a second request for the same clip can see the file exist
and be served a truncated MP4 or JPEG, and two requests can run ffmpeg against one path and leave
a corrupt entry cached for good, since nothing invalidates the cache. Each artifact is written
to a unique `<name>.<hex>.part.<ext>` beside it and renamed into place; the rename is atomic within
the cache directory, and the real extension stays last because ffmpeg and cv2 choose the format
from it.

### Tiles span the whole clip (2026-09-17)

A strip of the first eight seconds — pre-roll plus
the trigger — is not enough for cat/not-cat: in one 96 s clip a cat used the left box for eighty
seconds, and eight early tiles showed one paw at the top of the last frame, so the reviewer
labelled it `not_cat`. Cut that way, every visit also looks like "a cat enters and never leaves",
because the exit is minutes past the last tile. The strip covers the file: one tile per second up
to `MAX_TILES` (48), then every 2, 3, … seconds so the count stays bounded (`tile_seconds`).

### The stride comes from the footage, not the sidecar

The sidecar's `started_at → ended_at` is
the trigger-to-close span, not the file's: the file starts up to `buffer_seconds` earlier, because
`CircularOutput.start()` flushes from the oldest keyframe in the ring (that 96 s clip has 101
keyframes for a 95.9 s sidecar), so a stride sized from the sidecar caps the strip short of the
exit. The build dumps every keyframe, counts them, and thins in Python; nothing about the clip's
length is needed up front.
Consequences: the stride is only known once the clip is decoded, so the cache name carries it
(`clip<id>_strip<stride>s.jpg`, found by glob) and the caption is drawn **into** each tile rather
than sent to the page. A caption is the offset into the file, the same clock the player shows,
not seconds since the trigger.

### One decode serves both the strip and the full frames

The keyframes are dumped at full size
(`-q:v 3`), the thumbs are resized from those, and the full frames behind the tiles are kept as
`clip<id>_t<second>s.jpg` — ≤ 48 per clip, ~4 MB (2026-09-18) — so a click on a tile is a cache
hit rather than a second read of a 300 MB clip over CIFS (measured 12 s for a late tile of the
largest clip, 2026-09-18). The
frames are published before the strip, so the strip's existence means the set is complete, and
`frame()` answers from the cache alone: a slot with no file is out of range.

### Extraction decodes keyframes only (2026-09-17)

Decoding all of a 96 s clip takes 9.5 s on a
Pi 5 (~320 fps); with `-skip_frame nokey` it takes 1.7 s, and a five-minute clip fits comfortably
inside the 60 s ffmpeg timeout even when read from the share. That works because the recorder's
encoder writes a keyframe every `CAMERA_FPS` frames, which is one a second only while the camera
runs at `CAMERA_FPS`. The keyframe interval is pinned in `src/`, with `iperiod=CAMERA_FPS` (libav's
default on the Pi 5 is also 30), so a picamera2 upgrade cannot change it. The frame rate is not:
nothing in `src/` sets it, and 30 fps comes from the `FrameDurationLimits` of (33333, 33333) µs
that picamera2's `create_video_configuration` applies by default. A picamera2 default that
changed, or a camera that ignored it, would put keyframes at another spacing and the tiles on the
wrong seconds. ffmpeg renumbers the surviving frames, so `n` counts keyframes, i.e. seconds; a raw
stream has no
timestamps to select on instead, which is why the tests build their fixture with libx264 at
`keyint=30` rather than through cv2, whose writer sets its own GOP.

### Strips are built before the page asks (2026-09-27, ROADMAP B.3)

On its first request a strip
costs a read of the whole clip and a keyframe decode, 1.7 s for a 96 s clip, and a multi-clip event
fires one per clip at once. `StripsAhead` is one thread that builds the missing strips of the next 30
events in the review queue, one clip at a time, woken after every ingest, label, stage, split, join
and rescan, and every ten minutes for changes another process made (`main.py --stage`); a wake-up
during a walk starts it over from the front of the queue, since the front is what the page shows
next. The database lock is held only while the queue is read. A walk reads the cache directory once
rather than globbing it per clip: with 7,683 entries a glob took 6.9 ms, about 280 ms a walk on
every label (2026-09-27). The thread renices itself to 10
before its first build, and the ffmpeg it starts inherits that, so a build yields to the encoder:
building 10 clips from the share took 17.7 s while the service beside it held 30.00 fps, its
longest frame gap 62 ms (2026-09-27). A clip whose build fails is skipped for ten minutes, or every
label would rerun a 60 s ffmpeg timeout; it is tried again after that, since the usual cause, the
share being away, passes. A page request for a strip being built waits for that build (one lock per
clip in `ClipFrames`) rather than decoding the clip a second time, un-niced. With the strips built,
a 3-clip event's three strips were served in 16 ms together (2026-09-27).

### Leaving an event cancels its strips (2026-09-26)

A strip not yet built holds a connection while
ffmpeg decodes its clip, and a browser opens only six to one host, so an event with six or more
uncached clips held up every other request to the server until the builds ended. On a 14-clip
event whose builds each ran into the 60 s ffmpeg timeout, a fetch took 60 s with the strips
pending and 4 ms after cancelling them (headless Chromium, 2026-09-26). So every action that moves
to another event removes the pending strips' sources before sending its own request, and the
page does the same on `beforeunload`: Chrome sends a reload's request before the old document
unloads, so `pagehide` comes too late. When the page stays on the event after all (a failed
label, a Back and Forward before the fetch returned, a return from the back/forward cache), it
requests the strips again. The build itself runs on to the end on the server.

### Video is remuxed on demand, in the review server, never on the capture path

`-c copy` into
MP4 costs ~0.7 s for an 18 MB clip on a Pi 5 (2026-09-12) and no CPU worth mentioning; the
result is cached like the strips. Doing it at clip close would put ffmpeg on the Pi's capture
side for every clip whether or not anyone ever watches it. The `<video>` element uses
`preload="none"` so opening an event does not fetch every clip. Flask's `send_file` honours
`Range`, which is what lets the player seek; the test asserts a 206, since `Accept-Ranges` is
advertised either way.

## The review page

### Single-page UI

Labelling navigates no pages — next event and images are fetched and swapped
in place — so the undo history can live in a JS array rather than sessionStorage, and labelling
is one keypress with no page load between events.

### The address is the position (2026-09-26, ROADMAP B.3)

The review page's address is
`/review/<event id>?filter=<walk>`, and one route, `/api/review/next?filter=&after=`, serves every
walk, so the address holds exactly what the page needs to resume: a reload, Back or a pasted link
shows that event, and the next request continues the walk after it. Moving to another event pushes a
history entry, so Back steps through the events seen; the first event shown replaces the bare
`/review` it was reached by, or Back would land on an address that resolves to the same event. An id
the database no longer has (a regroup renumbers events) says so and falls back to the walk's next
event. A response that returns after Back or Forward has moved the address is dropped, and a label
moves on to the next event only if the address still names the event it labelled; otherwise a slow
request would undo the reviewer's Back. The review queue takes `after` like the other walks, but
the page never sends it: labelling removes an event from the queue, staged or not, so its oldest
event is always next. Undo stays in page state; the address holds position, not history.

### An empty walk names itself and points at the queue (2026-09-28)

A phone restored an old
`/review?filter=cat` tab, a walk that no longer holds anything because every cat visit is labelled
by name, and the page said "Nothing left to review here" while dozens of events waited in the
queue; the only way back was a small link at the end of the counts line. An empty walk now says
which walk it is and offers the queue as a full-size button with the number waiting (unlabelled
plus staged).

### The page never hides the grouping (2026-09-12)

Every clip of an event
is rendered with its own strip, times, close reason and the gap since the previous clip; a
multi-clip event says so in the header; the counter reports `multi_clip`; and `?filter=multi`
walks every multi-clip event, labelled or not, so the grouping rule's output can be audited as a
whole rather than noticed one event at a time.

### The multi-clip audit walks time order, not the multi-clip list (2026-09-16)

`next_multi`
takes the position of `after_id` in the full time order, which a split leaves intact, and returns
the next event from there that still has several clips. Located within the multi-clip events
instead, an id that has stopped being multi-clip — exactly what a split does to the event the
reviewer is looking at — falls through to the first entry, and the audit silently restarts at the
oldest event instead of advancing.

### Label shortcuts ignore modified and repeated keys (2026-09-22)

`Ctrl+C` to copy a time from
the page labelled the event `cat`, `Ctrl+Z` undid a label, and a held key labelled several events
in a row. Each was a silent write to the training set. The handler returns on any modifier or
autorepeat before matching a key.

### The label buttons are a bar fixed to the bottom of the screen (2026-09-22, ROADMAP B.3)

On a phone the buttons sat under the tiles, so every label meant a scroll to the bottom and a
small target. A bar fixed to the bottom keeps them in the thumb zone from
anywhere on the page, which is the pattern Zooniverse's mobile classifier and Material's docked
toolbar use for the same job; every target is 48 px, the minimum Android's accessibility guidance
gives. The actions used a few times a sitting (plain `cat`, join, next, rescan) sit behind
`more` in a sheet, so the bar holds only what is pressed once per event. Undo is a snackbar
that names the label just recorded: it confirms a tap, which otherwise gives no feedback, and it
takes undo out of the bar without hiding it. The bar and the keys are disabled while a label
request is in flight, because a phone tap repeats more easily than a keypress and a double tap
would label two events. Keyboard hints are hidden on devices with no hover pointer, since a
phone has no keys and the hints cost width. A global `[hidden] { display: none !important }`
rule exists because the sheet's own `display: flex` beat the attribute and showed it on load.

### The cats' names are configuration, not code (2026-09-21, ROADMAP B.3)

`CAT_NAMES` is a
comma-separated list of any length; the review side takes it as `Review.cat_names` and derives the
set of label values from it: the four fixed labels, then a name per cat, then `multiple`. The
`label.value` column always took any text, so no schema changed, and `counts()` already grouped by
value, so the per-cat counts A.2 asks for came free. A cat's name may not be one of the fixed
values or `multiple`, and names may not repeat; `Review` refuses such a list at construction, which
`build_review` reports as "Review unavailable" rather than serving a page whose keys collide.
`cat` was kept with the meaning "a cat, which one not decided": it was the value of every visit
labelled before names existed, it stays the honest answer when the tiles do not show the coat, and
keeping it meant migrating nothing. The page builds the cat buttons and their digit keys from the
list in JavaScript with `textContent`, rather than in the template, so a name is never interpolated
into a script string. An empty list leaves the page exactly as it was.

### Times are shown in the browser's locale (2026-09-28)

The review header and the timeline show
`Sun 27 Sep, 22:59` with the relative time on hover, from `toLocaleString` and
`Intl.RelativeTimeFormat`. Stored times carry no zone and are the Pi's local time, which a browser
on the same network shares, so they are read as local; C.6 makes that explicit.

### The timeline filters once, on the server (2026-09-28, ROADMAP B.6)

`GET /api/timeline` takes
the filter (label chips, a day range, an hour range, labelled-before) as its query string, which
is also the page's address, and `timeline.build_timeline` applies it to one row per event, so
pytest checks the only implementation. Filtering in the browser as a handle moves would have meant
a second implementation in JavaScript with no test; instead the page asks when a handle is
released, which on the LAN answers in milliseconds, and meanwhile only dims the bars outside the
brush. Each strip's counts are computed under every filter but its own (crossfilter's rule), so a
strip always shows what its brush could select rather than only what it already has. A brush
cannot wrap, so an hour range across midnight is a brushed range plus an "outside the range"
toggle, which sends it reversed (`hours=22-6`). A drag whose two ends round to the same boundary
selects the one day or hour it touched. Stage-everything sends ids, not the filter, so exactly
what the reviewer saw is staged, and it waits while a newer filter is loading. Only labelled events
not already staged are taken, by the server as well as the page: an unlabelled event is in the
queue already, and restaging would overwrite the reason and source an event was staged with. A
response carries the first 500 rows, the total, and the ids stageable among all of them, so the
page stays small as the archive grows without narrowing what staging can reach. An address the
server would refuse, hand-edited or stale, is read as no filter rather than drawn as an error.
The box is the third of the analysis frame (`ANALYSIS_SIZE`, the coordinate frame of a sidecar's
blobs) holding the first clip's trigger. That is mostly where the cat came in, not the box it
used, so A.4 replaces it with the boxes the cat stayed in; C.3 makes boxes configuration.

### The timeline query uses an index on `clip(event_id)` (2026-09-28)

Two correlated subqueries
per event, the first clip's trigger and whether any clip is staged, each scanned every clip
without it: 39 ms for 375 events, growing with the square of the archive, under the lock every
review request waits on. With the index, 2.5 ms (2026-09-28). A test reads the query plan.

### The strips are drawn for a thumb (2026-09-28)

The brush's grab zone is 28 px, and the axes
leave 22 px at each end so the first and last labels fit. Day ticks are every nth day counted from
the first: d3's own day ticks restart at each month, which crowds the ticks either side of it.
d3's brush ignores a touch that moves only a few pixels, so a slice is a drag, not a tap.

### The MJPEG stream is torn down when its tab is hidden

Each viewer of `/video_feed` costs a
JPEG encode of every frame on the server, so the Live tab sets the image source when it is shown
and removes it when it is not, which closes the connection and ends the generator.

### The page is modules with no build step (2026-09-28, ROADMAP B.6)

One 424-line script served
both tabs through eleven page-wide variables, and the timeline would have doubled it. Browsers
load `<script type="module">` directly, so the script is split by tab (`live.js`, `review.js`,
`timeline.js`), with `common.js` for fetching, escaping and times and `app.js` for the tabs,
and each tab's state is private to its module. The modules are static files, linted by eslint
as one of the gates, with eslint pinned by `package-lock.json`; inline in the template, the
script was invisible to every gate and its syntax errors reached the browser (2026-09-22). A framework was not adopted: React, Vue or Svelte
each need a bundler, a build on the Pi before every restart and generated files kept in step with
the source, for one small page served on a LAN. The template passes its settings as one JSON
block (`<script type="application/json" id="config">`, written by Jinja's `tojson`, which escapes
`<`, so a cat's name cannot close the tag) rather than as page-wide constants, and buttons name
their action in `data-action`, dispatched by one listener per tab, rather than calling globals
from `onclick`, which modules do not expose.

### d3 is vendored from a pinned package (2026-09-28)

The timeline's strips need a brush that
drags by mouse and by touch, time scales and axes; d3 does all three, and hand-written dragging
is where touch handling goes wrong. `package.json` pins d3 exactly, `npm run vendor` copies its
bundle and licence to `src/static/vendor/`, and the copy is committed, so the Pi serves it with no
build and no network. A gate (`npm run vendor:check`) fails if the copy differs from the pinned
package, so an upgrade is a version bump plus `npm run vendor`. The whole bundle (280 KB, cached)
is served rather than the few d3 modules used: those import one another by bare name, which a
browser cannot resolve without a bundler or an import map. codespell and eslint skip the vendor
directory.

### Page changes are checked by screenshot (2026-09-22)

`tests/manual/check_page.py` serves the
app with no camera from a copy of the review database and shoots `/review` with headless
chromium at a phone's portrait size (412 x 915) and a laptop size; `--path` shoots any address,
such as a filtered timeline. It exists because the phone layout's one shipped bug, a sheet visible
on load, passed every gate; only a rendered page shows a layout.
It copies the database so it never writes the live one, and reuses the live frame cache, whose
entries are published atomically, so it can run beside the service.
