import time

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent

from deskguard.ui import toast as toast_mod
from deskguard.ui.toast import ToastWindow


def click(w, pos):
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        e = QMouseEvent(kind, QPointF(pos), QPointF(pos), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        (w.mousePressEvent if kind == QMouseEvent.Type.MouseButtonPress else w.mouseReleaseEvent)(e)


def test_toast_never_takes_focus(qapp):
    t = ToastWindow()
    flags = t.windowFlags()
    for f in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint,
              Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowDoesNotAcceptFocus):
        assert flags & f
    assert t.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert t.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_toast_shows_counts_and_closes_after_person_leaves(qapp, monkeypatch):
    monkeypatch.setattr(toast_mod, "CLOSE_AFTER_LEFT_S", 0.2)
    t = ToastWindow()
    screen = QRect(0, 0, 1920, 1040)
    t.show_alert("person", 5.2, screen)
    assert t.isVisible()
    title, body = t.texts()
    assert title == "Someone is watching you" and "5 s" in body
    t.update_seconds(9.0)
    assert "9 s" in t.texts()[1]
    t.grab()  # paints
    # placed bottom-right inside the screen
    assert screen.contains(t.geometry().center())
    assert t.geometry().right() <= screen.right() + 1
    t.person_left()
    assert "left" in t.texts()[1]
    deadline = time.monotonic() + 2
    while t.isVisible() and time.monotonic() < deadline:
        qapp.processEvents()
    assert not t.isVisible()


def test_toast_buttons(qapp):
    t = ToastWindow()
    got = []
    t.show_preview_requested.connect(lambda: got.append("show"))
    t.dismissed.connect(lambda: got.append("dismissed"))
    t.show_alert("face", 6, QRect(0, 0, 1280, 720))
    click(t, t._buttons()["show"].center())
    assert got == ["show", "dismissed"] and not t.isVisible()
    t.show_alert("test", 0, QRect(0, 0, 1280, 720))
    assert t.texts()[0] == "Test alert"
    click(t, t._buttons()["dismiss"].center())
    assert not t.isVisible()


def test_hud_pill_is_passive_and_hides(qapp, monkeypatch):
    from deskguard.ui.toast import HudPill

    h = HudPill()
    flags = h.windowFlags()
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus and flags & Qt.WindowType.WindowTransparentForInput
    assert h.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    h._hide_timer.setInterval(100)
    h.show_message("hand", "Preview hidden – still watching", QRect(0, 0, 1280, 720))
    assert h.isVisible() and h.width() > 200
    h.grab()
    deadline = time.monotonic() + 2
    while h.isVisible() and time.monotonic() < deadline:
        qapp.processEvents()
    assert not h.isVisible()
