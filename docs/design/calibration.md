# Calibration: decisions

The decisions behind [Calibration](../DESIGN.md#calibration) in [DESIGN.md](../DESIGN.md), each with
the measurement, the failure or the constraint that settled it, and when. Start from the summary
there; read the decision here before changing what it covers. Words with a meaning of their own are
in the [glossary](../DESIGN.md#glossary).

## Contents

- [Calibration](#calibration)
  - [A data run ends in one command, `main.py --calibrate` (2026-10-03, ROADMAP A.2)](#a-data-run-ends-in-one-command-mainpy---calibrate-2026-10-03-roadmap-a2)
  - [A visit's strength is the largest blob it reaches](#a-visits-strength-is-the-largest-blob-it-reaches)
  - [What a data run counts (2026-10-03, ROADMAP A.2)](#what-a-data-run-counts-2026-10-03-roadmap-a2)
  - [A threshold is chosen by how much of each visit it records, not only by whether it starts a clip](#a-threshold-is-chosen-by-how-much-of-each-visit-it-records-not-only-by-whether-it-starts-a-clip)
  - [A box is used where the cat stays, not where it comes in](#a-box-is-used-where-the-cat-stays-not-where-it-comes-in)
  - [The replay runs the detector's rules over every frame it acted on](#the-replay-runs-the-detectors-rules-over-every-frame-it-acted-on)
  - [The settings come from the service, not the shell](#the-settings-come-from-the-service-not-the-shell)
  - [Doubts go to the review queue, each once, and hold back the result](#doubts-go-to-the-review-queue-each-once-and-hold-back-the-result)
  - [Boxes are the thirds of the frame until C.3](#boxes-are-the-thirds-of-the-frame-until-c3)

## Calibration

### A data run ends in one command, `main.py --calibrate` (2026-10-03, ROADMAP A.2)

Every installation
needs its own numbers, so the counting, the derivation and the replay are code anyone reruns
rather than an analysis done once by hand; `src/review/calibration.py` holds it, and reads the
metrics CSV by its header, so it needs nothing from the capture side.

### A visit's strength is the largest blob it reaches

A visit's strength is the largest blob it reaches, read from the CSV between its first clip's
start and its last clip's end, over lit frames. The blob that started a clip is the wrong
measure: it is set by the threshold the run used, by a head peeking in at the frame's edge, or,
when a visit is split in two, by a sitting cat's twitch. In the run that motivated this
(2026-10-03), the smallest starting blob was 29 px, the second half of a split visit, while the
weakest visit's largest blob was 2,768 px: a gray coat on the litter, whose cleaned blob breaks
into pieces (18,419 px of foreground around it). A trigger that ignores a peek still fires when
the whole cat steps in, so what decides recall is whether each visit ever reaches the trigger.

### What a data run counts (2026-10-03, ROADMAP A.2)

A visit is an event labeled with a cat
(`cat`, a cat's name or `multiple`), not a peek, that started at or after `--since` and that the
metrics CSV covers with a cleaned blob. Per-cat counts go by the label, so a visit labeled `cat`
or `multiple` counts toward the total and toward no cat's. A visit split into two events counts
twice until the reviewer joins them; the command does not join them, since only the footage
says whether it was one visit, but stages the earlier of the two with that question, naming
the later. Night is a visit starting
from 20:00 to 06:00. The weakest visit has settled when, with more than 20 visits, none of the
last 20 is weaker than the weakest of those before them. The weather is the reviewer's call:
the camera's auto-exposure holds the CSV's brightness level through the day, so an overcast day
does not show in it.

### A threshold is chosen by how much of each visit it records, not only by whether it starts a clip

Today one threshold both starts a clip and keeps it going, so a high one closes clips while
a cat sits still, which is the part of a visit worth seeing. Replayed on one run (2026-10-04), half
the weakest visit's peak (2876) missed no visit but recorded a median 86 % of each and left 53 of
129 visits under 80 %; 600 recorded 99 % and still cut false triggers by a third. So the command
replays thresholds doubling from the run's up to half the weakest visit's largest foreground count
and proposes the highest that misses none, records 98 % of the median visit, and leaves no more
than 5 % of the visits more under 80 % than the run's own threshold did, since a median says
nothing about the worst half. If none qualifies, not even the run's own, it says so and proposes
nothing. It also replays the two triggers A.3 plans: that top threshold to start with the run's to
hold (0.75 false triggers an hour, against 0.90, with the median visit 100 % recorded and 10 under
80 %), and the band, its lower edge at half the weakest visit's cleaned blob and its upper a
quarter above the strongest (about 26,000 px from a cat leaping; peaks of 30,000 to 89,000 px came
with an exposure shift as a cat moved), never above half the frame.

### A box is used where the cat stays, not where it comes in

A visit's box used to be the third
holding its first clip's trigger, mostly the cat's head appearing at the frame's top edge: on that
run it gave the right box 4 visits, while by where the cat stayed it had 42, and at least 36 of 129
visits stayed in more than one box. The CSV carries the cleaned blob's position on every frame, and
a stay in a box runs from the blob's first sighting there, at least half the weakest visit's peak,
to its last before it shows in another box. Its vanishing does not end the stay: a cat sitting
still fades into the background within seconds, so its blob can be gone for most of a squat (a rule
that ended a stay after a second without the blob found the right box used in 11 visits). Passing
back and forth makes separate short stays, so it does not add up to one. A box is used in a visit
with a stay of 10 s, and each box's weakest blob is reported, since a far or thin box could read
weaker. The review side still places events by their trigger until A.4 records where the cat
stayed.

### The replay runs the detector's rules over every frame it acted on

The replay runs the detector's rules over every frame it acted on: lit frames, and not the
30 frames after a restart (a gap of over two seconds in the CSV) while MOG2 warms up, since the
whole first frame reads as foreground. A restart also ends the clip being recorded at the last frame
before the gap, and the next clip's pre-roll reaches back no further than the restart. A clip's span
is the recorder's: from its first trigger, less the 5 s pre-roll but never before the end of the
clip before it, to `MOTION_TIMEOUT` after its last, and cut at the recorder's 300 s limit, the next
trigger opening another. A visit is missed when no trigger falls inside it: a clip that runs on from
a neighboring visit covers part of it without having triggered on it, so coverage alone would
flatter a high threshold. A clip that starts outside every visit is a false trigger. A visit's span
reaches back one second before its first clip's recorded start, because the detector logs the
frame that starts a clip a moment before the recorder notes the time; without that, every visit
added one false trigger to the run's own threshold. With these corrections the replay of the run's
threshold (150) gave 1.69 false triggers an hour, against 1.61 an hour of clips the detector really
recorded with no cat in them, close enough to compare settings. The CSV is read twice, once for the
peaks and once for the replay, rather than held in memory; rows a power cut or a tail still being
written left damaged are skipped and counted.

### The settings come from the service, not the shell

A data run's `MOTION_THRESHOLD` and
`METRICS_CSV` live in the service's drop-in, which a shell never sees, so `--calibrate` reads them
with `systemctl show catdetector --property=Environment` and says where its settings came from;
`--metrics` overrides the CSV. It holds the database open throughout, and with it the shared lock,
so a `--regroup` cannot renumber the events between the count and the staging.

### Doubts go to the review queue, each once, and hold back the result

The command stages what
needs a person's eye, with the question, under the source `calibration`: the same cat seconds
apart, the weakest visits, a visit whose frames show no blob at all (left out of the counts, since
a strength of 0 would drag every edge to 0), and `not_cat` events that held a blob the size of a
visit (between the weakest visit's peak and the 95th percentile of peaks) for as long as nine
visits in ten do. The last rule comes from the visits rather than a fixed size because people and
lighting changes produce visit-sized blobs too: a fixed 0.5 s staged 72 events of one run, the
visit-derived span 17. An `asked` row, on the event's first clip and keyed by the question, stops a
rerun asking again once the reviewer has answered; an event already staged is left alone and asked
another time. While any calibration question is unanswered the settings are printed as
provisional, without the drop-in lines, since the answers decide which visit is the weakest.

### Boxes are the thirds of the frame until C.3

Boxes are the thirds of the frame until C.3, and the command asks for a visit in each third
named by `--boxes` (all three by default), so an installation with fewer boxes can finish a run.
