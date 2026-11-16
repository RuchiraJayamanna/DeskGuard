"""App and tray icons, drawn with QPainter (no image files needed at runtime)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

STATUS_COLORS = {
    "live": "#22c55e",
    "starting": "#f59e0b",
    "reconnecting": "#f59e0b",
    "paused": "#9ca3af",
    "error": "#ef4444",
    "stopped": "#9ca3af",
    "motion": "#ef4444",
}

_cache: dict[str, QIcon] = {}


def draw_icon(size: int, status: str | None) -> QPixmap:
    s = float(size)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1f2937"))
    p.drawRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.22, s * 0.22)
    centre = QPointF(s * 0.5, s * 0.53)
    p.setBrush(QColor("#e5e7eb"))
    p.drawEllipse(centre, s * 0.29, s * 0.29)
    p.setBrush(QColor("#0f172a"))
    p.drawEllipse(centre, s * 0.19, s * 0.19)
    p.setBrush(QColor("#60a5fa"))
    p.drawEllipse(QPointF(s * 0.45, s * 0.47), s * 0.06, s * 0.06)
    if status:
        p.setBrush(QColor("#1f2937"))
        p.drawEllipse(QPointF(s * 0.80, s * 0.20), s * 0.17, s * 0.17)
        p.setBrush(QColor(STATUS_COLORS.get(status, "#9ca3af")))
        p.drawEllipse(QPointF(s * 0.80, s * 0.20), s * 0.12, s * 0.12)
    p.end()
    return pm


def icon(status: str | None = None) -> QIcon:
    key = status or ""
    if key not in _cache:
        ic = QIcon()
        for size in (16, 20, 24, 32, 48, 64, 256):
            ic.addPixmap(draw_icon(size, status))
        _cache[key] = ic
    return _cache[key]
