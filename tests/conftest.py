"""Shared fakes for the picamera2 hardware layer.

`picamera2` binds to the real camera when a `Picamera2` is *constructed*, not when
the module is imported, so these tests swap the class out and never touch hardware.
Anything that needs a real camera lives in tests/manual/ and is run by hand.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from numpy.typing import NDArray

Frame = NDArray[np.uint8]

MAIN_SIZE = (720, 1280)
LORES_SIZE = (480, 640)


def make_frame(shape: tuple[int, int] = LORES_SIZE, value: int = 0) -> Frame:
    """A solid-colour frame of the given (height, width)."""
    return np.full((*shape, 3), value, dtype=np.uint8)


class FakeCircularOutput:
    """Stands in for picamera2's CircularOutput, recording how it was driven."""

    def __init__(self, buffersize: int = 150) -> None:
        self.buffersize = buffersize
        self.fileoutput: Any = None
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.dead = False
        self.stop_delay = 0.0
        # Assigning fileoutput opens the file in the real class, so that is the
        # statement that fails when the share is gone -- not start().
        self.fail_on_fileoutput = False
        # Every path assigned to fileoutput, in order.
        self.files: list[Any] = []

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "fileoutput" and hasattr(self, "files"):
            if self.fail_on_fileoutput:
                raise OSError("network share went away")
            self.files.append(value)
            if value is not None:
                # The real setter open()s the path, so the file exists from here.
                Path(value).write_bytes(b"fake clip")
        super().__setattr__(name, value)

    def is_abandoned(self) -> bool:
        return self.dead

    def start(self) -> None:
        self.started = True
        self.start_calls += 1

    def stop(self) -> None:
        # The real stop() drains the whole ring buffer to disk; stop_delay lets a
        # test stand in for that being slow.
        if self.stop_delay:
            time.sleep(self.stop_delay)
        self.started = False
        self.stop_calls += 1


class FakeH264Encoder:
    def __init__(self, bitrate: int = 0, iperiod: int | None = None) -> None:
        self.bitrate = bitrate
        self.iperiod = iperiod


class FakePicamera2:
    """Minimal stand-in for Picamera2, covering only what this project calls."""

    def __init__(self) -> None:
        self.configured: Any = None
        self.started = False
        self.closed = False
        self.recording_encoder: Any = None
        self.recording_output: Any = None
        self.stop_encoder_calls: list[Any] = []
        self.stop_recording_calls = 0
        self.capture_array_calls = 0
        self.capture_arrays_calls = 0
        self._tick = 0

    def create_video_configuration(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    def configure(self, config: Any) -> None:
        self.configured = config

    def start(self) -> None:
        self.started = True

    def capture_array(self, name: str = "main") -> Frame:
        # Counted so a test can prove the loop no longer burns two requests.
        self.capture_array_calls += 1
        self._tick = (self._tick + 1) % 256
        shape = MAIN_SIZE if name == "main" else LORES_SIZE
        return make_frame(shape, self._tick)

    def capture_arrays(
        self, names: list[str] | None = None
    ) -> tuple[list[Frame], dict[str, Any]]:
        """One request serving several streams, as the real API does.

        Returns (arrays, metadata) -- not a bare list.
        """
        self.capture_arrays_calls += 1
        self._tick = (self._tick + 1) % 256
        wanted = names if names is not None else ["main"]
        arrays = [
            make_frame(MAIN_SIZE if n == "main" else LORES_SIZE, self._tick)
            for n in wanted
        ]
        return arrays, {"SensorTimestamp": self._tick}

    def start_recording(self, encoder: Any, output: Any, **kwargs: Any) -> None:
        self.recording_encoder = encoder
        self.recording_output = output

    def stop_encoder(self, encoders: Any = None) -> None:
        self.stop_encoder_calls.append(encoders)

    def stop_recording(self) -> None:
        self.stop_recording_calls += 1

    def close(self) -> None:
        self.closed = True


class StubRecorder:
    """Only the attributes the web layer reads from a MotionRecorder."""

    def __init__(self, recording: bool = False, filename: Path | None = None) -> None:
        self.recording = recording
        self.current_filename = filename
        self.motion_threshold = 300
        self.motion_timeout = 10


class StubSubtractor:
    """Replaces MOG2 so tests set the foreground pixel count exactly."""

    def __init__(self) -> None:
        self.motion_pixels = 0

    def apply(self, frame: Frame) -> Frame:
        mask: Frame = np.zeros(frame.shape[:2], dtype=np.uint8)
        mask.reshape(-1)[: self.motion_pixels] = 255
        return mask


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakePicamera2:
    """Patch CameraManager's Picamera2 so constructing one touches no hardware."""
    camera = FakePicamera2()
    monkeypatch.setattr("src.camera_manager.Picamera2", lambda: camera)
    return camera


@pytest.fixture
def fake_encoders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the encoder and circular buffer MotionRecorder builds.

    Note it patches _ResilientCircularOutput, the subclass the recorder actually
    constructs. Patching CircularOutput would not help: the subclass bound the
    real base class at import time.
    """
    monkeypatch.setattr("src.motion_recorder.H264Encoder", FakeH264Encoder)
    monkeypatch.setattr(
        "src.motion_recorder._ResilientCircularOutput", FakeCircularOutput
    )


@pytest.fixture
def camera_manager(fake_camera: FakePicamera2) -> Iterator[Any]:
    from src.camera_manager import CameraManager

    manager = CameraManager()
    yield manager
    manager.stop_frame_distribution()


@pytest.fixture
def recorder(camera_manager: Any, fake_encoders: None, tmp_path: Path) -> Iterator[Any]:
    from src.motion_recorder import MotionRecorder

    rec = MotionRecorder(
        camera_manager=camera_manager,
        video_directory=tmp_path / "clips",
        file_prefix="test",
        motion_threshold=1000,
        # make_frame() is black by default; 0 keeps every test frame lit.
        dark_brightness=0,
        motion_timeout=5,
        buffer_seconds=2,
        warmup_frames=3,
    )
    # Swap MOG2 out so tests dictate the foreground pixel count exactly.
    rec.background_subtractor = cast(Any, StubSubtractor())
    yield rec


@pytest.fixture
def recorder_with_real_mog2(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> Any:
    """Recorder keeping the real MOG2, for tests about shadow handling."""
    from src.motion_recorder import MotionRecorder

    return MotionRecorder(
        camera_manager=camera_manager,
        video_directory=tmp_path / "clips",
        motion_threshold=1000,
        warmup_frames=0,
    )
