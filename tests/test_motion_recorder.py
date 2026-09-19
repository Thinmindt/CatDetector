"""MotionRecorder: detection gating, clip lifecycle, and shared-camera cleanup."""

from __future__ import annotations

import datetime
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    LORES_SIZE,
    MAIN_SIZE,
    FakePicamera2,
    make_frame,
    needs_real_picamera2,
)

from src.clip_sidecar import ClipFacts, CloseReason, read_sidecar, sidecar_name
from src.motion_recorder import partial_name

# Mean grey levels either side of a dark cutoff of 10, as measured at night and by day.
DARK_CUTOFF = 10
NIGHT_GREY = 2
DAY_GREY = 60


def arm(recorder: Any) -> None:
    """Push the detector past its warmup with quiet frames."""
    recorder.background_subtractor.motion_pixels = 0
    for _ in range(recorder.warmup_frames):
        recorder.detect_motion(make_frame(LORES_SIZE))


# --- construction -----------------------------------------------------------


def test_registers_itself_as_a_frame_consumer(
    recorder: Any, camera_manager: Any
) -> None:
    assert recorder._process_frames in camera_manager._consumers


def test_starts_buffering_on_the_shared_camera(
    recorder: Any, fake_camera: FakePicamera2
) -> None:
    assert fake_camera.recording_output is recorder.circular_output
    assert fake_camera.recording_encoder is recorder.encoder


def test_the_encoder_pins_one_keyframe_a_second(recorder: Any) -> None:
    """The review tiles are cut at keyframes and assume one per second."""
    from src.motion_recorder import CAMERA_FPS

    assert recorder.encoder.iperiod == CAMERA_FPS


def test_creates_the_video_directory(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> None:
    from src.motion_recorder import MotionRecorder

    target = tmp_path / "nested" / "clips"
    MotionRecorder(camera_manager=camera_manager, video_directory=target)
    assert target.is_dir()


# --- detection --------------------------------------------------------------


def test_warmup_suppresses_motion_then_arms(recorder: Any) -> None:
    """Regression: MOG2 calls the whole first frame foreground, which used to
    trigger a clip on every startup."""
    recorder.background_subtractor.motion_pixels = 10**6  # far over threshold
    frame = make_frame(LORES_SIZE)

    during = [recorder.detect_motion(frame) for _ in range(recorder.warmup_frames)]
    assert during == [False] * recorder.warmup_frames

    # The very next frame is armed.
    assert recorder.detect_motion(frame) is True


def test_quiet_frames_do_not_report_motion(recorder: Any) -> None:
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 10
    assert recorder.detect_motion(make_frame(LORES_SIZE)) is False


def test_motion_pixels_are_recorded_for_tuning(recorder: Any) -> None:
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 2500
    recorder.detect_motion(make_frame(LORES_SIZE))
    assert recorder.last_motion_pixels == 2500


def test_motion_in_a_dark_scene_does_not_trigger(recorder: Any) -> None:
    """Regression: in the unlit room, sensor speckle alone crossed the threshold
    and recorded black video nonstop."""
    recorder.dark_brightness = DARK_CUTOFF
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 10**6

    assert recorder.detect_motion(make_frame(LORES_SIZE, NIGHT_GREY)) is False
    assert recorder.detect_motion(make_frame(LORES_SIZE, DAY_GREY)) is True


def test_logs_each_change_between_dark_and_lit(
    recorder: Any, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.dark_brightness = DARK_CUTOFF
    arm(recorder)

    with caplog.at_level(logging.INFO, logger="src.motion_recorder"):
        for grey in (NIGHT_GREY, NIGHT_GREY, DAY_GREY, DAY_GREY, NIGHT_GREY):
            recorder.detect_motion(make_frame(LORES_SIZE, grey))

    changes = [r.getMessage() for r in caplog.records if "Scene" in r.getMessage()]
    assert changes == [
        "Scene is dark; ignoring motion",
        "Scene is lit; detecting motion",
        "Scene is dark; ignoring motion",
    ]


def test_a_clip_abandoned_mid_write_says_so_in_its_sidecar(recorder: Any) -> None:
    """Regression: a clip whose writes failed shipped with close_reason 'timeout',
    so review and grouping read a truncated clip as an ordinary visit."""
    saved = recorder._start_saving()
    recorder.circular_output.dead = True

    recorder._stop_saving(CloseReason.TIMEOUT)
    recorder.cleanup()

    facts = read_sidecar(saved)
    assert facts is not None
    assert facts.close_reason is CloseReason.ABANDONED
    assert saved.exists()  # still promoted: a truncated clip beats no clip


# --- clip lifecycle ---------------------------------------------------------


def test_start_saving_attaches_a_file_and_starts_the_buffer(recorder: Any) -> None:
    path = recorder._start_saving()

    assert path is not None
    assert recorder.recording
    assert recorder.current_filename == path
    # The real setter stores an opened handle, not the path, so assert on what
    # was assigned rather than on what the attribute holds afterwards. Clips are
    # written under the partial name and promoted once closed.
    assert recorder.circular_output.files[-1] == partial_name(path)
    assert not path.exists()
    assert recorder.circular_output.start_calls == 1


def test_clips_are_named_h264_not_mp4(recorder: Any) -> None:
    """Regression: CircularOutput writes a raw elementary stream, not a container."""
    path = recorder._start_saving()
    assert path is not None
    assert path.suffix == ".h264"
    assert path.name.startswith("test_")


def test_start_saving_is_idempotent_while_recording(recorder: Any) -> None:
    recorder._start_saving()
    assert recorder._start_saving() is None
    assert recorder.circular_output.start_calls == 1


def test_stop_saving_stops_the_buffer_and_clears_state(recorder: Any) -> None:
    path = recorder._start_saving()

    saved = recorder._stop_saving(CloseReason.TIMEOUT)
    recorder._drain_thread.join(timeout=5)

    assert saved == path
    assert not recorder.recording
    assert recorder.current_filename is None
    assert recorder.circular_output.stop_calls == 1


def test_stop_saving_when_idle_does_nothing(recorder: Any) -> None:
    assert recorder._stop_saving(CloseReason.TIMEOUT) is None
    assert recorder.circular_output.stop_calls == 0


def test_a_failing_start_leaves_the_recorder_idle(recorder: Any) -> None:
    """The real failure point is the fileoutput assignment, which opens the
    file -- not start(), which is a trivial inherited setter that cannot raise."""
    recorder.circular_output.fail_on_fileoutput = True

    assert recorder._start_saving() is None
    assert not recorder.recording
    assert recorder.circular_output.start_calls == 0


# --- the consumer callback --------------------------------------------------


def test_motion_starts_a_clip(recorder: Any) -> None:
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000

    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert recorder.recording


def test_clip_stops_once_motion_has_been_quiet_for_the_timeout(
    recorder: Any,
) -> None:
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    assert recorder.recording

    recorder.background_subtractor.motion_pixels = 0
    recorder.last_motion_time = time.monotonic() - (recorder.motion_timeout + 1)
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert not recorder.recording


def test_clip_keeps_running_while_motion_continues(recorder: Any) -> None:
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000
    for _ in range(5):
        recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert recorder.recording
    assert recorder.circular_output.start_calls == 1


# --- cleanup ----------------------------------------------------------------


def test_cleanup_stops_the_encoder_not_the_shared_camera(
    recorder: Any, fake_camera: FakePicamera2
) -> None:
    """Regression: stop_recording() also stops the camera, which is shared with
    WebStreamer. Only the encoder belongs to the recorder."""
    encoder = recorder.encoder

    recorder.cleanup()

    assert fake_camera.stop_encoder_calls == [encoder]
    assert fake_camera.stop_recording_calls == 0


def test_cleanup_closes_an_open_clip(recorder: Any) -> None:
    recorder._start_saving()
    circular_output = recorder.circular_output

    recorder.cleanup()

    assert circular_output.stop_calls == 1
    assert not recorder.recording


# --- the fixes from review --------------------------------------------------


def test_draining_a_clip_does_not_block_the_caller(recorder: Any) -> None:
    """Regression: CircularOutput.stop() drains the whole ring buffer to disk.

    Inline on the capture thread that stalls frame distribution -- and
    picamera2's event loop with it -- for a multi-megabyte network write.
    """
    recorder._start_saving()
    drained = threading.Event()
    recorder.circular_output.stop_gate = drained

    recorder._stop_saving(CloseReason.TIMEOUT)

    assert recorder.circular_output.stop_calls == 0  # still draining, elsewhere
    drained.set()
    recorder._drain_thread.join(timeout=5)
    assert recorder.circular_output.stop_calls == 1


def test_a_new_clip_waits_for_the_previous_drain(recorder: Any) -> None:
    """Two clips must not share the output while one is still flushing."""
    recorder._start_saving()
    drained = threading.Event()
    recorder.circular_output.stop_gate = drained
    recorder._stop_saving(CloseReason.TIMEOUT)

    assert recorder._start_saving() is None  # drain still in flight

    drained.set()
    recorder._drain_thread.join(timeout=5)
    assert recorder._start_saving() is not None


def test_an_incomplete_clip_is_not_reported_as_saved(
    recorder: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: a clip whose writes failed was logged as a clean save."""
    recorder._start_saving()
    recorder.circular_output.dead = True

    with caplog.at_level(logging.INFO):
        recorder._stop_saving(CloseReason.TIMEOUT)
        recorder._drain_thread.join(timeout=5)

    assert "incomplete" in caplog.text
    assert "Stopped saving" not in caplog.text


def test_clip_stops_at_the_maximum_length(recorder: Any) -> None:
    """Regression: nothing bounded a clip, so a sunbeam could record all night."""
    arm(recorder)
    recorder.max_clip_seconds = 0.0
    recorder.background_subtractor.motion_pixels = 5000

    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert not recorder.recording


def test_refuses_to_record_without_free_space(recorder: Any) -> None:
    recorder.min_free_bytes = 10**18  # more than any real filesystem

    assert recorder._start_saving() is None
    assert not recorder.recording


def test_a_second_clip_in_the_same_second_does_not_overwrite(recorder: Any) -> None:
    """Regression: local timestamps repeat across the DST fall-back, and
    picamera2 opens clips "wb", so a collision silently truncated the earlier."""
    same_second = datetime.datetime(2026, 11, 1, 1, 30, 0)
    first = recorder._next_clip_path(same_second)
    first.write_bytes(b"existing clip")

    second = recorder._next_clip_path(same_second)

    assert second != first
    assert first.read_bytes() == b"existing clip"


def test_timeout_survives_a_wall_clock_step(
    recorder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: durations were measured with time.time(), so an NTP step
    backwards held a clip open for the size of the jump.

    This Pi has no RTC backup cell and timesyncd steps the clock at boot.
    Rewinding time.time() must not affect anything: with the old code the
    elapsed difference goes negative and the clip never closes.
    """
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    assert recorder.recording

    # Age the clip past the timeout, in whatever units the recorder stored.
    recorder.last_motion_time -= recorder.motion_timeout + 1

    # ... then step the wall clock 45 minutes backwards, as timesyncd would.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 2700)

    recorder.background_subtractor.motion_pixels = 0
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert not recorder.recording


def test_negative_warmup_is_rejected(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> None:
    from src.motion_recorder import MotionRecorder

    with pytest.raises(ValueError):
        MotionRecorder(
            camera_manager=camera_manager,
            video_directory=tmp_path / "clips",
            warmup_frames=-1,
        )


def test_zero_warmup_announces_that_it_is_armed(
    camera_manager: Any,
    fake_encoders: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """warmup_frames=0 legitimately means "no warmup", but it must still say so:
    the armed line is otherwise only logged from inside the warmup branch."""
    from src.motion_recorder import MotionRecorder

    with caplog.at_level(logging.INFO):
        MotionRecorder(
            camera_manager=camera_manager,
            video_directory=tmp_path / "clips",
            warmup_frames=0,
        )

    assert "armed" in caplog.text


@needs_real_picamera2
def test_storage_errors_never_escape_to_picamera2() -> None:
    """Regression, exercised against the real class rather than the fake.

    Clip bytes are written on the encoder's poll thread, the only one that
    returns camera buffers. The base CircularOutput only catches connection
    errors, so an OSError from the share would kill it and freeze every capture.
    """
    from src.motion_recorder import _ResilientCircularOutput

    class ExplodingFile:
        def __init__(self) -> None:
            self.writes = 0

        def write(self, data: bytes) -> int:
            self.writes += 1
            raise OSError("No space left on device")

        def flush(self) -> None:
            pass

    handle = ExplodingFile()
    output = _ResilientCircularOutput(buffersize=4)
    output._fileoutput = handle

    output._write(b"frame")  # must not raise
    assert output.dead
    assert handle.writes == 1

    output._write(b"another")  # and must stop hammering a dead share
    assert handle.writes == 1


def test_a_finished_clip_is_promoted_from_its_partial_name(recorder: Any) -> None:
    """The final name is what tells ClipTransfer a clip is complete."""
    path = recorder._start_saving()
    assert partial_name(path).exists()

    recorder._stop_saving(CloseReason.TIMEOUT)
    recorder._drain_thread.join(timeout=5)

    assert path.exists()
    assert not partial_name(path).exists()


def test_an_empty_clip_is_discarded_rather_than_promoted(
    recorder: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A clip that never got any bytes must not become a 0-byte capture event."""
    path = recorder._start_saving()
    partial_name(path).write_bytes(b"")

    with caplog.at_level(logging.WARNING):
        recorder._stop_saving(CloseReason.TIMEOUT)
        recorder._drain_thread.join(timeout=5)

    assert not path.exists()
    assert not partial_name(path).exists()
    assert "Discarded empty clip" in caplog.text


def build_recorder_over(camera_manager: Any, directory: Path) -> Any:
    from src.motion_recorder import MotionRecorder

    return MotionRecorder(camera_manager=camera_manager, video_directory=directory)


def test_a_clip_cut_off_mid_write_is_recovered_at_startup(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> None:
    """A power cut leaves the partial name, and only final names ever ship."""
    clip = tmp_path / "cat_video_20260910_080413.h264"
    partial_name(clip).write_bytes(b"half a visit")

    build_recorder_over(camera_manager, tmp_path)

    assert clip.read_bytes() == b"half a visit"
    assert not partial_name(clip).exists()


def test_an_empty_partial_clip_is_discarded_at_startup(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> None:
    clip = tmp_path / "cat_video_20260910_080413.h264"
    partial_name(clip).write_bytes(b"")

    build_recorder_over(camera_manager, tmp_path)

    assert not clip.exists()
    assert not partial_name(clip).exists()


def test_recovery_never_overwrites_a_finished_clip(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> None:
    clip = tmp_path / "cat_video_20260910_080413.h264"
    clip.write_bytes(b"the whole visit")
    partial_name(clip).write_bytes(b"something else")

    build_recorder_over(camera_manager, tmp_path)

    assert clip.read_bytes() == b"the whole visit"
    assert partial_name(clip).read_bytes() == b"something else"


# --- sidecars ---------------------------------------------------------------


def facts_of(clip: Path) -> ClipFacts:
    facts = read_sidecar(clip)
    assert facts is not None, f"{clip.name} has no sidecar"
    return facts


def record_one_clip(recorder: Any) -> Path:
    """Trigger a clip through the consumer callback and let it time out."""
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    path: Path | None = recorder.current_filename
    assert path is not None

    recorder.background_subtractor.motion_pixels = 0
    recorder.last_motion_time = time.monotonic() - (recorder.motion_timeout + 1)
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    recorder._drain_thread.join(timeout=5)
    return path


def test_a_finished_clip_gets_a_sidecar_with_its_facts(recorder: Any) -> None:
    path = record_one_clip(recorder)

    facts = facts_of(path)

    assert facts.close_reason == CloseReason.TIMEOUT
    assert facts.ended_at >= facts.started_at
    assert facts.trigger_blob is not None
    assert facts.trigger_blob.area > 0
    assert facts.last_blob == facts.trigger_blob


def test_last_blob_follows_motion_frames_only(recorder: Any) -> None:
    """A quiet frame must not wipe the position of the last thing that moved:
    it is what places the clip in a box once it closes."""
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000
    recorder.detect_motion(make_frame(LORES_SIZE))
    seen = recorder.last_blob

    recorder.background_subtractor.motion_pixels = 0
    recorder.detect_motion(make_frame(LORES_SIZE))

    assert seen is not None
    assert recorder.last_blob is seen


def test_the_sidecar_is_written_before_the_clip_is_promoted(
    recorder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ClipTransfer keys on the final name, so the facts must already exist."""
    seen: list[bool] = []
    real_promote = recorder._promote

    def spy(writing: Path | None, path: Path | None) -> bool:
        assert path is not None
        seen.append(sidecar_name(path).exists())
        return bool(real_promote(writing, path))

    monkeypatch.setattr(recorder, "_promote", spy)
    path = recorder._start_saving()
    recorder._stop_saving(CloseReason.TIMEOUT)
    recorder._drain_thread.join(timeout=5)

    assert seen == [True]
    assert path.exists()


def test_an_empty_clip_leaves_no_sidecar_behind(recorder: Any) -> None:
    path = recorder._start_saving()
    partial_name(path).write_bytes(b"")

    recorder._stop_saving(CloseReason.TIMEOUT)
    recorder._drain_thread.join(timeout=5)

    assert not sidecar_name(path).exists()


def test_each_way_a_clip_can_close_is_named(recorder: Any) -> None:
    arm(recorder)
    recorder.background_subtractor.motion_pixels = 5000
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    by_length = recorder.current_filename
    recorder._clip_started = time.monotonic() - (recorder.max_clip_seconds + 1)
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))
    recorder._drain_thread.join(timeout=5)

    by_shutdown = recorder._start_saving()
    recorder.cleanup()

    assert facts_of(by_length).close_reason == CloseReason.MAX_LENGTH
    assert facts_of(by_shutdown).close_reason == CloseReason.SHUTDOWN


@needs_real_picamera2
def test_outputframe_survives_the_ring_draining_mid_frame() -> None:
    """stop() can empty the ring between a frame's append and its popleft.

    The exception would otherwise kill the encoder's poll thread, which is the
    only one that returns camera buffers.
    """
    import collections
    import io

    from src.motion_recorder import _ResilientCircularOutput

    class DrainedRing:
        """A ring that stop() drains the instant a frame lands in it."""

        def __init__(self) -> None:
            self.frames: collections.deque[Any] = collections.deque()

        def __iadd__(self, frames: Any) -> DrainedRing:
            self.frames.extend(frames)
            self.frames.clear()
            return self

        def popleft(self) -> Any:
            return self.frames.popleft()

    sink = io.BytesIO()
    output = _ResilientCircularOutput(buffersize=4)
    output._fileoutput = sink
    output.recording = True
    output._firstframe = False

    output._circular = DrainedRing()
    output.outputframe(b"lost", keyframe=True)  # must not raise
    assert sink.getvalue() == b""

    output._circular = collections.deque(maxlen=4)
    output.outputframe(b"kept", keyframe=True)
    assert sink.getvalue() == b"kept"


def test_recorder_is_inert_after_cleanup(
    recorder: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The consumer stays registered after cleanup(), so a frame arriving late
    must not try to open a clip against a torn-down output."""
    arm(recorder)
    recorder.cleanup()

    recorder.background_subtractor.motion_pixels = 10**6
    with caplog.at_level(logging.DEBUG):
        recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert not recorder.recording
    assert caplog.text == ""
