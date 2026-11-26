"""Colour palettes for the PySide6 GUI.

The window used to hard-code GitHub-dark hex values in every painter and in one
big stylesheet, which made the app unreadable when the desktop is in light mode
and hid controls that relied on a single dark border colour. This module keeps
two complete palettes and one active ``P``; painters and the stylesheet read from
``P`` at draw time, so a palette swap needs no widget reconstruction.

Selection follows the desktop: Qt >= 6.5 exposes the system colour scheme, older
builds fall back to the window colour's lightness.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class Palette:
    """Every colour the GUI needs; one instance per scheme."""

    name: str
    bg: str
    panel: str
    panel2: str
    border: str
    border_strong: str
    text: str
    muted: str
    dim: str
    accent: str
    accent2: str
    accent_dark: str
    button_bg: str
    button_hover: str
    gridline: str
    badge_bg: str
    danger_bg: str
    danger_border: str
    danger_hover: str
    chart_line: str
    chart_line_dim: str
    heat_low: str
    heat_high: str
    heat_strong_text: str
    heat_weak_text: str
    band_colors: tuple[str, ...]


DARK = Palette(
    name="dark",
    bg="#0d1117",
    panel="#11161d",
    panel2="#161d26",
    border="#253040",
    border_strong="#304054",
    text="#e6edf3",
    muted="#8b9bb0",
    dim="#5d6b7e",
    accent="#38bdf8",
    accent2="#7dd3fc",
    accent_dark="#0284c7",
    button_bg="#1c2430",
    button_hover="#263244",
    gridline="#2b3645",
    badge_bg="#1e293b",
    danger_bg="#4c1d24",
    danger_border="#991b1b",
    danger_hover="#7f1d1d",
    chart_line="#38bdf8",
    chart_line_dim="#525b68",
    heat_low="#16324a",
    heat_high="#39befa",
    heat_strong_text="#0d1117",
    heat_weak_text="#dbeafe",
    band_colors=("#38bdf8", "#a78bfa", "#fb923c", "#f87171", "#4ade80", "#7dd3fc"),
)

LIGHT = Palette(
    name="light",
    bg="#f6f8fa",
    panel="#ffffff",
    panel2="#eef2f6",
    border="#d0d7de",
    border_strong="#b1bac4",
    text="#1f2328",
    muted="#59636e",
    dim="#7d8590",
    accent="#0969da",
    accent2="#218bff",
    accent_dark="#0550ae",
    button_bg="#f3f6fa",
    button_hover="#e6ebf1",
    gridline="#d8dee9",
    badge_bg="#ddf4ff",
    danger_bg="#ffebe9",
    danger_border="#ff8182",
    danger_hover="#ffd7d5",
    chart_line="#0969da",
    chart_line_dim="#8c959f",
    heat_low="#dbeafe",
    heat_high="#0b57d0",
    heat_strong_text="#ffffff",
    heat_weak_text="#0b3a63",
    band_colors=("#0969da", "#8250df", "#bc4c00", "#cf222e", "#1a7f37", "#218bff"),
)

P: Palette = DARK


def detect(app=None) -> Palette:
    """Pick the palette that matches the desktop's colour scheme."""
    import os

    forced = (os.environ.get("HIMOS_PSU_THEME") or "").strip().lower()
    if forced in ("dark", "light"):
        return DARK if forced == "dark" else LIGHT
    from PySide6 import QtCore, QtGui

    instance = app or QtGui.QGuiApplication.instance()
    if instance is not None:
        hints = instance.styleHints()
        scheme = getattr(hints, "colorScheme", None)
        if scheme is not None:
            try:
                value = scheme()
            except Exception:
                value = None
            if value is not None:
                name = getattr(value, "name", "")
                if name.lower() == "light":
                    return LIGHT
                if name.lower() == "dark":
                    return DARK
        palette = instance.palette()
        return LIGHT if palette.window().color().lightness() > 128 else DARK
    if QtCore is None:  # pragma: no cover - import guard only
        return DARK
    return DARK


def apply(palette: Palette | None = None) -> Palette:
    """Make ``palette`` the active one and let the widgets re-read their aliases."""
    global P
    if palette is not None:
        P = palette
    from . import gui_energy

    gui_energy.sync_theme(P)
    return P


def stylesheet() -> str:
    """Main-window stylesheet for the active palette."""
    return f"""
        QMainWindow, QWidget {{ background: {P.bg}; color: {P.text}; font-size: 13px; }}
        QGroupBox {{ border: 1px solid {P.border}; border-radius: 8px; margin-top: 10px;
                     padding: 10px; font-weight: bold; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px;
                            color: {P.accent2}; }}
        QPushButton {{ background: {P.button_bg}; border: 1px solid {P.border_strong};
                       border-radius: 6px; padding: 6px 12px; color: {P.text}; }}
        QPushButton:hover {{ background: {P.button_hover}; }}
        QPushButton:checked {{ background: {P.accent_dark}; border-color: {P.accent};
                               color: #ffffff; font-weight: bold; }}
        QPushButton:disabled {{ color: {P.dim}; border-color: {P.border}; }}
        QPushButton#danger {{ background: {P.danger_bg}; border-color: {P.danger_border}; }}
        QPushButton#danger:hover {{ background: {P.danger_hover}; }}
        QPushButton#navBtn {{ background: {P.panel2}; border: 1px solid {P.border_strong};
                              color: {P.text}; font-weight: bold; padding: 2px 10px; }}
        QPushButton#navBtn:hover {{ background: {P.button_hover}; border-color: {P.accent}; }}
        QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
            background: {P.button_bg}; border: 1px solid {P.border_strong};
            border-radius: 5px; padding: 4px 8px; color: {P.text}; }}
        QLabel#metricValue {{ font-size: 22px; font-weight: bold; color: {P.accent}; }}
        QLabel#badge {{ background: {P.badge_bg}; border: 1px solid {P.accent};
                        border-radius: 5px; padding: 2px 8px; color: {P.accent2};
                        font-weight: bold; }}
        QLabel#cardTitle {{ color: {P.text}; font-weight: bold; }}
        QLabel#cardUnit {{ color: {P.muted}; font-size: 11px; }}
        QLabel#hint {{ color: {P.dim}; font-size: 10px; }}
        QFrame#card {{ background: {P.panel}; border: 1px solid {P.border}; border-radius: 10px; }}
        QLabel#kpiTitle {{ color: {P.muted}; font-size: 11px; }}
        QLabel#kpiValue {{ color: {P.accent}; font-size: 24px; font-weight: bold; }}
        QLabel#kpiSub {{ color: {P.dim}; font-size: 10px; }}
        QTableWidget {{ background: transparent; border: none; gridline-color: {P.gridline};
                        color: {P.text}; }}
        QHeaderView::section {{ background: {P.panel2}; color: {P.muted}; border: none;
                                padding: 4px 6px; }}
        QTableWidget::item {{ color: {P.text}; padding: 2px 4px; }}
        QTableWidget::item:selected {{ background: {P.accent_dark}; color: #ffffff; }}
        QMessageBox, QDialog {{ background: {P.bg}; color: {P.text}; }}
        QTabWidget::pane {{ border: 1px solid {P.border}; border-radius: 10px; top: -1px; }}
        QTabBar::tab {{ background: transparent; color: {P.muted}; padding: 7px 18px;
                        margin-right: 4px; border: 1px solid transparent;
                        border-top-left-radius: 8px; border-top-right-radius: 8px; }}
        QTabBar::tab:selected {{ background: {P.panel}; color: {P.accent};
                                 border-color: {P.border}; border-bottom-color: {P.panel}; }}
        QTabBar::tab:hover {{ color: {P.text}; }}
    """


def interpolate(low: str, high: str, t: float):
    """Linearly blend two ``#rrggbb`` strings; used by the calendar heat map."""
    t = min(1.0, max(0.0, t))
    a = [int(low[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(high[i : i + 2], 16) for i in (1, 3, 5)]
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def palette_names() -> list[str]:
    return [f.name for f in fields(Palette)]
