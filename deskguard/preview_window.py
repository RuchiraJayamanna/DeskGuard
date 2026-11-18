"""The floating preview: a small frameless, always-on-top, never-focused window.

Window flags
- Tool                    : not shown in the taskbar or Alt+Tab.
- FramelessWindowHint     : no title bar.
- WindowStaysOnTopHint    : above normal windows.
- WindowDoesNotAcceptFocus: Qt sets WS_EX_NOACTIVATE on Windows, so clicking,
                            dragging or repainting never takes keyboard focus.
- WA_ShowWithoutActivating: showing the window doesn't activate it either.

Dragging and resizing are implemented manually (not with the OS move loop),
because Windows doesn't render live drags for non-activating windows.

Mouse controls
- Drag: move the window. Drag an edge: resize.
- Ctrl + wheel: digital zoom at the cursor. Ctrl + drag (while zoomed): pan.
- Zone edit mode: drag to draw an ignore zone, right-click a zone to delete it,
  "Done" / "Clear" buttons top-left (mouse only – the window never takes focus).
"""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from deskguard import win32
from deskguard.config import MAX_WINDOW_H, MAX_WINDOW_W, MAX_ZONES, MIN_WINDOW_H, MIN_WINDOW_W
from deskguard.icons import STATUS_COLORS, draw_glyph
from deskguard.presence import PresenceState
from deskguard.view import ViewTransform, clamp

EDGE = 7           # resize grip width in px
BTN = 18           # hover button size
BG = QColor("#111418")
BORDER = QColor(255, 255, 255, 38)
MOTION_RED = QColor("#ef4444")
ZONE_BLUE = QColor(59, 130, 246)
ZOOM_STEP = 1.25
MIN_ZONE_PX = 6     # smaller drags are treated as clicks, not zones
TEXT_BTNS = {"done": "Done", "clear": "Clear"}


class PreviewWindow(QWidget):
    exit_requested = Signal()
    hide_requested = Signal()
    menu_requested = Signal(QPoint)
    display_size_changed = Signal(int, int)  # physical pixels
    geometry_changed = Signal()
    user_interacted = Signal()
    view_changed = Signal(float, float, float)  # zoom, pan_x, pan_y
    zones_changed = Signal(object)              # list[list[float]] raw normalized
    zone_edit_finished = Signal()
    settings_requested = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setWindowTitle("DeskGuard")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_WINDOW_W, MIN_WINDOW_H)
        self.setMaximumSize(MAX_WINDOW_W, MAX_WINDOW_H)

        self._frame: np.ndarray | None = None
        self._state = "starting"
        self._message = ""
        self._motion_until = 0.0
        self._hover = False
        self._hover_btn: str | None = None
        self._pressed_btn: str | None = None
        self._drag: tuple[str, Qt.Edge, QPoint, QRect] | None = None
        self._press_pos = QPointF()
        self._pan_start: ViewTransform | None = None
        self._corners_applied = False

        # View parameters (mirror of what the worker renders) for mouse mapping.
        self._raw_size: tuple[int, int] | None = None
        self._crop = False
        self._zoom, self._pan = 1.0, (0.5, 0.5)
        self._flip = (False, False)
        self._zones: list[list[float]] = []
        self._zones_visible = False
        self._zone_edit = False
        self._draft: QRectF | None = None

        self._font = QFont(self.font())
        self._font.setPixelSize(11)
        self._bold = QFont(self._font)
        self._bold.setBold(True)

        # Watch my back overlay (boxes in raw-frame coordinates).
        self._presence: PresenceState | None = None
        self._presence_on = False
        self._alert_until = 0.0
        self._alert_timer = QTimer(self, singleShot=True, timeout=self.update)

        self._motion_timer = QTimer(self)
        self._motion_timer.setSingleShot(True)
        self._motion_timer.timeout.connect(self.update)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def set_frame(self, frame: np.ndarray | None) -> None:
        self._frame = frame
        self.update()

    def set_state(self, state: str, message: str) -> None:
        self._state, self._message = state, message
        if state != "live":
            self._frame = None  # never show a frozen image as if it were live
        self.update()

    def set_presence(self, state: PresenceState | None, enabled: bool) -> None:
        self._presence, self._presence_on = state, enabled
        self.update()

    def flash_alert(self, seconds: float) -> None:
        """Highlight the watcher(s) in red for a while."""
        self._alert_until = time.monotonic() + seconds
        self._alert_timer.start(int(seconds * 1000) + 50)
        self.update()

    def show_motion(self, seconds: float = 3.0) -> None:
        self._motion_until = time.monotonic() + seconds
        self._motion_timer.start(int(seconds * 1000) + 50)
        self.update()

    def set_view_params(self, raw_size: tuple[int, int] | None, crop: bool, zoom: float,
                        pan_x: float, pan_y: float, flip_h: bool, flip_v: bool) -> None:
        self._raw_size = raw_size
        self._crop, self._zoom, self._pan, self._flip = crop, zoom, (pan_x, pan_y), (flip_h, flip_v)
        self.update()

    def set_zones(self, zones: list[list[float]], visible: bool) -> None:
        self._zones = [list(z) for z in zones]
        self._zones_visible = visible
        self.update()

    def set_zone_edit(self, on: bool) -> None:
        self._zone_edit = on
        self._draft = None
        self.update()

    @property
    def zone_edit(self) -> bool:
        return self._zone_edit

    def transform(self) -> ViewTransform | None:
        """Current mapping between this widget (logical px) and the raw frame."""
        if self._raw_size is None:
            return None
        w, h = self._raw_size
        return ViewTransform(w, h, self.width(), self.height(), self._crop, self._zoom,
                             self._pan[0], self._pan[1], self._flip[0], self._flip[1])

    def physical_size(self) -> tuple[int, int]:
        dpr = self.devicePixelRatioF()
        return round(self.width() * dpr), round(self.height() * dpr)

    def set_on_top(self, on_top: bool) -> None:
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)  # hides the window
        if visible:
            self.show()  # WA_ShowWithoutActivating: no focus change
            win32.set_topmost(int(self.winId()), on_top)  # immediate, no activation
            win32.set_rounded_corners(int(self.winId()))

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, BG)

        frame = self._frame
        if frame is not None:
            h, w = frame.shape[:2]
            img = QImage(frame.data, w, h, frame.strides[0], QImage.Format.Format_BGR888)
            dpr = self.devicePixelRatioF()
            img.setDevicePixelRatio(dpr)
            p.drawImage(QPointF((r.width() - w / dpr) / 2, (r.height() - h / dpr) / 2), img)

        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self._font)

        if self._message:
            if frame is None:
                p.setPen(QColor("#d1d5db"))
                p.drawText(
                    QRectF(r).adjusted(12, 22, -12, -8),
                    int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                    self._message,
                )
            else:
                band = QRectF(0, r.height() - 20, r.width(), 20)
                p.fillRect(band, QColor(0, 0, 0, 160))
                p.setPen(QColor("#f3f4f6"))
                p.drawText(band.adjusted(6, 0, -6, 0), int(Qt.AlignmentFlag.AlignVCenter), self._elide(self._message, band.width() - 12))

        # Camera status dot (top-left): green = live, amber = starting/reconnecting,
        # grey = paused, red = error.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawEllipse(QPointF(11, 11), 6.5, 6.5)
        p.setBrush(QColor(STATUS_COLORS.get(self._state, "#9ca3af")))
        p.drawEllipse(QPointF(11, 11), 4.5, 4.5)

        self._paint_zones(p)
        self._paint_presence(p)

        motion = time.monotonic() < self._motion_until
        if motion:
            p.setPen(QPen(MOTION_RED, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(r).adjusted(1.5, 1.5, -1.5, -1.5))
            p.setFont(self._bold)
            badge = QRectF(22 + (18 if self._presence_on and not self._zone_edit else 0),
                           4 if not self._zone_edit else 26, 62, 15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(MOTION_RED)
            p.drawRoundedRect(badge, 3, 3)
            p.setPen(QColor("white"))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "MOTION")
        else:
            p.setPen(QPen(BORDER, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5))

        if self._zoom > 1.01:
            p.setFont(self._font)
            badge = QRectF(r.width() - 40, r.height() - 19, 34, 14)
            if self._zone_edit or (self._message and frame is not None):
                badge.translate(0, -20)  # keep clear of the bottom text band
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(badge, 3, 3)
            p.setPen(QColor("#f3f4f6"))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), f"{self._zoom:.1f}×")

        if self._zone_edit:
            p.setFont(self._bold)
            for name, rect in self._text_buttons().items():
                active = self._hover_btn == name
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(ZONE_BLUE.darker(130) if active else ZONE_BLUE)
                p.drawRoundedRect(QRectF(rect), 4, 4)
                p.setPen(QColor("white"))
                p.drawText(QRectF(rect), int(Qt.AlignmentFlag.AlignCenter), TEXT_BTNS[name])

        if self._hover:
            for name, rect in self._buttons().items():
                if name in TEXT_BTNS:
                    continue
                active = self._hover_btn == name
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(196, 43, 28) if active and name == "close" else QColor(0, 0, 0, 200 if active else 150))
                p.drawRoundedRect(QRectF(rect), 4, 4)
                p.setPen(QPen(QColor("white"), 1.4))
                c = QRectF(rect).center()
                if name == "close":
                    p.drawLine(QPointF(c.x() - 4, c.y() - 4), QPointF(c.x() + 4, c.y() + 4))
                    p.drawLine(QPointF(c.x() + 4, c.y() - 4), QPointF(c.x() - 4, c.y() + 4))
                else:
                    p.drawLine(QPointF(c.x() - 4, c.y()), QPointF(c.x() + 4, c.y()))
        p.end()

    def _box_rect(self, vt: ViewTransform, box: tuple[float, float, float, float]) -> QRectF:
        return self._zone_rect(vt, list(box))

    def _paint_presence(self, p: QPainter) -> None:
        if not self._presence_on or self._zone_edit:
            return
        st = self._presence
        watching = st is not None and st.watching
        alert = time.monotonic() < self._alert_until
        # Shield next to the status dot: green = watching your back, red = someone there.
        color = MOTION_RED if (watching or alert) else QColor("#22c55e")
        if st is not None and st.error:
            color = QColor("#9ca3af")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawEllipse(QPointF(29, 11), 8, 8)
        draw_glyph(p, "shield", QRectF(23, 5, 12, 12), color, 1.4)

        vt = self.transform()
        if st is None or vt is None or self._frame is None:
            return
        ox, oy = vt.offset()
        ow, oh = vt.output_size()
        p.save()
        p.setClipRect(QRectF(ox, oy, ow, oh))
        if st.me_body is not None:
            rect = self._box_rect(vt, st.me_body)
            p.setPen(QPen(QColor(34, 197, 94, 150), 1.2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
            p.setFont(self._font)
            tag = QRectF(rect.left() + 4, rect.top() + 3, 28, 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(22, 101, 52, 200))
            p.drawRoundedRect(tag, 3, 3)
            p.setPen(QColor("white"))
            p.drawText(tag, int(Qt.AlignmentFlag.AlignCenter), "You")
        boxes = list(st.strangers) + ([st.person] if st.person is not None and not st.strangers else [])
        for box in boxes:
            rect = self._box_rect(vt, box).adjusted(-2, -2, 2, 2)
            fill = QColor(MOTION_RED)
            fill.setAlpha(60 if alert else 28)
            p.setPen(QPen(MOTION_RED, 2.5 if alert else 1.6))
            p.setBrush(fill)
            p.drawRoundedRect(rect, 6, 6)
        p.restore()
        if watching and st.watch_seconds >= 1:
            p.setFont(self._bold)
            text = f"Watching you · {int(st.watch_seconds)} s"
            w = p.fontMetrics().horizontalAdvance(text) + 14
            badge = QRectF((self.width() - w) / 2, self.height() - 24, w, 17)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(MOTION_RED)
            p.drawRoundedRect(badge, 8.5, 8.5)
            p.setPen(QColor("white"))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), text)

    def _zone_rect(self, vt: ViewTransform, zone: list[float]) -> QRectF:
        x, y, w, h = zone
        x0, y0 = vt.raw_to_widget(x, y)
        x1, y1 = vt.raw_to_widget(x + w, y + h)
        return QRectF(QPointF(min(x0, x1), min(y0, y1)), QPointF(max(x0, x1), max(y0, y1)))

    def _paint_zones(self, p: QPainter) -> None:
        if not (self._zone_edit or (self._zones_visible and self._zones)):
            return
        vt = self.transform()
        if vt is None:
            return
        p.save()
        ox, oy = vt.offset()
        ow, oh = vt.output_size()
        p.setClipRect(QRectF(ox, oy, ow, oh))
        for zone in self._zones:
            rect = self._zone_rect(vt, zone)
            if self._zone_edit:
                fill = QColor(ZONE_BLUE)
                fill.setAlpha(70)
                p.fillRect(rect, fill)
                p.fillRect(rect, QBrush(QColor(255, 255, 255, 60), Qt.BrushStyle.BDiagPattern))
                p.setPen(QPen(ZONE_BLUE, 1.5))
            else:
                p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
        if self._draft is not None:
            p.setPen(QPen(ZONE_BLUE.lighter(130), 1.5, Qt.PenStyle.DashLine))
            p.drawRect(self._draft)
        p.restore()
        if self._zone_edit:
            r = self.rect()
            band = QRectF(0, r.height() - 20, r.width(), 20)
            p.fillRect(band, QColor(0, 0, 0, 170))
            p.setFont(self._font)
            p.setPen(QColor("#f3f4f6"))
            hint = "Drag over yourself to ignore motion · right-click a zone to delete"
            if len(self._zones) >= MAX_ZONES:
                hint = f"Maximum {MAX_ZONES} zones · right-click a zone to delete"
            p.drawText(band.adjusted(6, 0, -6, 0), int(Qt.AlignmentFlag.AlignVCenter),
                       self._elide(hint, band.width() - 12))

    def _elide(self, text: str, width: float) -> str:
        return self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width))

    def _buttons(self) -> dict[str, QRect]:
        w = self.width()
        buttons = {
            "close": QRect(w - BTN - 4, 4, BTN, BTN),
            "hide": QRect(w - 2 * BTN - 8, 4, BTN, BTN),
        }
        buttons.update(self._text_buttons())
        return buttons

    def _text_buttons(self) -> dict[str, QRect]:
        if not self._zone_edit:
            return {}
        return {"done": QRect(22, 4, 42, BTN), "clear": QRect(68, 4, 42, BTN)}

    # ------------------------------------------------------------------ #
    # Mouse: buttons, drag to move, edges to resize, right-click menu
    # ------------------------------------------------------------------ #
    def _edges_at(self, pos: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if pos.x() < EDGE:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() < EDGE:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edge) -> Qt.CursorShape:
        E = Qt.Edge
        if edges in (E.LeftEdge | E.TopEdge, E.RightEdge | E.BottomEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (E.RightEdge | E.TopEdge, E.LeftEdge | E.BottomEdge):
            return Qt.CursorShape.SizeBDiagCursor
        if edges in (E.LeftEdge, E.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        if edges in (E.TopEdge, E.BottomEdge):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _button_at(self, pos: QPoint) -> str | None:
        for name, rect in self._buttons().items():
            if rect.contains(pos):
                return name
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.user_interacted.emit()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        btn = self._button_at(pos)
        if btn is not None:
            self._drag = ("button", Qt.Edge(0), QPoint(), QRect())
            self._pressed_btn = btn
            return
        edges = self._edges_at(pos)
        if edges != Qt.Edge(0):
            kind = "resize"
        elif self._zone_edit and self._raw_size is not None:
            kind = "zone"
        elif event.modifiers() & Qt.KeyboardModifier.ControlModifier and self._zoom > 1.01:
            kind = "pan"
            self._pan_start = self.transform()
        else:
            kind = "move"
        self._drag = (kind, edges, event.globalPosition().toPoint(), self.geometry())
        self._press_pos = event.position()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag is None:
            btn = self._button_at(pos)
            if btn != self._hover_btn:
                self._hover_btn = btn
                self.update()
            if btn is not None:
                shape = Qt.CursorShape.ArrowCursor
            else:
                edges = self._edges_at(pos)
                shape = self._cursor_for(edges)
                if edges == Qt.Edge(0) and self._zone_edit:
                    shape = Qt.CursorShape.CrossCursor
            self.setCursor(QCursor(shape))
            return
        kind, edges, start, geo = self._drag
        if kind == "button":
            return
        if kind == "zone":
            self._draft = QRectF(self._press_pos, event.position()).normalized().intersected(QRectF(self.rect()))
            self.update()
            return
        delta = event.globalPosition().toPoint() - start
        if kind == "pan":
            vt = self._pan_start
            if vt is not None:
                self._pan = vt.pan_by(delta.x(), delta.y())
                self.view_changed.emit(self._zoom, *self._pan)
                self.update()
            return
        if kind == "move":
            self.move(geo.topLeft() + delta)
            return
        g = QRect(geo)
        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        if edges & Qt.Edge.LeftEdge:
            g.setLeft(min(geo.left() + delta.x(), geo.right() - min_w + 1))
        if edges & Qt.Edge.RightEdge:
            g.setRight(max(geo.right() + delta.x(), geo.left() + min_w - 1))
        if edges & Qt.Edge.TopEdge:
            g.setTop(min(geo.top() + delta.y(), geo.bottom() - min_h + 1))
        if edges & Qt.Edge.BottomEdge:
            g.setBottom(max(geo.bottom() + delta.y(), geo.top() + min_h - 1))
        g.setWidth(min(g.width(), self.maximumWidth()))
        g.setHeight(min(g.height(), self.maximumHeight()))
        self.setGeometry(g)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        if drag[0] == "button":
            btn = self._button_at(event.position().toPoint())
            if btn == self._pressed_btn:
                self._click_button(btn)
        elif drag[0] == "zone":
            self._finish_zone()
        elif drag[0] in ("move", "resize"):
            self.geometry_changed.emit()

    def _click_button(self, btn: str | None) -> None:
        if btn == "close":
            self.exit_requested.emit()
        elif btn == "hide":
            self.hide_requested.emit()
        elif btn == "done":
            self.set_zone_edit(False)
            self.zone_edit_finished.emit()
        elif btn == "clear":
            self._zones = []
            self.zones_changed.emit([])
            self.update()

    def _finish_zone(self) -> None:
        draft, self._draft = self._draft, None
        vt = self.transform()
        self.update()
        if draft is None or vt is None or draft.width() < MIN_ZONE_PX or draft.height() < MIN_ZONE_PX:
            return
        if len(self._zones) >= MAX_ZONES:
            return
        u0, v0 = vt.widget_to_raw(draft.left(), draft.top())
        u1, v1 = vt.widget_to_raw(draft.right(), draft.bottom())
        x0, x1 = sorted((clamp(u0, 0.0, 1.0), clamp(u1, 0.0, 1.0)))
        y0, y1 = sorted((clamp(v0, 0.0, 1.0), clamp(v1, 0.0, 1.0)))
        if x1 - x0 < 0.005 or y1 - y0 < 0.005:
            return  # drawn entirely in the letterbox
        self._zones.append([round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)])
        self.zones_changed.emit([list(z) for z in self._zones])

    def _zone_at(self, pos: QPointF) -> int | None:
        vt = self.transform()
        if vt is None:
            return None
        for i in reversed(range(len(self._zones))):
            if self._zone_rect(vt, self._zones[i]).contains(pos):
                return i
        return None

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        vt = self.transform()
        if vt is None or not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.ignore()
            return
        steps = event.angleDelta().y() / 120
        if not steps:
            return
        self.user_interacted.emit()
        pos = event.position()
        zoom, pan_x, pan_y = vt.zoom_at(pos.x(), pos.y(), self._zoom * ZOOM_STEP**steps)
        if zoom < 1.01:
            zoom, pan_x, pan_y = 1.0, 0.5, 0.5
        self._zoom, self._pan = zoom, (pan_x, pan_y)
        self.view_changed.emit(zoom, pan_x, pan_y)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._zone_edit:
            self.settings_requested.emit()

    def contextMenuEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.user_interacted.emit()
        if self._zone_edit:
            idx = self._zone_at(QPointF(event.pos()))
            if idx is not None:
                del self._zones[idx]
                self.zones_changed.emit([list(z) for z in self._zones])
                self.update()
                return
        self.menu_requested.emit(event.globalPos())

    def enterEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._hover = False
        self._hover_btn = None
        self.unsetCursor()
        self.update()

    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.display_size_changed.emit(*self.physical_size())
        super().resizeEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().showEvent(event)
        if not self._corners_applied:
            self._corners_applied = True
            win32.set_rounded_corners(int(self.winId()))
            handle = self.windowHandle()
            if handle is not None:  # DPI changes when dragged to another monitor
                handle.screenChanged.connect(lambda _s: self.display_size_changed.emit(*self.physical_size()))
        self.display_size_changed.emit(*self.physical_size())
