"""Windows 11 (Fluent) look: colour tokens for dark and light, accent colour, and
one stylesheet for the Settings window.

The theme follows Windows' "app mode" (light/dark) unless the user picks one,
and uses the Windows accent colour when it can be read.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtGui import QColor

TEXT_FAMILIES = ("Segoe UI Variable Text", "Segoe UI")
DISPLAY_FAMILIES = ("Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI")


def font_family(candidates: tuple[str, ...]) -> str:
    """First installed family (Qt style sheets accept only one family name)."""
    from PySide6.QtGui import QFontDatabase

    installed = set(QFontDatabase.families())
    for name in candidates:
        if name in installed:
            return name
    from PySide6.QtWidgets import QApplication

    return QApplication.font().family()


@dataclass(frozen=True)
class Palette:
    dark: bool
    window: str        # page background
    sidebar: str
    card: str
    card_hover: str
    stroke: str        # card border
    divider: str
    text: str
    text2: str         # secondary text
    text3: str         # disabled / tertiary
    control: str       # combo/button fill
    control_hover: str
    accent: str
    on_accent: str
    danger: str
    success: str
    warning: str

    def q(self, name: str) -> QColor:
        return QColor(getattr(self, name))


DARK_BASE = dict(
    dark=True, window="#202020", sidebar="#1b1b1b", card="#2b2b2b", card_hover="#323232",
    stroke="#363636", divider="#333333", text="#ffffff", text2="#c5c5c5", text3="#7a7a7a",
    control="#373737", control_hover="#3e3e3e", on_accent="#000000",
    danger="#ff6b6b", success="#6ccb5f", warning="#fce100",
)
LIGHT_BASE = dict(
    dark=False, window="#f3f3f3", sidebar="#ebebeb", card="#fbfbfb", card_hover="#f5f5f5",
    stroke="#e3e3e3", divider="#e5e5e5", text="#1a1a1a", text2="#5c5c5c", text3="#a0a0a0",
    control="#ffffff", control_hover="#f7f7f7", on_accent="#ffffff",
    danger="#c42b1c", success="#0f7b0f", warning="#9d5d00",
)


def _reg_dword(path: str, name: str) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return int(value)
    except (OSError, ValueError):
        return None


def system_prefers_dark() -> bool:
    light = _reg_dword(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme")
    return light == 0 if light is not None else True


def system_accent() -> QColor:
    """Windows accent colour (DWM stores it as 0xAABBGGRR), or Windows blue."""
    value = _reg_dword(r"Software\Microsoft\Windows\DWM", "AccentColor")
    if value is None:
        return QColor("#0078d4")
    return QColor(value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def palette_for(theme: str) -> Palette:
    dark = system_prefers_dark() if theme == "system" else theme == "dark"
    accent = system_accent()
    if dark:
        # Fluent uses a lighter accent tint on dark backgrounds.
        accent = accent.lighter(150) if accent.lightnessF() < 0.55 else accent
        base = DARK_BASE
    else:
        accent = accent.darker(115) if accent.lightnessF() > 0.5 else accent
        base = LIGHT_BASE
    # Text/knob colour on accent fills: whichever contrasts better (WCAG luminance).
    def lum(c: QColor) -> float:
        def ch(v: float) -> float:
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())
    on_accent = "#000000" if lum(accent) > 0.36 else "#ffffff"
    return Palette(accent=accent.name(), **{**base, "on_accent": on_accent})


_current: Palette = Palette(accent="#60cdff", **DARK_BASE)


def current() -> Palette:
    return _current


def set_current(p: Palette) -> None:
    global _current
    _current = p


def stylesheet(p: Palette) -> str:
    sel_text = p.on_accent
    FONT_FAMILY = f'"{font_family(TEXT_FAMILIES)}"'
    FONT_DISPLAY = f'"{font_family(DISPLAY_FAMILIES)}"'
    return f"""
    QWidget#SettingsRoot {{ background: {p.window}; color: {p.text}; font-family: {FONT_FAMILY}; font-size: 14px; }}
    QWidget#SettingsRoot QWidget {{ font-family: {FONT_FAMILY}; }}
    QFrame#Sidebar {{ background: {p.sidebar}; border: none; }}
    QWidget#Page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {p.window}; border: none; }}
    QLabel {{ color: {p.text}; background: transparent; }}
    QLabel[role="pageTitle"] {{ font-family: {FONT_DISPLAY}; font-size: 28px; font-weight: 600; }}
    QLabel[role="section"] {{ font-size: 14px; font-weight: 600; padding-top: 10px; }}
    QLabel[role="cardTitle"] {{ font-size: 14px; }}
    QLabel[role="caption"] {{ color: {p.text2}; font-size: 12px; }}
    QLabel[role="appName"] {{ font-size: 15px; font-weight: 600; }}
    QLabel[role="heroTitle"] {{ font-family: {FONT_DISPLAY}; font-size: 20px; font-weight: 600; }}
    QLabel[role="hint"] {{ font-size: 16px; font-weight: 600; }}
    QFrame#Card {{ background: {p.card}; border: 1px solid {p.stroke}; border-radius: 8px; }}
    QFrame#Card:hover {{ background: {p.card_hover}; }}
    QFrame#Hero {{ background: {p.card}; border: 1px solid {p.stroke}; border-radius: 12px; }}
    QPushButton {{
        background: {p.control}; color: {p.text}; border: 1px solid {p.stroke};
        border-bottom-color: {p.divider}; border-radius: 5px; padding: 5px 14px; min-height: 22px;
    }}
    QPushButton:hover {{ background: {p.control_hover}; }}
    QPushButton:pressed {{ color: {p.text2}; }}
    QPushButton:disabled {{ color: {p.text3}; }}
    QPushButton[accent="true"] {{ background: {p.accent}; color: {sel_text}; border: 1px solid {p.accent}; }}
    QPushButton[accent="true"]:hover {{ background: {QColor(p.accent).lighter(108).name()}; }}
    QPushButton[accent="true"]:disabled {{ background: {p.control}; color: {p.text3}; border-color: {p.stroke}; }}
    QPushButton[danger="true"] {{ color: {p.danger}; }}
    QComboBox {{
        background: {p.control}; color: {p.text}; border: 1px solid {p.stroke}; border-radius: 5px;
        padding: 4px 10px; min-height: 22px; min-width: 150px;
    }}
    QComboBox:hover {{ background: {p.control_hover}; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox QAbstractItemView {{
        background: {p.card}; color: {p.text}; border: 1px solid {p.stroke}; border-radius: 6px;
        selection-background-color: {p.control_hover}; selection-color: {p.text}; outline: none; padding: 4px;
    }}
    QSlider::groove:horizontal {{ height: 4px; background: {p.text3}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {p.accent}; border: 4px solid {p.control}; width: 12px; height: 12px;
        margin: -8px 0; border-radius: 10px;
    }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.text3}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
    QToolTip {{ background: {p.card}; color: {p.text}; border: 1px solid {p.stroke}; padding: 4px; }}
    """
