"""Smoke tests for the preview window and app wiring (offscreen Qt)."""

import numpy as np
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from deskguard.preview_window import PreviewWindow


def test_window_flags_never_take_focus(qapp):
    w = PreviewWindow()
    flags = w.windowFlags()
    for f in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint,
              Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowDoesNotAcceptFocus):
        assert flags & f
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert w.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_paints_frame_states_and_motion(qapp):
    w = PreviewWindow()
    w.resize(240, 150)
    w.set_state("live", "")
    frame = np.zeros((150, 200, 3), np.uint8)
    frame[:, :, 2] = 255  # red in BGR
    w.set_frame(frame)
    img = w.grab().toImage()
    c = img.pixelColor(120, 75)
    assert (c.red(), c.green(), c.blue()) == (255, 0, 0)
    w.show_motion(1)
    w.grab()
    w.set_state("error", "No webcam found.")
    assert w._frame is None  # stale frames are dropped on error
    w.grab()


def test_drag_moves_and_edge_resizes(qapp):
    w = PreviewWindow()
    w.setGeometry(100, 100, 240, 150)
    w.show()
    qapp.processEvents()

    def ev(kind, local, global_, button=Qt.MouseButton.LeftButton):
        buttons = button if kind != QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
        return QMouseEvent(kind, QPointF(local), QPointF(global_), button, buttons, Qt.KeyboardModifier.NoModifier)

    # drag from the middle
    w.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, QPoint(100, 80), QPoint(200, 180)))
    w.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, QPoint(100, 80), QPoint(250, 210)))
    w.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, QPoint(100, 80), QPoint(250, 210)))
    assert (w.x(), w.y()) == (150, 130)

    # resize from bottom-right corner
    w.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, QPoint(238, 148), QPoint(388, 278)))
    w.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, QPoint(238, 148), QPoint(468, 318)))
    w.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, QPoint(238, 148), QPoint(468, 318)))
    assert (w.width(), w.height()) == (320, 190)

    # can't shrink below minimum
    w.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, QPoint(318, 188), QPoint(468, 318)))
    w.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, QPoint(318, 188), QPoint(100, 100)))
    w.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, QPoint(318, 188), QPoint(100, 100)))
    assert (w.width(), w.height()) == (160, 100)
    w.close()


def test_close_and_hide_buttons(qapp):
    w = PreviewWindow()
    w.setGeometry(0, 0, 240, 150)
    got = []
    w.exit_requested.connect(lambda: got.append("exit"))
    w.hide_requested.connect(lambda: got.append("hide"))
    for name in ("close", "hide"):
        c = w._buttons()[name].center()
        for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
            e = QMouseEvent(kind, QPointF(c), QPointF(c), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            (w.mousePressEvent if kind == QMouseEvent.Type.MouseButtonPress else w.mouseReleaseEvent)(e)
    assert got == ["exit", "hide"]


def test_app_starts_builds_menu_and_saves(qapp, tmp_path, monkeypatch):
    from deskguard import app as app_mod
    from deskguard.config import ConfigStore
    from tests.test_camera import FakeCameraSystem

    system = FakeCameraSystem()
    from deskguard.camera_worker import CameraWorker
    monkeypatch.setattr(app_mod, "CameraWorker", lambda: CameraWorker(open_fn=system.open))
    monkeypatch.setattr(app_mod, "list_cameras", lambda: ["Fake Cam A", "Fake Cam B"])

    store = ConfigStore(tmp_path / "config.json")
    guard = app_mod.DeskGuardApp(qapp, store)
    guard.start()
    from tests.conftest import wait_until
    assert wait_until(qapp, lambda: guard.state == "live")
    assert wait_until(qapp, lambda: guard.window._frame is not None)
    # default size & top-right placement
    g = guard.window.geometry()
    assert (g.width(), g.height()) == (240, 150)

    guard._build_menu()
    titles = [a.text() for a in guard.menu.actions()]
    assert "Hide preview" in titles and any(t.startswith("Exit") for t in titles)

    guard.tray.show()  # offscreen Qt has no real tray; pretend it is there
    # hide -> worker idle, show again -> frames resume
    guard.hide_preview()
    assert not guard.window.isVisible()
    guard.show_preview()
    guard.window.set_frame(None)
    assert wait_until(qapp, lambda: guard.window._frame is not None)

    # change a setting through the same path the menu uses
    guard._set_fps(15)
    guard._set_opacity(85)
    guard.window.move(50, 60)
    guard._save_geometry()
    saved = store.load()
    assert (saved.fps, saved.opacity, saved.window_x, saved.window_y) == (15, 85, 50, 60)

    guard.worker.stop()
