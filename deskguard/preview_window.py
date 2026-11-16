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
"""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from deskguard import win32
from deskguard.config import MAX_WINDOW_H, MAX_WINDOW_W, MIN_WINDOW_H, MIN_WINDOW_W
from deskguard.icons import STATUS_COLORS

EDGE = 7           # resize grip width in px
BTN = 18           # hover button size
BG = QColor("#111418")
BORDER = QColor(255, 255, 255, 38)
MOTION_RED = QColor("#ef4444")


class PreviewWindow(QWidget):
    exit_requested = Signal()
    hide_requested = Signal()
    menu_requested = Signal(QPoint)
    display_size_changed = Signal(int, int)  # physical pixels
    geometry_changed = Signal()
    user_interacted = Signal()

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
        self._corners_applied = False

        self._font = QFont(self.font())
        self._font.setPixelSize(11)
        self._bold = QFont(self._font)
        self._bold.setBold(True)

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

    def show_motion(self, seconds: float = 3.0) -> None:
        self._motion_until = time.monotonic() + seconds
        self._motion_timer.start(int(seconds * 1000) + 50)
        self.update()

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

        motion = time.monotonic() < self._motion_until
        if motion:
            p.setPen(QPen(MOTION_RED, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(r).adjusted(1.5, 1.5, -1.5, -1.5))
            p.setFont(self._bold)
            badge = QRectF(22, 4, 62, 15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(MOTION_RED)
            p.drawRoundedRect(badge, 3, 3)
            p.setPen(QColor("white"))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "MOTION")
        else:
            p.setPen(QPen(BORDER, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5))

        if self._hover:
            for name, rect in self._buttons().items():
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

    def _elide(self, text: str, width: float) -> str:
        return self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width))

    def _buttons(self) -> dict[str, QRect]:
        w = self.width()
        return {
            "close": QRect(w - BTN - 4, 4, BTN, BTN),
            "hide": QRect(w - 2 * BTN - 8, 4, BTN, BTN),
        }

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
        kind = "resize" if edges != Qt.Edge(0) else "move"
        self._drag = (kind, edges, event.globalPosition().toPoint(), self.geometry())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag is None:
            btn = self._button_at(pos)
            if btn != self._hover_btn:
                self._hover_btn = btn
                self.update()
            self.setCursor(QCursor(self._cursor_for(self._edges_at(pos)) if btn is None else Qt.CursorShape.ArrowCursor))
            return
        kind, edges, start, geo = self._drag
        if kind == "button":
            return
        delta = event.globalPosition().toPoint() - start
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
                (self.exit_requested if btn == "close" else self.hide_requested).emit()
        else:
            self.geometry_changed.emit()

    def contextMenuEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.user_interacted.emit()
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
