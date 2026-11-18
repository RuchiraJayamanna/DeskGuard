"""Settings window + app wiring with a fake camera and a fake face engine."""

import pytest

from deskguard.config import ConfigStore
from deskguard.faces import FaceProfile
from tests.conftest import wait_until


@pytest.fixture
def guard(qapp, tmp_path, monkeypatch):
    from deskguard import app as app_mod
    from deskguard.camera_worker import CameraWorker
    from tests.test_camera import FakeCameraSystem, FakeFaceEngine

    system = FakeCameraSystem()
    engine = FakeFaceEngine()
    monkeypatch.setattr(app_mod, "CameraWorker", lambda: CameraWorker(open_fn=system.open, engine=engine))
    monkeypatch.setattr(app_mod, "list_cameras", lambda: ["Fake Cam A", "Fake Cam B"])
    monkeypatch.setattr("deskguard.win32.set_autostart", lambda on: True)
    store = ConfigStore(tmp_path / "config.json")
    g = app_mod.DeskGuardApp(qapp, store, profile=FaceProfile(tmp_path / "face.json"))
    g.engine = engine
    g.start()
    assert wait_until(qapp, lambda: g.state == "live")
    yield g
    if g.settings is not None:
        g.settings.close()
    g.worker.stop()


def test_settings_window_controls_save(qapp, guard):
    guard.open_settings()
    w = guard.settings
    assert w is not None and w.isVisible()
    for key in ("watch", "motion", "camera", "view", "window", "privacy"):
        w.show_page(key)
        w.grab()  # every page paints
    w.c["motion_enabled"].click()
    assert guard.store.load().motion_enabled is True
    w.c["fps"].setCurrentIndex(w.c["fps"].findData(15))
    assert guard.store.load().fps == 15
    w.c["flip_h"].click()
    assert guard.store.load().flip_h is False
    w.c["watch_dwell_s"].setCurrentIndex(w.c["watch_dwell_s"].findData(10))
    assert guard.store.load().watch_dwell_s == 10
    # a change made elsewhere (quick menu) shows up in the window
    guard.set_opacity(70)
    assert w.c["opacity"].currentData() == 70
    w.c["theme"].setCurrentIndex(w.c["theme"].findData("light"))
    assert guard.store.load().theme == "light"
    from deskguard.ui import theme
    assert theme.current().dark is False
    w.close()
    qapp.processEvents()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert guard.settings is None  # destroyed on close: no RAM while closed


def test_turning_on_watch_without_face_opens_face_setup(qapp, guard):
    import numpy as np

    from deskguard.faces import Face

    v = np.zeros(128, np.float32)
    v[0] = 1
    guard.engine.faces = [Face((0.4, 0.3, 0.2, 0.35), 0.97, v)]
    guard.set_presence_enabled(True)
    w = guard.settings
    assert w is not None and w.stack.currentWidget() is w.face_page
    assert wait_until(qapp, lambda: w.face_page.action_btn.isEnabled() and w.face_page._done, timeout=10)
    assert guard.profile.enrolled
    w.face_page.action_btn.click()
    assert w.stack.currentWidget() is w.pages["watch"]
    assert "Set up ✓" in w.face_card.description.text()
    # forget = file deleted
    w.face_forget_btn.click()
    assert not guard.profile.enrolled and not guard.profile.path.exists()


def test_watcher_alert_shows_toast_and_red_tray(qapp, guard):
    guard._on_watcher_alert("person", 5.0)
    assert guard.toast is not None and guard.toast.isVisible()
    assert guard.watch_status()[0] in ("alert", "off")
    guard._on_watcher_cleared()
    assert not guard._alert_active
    guard.test_alert()
    assert guard.toast.kind == "test"
    guard.toast.dismiss()


def test_quick_menu_is_short(qapp, guard):
    guard._build_menu()
    titles = [a.text() for a in guard.menu.actions() if a.text()]
    assert "Watch my back" in titles and "Settings…" in titles and len(titles) <= 10


def test_gestures_page_and_actions(qapp, guard, monkeypatch):
    calls = []
    monkeypatch.setattr("deskguard.win32.show_desktop", lambda: calls.append("desktop") or True)
    monkeypatch.setattr("deskguard.win32.lock_workstation", lambda: calls.append("lock") or True)
    guard.open_settings("gestures")
    w = guard.settings
    w.grab()
    w.c["hand_gestures"].click()
    assert guard.store.load().hand_gestures is True and w.gesture_cards[0].isEnabled()
    box = w.c["action_palm"]
    box.setCurrentIndex(box.findData("snooze"))
    assert guard.store.load().gesture_actions["palm"] == "snooze"
    w._on_gesture_progress("fist", 0.5)
    assert "Fist" in w.gesture_live.text.text()
    w._on_gesture_seen("thumbs_up")
    assert "Thumbs up" in w.gesture_live.text.text()

    # palm is now mapped to snooze
    guard.cfg.presence_enabled = True
    guard._on_gesture("palm")
    assert guard.snoozed and guard.worker.snoozed and guard.hud is not None and "snoozed" in guard.hud.text
    guard.resume_alerts()
    assert not guard.snoozed
    # thumbs up dismisses an alert, nod too
    guard._on_watcher_alert("face", 6)
    assert guard.worker._armed
    guard._on_gesture("thumbs_up")
    assert not guard.toast.isVisible() and not guard._alert_active and not guard.worker._armed
    guard._on_watcher_alert("face", 6)
    guard._on_gesture("nod")
    assert not guard.toast.isVisible()
    # fist toggles Watch my back without opening anything
    before = guard.cfg.presence_enabled
    guard._on_gesture("fist")
    assert guard.cfg.presence_enabled is (not before)
    # look away -> chosen privacy action
    guard._on_gesture("look_away")
    guard.set_gesture_option("privacy_action", "lock")
    guard._on_gesture("look_away")
    assert calls == ["desktop", "lock"]
    guard.tray.show()
    guard.set_gesture_option("privacy_action", "hide_preview")
    guard._on_gesture("look_away")
    assert not guard.window.isVisible()


def test_start_in_background(qapp, tmp_path, monkeypatch):
    from deskguard import app as app_mod
    from deskguard.camera_worker import CameraWorker
    from tests.test_camera import FakeCameraSystem

    system = FakeCameraSystem()
    monkeypatch.setattr(app_mod, "CameraWorker", lambda: CameraWorker(open_fn=system.open))
    monkeypatch.setattr(app_mod, "list_cameras", lambda: ["Fake Cam A"])
    monkeypatch.setattr(app_mod.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    store = ConfigStore(tmp_path / "config.json")
    cfg = store.load()
    cfg.start_in_background = True
    store.save(cfg)
    g = app_mod.DeskGuardApp(qapp, store, profile=FaceProfile(tmp_path / "face.json"))
    g.start()
    try:
        assert wait_until(qapp, lambda: g.state == "live")
        assert not g.window.isVisible() and not g.worker._visible
    finally:
        g.worker.stop()
