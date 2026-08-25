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
    assert recorder.circular_output.fileoutput == path
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

    assert saved == path
    assert not recorder.recording
    assert recorder.current_filename is None
    assert recorder.circular_output.stop_calls == 1


def test_stop_saving_when_idle_does_nothing(recorder: Any) -> None:
    assert recorder._stop_saving() is None
    assert recorder.circular_output.stop_calls == 0


def test_a_failing_start_leaves_the_recorder_idle(recorder: Any) -> None:
    def explode() -> None:
        raise OSError("network share went away")

    recorder.circular_output.start = explode

    assert recorder._start_saving() is None
    assert not recorder.recording


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
    recorder.last_motion_time = time.time() - (recorder.motion_timeout + 1)
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
