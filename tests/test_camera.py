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
        self.left_dark = False          # left half black, right half grey (to test flips)
        self.props = {}                 # cv2 prop id -> value (hardware settings)
        self.prop_sets = []
        self.settings_opened = 0

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
        if self.left_dark:
            frame[:, : w // 2] = 0
        return True, frame

    def get(self, prop):
        return self.props.get(prop, -1.0)

    def set(self, prop, value):
        import cv2

        if prop == cv2.CAP_PROP_SETTINGS:
            self.settings_opened += 1
            return True
        self.prop_sets.append((prop, value))
        self.props[prop] = value
        return True

    def release(self):
        self.released = True


class FakeCameraSystem:
    def __init__(self, fps=30, size=(640, 480)):
        self.fps, self.size = fps, size
        self.available = True
        self.opens = 0
        self.captures: list[FakeCapture] = []
        self.backend = "fake"
        self.left_dark = False

    def open(self, index, width, height, fps):
        self.opens += 1
        if not self.available:
            raise CameraError("No webcam found.")
        cap = FakeCapture(self.fps, self.size)
        cap.left_dark = self.left_dark
        self.captures.append(cap)
        return OpenedCamera(cap, *self.size, self.backend)


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


def test_mirror_and_zoom_applied_to_preview(qapp, worker, system):
    system.left_dark = True
    worker.configure_camera(0, 640, 480, 10)
    worker.set_view(False, False, 1.0, 0.5, 0.5)
    worker.start()
    assert wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    f = collect_frames(qapp, worker, 0.5)[-1]
    assert f[75, 10, 0] == 0 and f[75, -10, 0] == 100      # not mirrored: dark on the left
    worker.set_view(True, False, 1.0, 0.5, 0.5)
    time.sleep(0.3)
    f = collect_frames(qapp, worker, 0.5)[-1]
    assert f[75, 10, 0] == 100 and f[75, -10, 0] == 0      # mirrored: dark on the right
    worker.set_view(False, False, 4.0, 0.9, 0.5)            # zoomed into the grey right side
    time.sleep(0.3)
    f = collect_frames(qapp, worker, 0.5)[-1]
    assert f.shape == (150, 200, 3) and (f == 100).all()


def test_settings_dialog_unsupported_backend(qapp, worker, system):
    from deskguard.camera_worker import NO_SETTINGS_MSG

    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    assert wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    worker.request_settings_dialog()
    assert wait_until(qapp, lambda: ("live", NO_SETTINGS_MSG) in worker.states)
    assert system.captures[0].settings_opened == 0


def test_settings_dialog_changes_are_saved_and_reapplied(qapp, worker, system, monkeypatch):
    import cv2

    from deskguard import camera_worker

    monkeypatch.setattr(camera_worker, "PROPS_POLL_S", 0.1)
    system.backend = "DirectShow"
    saved = []
    worker.camera_props_changed.connect(lambda d: saved.append(d))
    worker.configure_camera(0, 640, 480, 10)
    worker.start()
    assert wait_until(qapp, lambda: any(s == "live" for s, _ in worker.states))
    cap = system.captures[0]
    cap.props.update({cv2.CAP_PROP_BRIGHTNESS: 0.0, cv2.CAP_PROP_FOCUS: 10.0, cv2.CAP_PROP_AUTOFOCUS: 1.0})
    worker.request_settings_dialog()
    assert wait_until(qapp, lambda: cap.settings_opened == 1)
    time.sleep(0.2)
    # user changes brightness in the dialog; autofocus moves focus on its own
    cap.props[cv2.CAP_PROP_BRIGHTNESS] = 20.0
    cap.props[cv2.CAP_PROP_FOCUS] = 30.0
    assert wait_until(qapp, lambda: bool(saved))
    assert saved[-1] == {"brightness": 20.0}               # auto-focus drift is not saved

    # reopen (e.g. reconnect) re-applies the saved values
    worker.configure_camera(1, 640, 480, 10)
    assert wait_until(qapp, lambda: len(system.captures) == 2 and system.captures[1].prop_sets)
    assert system.captures[1].prop_sets == [(cv2.CAP_PROP_BRIGHTNESS, 20.0)]


class FakeFaceEngine:
    """Stands in for the YuNet/SFace engine: faces come from `self.faces`."""

    def __init__(self):
        self.loaded = False
        self.loads = self.unloads = 0
        self.faces = []
        self.n = 0

    def load(self):
        self.loaded = True
        self.loads += 1

    def unload(self):
        if self.loaded:
            self.unloads += 1
        self.loaded = False

    def detect(self, frame):
        return list(self.faces)

    def embed(self, frame, face):
        self.n += 1
        v = face.row.copy()
        v[2 + self.n % 50] = 0.3  # vary samples slightly
        return v / np.linalg.norm(v)


def _vec(i):
    v = np.zeros(128, np.float32)
    v[i] = 1.0
    return v


def test_watch_my_back_alerts_and_unloads(qapp, system, tmp_path):
    from deskguard.faces import Face, FaceProfile

    engine = FakeFaceEngine()
    profile = FaceProfile(tmp_path / "face.json")
    profile.vectors = [_vec(0)] * 3
    w = CameraWorker(open_fn=system.open, engine=engine, profile=profile)
    alerts, states = [], []
    w.watcher_alert.connect(lambda kind, secs: alerts.append((kind, secs)))
    w.presence_changed.connect(states.append)
    w.configure_camera(0, 640, 480, 10)
    try:
        w.start()
        assert wait_until(qapp, lambda: w._state is not None and w._state.value == "live")
        assert engine.loads == 0  # models are not loaded until the feature is on
        engine.faces = [Face((0.6, 0.3, 0.15, 0.25), 0.95, _vec(0)), Face((0.1, 0.2, 0.08, 0.12), 0.9, _vec(1))]
        w.set_presence(True, 1.0, False, 0.363, 60, "medium")
        assert wait_until(qapp, lambda: bool(alerts), timeout=5)
        assert alerts[0][0] == "face" and engine.loads == 1
        assert any(s.me_body is not None for s in states)
        assert w.detector._dynamic_zones  # MOTION ignores the user's body too
        w.set_presence(False, 1.0, False, 0.363, 60, "medium")
        assert wait_until(qapp, lambda: engine.unloads == 1)
        assert w.detector._dynamic_zones == []
        # hidden preview keeps watching while the feature is on
        w.set_visible(False)
        assert w._mode() == "idle"
        w.set_presence(True, 1.0, False, 0.363, 60, "medium")
        assert w._mode() == "monitor"
    finally:
        assert w.stop()
    assert not engine.loaded  # released on stop


def test_face_enrolment_saves_profile(qapp, system, tmp_path):
    from deskguard.faces import Face, FaceProfile

    engine = FakeFaceEngine()
    profile = FaceProfile(tmp_path / "face.json")
    w = CameraWorker(open_fn=system.open, engine=engine, profile=profile)
    progress, frames = [], []
    w.enroll_progress.connect(progress.append)
    w.enroll_frame_ready.connect(lambda: frames.append(w.take_enroll_frame()))
    w.configure_camera(0, 640, 480, 10)
    w.set_view(True, False, 1.0, 0.5, 0.5)
    engine.faces = [Face((0.4, 0.3, 0.2, 0.35), 0.97, _vec(0))]
    try:
        w.start()
        w.start_enroll()
        assert wait_until(qapp, lambda: any(p["done"] for p in progress), timeout=10)
    finally:
        assert w.stop()
    assert profile.enrolled and (tmp_path / "face.json").exists()
    done = [p for p in progress if p["done"]][0]
    assert done["flip_h"] is True and done["progress"] == 1.0
    assert any(f is not None and f.shape == (180, 320, 3) for f in frames)
    assert not w.enrolling


class FakeSegmenterOff:
    loaded = False

    def load(self):
        raise __import__("deskguard.faces", fromlist=["FaceEngineError"]).FaceEngineError("no model in tests")

    def unload(self):
        pass

    def segment(self, frame):
        return None


class FakeHandModelsW:
    def __init__(self):
        from tests.test_gestures import hand
        self.loaded = False
        self.loads = 0
        # an open palm whose wrist sits in the user's body area (frame 640x480)
        self.lm = hand() * 1.0 + [330, 150]

    def load(self):
        self.loaded, self.loads = True, self.loads + 1

    def unload(self):
        self.loaded = False

    def detect_palms(self, image):
        return np.array([np.r_[0, 0, 10, 10, np.zeros(14), 0.9]], np.float32)

    def landmarks(self, image, palm):
        return self.lm.copy()


def test_hand_gesture_and_snooze(qapp, system, tmp_path):
    from deskguard.faces import Face, FaceProfile

    engine = FakeFaceEngine()
    profile = FaceProfile(tmp_path / "face.json")
    profile.vectors = [_vec(0)] * 3
    hands = FakeHandModelsW()
    w = CameraWorker(open_fn=system.open, engine=engine, profile=profile,
                     segmenter=FakeSegmenterOff(), hand_models=hands)
    gestures, progress, alerts = [], [], []
    w.gesture.connect(gestures.append)
    w.gesture_progress.connect(lambda name, p: progress.append((name, p)))
    w.watcher_alert.connect(lambda k, s: alerts.append(k))
    engine.faces = [Face((0.5, 0.2, 0.15, 0.25), 0.95, _vec(0))]  # the user
    w.configure_camera(0, 640, 480, 10)
    try:
        w.start()
        assert wait_until(qapp, lambda: w._state is not None and w._state.value == "live")
        w.set_gestures(True, False, False, 2)
        assert wait_until(qapp, lambda: "palm" in gestures, timeout=6)
        assert hands.loads == 1 and any(name == "palm" and 0 < p < 1 for name, p in progress)
        # snoozed alerts are not sent
        w.snooze(60)
        engine.faces = [Face((0.5, 0.2, 0.15, 0.25), 0.95, _vec(0)), Face((0.1, 0.2, 0.08, 0.12), 0.9, _vec(1))]
        w.set_presence(True, 1.0, False, 0.363, 60, "medium")
        t_end = time.monotonic() + 3
        while time.monotonic() < t_end:
            qapp.processEvents()
            time.sleep(0.02)
        assert alerts == [] and w.snoozed
        w.snooze(0)
        assert not w.snoozed
        w.set_gestures(False, False, False, 2)
        assert wait_until(qapp, lambda: not hands.loaded)
    finally:
        assert w.stop()
