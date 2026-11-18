"""Smoke tests for the preview window and app wiring (offscreen Qt)."""

import numpy as np
import pytest
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

    from deskguard.faces import FaceProfile

    store = ConfigStore(tmp_path / "config.json")
    guard = app_mod.DeskGuardApp(qapp, store, profile=FaceProfile(tmp_path / "face.json"))
    guard.start()
    from tests.conftest import wait_until
    assert wait_until(qapp, lambda: guard.state == "live")
    assert wait_until(qapp, lambda: guard.window._frame is not None)
    # default size & top-right placement
    g = guard.window.geometry()
    assert (g.width(), g.height()) == (256, 144)

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
    guard.set_fps(15)
    guard.set_opacity(85)
    guard.window.move(50, 60)
    guard._save_geometry()
    saved = store.load()
    assert (saved.fps, saved.opacity, saved.window_x, saved.window_y) == (15, 85, 50, 60)

    guard.worker.stop()


def _mouse(kind, pos, button=Qt.MouseButton.LeftButton, mods=Qt.KeyboardModifier.NoModifier):
    buttons = button if kind != QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    return QMouseEvent(kind, QPointF(pos), QPointF(pos), button, buttons, mods)


def _drag(w, a, b, mods=Qt.KeyboardModifier.NoModifier):
    w.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, a, mods=mods))
    w.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, b, mods=mods))
    w.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, b, mods=mods))


def test_zone_edit_draws_zone_in_raw_coords_with_mirror(qapp):
    w = PreviewWindow()
    w.setGeometry(0, 0, 256, 144)
    w.set_view_params((640, 360), False, 1.0, 0.5, 0.5, True, False)  # mirrored
    zones = []
    w.zones_changed.connect(lambda z: zones.append(z))
    w.set_zone_edit(True)
    _drag(w, QPoint(20, 40), QPoint(84, 112))  # left quarter of the preview
    assert len(zones) == 1
    x, y, zw, zh = zones[-1][0]
    # mirrored: left of the preview is the right of the raw frame
    assert x == pytest.approx(1 - 84 / 256, abs=0.01) and zw == pytest.approx(64 / 256, abs=0.01)
    assert y == pytest.approx(40 / 144, abs=0.01) and zh == pytest.approx(72 / 144, abs=0.01)
    assert (w.x(), w.y()) == (0, 0)  # drawing a zone did not move the window
    w.grab()  # paints zones without error

    # right-click on the zone deletes it
    from PySide6.QtGui import QContextMenuEvent
    w.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(50, 70), QPoint(50, 70)))
    assert zones[-1] == []

    # tiny drags are clicks, not zones; Done leaves edit mode
    _drag(w, QPoint(100, 60), QPoint(102, 61))
    assert zones[-1] == []
    done = []
    w.zone_edit_finished.connect(lambda: done.append(1))
    c = w._buttons()["done"].center()
    _drag(w, c, c)
    assert done == [1] and not w.zone_edit
    # outside edit mode a drag moves the window again
    w.show()
    qapp.processEvents()
    start = w.pos()
    w.mousePressEvent(QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(100, 60), QPointF(100, 60),
                                  Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    w.mouseMoveEvent(QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(100, 60), QPointF(130, 70),
                                 Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert w.pos() == start + QPoint(30, 10)
    w.close()


def test_ctrl_wheel_zooms_and_ctrl_drag_pans(qapp):
    from PySide6.QtCore import QPoint as P
    from PySide6.QtGui import QWheelEvent

    w = PreviewWindow()
    w.setGeometry(0, 0, 256, 144)
    w.set_view_params((640, 360), False, 1.0, 0.5, 0.5, False, False)
    views = []
    w.view_changed.connect(lambda *v: views.append(v))

    def wheel(dy, mods):
        return QWheelEvent(QPointF(64, 72), QPointF(64, 72), P(0, 0), P(0, dy), Qt.MouseButton.NoButton,
                           mods, Qt.ScrollPhase.NoScrollPhase, False)

    w.wheelEvent(wheel(120, Qt.KeyboardModifier.NoModifier))
    assert views == []  # plain wheel does nothing
    w.wheelEvent(wheel(240, Qt.KeyboardModifier.ControlModifier))
    zoom, px, py = views[-1]
    assert zoom == pytest.approx(1.5625) and px < 0.5  # zoomed towards the cursor (left)
    w.grab()  # zoom badge paints

    _drag(w, QPoint(100, 70), QPoint(140, 70), mods=Qt.KeyboardModifier.ControlModifier)
    assert views[-1][1] < px  # dragged right => view moved left
    w.wheelEvent(wheel(-1200, Qt.KeyboardModifier.ControlModifier))
    assert views[-1] == (1.0, 0.5, 0.5)  # zoomed fully out snaps back to the full view
    w.close()
