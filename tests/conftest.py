"""Shared fakes for the picamera2 hardware layer, and fixtures for the review side.

`picamera2` binds to the real camera when a `Picamera2` is *constructed*, not when
the module is imported, so these tests swap the class out and never touch hardware.
Anything that needs a real camera lives in tests/manual/ and is run by hand.
"""

from __future__ import annotations

import datetime
import importlib.util
import subprocess
import sys
import threading
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from src.clips.blob import Blob
from src.clips.frame import Frame
from src.clips.sidecar import ClipFacts, CloseReason, write_sidecar
from src.review.capture_db import CaptureDB

PICAMERA2_IS_STUBBED = importlib.util.find_spec("picamera2") is None


def stub_picamera2() -> None:
    """Register bare picamera2 modules so src/ imports without the camera stack.

    Only the three names src/ imports exist, as bare classes; every fixture below
    replaces them before anything is constructed.
    """
    modules = {
        "picamera2": {"Picamera2"},
        "picamera2.encoders": {"H264Encoder"},
        "picamera2.outputs": {"CircularOutput"},
    }
    for module_name, class_names in modules.items():
        module = types.ModuleType(module_name)
        vars(module).update({name: type(name, (), {}) for name in class_names})
        sys.modules[module_name] = module


if PICAMERA2_IS_STUBBED:
    stub_picamera2()

needs_real_picamera2 = pytest.mark.skipif(
    PICAMERA2_IS_STUBBED, reason="exercises the real CircularOutput"
)

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
        # The real stop() drains the whole ring buffer to disk; a gate here makes
        # stop() block until a test opens it, so the test can see who waited.
        self.stop_gate: threading.Event | None = None
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

    def begin(self, path: Path) -> None:
        self.dead = False
        self.fileoutput = path
        self.start()

    def start(self) -> None:
        self.started = True
        self.start_calls += 1

    def stop(self) -> None:
        if self.stop_gate is not None:
            self.stop_gate.wait(timeout=5)
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


class StubDetector:
    """Only the attributes the web layer reads from a MotionDetector."""

    def __init__(self) -> None:
        self.motion_threshold = 300


class StubRecorder:
    """Only the attributes the web layer reads from a MotionRecorder."""

    def __init__(self, recording: bool = False, filename: Path | None = None) -> None:
        self.recording = recording
        self.current_filename = filename
        self.detector = StubDetector()
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
    monkeypatch.setattr("src.capture.camera_manager.Picamera2", lambda: camera)
    return camera


@pytest.fixture
def fake_encoders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the encoder and circular buffer MotionRecorder builds.

    Note it patches _ResilientCircularOutput, the subclass the recorder actually
    constructs. Patching CircularOutput would not help: the subclass bound the
    real base class at import time.
    """
    monkeypatch.setattr("src.capture.motion_recorder.H264Encoder", FakeH264Encoder)
    monkeypatch.setattr(
        "src.capture.motion_recorder._ResilientCircularOutput", FakeCircularOutput
    )


@pytest.fixture
def camera_manager(fake_camera: FakePicamera2) -> Iterator[Any]:
    from src.capture.camera_manager import CameraManager

    manager = CameraManager()
    yield manager
    manager.stop_frame_distribution()


DETECTOR_SETTINGS: dict[str, Any] = {
    "motion_threshold": 1000,
    # make_frame() is black by default; 0 keeps every test frame lit.
    "dark_brightness": 0,
    "mog2_history": 500,
    "warmup_frames": 3,
}


def make_detector(*, stub_mog2: bool = True, **overrides: Any) -> Any:
    """A detector over StubSubtractor, so tests dictate the foreground pixel
    count exactly; stub_mog2=False keeps the real MOG2."""
    from src.capture.motion_detector import MotionDetector

    detector = MotionDetector(**{**DETECTOR_SETTINGS, **overrides})
    if stub_mog2:
        detector.background_subtractor = cast(Any, StubSubtractor())
    return detector


@pytest.fixture
def make_recorder(
    camera_manager: Any, fake_encoders: None, tmp_path: Path
) -> Callable[..., Any]:
    """A recorder over the fakes and a stubbed detector; keyword overrides
    reach whichever constructor owns them."""
    from src.capture.motion_recorder import MotionRecorder

    def build(*, stub_mog2: bool = True, **overrides: Any) -> Any:
        settings: dict[str, Any] = {
            "video_directory": tmp_path / "clips",
            "file_prefix": "test",
            "motion_timeout": 5,
            "buffer_seconds": 2,
        }
        for_detector = {k: v for k, v in overrides.items() if k in DETECTOR_SETTINGS}
        for_recorder = {k: v for k, v in overrides.items() if k not in for_detector}
        detector = make_detector(stub_mog2=stub_mog2, **for_detector)
        return MotionRecorder(
            camera_manager, detector=detector, **{**settings, **for_recorder}
        )

    return build


@pytest.fixture
def recorder(make_recorder: Callable[..., Any]) -> Any:
    return make_recorder()


# --- captures: clips on disk, their sidecars and the review database -----------

T0 = datetime.datetime(2026, 9, 12, 8, 0, 0)
BOX_1 = (100, 240)
BOX_2 = (320, 240)
BOX_3 = (540, 240)

# Grouping thresholds the capture tests build their fixtures against.
GAP = 60
DISTANCE = 100


def at(seconds: int) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


def make_clip_file(path: Path, content: bytes = b"fake h264") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_clip_with_facts(
    directory: Path,
    started: datetime.datetime,
    seconds: int,
    place: tuple[int, int] = BOX_1,
    reason: CloseReason = CloseReason.TIMEOUT,
) -> Path:
    """A clip file named for its start, plus the sidecar the recorder writes."""
    path = make_clip_file(directory / f"cat_video_{started:%Y%m%d_%H%M%S}.h264")
    blob = Blob(area=400, x=place[0] - 10, y=place[1] - 10, w=20, h=20)
    write_sidecar(
        path,
        ClipFacts(
            started_at=started,
            ended_at=started + datetime.timedelta(seconds=seconds),
            close_reason=reason,
            trigger_blob=blob,
            last_blob=blob,
        ),
    )
    return path


def visit(directory: Path, start: int, *offsets: int) -> None:
    """Clips at start and each offset, close enough to group as one visit."""
    for offset in (0, *offsets):
        make_clip_with_facts(directory, at(start + offset), seconds=15)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = CaptureDB(
        tmp_path / "captures.db", gap_seconds=GAP, box_distance_px=DISTANCE
    )
    yield database
    database.close()


@pytest.fixture
def clip_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "captures"
    make_clip_file(directory / "cat_video_20260825_072452.h264")
    make_clip_file(directory / "cat_video_20260826_090000.h264")
    return directory


def write_test_video(path: Path, seconds: int = 3) -> Path:
    """A raw H.264 stream like the recorder's: 30 fps, a keyframe every second.

    Frame n is a flat grey of n // 2, so a decoded frame says where it came from.
    """
    raw = b"".join(
        np.full((120, 160, 3), n // 2, dtype=np.uint8).tobytes()
        for n in range(seconds * 30)
    )
    command = [
        "ffmpeg",
        *("-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24"),
        *("-s", "160x120", "-r", "30", "-i", "-"),
        *("-c:v", "libx264", "-x264-params", "keyint=30:min-keyint=30:scenecut=0"),
        *("-pix_fmt", "yuv420p", "-f", "h264", "-y", str(path)),
    ]
    subprocess.run(command, input=raw, check=True)  # noqa: S603 -- fixed argv
    return path


# Encoded once per module: tests read the clips and write only their own caches.
@pytest.fixture(scope="module")
def short_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_test_video(tmp_path_factory.mktemp("clips") / "short.h264", seconds=3)


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_test_video(tmp_path_factory.mktemp("clips") / "long.h264", seconds=12)
