# Python Style Guide

Conventions for writing and reviewing Python in this project. Ruff and mypy enforce the
mechanical parts; this document covers the judgement they cannot.

The guiding idea: **the code should say what it does, and comments should say what the code
cannot.** Most style debates resolve once you ask which of those two a line is doing.

---

## Naming over narration

A comment that explains what the next line does is a missing name. Extract a predicate or
introduce a constant instead — names cannot drift from the code the way comments can.

```python
# avoid
# The previous job is still running and still owns the handle.
if self._worker is not None and self._worker.is_alive():
    return None

# prefer
if self._previous_job_is_still_running():
    return None
```

The same applies to literals. A number that needs a comment wants a constant:

```python
# avoid
sock.settimeout(5.0)  # give the remote end time to answer

# prefer
CONNECT_TIMEOUT_SECONDS = 5.0
sock.settimeout(CONNECT_TIMEOUT_SECONDS)
```

Prefer small, well-named helpers over long functions punctuated by section comments. If you
find yourself writing `# --- validation ---`, that block is a function.

## Comments

Comments describe **current behaviour**, not the reasoning that produced it. Keep them short.
Assume the reader is busy, competent, and already looking at the code.

Write one when the code cannot state the fact itself:

- surprising behaviour of a third-party call
- a constraint the code must respect
- a non-obvious consequence a caller needs to know

Do not write:

- what the next line does — name it instead
- why an alternative was rejected
- measurements, benchmarks, or history

```python
# avoid
# Two calls would consume two separate requests, so the frames would be
# different exposures and the loop would run at half rate (measured 15 vs 30/s).
arrays, _ = camera.capture_arrays(["main", "lores"])

# prefer
"""Both streams from one request."""
```

Rationale, benchmarks and rejected alternatives go in a **design document**, read once rather
than re-read on every visit to the file. A comment beginning "deliberately", "we used to", or
"this would otherwise" is describing a decision and belongs there.

The exception is a `noqa`, which needs its reason inline to be reviewable. Keep it to a clause.

## Design documents

The design document is where the reasoning lives, and it has a failure mode of its own: being
written from the vantage point of the day it was written. An entry is read months later by
someone who was not there, so each one gives three things and nothing else:

- **What** the decision is, in present tense, as a fact about the code.
- **Why** — the measurement, the failure mode, or the constraint. This is the part a reader
  cannot recover from the code.
- **When**, as a date on the decision: `**Draining happens off-thread** (2026-08-29).` It says
  how fresh the measurement is and gives `git log` a handle.

Then the rules that follow from that:

- **Never write a pending state.** "Still to be confirmed once the cooler is fitted" is false
  within a week and nothing flags it. Pending work goes in the roadmap, where an unchecked box
  *is* the status.
- **A rejected alternative is a fact about the world, not a story about the code.** "Two
  `capture_array` calls run at 15 fps", not "we used to make two calls". The first stays true
  after the function is renamed; the second is git's job, and it ages.
- **An incident is evidence, not narrative.** "A detector started by hand stays down after a
  power cut until a person notices; one such gap cost 4.5 h" carries everything the reader
  needs. The date of the incident, who noticed and what happened next do not.
- **Sample size is part of the measurement.** "From one visit" or "from 22 clips" is what lets
  the next person decide whether to trust the number or re-measure.

The test is the same one as for comments, one level up: would the sentence still be true and
still be useful if the reader had no idea when it was written?

## Plans

The roadmap is the plan as it stands today: what is decided, what is next, what is open. It is
the one document allowed a pending state, because an unchecked box *is* the status.

- **A feature starts as a roadmap entry** that says what, how, and **done means**: what a check
  or a person will see when it is finished. Write the entry and the code on one branch, entry
  first, so the box is ticked by the change that earns it.
- **Finished work leaves.** A done item, the history behind a decision and a measurement that
  fed a decision already made move verbatim to an archive beside the roadmap, under the same
  heading, with the date they moved.
- **Section numbers never change.** Commit messages and the design document cite them; the
  archive keeps the history under the number, and the roadmap keeps the plan.
- A to-do list of things only the owner can do follows the same rule: open items only, done
  ones in its archive.

## Docstrings

One line if one line does it. Say what the thing is for; add a second paragraph only for a
consequence the caller must know — what blocks, what raises, what runs on another thread.

Do not restate the signature. Types already do that, and the type checker enforces them.

```python
def capture(self) -> tuple[Frame, Frame]:
    """Both streams from one request. Blocks until the next frame."""
```

## Logging

Use a module-level logger; never `print()` in application code.

```python
log = logging.getLogger(__name__)
```

`print()` is block-buffered when stdout is not a terminal, so under a service manager the
buffered lines are lost if the process is killed rather than exiting cleanly. Logging also
gives levels, timestamps, and module names for free.

- **Lazy formatting.** `log.info("Saved %s", path)`, not `log.info(f"Saved {path}")`. The
  arguments are not rendered when the level is disabled.
- **`log.exception(...)` inside `except`.** It attaches the traceback and takes no exception
  argument.
- **Configure the root at `WARNING`, and raise only your own loggers to `INFO`.** A blanket
  `INFO` root switches on every third-party library, and chatty dependencies bury your output.

Standalone scripts whose output *is* the product are the one place `print()` is right. Say so
in the script's docstring so it does not look like an oversight.

## Interfaces versus internals

A leading underscore is a claim that nothing outside the class touches this. The moment another
class calls it, that claim is false — rename it. A private method with external callers is
worse than a public one, because it tells readers a boundary exists where it does not.

**Hardware sits behind a boundary.** Code that talks to a device — a camera, a sensor, a GPIO
pin, an accelerator — lives in one module per implementation, behind an interface the rest of
the program depends on. Build for the hardware you have, as the first implementation rather than
the only one: a replacement camera, a cheaper board or a second unit should be a new module, not
an edit through the whole program. Everything specific to the device (its sensor modes, formats,
frame rates, quirks) stays inside its implementation, and the docs name it as that
implementation, not as a requirement of the project. Enforce the boundary with the linter: ban
the device library's imports everywhere except its implementation, and test the rest of the
program against a fake of the interface.

```toml
[tool.ruff.lint]
extend-select = ["TID251"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"picamera2".msg = "only the camera implementation module may import picamera2"

[tool.ruff.lint.per-file-ignores]
"src/capture/picamera2_camera.py" = ["TID251"]
```

## Errors and exceptions

Catch what you can act on. A bare `except Exception` is defensible at a process boundary or in a
loop that must not die, and suspicious anywhere else.

Choose the log level by whether the failure is expected:

- **Unexpected** — a catch-all, a cleanup path, a bug: `log.exception(...)`, because the stack
  is the useful part.
- **Anticipated** — a full disk, a missing file, a refused connection: `log.error(...)` with the
  error text. A traceback for a condition you predicted is noise, and at volume it is expensive.

Raise with a clear message at the raise site. A bespoke exception class is worth it when callers
need to catch that specific failure, not merely to satisfy a linter.

## Tests

**Assert something that would fail if the behaviour were deleted.** A test asserting only that a
call did not raise usually passes forever, including after the feature is removed. Check the
output, the state, or the side effect.

**Mutation-check a regression test once.** Reintroduce the bug and confirm the test fails. A
regression test that never failed has not been shown to work.

**Fakes honour the real contract.** When production code starts calling a new method, the fake
grows it too. A fake that has silently diverged tests a shape that does not exist — and the
symptom is often an exception inside a worker thread rather than a clean failure.

**Bound your waits.** Join threads and poll conditions with a timeout, and assert on the result.
`assert not thread.is_alive()` after an unbounded join is a tautology, and a regression hangs the
suite instead of failing it. Give the whole suite a timeout too.

**Patch the name the code looks up.** A subclass binds its base class at import time, so
patching the base in its module changes nothing for a subclass that already exists. Patch the
class the code under test constructs, where it constructs it.

**Keep manual and hardware scripts out of collection by name.** A script named `check_*.py`
matches none of pytest's globs, so it cannot be collected by accident. `norecursedirs` is not
enough: it suppresses directory walking and does nothing for an explicitly named path.

**Never read a streaming or infinite response through a buffering test client.** It reads to
the end, the end never comes, and the suite hangs with no failure. Build the response in a
request context and take what you need from the generator directly.

**A layout is verified by looking at it.** No gate can see a page. Render it at a phone size and
a desk size, read the screenshots, and only then call the change done.

**Prove a comment-only change with the AST.** Parse each file before and after, strip the
docstrings, and compare `ast.dump`. Equal dumps mean no behaviour changed.

## Tooling

Every commit must pass the linter, the formatter check, the type checker, and the tests.

**One script runs every gate, and CI runs that same script,** in the order that fails fastest.
A gate that only CI runs is found after the push; one that only a laptop runs drifts.

**Lint the shell and spell-check everything.** Shell scripts go through shellcheck and every
tracked file through codespell, as gates like any other. Pin both and install them without
root — as dev dependencies, or through `uvx` (`uvx --from shellcheck-py==<version> shellcheck`,
`uvx codespell==<version>`) — so every machine and CI run the same version.

**Adopt lint rules by measuring, not by reputation.** Turn a rule on, look at what it actually
flags, and decide. Rules that report nothing today still cost nothing and guard the future;
rules that report a hundred things need a plan, not a bulk `noqa`.

**Treat complexity as a ratchet.** Set the limit at the current worst function so new complexity
must be extracted rather than absorbed. Raise it deliberately, never to silence a warning.

**Exempt tests where the rule does not fit.** In test code the literals often *are* the expected
values, `assert` is the mechanism, and fixtures are unused by design. Exempting those is honest;
it is not licence to leave every literal bare, because a number that is a *threshold* rather than
an expected value still wants a name.

**Every `noqa` carries its reason.** A bare code records only that someone silenced it.

```python
app.run(host="0.0.0.0", port=port)  # noqa: S104 -- LAN-only by design, see the README
```

If you cannot write the reason in a clause, you probably should not be suppressing the rule.
