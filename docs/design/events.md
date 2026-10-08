# From clips to events: decisions

The decisions behind [From clips to events](../DESIGN.md#from-clips-to-events) in
[DESIGN.md](../DESIGN.md), each with the measurement, the failure or the constraint that settled it,
and when. Start from the summary there; read the decision here before changing what it covers. Words
with a meaning of their own are in the [glossary](../DESIGN.md#glossary).

## Contents

- [From clips to events](#from-clips-to-events)
  - [Clips are grouped into events at ingest, by one rule, from the sidecars](#clips-are-grouped-into-events-at-ingest-by-one-rule-from-the-sidecars)
  - [Ingest reads only the sidecars of clips it has not seen (2026-09-16)](#ingest-reads-only-the-sidecars-of-clips-it-has-not-seen-2026-09-16)
  - [Event ids are stable across rescans](#event-ids-are-stable-across-rescans)
  - [Labels follow clips, not events](#labels-follow-clips-not-events)
  - [The label table is separate from event (per the roadmap)](#the-label-table-is-separate-from-event-per-the-roadmap)
  - [A labeled event can be put back in the queue with its label (2026-09-27, ROADMAP B.3)](#a-labeled-event-can-be-put-back-in-the-queue-with-its-label-2026-09-27-roadmap-b3)
  - [A staging row is written against the event's first clip (2026-09-27)](#a-staging-row-is-written-against-the-events-first-clip-2026-09-27)
  - [A peek is a mark beside the label, not a label (2026-10-03, ROADMAP B.3)](#a-peek-is-a-mark-beside-the-label-not-a-label-2026-10-03-roadmap-b3)
  - [Poop counts are typed from the cleaning clip, one per box (2026-09-16, ROADMAP B.5b)](#poop-counts-are-typed-from-the-cleaning-clip-one-per-box-2026-09-16-roadmap-b5b)
  - [Split and join are overrides on clips, not edits to events](#split-and-join-are-overrides-on-clips-not-edits-to-events)
  - [A join that touches an undated clip is refused (2026-09-27)](#a-join-that-touches-an-undated-clip-is-refused-2026-09-27)
  - [`--regroup` refuses while another process has the database (2026-09-27)](#--regroup-refuses-while-another-process-has-the-database-2026-09-27)
  - [A clip with no sidecar is `recovered`](#a-clip-with-no-sidecar-is-recovered)
  - [Media URLs name the clip (2026-09-27)](#media-urls-name-the-clip-2026-09-27)
  - [A database from before grouping is refused, not migrated](#a-database-from-before-grouping-is-refused-not-migrated)

## From clips to events

### Clips are grouped into events at ingest, by one rule, from the sidecars

`event_grouping` holds the rule as a pure function over `ClipRow`s so it can be tested against
synthetic timelines with no database. `CaptureDB` calls it wherever clips are placed: at ingest,
for every rescan; in `regroup`, on demand; and after every split or join. The thresholds
(`EVENT_GAP_SECONDS`, `EVENT_BOX_DISTANCE_PX`) are `CaptureDB` constructor arguments with no
defaults of their own; `main.py` passes them from `Config`, which reads the environment when the
process starts, so a changed setting takes effect at the next start or `main.py --regroup`.

### Ingest reads only the sidecars of clips it has not seen (2026-09-16)

Reading every sidecar
and letting `INSERT OR IGNORE` discard the known ones costs one CIFS round trip per clip in the
archive on every rescan, and the archive only grows: the startup ingest, and every rescan from the
review page, would take longer each day. `ingest` selects the known paths once and skips them.

### Event ids are stable across rescans

Links into the page (an address, a bookmark) and the undo history
hold event ids, so `ingest` may extend an event or create one but never renumbers.
Each group keeps the lowest event id already among its clips. A clip that arrives late, held back
by an outage of the share, can bridge two existing events; they merge under the lower id. Only `regroup`
rebuilds from scratch, and then ids do change, which is why it is a deliberate command and not part
of a rescan.

### Labels follow clips, not events

Before any placement pass, every clip remembers its
event's label. Afterwards an event keeps a label its clips agree on and drops one they contest,
returning to the review queue with a warning in the log. That is what lets `regroup` change the
gap threshold without discarding a day of labeling, and what stops a bridge merge from silently
attributing a not-cat clip to a cat.

### The label table is separate from event (per the roadmap)

The label table is separate from event (per the roadmap), so predictions have somewhere to go,
but its key is still the event: an event holds one label, and writing one replaces its value and
source alike. Before a model's prediction can sit beside a human label (B.6), the key must become
`(event_id, source)`, or predictions get a table of their own (roadmap B.2).

### A labeled event can be put back in the queue with its label (2026-09-27, ROADMAP B.3)

The review queue is every unlabeled event and every **staged** one, in time order, and a label
press takes an event out of it either way. Staging exists because the queue was implicit: the only
way back into it was to erase a label, which loses the judgment being checked. A staged event keeps
its label, so confirming it is the same keystroke that made it; the page shows the reason beside
the label. A staging row names who staged it (`human`, `agent` or `model` through the API, and
`grouping` or `calibration` from the code itself), so the same queue serves a reviewer's re-audit,
an agent's finding and, later, a model's low-confidence prediction.

### A staging row is written against the event's first clip (2026-09-27)

A staging row is written against the event's first clip (2026-09-27) and read back across
all of its clips. If a merge brings two staged clips together, the event shows the earlier
staging, and a label clears both.

### A peek is a mark beside the label, not a label (2026-10-03, ROADMAP B.3)

A cat that looks
in and uses no box is still that cat, which is what the classifier learns from, but it is not a
visit, and visits are what the data run counts and what a habit alert compares. A label of its
own would lose which cat it was, so the label keeps the cat and a `peek` row marks the event. The
row is written on **every** clip of the event, not the first as staging is, and an event is a
peek only while all its clips carry it: a join with a real visit, or a new clip grouped in at
ingest, makes the whole a visit, while a split leaves both halves peeks. A merged event that
might hold a visit is therefore counted as one, which is the error recall prefers. The rows of
an event that is no longer a peek are deleted, so a later split cannot revive the mark, and when
ingest or a regroup caused it rather than the reviewer, the event is staged (source `grouping`)
so someone looks at the clip that changed it. A label that is not a cat clears the mark. The `p`
key toggles the mark and stays on the event, so a peek is `p` and then the cat's digit.

### Poop counts are typed from the cleaning clip, one per box (2026-09-16, ROADMAP B.5b)

Whoever scoops signals the count by hand over the box; the reviewer reads it out of the clip.
The rows are written against the event's first clip, as staging is. The box is a **position in
frame, numbered left to right** — `BOXES` in `timeline.py` holds 1 to 3 until ROADMAP C.3 makes
it a setting — not a box identity: nothing in the pipeline knows one box from another, and the
signal is made over the box concerned.

### Split and join are overrides on clips, not edits to events

A split marks the clip that begins
the new visit (`boundary = 'split'`); a join marks every clip of the two events but the earliest
with the path of the clip before it in time (`boundary = 'join'`, `joins = <path>`). Both then
re-run the ordinary placement, so the rule and the overrides always agree and `regroup` reproduces
the reviewer's decisions. The split's later half starts unlabeled, because it is a visit the
reviewer has not judged; the first half keeps its id and label. A join names its target clip
rather than "the previous event" so that interleaved boxes cannot make it land on the wrong visit.
It marks every clip, not only the later event's first, because placement runs in time order and
honors a join only to a clip already placed: when two events' clips interleave (two cats, two
boxes, at once), the later event's first clip starts before the earlier event's last, and the
single mark it used to write was silently ignored (2026-10-03). A join that grouping still keeps
apart is refused and rolled back rather than reported as done.

### A join that touches an undated clip is refused (2026-09-27)

Grouping places only clips with a
start time; one whose name did not parse stands alone whatever its overrides say. A join onto one
recorded an override that grouping then ignored, and the page showed a merge that had not happened.
The API answers 409 with the reason instead.

### `--regroup` refuses while another process has the database (2026-09-27)

A regroup renumbers
events, and the review page holds event ids: a label posted during a regroup, or after it by a page
loaded before it, lands on whatever event now has that id. A transaction would stop the first and
not the second. So every `CaptureDB` holds a shared `flock` on `<database>.lock` for as long as it
is open, and `regroup` takes it exclusive without waiting, refusing if anything else holds it. A
separate file, because closing any descriptor of the database file itself drops the POSIX locks
SQLite holds on it. Linux's `flock` drops the shared lock before it tries for the exclusive one, so
a refused regroup takes the shared lock back. Opening waits up to a minute for a regroup's exclusive
lock rather than failing, so a service restarted while one runs still gets its Review tab.
`--stage` takes the shared lock like the service does and runs beside it, since staging renumbers
nothing.

### A clip with no sidecar is `recovered`

It was cut off by a crash before the recorder could write one, or the write failed, which the
recorder logs before shipping the clip anyway. A sidecar that cannot be read or parsed counts the
same, and since ingest never reads a known path again, one failed read from the share leaves a clip
`recovered` for good. Its start comes from the filename and its end is estimated from size at
`H264_BITRATE` (`BYTES_PER_SECOND` in `clip_scan`). It has no centroids, and the rule lets an
unknown position match on time alone rather than strand the clip in its own event.

### Media URLs name the clip (2026-09-27)

The strip, frame and video routes are
`/review/clip/<clip id>/...`. Named by event and index, one URL served a different clip after a split,
join or regroup, and a browser that had cached it showed the previous clip's tiles. A clip's files
never change, so a URL that names the clip is safe to cache for good.

### A database from before grouping is refused, not migrated

The only database that predated
grouping held a handful of test events and was carried across once by hand (2026-09-12).
Migration code for them would outlive its purpose; a clear error at startup is enough.
