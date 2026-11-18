"""'Someone is watching you' toast: a Windows 11-style card at the bottom-right.

Silent, never takes keyboard focus (same window flags as the preview), never
appears in the taskbar. Only one toast exists; a new alert updates it.
"""

from __future__ import annotations

import time

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from deskguard.icons import draw_glyph
from deskguard.ui import theme

SHADOW = 14                 # transparent margin used for the soft shadow
CARD_W, CARD_H = 364, 132
CLOSE_AFTER_LEFT_S = 6.0    # stays this long after the person left
MAX_VISIBLE_S = 60.0        # hard limit even if someone keeps standing there
SCREEN_MARGIN = 12

TEXTS = {
    "face": ("Someone is watching you", "An unknown face has been behind you for {s} s."),
    "person": ("Someone is watching you", "A person has been standing behind you for {s} s."),
    "test": ("Test alert", "This is how DeskGuard tells you someone is behind you."),
}


class ToastWindow(QWidget):
    show_preview_requested = Signal()
    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setWindowTitle("DeskGuard alert")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setFixedSize(CARD_W + 2 * SHADOW, CARD_H + 2 * SHADOW)

        self.kind = "person"
        self._since = 0.0          # monotonic time the person was first seen
        self._present = False
        self._shown_at = 0.0
        self._hover_btn: str | None = None
        self._pressed: str | None = None
        self._hovered = False

        self._tick = QTimer(self, interval=1000, timeout=self._on_tick)
        self._close_timer = QTimer(self, singleShot=True, timeout=self.dismiss)
        self._anim: QParallelAnimationGroup | None = None

        self._title_font = QFont(self.font())
        self._title_font.setPixelSize(15)
        self._title_font.setWeight(QFont.Weight.DemiBold)
        self._body_font = QFont(self.font())
        self._body_font.setPixelSize(13)
        self._small_font = QFont(self.font())
        self._small_font.setPixelSize(12)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def show_alert(self, kind: str, seconds: float, screen: QRect | None = None) -> None:
        """Show (or update) the toast. `seconds` = how long the person has been there."""
        self.kind = kind
        self._since = time.monotonic() - max(0.0, seconds)
        self._present = kind != "test"
        self._close_timer.stop()
        if kind == "test":
            self._close_timer.start(int(CLOSE_AFTER_LEFT_S * 1000))
        if not self.isVisible():
            self._shown_at = time.monotonic()
            self._appear(screen)
        self._tick.start()
        self.update()

    def update_seconds(self, seconds: float) -> None:
        if self.isVisible() and self.kind != "test":
            self._since = time.monotonic() - max(0.0, seconds)
            self._present = True
            self._close_timer.stop()
            self.update()

    def person_left(self) -> None:
        """Start the auto-close countdown (paused while the mouse is over the toast)."""
        if not self.isVisible():
            return
        self._present = False
        self._tick.stop()
        if not self._hovered:
            self._close_timer.start(int(CLOSE_AFTER_LEFT_S * 1000))
        self.update()

    def dismiss(self) -> None:
        self._tick.stop()
        self._close_timer.stop()
        if self.isVisible():
            self.hide()
            self.dismissed.emit()

    def seconds(self) -> int:
        return int(time.monotonic() - self._since)

    def texts(self) -> tuple[str, str]:
        title, body = TEXTS.get(self.kind, TEXTS["person"])
        body = body.format(s=self.seconds())
        if self.kind != "test" and not self._present:
            body = "They have left. " + body.replace("has been", "was").replace(" for", " for about")
        return title, body

    # ------------------------------------------------------------------ #
    def _appear(self, screen: QRect | None) -> None:
        if screen is None:
            from PySide6.QtGui import QGuiApplication

            primary = QGuiApplication.primaryScreen()
            screen = primary.availableGeometry() if primary else QRect(0, 0, 1280, 720)
        # The card sits ~SCREEN_MARGIN from the screen edge; the transparent
        # shadow margin around it stays on screen too.
        gap = max(0, SCREEN_MARGIN - SHADOW)
        end = QPoint(screen.right() - self.width() + 1 - gap, screen.bottom() - self.height() + 1 - gap)
        start = end + QPoint(0, 18)
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()  # WA_ShowWithoutActivating: no focus change
        fade = QPropertyAnimation(self, b"windowOpacity", self, duration=180, startValue=0.0, endValue=1.0)
        slide = QPropertyAnimation(self, b"pos", self, duration=180, startValue=start, endValue=end,
                                   easingCurve=QEasingCurve.Type.OutCubic)
        self._anim = QParallelAnimationGroup(self)
        self._anim.addAnimation(fade)
        self._anim.addAnimation(slide)
        self._anim.start()

    def _on_tick(self) -> None:
        if time.monotonic() - self._shown_at > MAX_VISIBLE_S:
            self.person_left()
        self.update()

    def _card(self) -> QRectF:
        return QRectF(SHADOW, SHADOW, CARD_W, CARD_H)

    def _buttons(self) -> dict[str, QRectF]:
        c = self._card()
        bw, bh = (c.width() - 16 * 2 - 8) / 2, 32
        y = c.bottom() - 16 - bh
        return {
            "show": QRectF(c.left() + 16, y, bw, bh),
            "dismiss": QRectF(c.left() + 16 + bw + 8, y, bw, bh),
            "close": QRectF(c.right() - 34, c.top() + 8, 26, 26),
        }

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = self._card()

        # soft shadow
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(SHADOW, 0, -2):
            a = int(36 * (1 - i / SHADOW) ** 2) + 2
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(card.adjusted(-i, -i + 4, i, i + 4), 12 + i, 12 + i)

        p.setBrush(QColor(pal.card if pal.dark else "#ffffff"))
        p.setPen(QPen(pal.q("stroke"), 1))
        p.drawRoundedRect(card, 12, 12)

        # header row: app name + time
        p.setFont(self._small_font)
        p.setPen(pal.q("text2"))
        head = QRectF(card.left() + 16, card.top() + 12, card.width() - 60, 16)
        live = self._present and self.kind != "test"
        p.drawText(head, int(Qt.AlignmentFlag.AlignVCenter), "DeskGuard · " + ("now" if live else "just now"))

        # red shield badge
        danger = QColor(pal.danger)
        badge = QRectF(card.left() + 16, card.top() + 38, 40, 40)
        fill = QColor(danger)
        fill.setAlphaF(0.16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawEllipse(badge)
        draw_glyph(p, "eye" if self.kind == "face" else "alert", badge.adjusted(10, 10, -10, -10), danger, 2.0)
        if live and int(time.monotonic() * 2) % 2 == 0:  # gentle "live" dot
            p.setBrush(danger)
            p.drawEllipse(QPointF(badge.right() - 4, badge.top() + 5), 4, 4)

        title, body = self.texts()
        text_x = badge.right() + 12
        p.setPen(pal.q("text"))
        p.setFont(self._title_font)
        p.drawText(QRectF(text_x, card.top() + 36, card.right() - 16 - text_x, 22),
                   int(Qt.AlignmentFlag.AlignVCenter), title)
        p.setPen(pal.q("text2"))
        p.setFont(self._body_font)
        p.drawText(QRectF(text_x, card.top() + 58, card.right() - 16 - text_x, 20),
                   int(Qt.AlignmentFlag.AlignVCenter),
                   p.fontMetrics().elidedText(body, Qt.TextElideMode.ElideRight, int(card.right() - 16 - text_x)))

        # buttons
        btns = self._buttons()
        p.setFont(self._body_font)
        for name in ("show", "dismiss"):
            r = btns[name]
            hover = self._hover_btn == name
            if name == "show":
                col = QColor(pal.accent)
                if hover:
                    col = col.lighter(110)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                text_col = QColor(pal.on_accent)
            else:
                p.setPen(QPen(pal.q("stroke"), 1))
                p.setBrush(pal.q("control_hover") if hover else pal.q("control"))
                text_col = pal.q("text")
            p.drawRoundedRect(r, 5, 5)
            p.setPen(text_col)
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "Show preview" if name == "show" else "Dismiss")
        close = btns["close"]
        if self._hover_btn == "close":
            hover_bg = pal.q("text")
            hover_bg.setAlphaF(0.08)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(hover_bg)
            p.drawRoundedRect(close, 4, 4)
        draw_glyph(p, "close", close.adjusted(8, 8, -8, -8), pal.q("text2"), 1.3)
        p.end()

    # ------------------------------------------------------------------ #
    def _button_at(self, pos: QPointF) -> str | None:
        for name, r in self._buttons().items():
            if r.contains(pos):
                return name
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        btn = self._button_at(event.position())
        if btn != self._hover_btn:
            self._hover_btn = btn
            self.setCursor(Qt.CursorShape.PointingHandCursor if btn else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._pressed = self._button_at(event.position()) if event.button() == Qt.MouseButton.LeftButton else None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        btn = self._button_at(event.position())
        pressed, self._pressed = self._pressed, None
        if btn is None or btn != pressed:
            return
        if btn == "show":
            self.show_preview_requested.emit()
        self.dismiss()

    def enterEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._hovered = True
        self._close_timer.stop()

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._hovered = False
        self._hover_btn = None
        self.unsetCursor()
        self.update()
        if self.isVisible() and (not self._present or self.kind == "test"):
            self._close_timer.start(int(CLOSE_AFTER_LEFT_S * 1000))
