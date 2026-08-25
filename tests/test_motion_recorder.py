"""MotionRecorder: detection gating, clip lifecycle, and shared-camera cleanup."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import LORES_SIZE, MAIN_SIZE, FakePicamera2, StubSubtractor, make_frame


@pytest.fixture
def recorder(camera_manager: Any, fake_encoders: None, tmp_path: Path) -> Iterator[Any]:
    from src.motion_recorder import MotionRecorder

    rec = MotionRecorder(
        camera_manager=camera_manager,
        video_directory=tmp_path / "clips",
        file_prefix="test",
        motion_threshold=1000,
        motion_timeout=5,
        buffer_seconds=2,
        warmup_frames=3,
    )
    # Swap MOG2 out so tests dictate the foreground pixel count exactly.
    rec.background_subtractor = cast(Any, StubSubtractor())
    yield rec


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


# --- clip lifecycle ---------------------------------------------------------


def test_start_saving_attaches_a_file_and_starts_the_buffer(recorder: Any) -> None:
    path = recorder._start_saving()

    assert path is not None
    assert recorder.recording
    assert recorder.current_filename == path
    # The real setter stores an opened handle, not the path, so assert on what
    # was assigned rather than on what the attribute holds afterwards.
    assert recorder.circular_output.files[-1] == path
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

    saved = recorder._stop_saving()
    recorder._drain_thread.join(timeout=5)

    assert saved == path
    assert not recorder.recording
    assert recorder.current_filename is None
    assert recorder.circular_output.stop_calls == 1


def test_stop_saving_when_idle_does_nothing(recorder: Any) -> None:
    assert recorder._stop_saving() is None
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
    recorder.circular_output.stop_delay = 1.0

    started = time.monotonic()
    recorder._stop_saving()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5  # handed off, not waited on
    recorder._drain_thread.join(timeout=5)
    assert recorder.circular_output.stop_calls == 1


def test_a_new_clip_waits_for_the_previous_drain(recorder: Any) -> None:
    """Two clips must not share the output while one is still flushing."""
    recorder._start_saving()
    recorder.circular_output.stop_delay = 1.0
    recorder._stop_saving()

    assert recorder._start_saving() is None  # drain still in flight

    recorder._drain_thread.join(timeout=5)
    assert recorder._start_saving() is not None


def test_an_incomplete_clip_is_not_reported_as_saved(
    recorder: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: a clip whose writes failed was logged as a clean save."""
    recorder._start_saving()
    recorder.circular_output.dead = True

    recorder._stop_saving()
    recorder._drain_thread.join(timeout=5)

    output = capsys.readouterr().out
    assert "incomplete" in output
    assert "Stopped saving" not in output


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
    first = recorder._next_path()
    first.write_bytes(b"existing clip")

    second = recorder._next_path()

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
    capsys: pytest.CaptureFixture[str],
) -> None:
    """warmup_frames=0 legitimately means "no warmup", but it must still say so:
    the armed line is only printed from inside the warmup branch."""
    from src.motion_recorder import MotionRecorder

    MotionRecorder(
        camera_manager=camera_manager,
        video_directory=tmp_path / "clips",
        warmup_frames=0,
    )

    assert "armed" in capsys.readouterr().out


def test_storage_errors_never_escape_to_picamera2() -> None:
    """Regression, exercised against the real class rather than the fake.

    Frames are written inline on picamera2's camera event-loop thread. The base
    CircularOutput only catches connection errors, so an OSError from the CIFS
    share would propagate into that thread and kill it, hanging every capture.
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


def test_recorder_is_inert_after_cleanup(
    recorder: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The consumer stays registered after cleanup(), so a frame arriving late
    must not try to open a clip against a torn-down output."""
    arm(recorder)
    recorder.cleanup()
    capsys.readouterr()

    recorder.background_subtractor.motion_pixels = 10**6
    recorder._process_frames(make_frame(MAIN_SIZE), make_frame(LORES_SIZE))

    assert not recorder.recording
    assert capsys.readouterr().out == ""
