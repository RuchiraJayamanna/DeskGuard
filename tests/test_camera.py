"""Camera worker tests using a fake camera (no webcam needed)."""

import threading
import time

import numpy as np
import pytest

from deskguard.camera import CameraError, OpenedCamera
from deskguard.camera_worker import CameraWorker
from tests.conftest import wait_until


class FakeCapture:
    """Behaves like cv2.VideoCapture delivering `fps` frames per second."""

    def __init__(self, fps=30, size=(640, 480)):
        self.interval = 1.0 / fps
        self.size = size
        self.connected = True
        self.released = False
        self.grabs = 0
        self.retrieves = 0
        self._next = time.monotonic()
        self.lock = threading.Lock()

    def grab(self):
        if not self.connected or self.released:
            time.sleep(0.01)
            return False
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next = max(self._next + self.interval, time.monotonic())
        self.grabs += 1
        return True

    def retrieve(self, image=None):
        self.retrieves += 1
        w, h = self.size
        frame = image if image is not None and image.shape == (h, w, 3) else np.empty((h, w, 3), np.uint8)
        frame[:] = 100
        return True, frame

    def release(self):
        self.released = True


class FakeCameraSystem:
    def __init__(self, fps=30, size=(640, 480)):
        self.fps, self.size = fps, size
        self.available = True
        self.opens = 0
        self.captures: list[FakeCapture] = []

    def open(self, index, width, height, fps):
        self.opens += 1
        if not self.available:
            raise CameraError("No webcam found.")
        cap = FakeCapture(self.fps, self.size)
        self.captures.append(cap)
        return OpenedCamera(cap, *self.size, "fake")


@pytest.fixture
def system():
    return FakeCameraSystem()


@pytest.fixture
def worker(qapp, system):
    w = CameraWorker(open_fn=system.open)
    states = []
    w.state_changed.connect(lambda s, m: states.append((s, m)))
    w.states = states
    w.set_display(240, 150, False)
    yield w
    w.stop()


def collect_frames(qapp, worker, seconds):
    frames = []

    def on_frame():
        f = worker.take_frame()
        if f is not None:
            frames.append(f)

    worker.frame_ready.connect(on_frame)
    worker.take_frame()  # drain anything produced before we started listening
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)
    worker.frame_ready.disconnect(on_frame)
    return frames


def test_live_preview_at_target_fps_and_size(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    assert wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    frames = collect_frames(qapp, worker, 2.0)
    assert 15 <= len(frames) <= 23, len(frames)  # ~10 fps
    # 4:3 fitted into 240x150 => 200x150, BGR
    assert frames[-1].shape == (150, 200, 3)
    cap = system.captures[0]
    # camera runs at 30 fps but only ~1/3 of frames are decoded
    assert cap.retrieves < cap.grabs * 0.6


def test_fifteen_fps_and_crop(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 15)
    worker.set_display(240, 150, True)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    frames = collect_frames(qapp, worker, 2.0)
    assert 24 <= len(frames) <= 34, len(frames)
    assert frames[-1].shape == (150, 240, 3)


def test_slow_gui_never_queues_frames(qapp, worker, system):
    """If the GUI doesn't consume frames, only one signal is outstanding."""
    emitted = []
    worker.frame_ready.connect(lambda: emitted.append(1))
    worker.configure_camera(0, 640, 480, 15)
    worker.start()
    time.sleep(1.5)  # GUI blocked: no processEvents, no take_frame
    qapp.processEvents()
    assert 1 <= len(emitted) <= 2  # at most one re-signal per second, never a queue
    assert worker.take_frame() is not None
    assert worker.take_frame() is None


def test_hidden_without_monitoring_stops_reading(qapp, worker, system, monkeypatch):
    from deskguard import camera_worker

    monkeypatch.setattr(camera_worker, "IDLE_RELEASE_S", 0.3)  # default: release is off
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    worker.set_visible(False)
    time.sleep(0.6)
    cap = system.captures[0]
    before = cap.grabs
    time.sleep(1.0)
    assert cap.grabs == before  # idle: camera open, nothing read
    assert not cap.released
    worker.set_visible(True)
    assert len(collect_frames(qapp, worker, 1.0)) >= 5


def test_hidden_idle_releases_camera_then_reopens_on_show(qapp, worker, system, monkeypatch):
    from deskguard import camera_worker

    monkeypatch.setattr(camera_worker, "IDLE_RELEASE_S", 0.5)
    worker.set_release_when_hidden(True)
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    worker.set_visible(False)
    assert wait_until(qapp, lambda: worker.states[-1] == ("paused", camera_worker.HIDDEN_MSG), 3)
    assert system.captures[0].released
    assert not worker.paused                  # not a user pause
    time.sleep(0.5)
    assert system.opens == 1                  # stays released while hidden
    worker.set_visible(True)
    assert wait_until(qapp, lambda: worker.states[-1][0] == "live", 3)
    assert system.opens == 2
    assert len(collect_frames(qapp, worker, 1.0)) >= 5


def test_hidden_with_motion_monitoring_keeps_detecting(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 10)
    worker.set_motion(True, "medium", 5, monitor_hidden=True)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    worker.set_visible(False)
    time.sleep(0.5)
    cap = system.captures[0]
    r0 = cap.retrieves
    frames = collect_frames(qapp, worker, 1.0)
    assert frames == []                       # nothing sent to the GUI
    assert 3 <= cap.retrieves - r0 <= 8       # ~5 fps detection


def test_disconnect_and_recover(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    system.available = False
    system.captures[0].connected = False      # unplug
    assert wait_until(qapp, lambda: any(s == "reconnecting" for s, _ in worker.states), 5)
    assert system.captures[0].released
    assert wait_until(qapp, lambda: worker.states[-1][0] == "error", 5)
    system.available = True                   # plug back in
    assert wait_until(qapp, lambda: worker.states[-1][0] == "live", 8)
    assert len(collect_frames(qapp, worker, 1.0)) >= 5


def test_no_camera_at_start_keeps_retrying_with_backoff(qapp, worker, system):
    system.available = False
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    assert wait_until(qapp, lambda: any(s == "error" for s, _ in worker.states))
    time.sleep(3.0)
    assert 1 <= system.opens <= 3             # retries are spaced out, not hammering
    assert "No webcam" in worker.states[-1][1]


def test_pause_releases_camera_and_resume_reopens(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    worker.set_paused(True)
    assert wait_until(qapp, lambda: worker.states[-1][0] == "paused")
    assert system.captures[0].released
    worker.set_paused(False)
    assert wait_until(qapp, lambda: worker.states[-1][0] == "live")
    assert len(system.captures) == 2


def test_resolution_change_reopens_once(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    worker.set_fps(15)                         # fps change: no reopen
    time.sleep(0.3)
    assert system.opens == 1
    worker.configure_camera(0, 1280, 720, 15)  # resolution change: reopen
    assert wait_until(qapp, lambda: system.opens == 2)
    assert system.captures[0].released


def test_unsupported_resolution_reported(qapp, system):
    system.size = (640, 480)
    w = CameraWorker(open_fn=system.open)
    states = []
    w.state_changed.connect(lambda s, m: states.append((s, m)))
    w.configure_camera(0, 1920, 1080, 10)
    w.start()
    try:
        assert wait_until(qapp, lambda: any(s == "live" for s, _ in states))
        live_msg = [m for s, m in states if s == "live"][0]
        assert "not supported" in live_msg and "640×480" in live_msg
    finally:
        w.stop()


def test_stop_releases_camera(qapp, worker, system):
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    assert worker.stop()
    assert system.captures[0].released


def test_open_exception_does_not_crash_worker(qapp):
    calls = []

    def bad_open(*a):
        calls.append(1)
        raise RuntimeError("driver exploded")

    w = CameraWorker(open_fn=bad_open)
    states = []
    w.state_changed.connect(lambda s, m: states.append((s, m)))
    w.start()
    try:
        assert wait_until(qapp, lambda: any(s == "error" for s, _ in states))
        assert w.isRunning()
    finally:
        assert w.stop()


class FakeDshowCap:
    """Records property changes; delivers frames of its current size."""

    def __init__(self, size=(640, 480)):
        self.size = size
        self.sets = []

    def read(self):
        w, h = self.size
        return True, np.zeros((h, w, 3), np.uint8)

    def set(self, prop, value):
        import cv2

        self.sets.append(prop)
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self.size = (int(value), self.size[1])
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self.size = (self.size[0], int(value))
        return True


def test_dshow_default_size_changes_nothing():
    """Every DirectShow property change rebuilds the graph and leaks memory."""
    import cv2

    from deskguard.camera import _configure

    cap = FakeDshowCap((640, 480))
    frame = _configure(cap, cv2.CAP_DSHOW, 640, 480, 10)
    assert frame.shape == (480, 640, 3)
    assert cap.sets == []


def test_dshow_hd_sets_mjpg_and_size_but_not_fps():
    import cv2

    from deskguard.camera import _configure

    cap = FakeDshowCap((640, 480))
    frame = _configure(cap, cv2.CAP_DSHOW, 1280, 720, 10)
    assert frame.shape == (720, 1280, 3)
    assert cap.sets == [cv2.CAP_PROP_FOURCC, cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT]
