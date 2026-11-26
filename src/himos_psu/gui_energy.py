"""Energy dashboard widgets and tariff editor for the PySide6 GUI."""

from __future__ import annotations

import calendar
import math
import time
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from .config import default_config_path, load_config, parse_saved_profile, save_config
from .energy import (
    CN_TOU_PRESET,
    CN_TOU_PRESET_HEATING,
    PRICE_DECIMALS,
    Band,
    EnergyStore,
    SeasonWindow,
    TariffPlan,
    format_duration,
    format_kwh,
    format_yuan,
    local_day_end,
    local_day_start,
    parse_band_spec,
    parse_monthday,
    preset_plan,
    seasonal_preset,
    validate_bands,
)
from .protocol import FanMode

from . import theme
from .theme import DARK, Palette

# These aliases are rebound by sync_theme() whenever the palette changes, and the
# painters below read them at paint time, so a theme swap does not need new widgets.
BG = DARK.bg
PANEL = DARK.panel
PANEL2 = DARK.panel2
BORDER = DARK.border
BORDER_STRONG = DARK.border_strong
TEXT = DARK.text
MUTED = DARK.muted
DIM = DARK.dim
ACCENT = DARK.accent
ACCENT2 = DARK.accent2
ACCENT_DARK = DARK.accent_dark
BUTTON_BG = DARK.button_bg
BUTTON_HOVER = DARK.button_hover
GRIDLINE = DARK.gridline
DANGER_BG = DARK.danger_bg
BAND_COLORS = DARK.band_colors
ORANGE = DARK.band_colors[2]
GREEN = DARK.band_colors[4]
PURPLE = DARK.band_colors[1]
RED = DARK.band_colors[3]
CHART_LINE = DARK.chart_line
HEAT_LOW = DARK.heat_low
HEAT_HIGH = DARK.heat_high
HEAT_STRONG_TEXT = DARK.heat_strong_text
HEAT_WEAK_TEXT = DARK.heat_weak_text


def sync_theme(palette: Palette) -> None:
    """Rebind the module-level colour aliases to ``palette``."""
    global BG, PANEL, PANEL2, BORDER, BORDER_STRONG, TEXT, MUTED, DIM
    global ACCENT, ACCENT2, ACCENT_DARK, BUTTON_BG, BUTTON_HOVER, GRIDLINE, DANGER_BG
    global BAND_COLORS, CHART_LINE, HEAT_LOW, HEAT_HIGH, HEAT_STRONG_TEXT, HEAT_WEAK_TEXT
    global ORANGE, GREEN, PURPLE, RED
    BG = palette.bg
    PANEL = palette.panel
    PANEL2 = palette.panel2
    BORDER = palette.border
    BORDER_STRONG = palette.border_strong
    TEXT = palette.text
    MUTED = palette.muted
    DIM = palette.dim
    ACCENT = palette.accent
    ACCENT2 = palette.accent2
    ACCENT_DARK = palette.accent_dark
    BUTTON_BG = palette.button_bg
    BUTTON_HOVER = palette.button_hover
    GRIDLINE = palette.gridline
    DANGER_BG = palette.danger_bg
    BAND_COLORS = palette.band_colors
    ORANGE, PURPLE, RED, GREEN = palette.band_colors[2], palette.band_colors[1], palette.band_colors[3], palette.band_colors[4]
    CHART_LINE = palette.chart_line
    HEAT_LOW = palette.heat_low
    HEAT_HIGH = palette.heat_high
    HEAT_STRONG_TEXT = palette.heat_strong_text
    HEAT_WEAK_TEXT = palette.heat_weak_text


# --------------------------------------------------------------------------- widgets
class Card(QtWidgets.QFrame):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")


class KpiCard(Card):
    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("kpiTitle")
        layout.addWidget(self.title)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.value = QtWidgets.QLabel("--")
        self.value.setObjectName("kpiValue")
        self.unit = QtWidgets.QLabel("")
        self.unit.setObjectName("cardUnit")
        row.addWidget(self.value, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.unit, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        layout.addLayout(row)
        self.sub = QtWidgets.QLabel("--")
        self.sub.setObjectName("kpiSub")
        layout.addWidget(self.sub)

    def set(self, value: str, unit: str = "", sub: str = "", color: str = ACCENT) -> None:
        self.value.setText(value)
        self.unit.setText(unit)
        self.sub.setText(sub)
        self.value.setStyleSheet(f"color: {color}; font-size: 24px; font-weight: bold;")


class PowerChart(QtWidgets.QWidget):
    """Area chart of measured power (W) or daily energy (kWh)."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self._values: list[float | None] = []
        self._labels: list[str] = []
        self._details: list[str] = []
        self._unit = "W"
        self._caption = ""
        self._peak_prefix = "峰值"
        self._hover: int | None = None

    def set_data(self, values: Sequence[float | None], labels: Sequence[str], unit: str = "W",
                 caption: str = "", peak_prefix: str = "峰值",
                 details: Sequence[str] | None = None) -> None:
        self._values = list(values)
        self._labels = list(labels)
        self._details = list(details) if details is not None else []
        self._unit = unit
        self._caption = caption
        self._peak_prefix = peak_prefix
        self._hover = None
        self.update()

    # -- hover -------------------------------------------------------------
    def _plot_rect(self) -> QtCore.QRectF:
        pad_l, pad_r, pad_t, pad_b = 48, 14, 26, 26
        return QtCore.QRectF(pad_l, pad_t, max(60.0, self.width() - pad_l - pad_r),
                             max(50.0, self.height() - pad_t - pad_b))

    def _index_at(self, pos: QtCore.QPointF) -> int | None:
        if not self._values:
            return None
        plot = self._plot_rect()
        count = len(self._values)
        width = plot.width() / max(1, count - 1) if count > 1 else plot.width()
        if width <= 0:
            return None
        idx = round((pos.x() - plot.left()) / width)
        return max(0, min(count - 1, int(idx)))

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        idx = self._index_at(event.position())
        if idx != self._hover:
            self._hover = idx
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 (Qt naming)
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(BG))

        pad_l, pad_r, pad_t, pad_b = 48, 14, 26, 26
        plot = QtCore.QRectF(pad_l, pad_t, max(60.0, self.width() - pad_l - pad_r),
                             max(50.0, self.height() - pad_t - pad_b))
        values = [v for v in self._values if v is not None]
        if not values:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "暂无采集数据")
            return

        vmax = max(values)
        if self._unit == "W":
            step = 50.0 if vmax <= 500 else (100.0 if vmax <= 2000 else 500.0)
        else:
            step = 0.5 if vmax <= 5 else (1.0 if vmax <= 20 else 5.0)
        # Three grid bands, so keep the top a multiple of 3*step and the labels round
        # (350 used to show as 117 / 233 / 350).
        top = max(3 * step, math.ceil(vmax * 1.15 / (3 * step)) * (3 * step))

        for i in range(4):
            gy = plot.bottom() - (i / 3.0) * plot.height()
            p.setPen(QtGui.QPen(QtGui.QColor(BORDER), 1, QtCore.Qt.PenStyle.DashLine))
            p.drawLine(QtCore.QPointF(plot.left(), gy), QtCore.QPointF(plot.right(), gy))
            label = f"{top * i / 3:.1f}" if self._unit != "W" else f"{top * i / 3:.0f}"
            p.setFont(QtGui.QFont("", 7))
            p.setPen(QtGui.QColor(DIM))
            p.drawText(QtCore.QRectF(2, gy - 8, pad_l - 8, 16),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, label)

        count = len(self._values)
        width = plot.width() / max(1, count - 1) if count > 1 else plot.width()
        points: list[QtCore.QPointF | None] = []
        for idx, value in enumerate(self._values):
            if value is None:
                points.append(None)
                continue
            x = plot.left() + idx * width
            y = plot.bottom() - (value / top) * plot.height()
            points.append(QtCore.QPointF(x, y))

        # area fill per contiguous run so gaps stay visibly empty
        run: list[QtCore.QPointF] = []
        runs: list[list[QtCore.QPointF]] = []
        for pt in points:
            if pt is None:
                if run:
                    runs.append(run)
                    run = []
            else:
                run.append(pt)
        if run:
            runs.append(run)
        for seg in runs:
            if len(seg) < 2:
                continue
            area = QtGui.QPainterPath()
            area.moveTo(seg[0].x(), plot.bottom())
            for pt in seg:
                area.lineTo(pt)
            area.lineTo(seg[-1].x(), plot.bottom())
            area.closeSubpath()
            grad = QtGui.QLinearGradient(0, plot.top(), 0, plot.bottom())
            c_top = QtGui.QColor(ACCENT)
            c_top.setAlpha(110)
            c_bottom = QtGui.QColor(ACCENT)
            c_bottom.setAlpha(6)
            grad.setColorAt(0.0, c_top)
            grad.setColorAt(1.0, c_bottom)
            p.fillPath(area, QtGui.QBrush(grad))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.setPen(QtGui.QPen(QtGui.QColor(ACCENT2), 2.0))
            p.drawPolyline(QtGui.QPolygonF(seg))

        if values:
            peak = max(values)
            peak_idx = max(range(count), key=lambda i: self._values[i] if self._values[i] is not None else -1)
            if points[peak_idx] is not None:
                px = points[peak_idx]
                p.setPen(QtGui.QPen(QtGui.QColor(ORANGE), 1, QtCore.Qt.PenStyle.DashLine))
                p.drawLine(QtCore.QPointF(px.x(), px.y()), QtCore.QPointF(px.x(), plot.bottom()))
                p.setPen(QtGui.QPen(QtGui.QColor(BG), 2))
                p.setBrush(QtGui.QColor(ORANGE))
                p.drawEllipse(px, 4.0, 4.0)
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                label = (f"{self._peak_prefix} {peak:.0f} {self._unit}" if self._unit == "W"
                         else f"{self._peak_prefix} {peak:.2f} {self._unit}")
                p.setFont(QtGui.QFont("", 8))
                p.setPen(QtGui.QColor(ORANGE))
                tx = min(max(plot.left(), px.x() - 70), plot.right() - 130)
                ty = max(plot.top(), px.y() - 20)
                p.drawText(QtCore.QRectF(tx, ty, 130, 16), QtCore.Qt.AlignmentFlag.AlignLeft, label)

        if self._labels:
            slots = [i for i, lbl in enumerate(self._labels) if lbl]
            step_idx = max(1, len(slots) // 7)
            for pos, idx in enumerate(slots):
                if pos % step_idx:
                    continue
                x = plot.left() + idx * width
                p.setFont(QtGui.QFont("", 7))
                p.setPen(QtGui.QColor(DIM))
                p.drawText(QtCore.QRectF(x - 22, plot.bottom() + 4, 44, 14),
                           QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop,
                           self._labels[idx])

        self._paint_hover(p, plot, points, width)

        if self._caption:
            p.setFont(QtGui.QFont("", 8))
            p.setPen(QtGui.QColor(DIM))
            p.drawText(QtCore.QRectF(pad_l, 4, self.width() - pad_l - pad_r, 16),
                       QtCore.Qt.AlignmentFlag.AlignLeft, self._caption)

    def _paint_hover(self, p: QtGui.QPainter, plot: QtCore.QRectF,
                     points: list[QtCore.QPointF | None], width: float) -> None:
        """Crosshair + value box under the cursor, like a trading chart."""
        idx = self._hover
        if idx is None or not (0 <= idx < len(self._values)):
            return
        x = plot.left() + idx * width
        pt = points[idx] if idx < len(points) else None
        p.setPen(QtGui.QPen(QtGui.QColor(ACCENT2), 1, QtCore.Qt.PenStyle.DashLine))
        p.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
        if pt is not None:
            p.setPen(QtGui.QPen(QtGui.QColor(BG), 2))
            p.setBrush(QtGui.QColor(ACCENT2))
            p.drawEllipse(pt, 4.0, 4.0)
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        detail = self._details[idx] if idx < len(self._details) else ""
        if not detail:
            label = self._labels[idx] if idx < len(self._labels) else ""
            value = self._values[idx]
            if value is None:
                detail = f"{label} · 无数据".strip(" ·")
            else:
                detail = f"{label} · {value:.0f} {self._unit}" if self._unit == "W" else f"{label} · {value:.2f} {self._unit}"

        # Rough text width (CJK glyphs are wider) keeps the box from being clipped
        # without asking Qt for font metrics on every mouse move.
        text_w = sum(11.0 if ord(ch) > 0x2E80 else 7.0 for ch in detail)
        box_w = min(max(120.0, text_w + 18), plot.width() - 8)
        box_h = 22.0
        box_x = min(max(plot.left() + 4, x + 12), plot.right() - box_w - 4)
        if box_x < x + 12:  # flipped to the left side: keep it off the cursor
            box_x = max(plot.left() + 4, x - box_w - 12)
        box_y = plot.top() + 6
        box = QtCore.QRectF(box_x, box_y, box_w, box_h)
        p.setPen(QtGui.QPen(QtGui.QColor(BORDER_STRONG), 1))
        p.setBrush(QtGui.QColor(PANEL2))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QtGui.QColor(TEXT))
        p.setFont(QtGui.QFont("", 8))
        p.drawText(box.adjusted(9, 0, -6, 0),
                   QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, detail)


class SplitDonut(QtWidgets.QWidget):
    """Donut of tariff-band shares with legend and centre totals."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(170)
        self._slices: list[dict[str, Any]] = []
        self._center_top = "--"
        self._center_mid = ""
        self._center_bottom = ""

    def set_data(self, slices: Sequence[dict[str, Any]], center_top: str, center_mid: str, center_bottom: str) -> None:
        self._slices = list(slices)
        self._center_top = center_top
        self._center_mid = center_mid
        self._center_bottom = center_bottom
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(BG))
        if not self._slices:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "暂无数据")
            return
        size = min(150.0, self.height() - 24.0)
        rect = QtCore.QRectF(16, (self.height() - size) / 2, size, size)
        start = 90.0
        for idx, item in enumerate(self._slices):
            sweep = -float(item["share"]) * 360.0
            if sweep == 0:
                continue
            p.setBrush(QtGui.QColor(BAND_COLORS[idx % len(BAND_COLORS)]))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawPie(rect, int(start * 16), int(sweep * 16))
            start += sweep
        inner = rect.adjusted(size * 0.27, size * 0.27, -size * 0.27, -size * 0.27)
        p.setBrush(QtGui.QColor(PANEL))
        p.drawEllipse(inner)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        p.setPen(QtGui.QColor(TEXT))
        p.setFont(QtGui.QFont("", 15, QtGui.QFont.Weight.Bold))
        p.drawText(QtCore.QRectF(inner.x(), inner.y() + 8, inner.width(), 24),
                   QtCore.Qt.AlignmentFlag.AlignHCenter, self._center_top)
        p.setFont(QtGui.QFont("", 8))
        p.setPen(QtGui.QColor(MUTED))
        p.drawText(QtCore.QRectF(inner.x(), inner.y() + 32, inner.width(), 14),
                   QtCore.Qt.AlignmentFlag.AlignHCenter, self._center_mid)
        p.setPen(QtGui.QColor(ORANGE))
        p.drawText(QtCore.QRectF(inner.x(), inner.y() + 48, inner.width(), 14),
                   QtCore.Qt.AlignmentFlag.AlignHCenter, self._center_bottom)

        lx = rect.right() + 22
        # Column offsets: the name may be "统一电价" (4 CJK ≈ 48px), so it needs more
        # room than the old 40px box that used to run into the kWh figure.
        name_dx, name_w = 16, 56
        kwh_dx, kwh_w = 78, 78
        pct_dx, pct_w = 158, 52
        for idx, item in enumerate(self._slices):
            ly = 18 + idx * 26
            if ly > self.height() - 16:
                break
            p.setBrush(QtGui.QColor(BAND_COLORS[idx % len(BAND_COLORS)]))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(QtCore.QRectF(lx, ly + 4, 9, 9), 2, 2)
            p.setPen(QtGui.QColor(TEXT))
            p.setFont(QtGui.QFont("", 9))
            p.drawText(QtCore.QRectF(lx + name_dx, ly, name_w, 18),
                       QtCore.Qt.AlignmentFlag.AlignLeft, str(item["name"]))
            p.setPen(QtGui.QColor(MUTED))
            p.setFont(QtGui.QFont("", 9))
            p.drawText(QtCore.QRectF(lx + kwh_dx, ly, kwh_w, 18), QtCore.Qt.AlignmentFlag.AlignLeft,
                       f"{item['kwh']:.2f} kWh")
            p.drawText(QtCore.QRectF(lx + pct_dx, ly, pct_w, 18),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                       f"{item['share'] * 100:.0f}%")


class CalendarHeat(QtWidgets.QWidget):
    """Month calendar with per-day energy intensity; clickable days."""

    dayClicked = QtCore.Signal(object)  # datetime.date

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._year = date.today().year
        self._month = date.today().month
        self._values: dict[int, float] = {}
        self._selected: date | None = None

    def set_month(self, year: int, month: int, values: dict[int, float]) -> None:
        self._year, self._month, self._values = year, month, values
        self.update()

    def set_selected(self, day: date | None) -> None:
        self._selected = day
        self.update()

    # -- geometry ----------------------------------------------------------
    def _grid(self) -> tuple[int, int, float, float]:
        days = calendar.monthrange(self._year, self._month)[1]
        offset = date(self._year, self._month, 1).weekday()  # Monday == 0
        rows = (offset + days + 6) // 7
        cell_w = (self.width() - 6 * 4) / 7.0
        cell_h = (self.height() - 18 - (rows - 1) * 4) / rows
        return offset, days, cell_w, cell_h

    def _cell_rect(self, day: int) -> tuple[QtCore.QRectF, int, int]:
        offset, _days, cell_w, cell_h = self._grid()
        row, col = divmod(offset + day - 1, 7)
        x = col * (cell_w + 4)
        y = 18 + row * (cell_h + 4)
        return QtCore.QRectF(x, y, cell_w, cell_h), row, col

    def day_at(self, pos: QtCore.QPoint) -> int | None:
        """Which day number is under ``pos`` (None for gaps/headers)."""
        _offset, days, _cw, _ch = self._grid()
        for day in range(1, days + 1):
            if self._cell_rect(day)[0].contains(QtCore.QPointF(pos)):
                return day
        return None

    # -- events ------------------------------------------------------------
    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            day = self.day_at(event.position().toPoint())
            if day is not None:
                self.dayClicked.emit(date(self._year, self._month, day))
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        day = self.day_at(event.position().toPoint())
        if day is None:
            QtWidgets.QToolTip.hideText()
        else:
            when = date(self._year, self._month, day)
            value = self._values.get(day)
            detail = f"{value:.3f} kWh" if value else "暂无记录"
            QtWidgets.QToolTip.showText(
                event.globalPosition().toPoint(), f"{when:%Y-%m-%d} · {detail}\n点击查看当日曲线", self
            )
        super().mouseMoveEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(BG))
        offset, days, cell_w, cell_h = self._grid()
        p.setFont(QtGui.QFont("", 7))
        for col, name in enumerate("一二三四五六日"):
            p.setPen(QtGui.QColor(DIM))
            p.drawText(QtCore.QRectF(col * (cell_w + 4), 0, cell_w, 14),
                       QtCore.Qt.AlignmentFlag.AlignHCenter, name)
        vmax = max(self._values.values()) if self._values else 1.0
        today = date.today()
        for day in range(1, days + 1):
            rect, _row, _col = self._cell_rect(day)
            value = self._values.get(day, 0.0)
            if value > 0:
                t = (value / vmax) ** 0.85
                color = QtGui.QColor(*theme.interpolate(HEAT_LOW, HEAT_HIGH, t))
                text_color = HEAT_STRONG_TEXT if t > 0.5 else HEAT_WEAK_TEXT
            else:
                color = QtGui.QColor(PANEL2)
                text_color = DIM
            p.setBrush(color)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(rect, 3, 3)
            is_today = (self._year, self._month, day) == (today.year, today.month, today.day)
            is_selected = self._selected == date(self._year, self._month, day)
            if is_today or is_selected:
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                if is_selected:
                    # Selected: thick accent ring + a second inner ring so it reads at a glance.
                    p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 2.6))
                    p.drawRoundedRect(QtCore.QRectF(rect.x() + 1.5, rect.y() + 1.5,
                                                    rect.width() - 3, rect.height() - 3), 3, 3)
                else:
                    p.setPen(QtGui.QPen(QtGui.QColor(ACCENT2), 1.2))
                    p.drawRoundedRect(QtCore.QRectF(rect.x() + 0.5, rect.y() + 0.5,
                                                    rect.width() - 1, rect.height() - 1), 3, 3)
            p.setFont(QtGui.QFont("", 8, QtGui.QFont.Weight.Bold if is_selected else QtGui.QFont.Weight.Normal))
            p.setPen(QtGui.QColor(text_color))
            p.drawText(QtCore.QRectF(rect.x() + 4, rect.y(), rect.width() - 8, rect.height()),
                       QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, str(day))


class LoadBars(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self._rows: list[dict[str, Any]] = []
        self._note = ""

    def set_data(self, rows: Sequence[dict[str, Any]], note: str) -> None:
        self._rows = list(rows)
        self._note = note
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(BG))
        if not self._rows:
            return
        row_h = min(24.0, (self.height() - 20) / max(1, len(self._rows)))
        for idx, row in enumerate(self._rows):
            y = idx * row_h
            p.setFont(QtGui.QFont("", 8))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QtCore.QRectF(0, y, 54, row_h), QtCore.Qt.AlignmentFlag.AlignVCenter, row["label"])
            track = QtCore.QRectF(58, y + row_h / 2 - 4, max(40.0, self.width() - 130), 8)
            p.setBrush(QtGui.QColor(PANEL2))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(track, 4, 4)
            p.setBrush(QtGui.QColor(BAND_COLORS[idx % len(BAND_COLORS)]))
            p.drawRoundedRect(QtCore.QRectF(track.x(), track.y(), max(4.0, track.width() * row["share"]), 8), 4, 4)
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(QtCore.QRectF(self.width() - 58, y, 54, row_h),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                       f"{row['share'] * 100:.0f}%")
        p.setFont(QtGui.QFont("", 7))
        p.setPen(QtGui.QColor(DIM))
        p.drawText(QtCore.QRectF(0, self.height() - 16, self.width(), 14),
                   QtCore.Qt.AlignmentFlag.AlignLeft, self._note)


# --------------------------------------------------------------------------- tariff dialog
class TariffDialog(QtWidgets.QDialog):
    """Create/edit a tariff plan (fixed, time-of-use, optionally seasonal)."""

    def __init__(self, store: EnergyStore, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("电价方案")
        self.resize(620, 560)
        # Rows are kept per season so switching the editor never loses edits.
        self._rows: dict[str, list[tuple[str, str, str, float]]] = {"normal": [], "heating": []}
        self._editing = "normal"
        self._seeded = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit()
        self.kind_combo = QtWidgets.QComboBox()
        self.kind_combo.addItem("固定电价（元/kWh）", "fixed")
        self.kind_combo.addItem("峰谷分时（尖/峰/平/谷）", "tou")
        self.price_spin = QtWidgets.QDoubleSpinBox()
        self.price_spin.setDecimals(PRICE_DECIMALS)
        self.price_spin.setRange(0.0, 20.0)
        self.price_spin.setSingleStep(0.0001)
        self.price_spin.setValue(0.5550)
        form.addRow("方案名称", self.name_edit)
        form.addRow("计费方式", self.kind_combo)
        form.addRow("固定单价", self.price_spin)
        layout.addLayout(form)

        self.tou_box = QtWidgets.QWidget()
        tou_layout = QtWidgets.QVBoxLayout(self.tou_box)
        tou_layout.setContentsMargins(0, 0, 0, 0)
        tou_layout.setSpacing(8)

        season_row = QtWidgets.QHBoxLayout()
        self.season_check = QtWidgets.QCheckBox("区分采暖期（时段与电价不同）")
        self.season_start_edit = QtWidgets.QLineEdit("11-01")
        self.season_end_edit = QtWidgets.QLineEdit("03-31")
        for edit in (self.season_start_edit, self.season_end_edit):
            edit.setMaximumWidth(64)
            edit.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        season_row.addWidget(self.season_check)
        season_row.addSpacing(8)
        season_row.addWidget(QtWidgets.QLabel("采暖期"))
        season_row.addWidget(self.season_start_edit)
        season_row.addWidget(QtWidgets.QLabel("至（次年）"))
        season_row.addWidget(self.season_end_edit)
        season_row.addStretch(1)
        tou_layout.addLayout(season_row)

        editor_row = QtWidgets.QHBoxLayout()
        editor_row.addWidget(QtWidgets.QLabel("正在编辑"))
        self.season_combo = QtWidgets.QComboBox()
        self.season_combo.addItem("非采暖期（其余日期）", "normal")
        self.season_combo.addItem("采暖期", "heating")
        editor_row.addWidget(self.season_combo)
        editor_row.addStretch(1)
        tou_layout.addLayout(editor_row)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["开始", "结束", "时段名", "单价"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.hint = QtWidgets.QLabel("峰谷时段（必须从 00:00 连续覆盖到 24:00）")
        tou_layout.addWidget(self.hint)
        tou_layout.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton("添加时段")
        self.del_btn = QtWidgets.QPushButton("删除选中")
        self.preset_btn = QtWidgets.QPushButton("载入示例峰谷")
        self.add_btn.clicked.connect(self._add_row)
        self.del_btn.clicked.connect(self._del_row)
        self.preset_btn.clicked.connect(self._load_preset)
        row.addWidget(self.add_btn)
        row.addWidget(self.del_btn)
        row.addWidget(self.preset_btn)
        row.addStretch(1)
        tou_layout.addLayout(row)
        layout.addWidget(self.tou_box, 1)

        self.recalc_check = QtWidgets.QCheckBox("保存后按新电价重算历史费用")
        self.recalc_check.setChecked(True)
        layout.addWidget(self.recalc_check)

        self.fixed_hint = QtWidgets.QLabel(
            "固定电价：所有用电按上面这一个单价计算，不需要时段表。"
        )
        self.fixed_hint.setObjectName("hint")
        layout.addWidget(self.fixed_hint)

        note = QtWidgets.QLabel(
            "电价保留 4 位小数（如 0.5469）。价格为示例默认值，请按当地居民峰谷/阶梯电价填写；"
            "本工具只做价格计算，不联网查询。"
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        layout.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.kind_combo.currentIndexChanged.connect(self._sync_mode)
        self.season_check.toggled.connect(self._on_season_toggled)
        self.season_combo.currentIndexChanged.connect(self._on_season_editor_changed)
        self._load_plan(store.active_plan())
        self._sync_mode()

    # -- helpers -----------------------------------------------------------
    def _load_plan(self, plan: TariffPlan | None) -> None:
        plan = plan or TariffPlan(name="我的电价", kind="fixed", price=0.5550,
                                  effective_from=date.today().isoformat())
        self.name_edit.setText(plan.name)
        self.kind_combo.setCurrentIndex(1 if plan.is_tou else 0)
        self.price_spin.setValue(plan.price or 0.5550)
        self._rows["normal"] = [_band_tuple(band) for band in plan.bands]
        season = plan.season
        self._rows["heating"] = [_band_tuple(band) for band in (season.bands if season else ())]
        if season is not None:
            self.season_start_edit.setText(f"{season.start[0]:02d}-{season.start[1]:02d}")
            self.season_end_edit.setText(f"{season.end[0]:02d}-{season.end[1]:02d}")
        # Block signals: rendering the table must not look like a user edit.
        self.season_check.blockSignals(True)
        self.season_check.setChecked(season is not None)
        self.season_check.blockSignals(False)
        self._editing = "normal"
        self.season_combo.blockSignals(True)
        self.season_combo.setCurrentIndex(0)
        self.season_combo.blockSignals(False)
        self._render_table("normal")

    def _render_table(self, key: str) -> None:
        self.table.setRowCount(0)
        for start, end, name, price in self._rows[key]:
            self._append_row(start, end, name, price)
        self.hint.setText(
            "采暖期峰谷时段（必须从 00:00 连续覆盖到 24:00）" if key == "heating"
            else "非采暖期峰谷时段（必须从 00:00 连续覆盖到 24:00）"
        )

    def _append_row(self, start: str, end: str, name: str, price: float) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, text in enumerate((start, end, name, f"{price:.{PRICE_DECIMALS}f}")):
            item = QtWidgets.QTableWidgetItem(str(text))
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, col, item)

    def _capture_table(self) -> list[tuple[str, str, str, float]]:
        rows: list[tuple[str, str, str, float]] = []
        for index in range(self.table.rowCount()):
            try:
                start = self.table.item(index, 0).text().strip()
                end = self.table.item(index, 1).text().strip()
                name = self.table.item(index, 2).text().strip() or "平"
                price = float(self.table.item(index, 3).text().strip())
            except (AttributeError, ValueError):
                continue  # 保留原值：非法行在保存时统一报错
            rows.append((start, end, name, price))
        return rows

    def _add_row(self) -> None:
        self._append_row("00:00", "24:00", "平", 0.5550)

    def _del_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _load_preset(self) -> None:
        self.kind_combo.setCurrentIndex(1)
        if self.season_check.isChecked():
            plan = seasonal_preset()
            self._rows["normal"] = [_band_tuple(band) for band in plan.bands]
            self._rows["heating"] = [_band_tuple(band) for band in plan.season.bands]  # type: ignore[union-attr]
        else:
            self._rows["normal"] = [_band_tuple(band) for band in preset_plan().bands]
        self._render_table(self._editing)

    def _on_season_toggled(self, enabled: bool) -> None:
        if enabled and not self._rows["heating"]:
            # Seed the heating table so the user edits values instead of typing bands.
            self._rows["heating"] = [_band_tuple(band) for band in parse_band_spec(CN_TOU_PRESET_HEATING)]
        self._sync_mode()

    def _on_season_editor_changed(self) -> None:
        self._rows[self._editing] = self._capture_table()
        self._editing = "heating" if self.season_combo.currentData() == "heating" else "normal"
        self._render_table(self._editing)

    def _sync_mode(self) -> None:
        tou = self.kind_combo.currentData() == "tou"
        # Switching a fixed plan to TOU used to leave an empty mandatory table behind;
        # seed the example shape once so the user edits numbers instead of inventing rows.
        if tou and not self._rows["normal"] and not self._rows["heating"] and not self._seeded:
            self._rows["normal"] = [_band_tuple(band) for band in parse_band_spec(CN_TOU_PRESET)]
            self._seeded = True
            self._render_table(self._editing)
        # A fixed plan has no bands at all: hide the whole editor instead of showing
        # a disabled, empty table that looks broken.
        self.tou_box.setVisible(tou)
        self.fixed_hint.setVisible(not tou)
        self.price_spin.setEnabled(not tou)
        seasonal = tou and self.season_check.isChecked()
        for widget in (self.season_check,):
            widget.setEnabled(tou)
        for widget in (self.season_start_edit, self.season_end_edit, self.season_combo):
            widget.setEnabled(seasonal)
        if tou:
            self.hint.setText(
                ("采暖期" if self._editing == "heating" else "非采暖期")
                + "峰谷时段（必须从 00:00 连续覆盖到 24:00）"
            )
            self.resize(620, 640)
        else:
            self.resize(620, 300)

    def _collect_bands(self, key: str) -> tuple[Band, ...]:
        source = self._capture_table() if key == self._editing else self._rows[key]
        bands: list[Band] = []
        for index, (start, end, name, price) in enumerate(source):
            try:
                bands.append(Band(parse_hm(start), parse_hm(end), name, price))
            except ValueError as exc:
                raise ValueError(f"第 {index + 1} 行格式不正确: {exc}") from exc
        return tuple(bands)

    def _build_season(self) -> SeasonWindow | None:
        if not self.season_check.isChecked():
            return None
        start = parse_monthday(self.season_start_edit.text() or "11-01")
        end = parse_monthday(self.season_end_edit.text() or "03-31")
        return SeasonWindow(label="采暖期", start=start, end=end, bands=validate_bands(self._collect_bands("heating")))

    def _save(self) -> None:
        kind = self.kind_combo.currentData()
        try:
            if kind == "tou":
                season = self._build_season()
                plan = TariffPlan(
                    name=self.name_edit.text().strip() or "峰谷方案",
                    kind="tou",
                    bands=validate_bands(self._collect_bands("normal")),
                    season=season,
                    effective_from=date.today().isoformat(),
                )
            else:
                plan = TariffPlan(name=self.name_edit.text().strip() or "固定电价", kind="fixed",
                                  price=self.price_spin.value(), effective_from=date.today().isoformat())
            self.store.set_plan(plan)
            if self.recalc_check.isChecked():
                self.store.recalc_costs()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


def _band_tuple(band: Band) -> tuple[str, str, str, float]:
    start = f"{band.start_min // 60:02d}:{band.start_min % 60:02d}"
    end = f"{band.end_min // 60:02d}:{band.end_min % 60:02d}"
    return start, end, band.name, band.price


def parse_hm(text: str) -> int:
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour == 24 and minute == 0:
        return 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间越界：{text!r}")
    return hour * 60 + minute


# --------------------------------------------------------------------------- energy tab
class EnergyTab(QtWidgets.QWidget):
    def __init__(self, db_path: str | Path, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.db_path = Path(db_path)
        self._store: EnergyStore | None = None
        self._rated_w = 1300.0
        self._state = "未启动"
        self._cal_year = date.today().year
        self._cal_month = date.today().month
        self._range = "today"
        self._selected_day: date | None = None
        self._month_values: dict[int, float] = {}
        self._build()
        self._open()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)
        self.refresh()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        kpis = QtWidgets.QHBoxLayout()
        kpis.setSpacing(8)
        self.kpi_today = KpiCard("今日用电")
        self.kpi_month = KpiCard("本月用电")
        self.kpi_total = KpiCard("累计用电")
        self.kpi_runtime = KpiCard("今日运行")
        for card in (self.kpi_today, self.kpi_month, self.kpi_total, self.kpi_runtime):
            kpis.addWidget(card, 1)
        root.addLayout(kpis)

        middle = QtWidgets.QHBoxLayout()
        middle.setSpacing(8)
        chart_card = Card()
        chart_layout = QtWidgets.QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(12, 8, 10, 8)
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("用电曲线")
        title.setObjectName("cardTitle")
        head.addWidget(title)
        self.range_combo = QtWidgets.QComboBox()
        self.range_combo.addItem("今日（每小时平均功率）", "today")
        self.range_combo.addItem("今日（每 5 分钟）", "fine")
        self.range_combo.addItem("近 7 天（每日电量）", "week")
        self.range_combo.addItem("近 30 天（每日电量）", "month")
        self.range_combo.addItem("选中日（点击日历）", "day")
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        head.addWidget(self.range_combo)
        head.addStretch(1)
        chart_layout.addLayout(head)
        self.chart = PowerChart()
        chart_layout.addWidget(self.chart, 1)
        middle.addWidget(chart_card, 3)

        donut_card = Card()
        donut_layout = QtWidgets.QVBoxLayout(donut_card)
        donut_layout.setContentsMargins(12, 8, 10, 8)
        donut_title = QtWidgets.QLabel("本月分时段用电")
        donut_title.setObjectName("cardTitle")
        donut_layout.addWidget(donut_title)
        self.donut = SplitDonut()
        donut_layout.addWidget(self.donut, 1)
        self.band_note = QtWidgets.QLabel("")
        self.band_note.setObjectName("hint")
        donut_layout.addWidget(self.band_note)
        middle.addWidget(donut_card, 2)
        root.addLayout(middle, 3)

        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(8)

        cal_card = Card()
        cal_layout = QtWidgets.QVBoxLayout(cal_card)
        cal_layout.setContentsMargins(12, 8, 10, 8)
        cal_head = QtWidgets.QHBoxLayout()
        cal_title = QtWidgets.QLabel("用电日历")
        cal_title.setObjectName("cardTitle")
        cal_head.addWidget(cal_title)
        cal_head.addStretch(1)
        self.cal_month_label = QtWidgets.QLabel("")
        self.cal_month_label.setObjectName("cardTitle")
        cal_head.addWidget(self.cal_month_label)
        cal_head.addSpacing(10)
        self.cal_prev = QtWidgets.QPushButton("◀")
        self.cal_next = QtWidgets.QPushButton("▶")
        self.cal_today = QtWidgets.QPushButton("今天")
        self.cal_prev.setToolTip("上一个月")
        self.cal_next.setToolTip("下一个月")
        self.cal_today.setToolTip("回到本月并取消选中日期")
        for btn in (self.cal_prev, self.cal_next, self.cal_today):
            btn.setObjectName("navBtn")
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cal_prev.setFixedWidth(34)
        self.cal_next.setFixedWidth(34)
        self.cal_prev.clicked.connect(lambda: self._shift_month(-1))
        self.cal_next.clicked.connect(lambda: self._shift_month(1))
        self.cal_today.clicked.connect(self._goto_current_month)
        cal_head.addWidget(self.cal_prev)
        cal_head.addWidget(self.cal_next)
        cal_head.addWidget(self.cal_today)
        cal_layout.addLayout(cal_head)
        self.calendar = CalendarHeat()
        self.calendar.dayClicked.connect(self._on_day_clicked)
        cal_layout.addWidget(self.calendar, 1)
        self.cal_note = QtWidgets.QLabel("")
        self.cal_note.setObjectName("hint")
        cal_layout.addWidget(self.cal_note)
        bottom.addWidget(cal_card, 3)

        load_card = Card()
        load_layout = QtWidgets.QVBoxLayout(load_card)
        load_layout.setContentsMargins(12, 8, 10, 8)
        load_title = QtWidgets.QLabel("负载率分布（本月）")
        load_title.setObjectName("cardTitle")
        load_layout.addWidget(load_title)
        self.load_bars = LoadBars()
        load_layout.addWidget(self.load_bars, 1)
        bottom.addWidget(load_card, 2)

        sess_card = Card()
        sess_layout = QtWidgets.QVBoxLayout(sess_card)
        sess_layout.setContentsMargins(12, 8, 10, 8)
        sess_title = QtWidgets.QLabel("最近供电会话")
        sess_title.setObjectName("cardTitle")
        sess_layout.addWidget(sess_title)
        self.sessions = QtWidgets.QTableWidget(0, 4)
        self.sessions.setHorizontalHeaderLabels(["时间", "时长", "用电", "费用"])
        header = self.sessions.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.sessions.verticalHeader().setVisible(False)
        self.sessions.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sessions.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        sess_layout.addWidget(self.sessions, 1)
        bottom.addWidget(sess_card, 2)
        root.addLayout(bottom, 3)

        footer = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("hint")
        footer.addWidget(self.status_label, 1)
        self.export_btn = QtWidgets.QToolButton()
        self.export_btn.setText("导出 CSV")
        self.export_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.export_btn)
        for label, kind in (("分钟明细", "minute"), ("每日汇总", "daily"), ("原始样本", "samples"),
                            ("供电会话", "sessions")):
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, k=kind: self._export(k))
        self.export_btn.setMenu(menu)
        footer.addWidget(self.export_btn)
        self.tariff_btn = QtWidgets.QPushButton("电价设置")
        self.tariff_btn.clicked.connect(self._open_tariff)
        footer.addWidget(self.tariff_btn)
        root.addLayout(footer)

    # -- store -------------------------------------------------------------
    def _open(self) -> None:
        try:
            self._store = EnergyStore(self.db_path, create=False)
        except Exception:
            self._store = None

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """Stop the refresh timer and release sqlite before the widget dies."""
        self._timer.stop()
        if self._store is not None:
            self._store.close()
            self._store = None
        super().closeEvent(event)

    def set_info(self, info: Any) -> None:
        rated = getattr(info, "rated_power", 0) if info is not None else 0
        if rated:
            self._rated_w = float(rated)

    def set_collector_state(self, text: str) -> None:
        self._state = text
        self.refresh()

    # -- refresh -----------------------------------------------------------
    def refresh(self) -> None:
        if self._store is None:
            self._open()
        if self._store is None:
            self.status_label.setText("尚未采集：运行 `himos-psu collect` 或保持 GUI 打开以开始记录")
            return
        try:
            self._refresh_kpis()
            self._refresh_chart()
            self._refresh_donut()
            self._refresh_calendar()
            self._refresh_load()
            self._refresh_sessions()
            self._refresh_status()
        except Exception as exc:  # keep the timer alive; surface the reason
            self.status_label.setText(f"读取能耗数据失败：{exc}")

    def _today_bounds(self) -> tuple[int, int]:
        today = date.today()
        return local_day_start(today), min(local_day_end(today), int(time.time()))

    def _refresh_kpis(self) -> None:
        assert self._store is not None
        store = self._store
        start, end = self._today_bounds()
        today = store.report("今日", start, end)

        month_start = local_day_start(date.today().replace(day=1))
        month = store.report("本月", month_start, end)

        first = store.first_ts()
        total = store.report("累计", local_day_start(datetime.fromtimestamp(first).date()) if first else start, end)

        self.kpi_today.set(format_kwh(today.kwh), "kWh",
                           f"费用 {format_yuan(today.cost_cents)} · 平均 {today.avg_w:.0f} W")
        days_in_month = calendar.monthrange(date.today().year, date.today().month)[1]
        day_of_month = max(1, date.today().day)
        projected = month.cost_yuan / day_of_month * days_in_month if month.kwh > 0 else 0.0
        self.kpi_month.set(format_kwh(month.kwh), "kWh",
                           f"已用 {format_yuan(month.cost_cents)} · 预估月账单 ¥{projected:.2f}", ACCENT2)
        since = datetime.fromtimestamp(first).strftime("%Y-%m-%d") if first else "尚未开始"
        recorded = store.recorded_days()
        # "自 X 起" alone reads like continuous coverage; say how many days really have data.
        self.kpi_total.set(format_kwh(total.kwh), "kWh",
                           f"自 {since} 起 · 已记录 {recorded} 天", PURPLE)
        self.kpi_total.setToolTip(
            f"累计区间从首个有数据的分钟（{since}）到今天；其间实际有数据的是 {recorded} 天，"
            "没开采集的日子不计入。"
        )
        peak_ratio = (today.peak_w / self._rated_w * 100.0) if self._rated_w else 0.0
        active = store.active_seconds(start, end)
        self.kpi_runtime.set(format_duration(active), "",
                             f"峰值 {today.peak_w:.0f} W · 峰值负载 {peak_ratio:.1f}%", GREEN)
        self.kpi_runtime.setToolTip("统计功率高于 20 W 的时长，区别于单纯的采集覆盖时长")

    def _refresh_chart(self) -> None:
        assert self._store is not None
        store = self._store
        today = date.today()
        if self._range in ("today", "fine", "day"):
            if self._range == "day":
                day = self._selected_day or today
                step, start = 3600, local_day_start(day)
                end = min(local_day_end(day), int(time.time()))
                caption = f"{day:%Y-%m-%d} 每小时平均功率，缺口留空"
                peak_prefix = "小时均值峰值"
            else:
                step = 300 if self._range == "fine" else 3600
                start, end = self._today_bounds()
                caption = ("5 分钟平均功率，缺口留空" if self._range == "fine"
                           else "每小时平均功率，缺口留空")
                peak_prefix = "5 分钟峰值" if self._range == "fine" else "小时均值峰值"
            series = store.power_series(start, end, step)
            values: list[float | None] = []
            labels: list[str] = []
            details: list[str] = []
            for item in series:
                when = datetime.fromtimestamp(item["ts"])
                values.append(item["avg"])
                if self._range == "fine":
                    # Only every third hour gets a label; the rest stay blank so the
                    # axis does not turn into a grey band.
                    labels.append(f"{when:%H:%M}" if when.minute == 0 and when.hour % 3 == 0 else "")
                else:
                    labels.append(f"{when:%H}")
                if item["avg"] is None:
                    details.append(f"{when:%H:%M} · 无数据")
                else:
                    details.append(
                        f"{when:%H:%M} · 平均 {item['avg']:.0f} W"
                        f" · 最高 {item['max']:.0f} W · 最低 {item['min']:.0f} W"
                    )
            self.chart.set_data(values, labels, "W", caption,
                                peak_prefix=peak_prefix, details=details)
        else:
            days = 6 if self._range == "week" else 29
            series = store.daily_series(today - timedelta(days=days), today)
            values = [item["kwh"] for item in series]
            labels = [item["date"].strftime("%m-%d") for item in series]
            details = [f"{item['date']:%Y-%m-%d} · {item['kwh']:.3f} kWh" for item in series]
            self.chart.set_data(values, labels, "kWh", "每日电量，未采集的日期为 0",
                                peak_prefix="单日峰值", details=details)

    def _refresh_donut(self) -> None:
        assert self._store is not None
        store = self._store
        start = local_day_start(date.today().replace(day=1))
        end = min(local_day_end(date.today()), int(time.time()))
        bands = store.band_breakdown(start, end)
        month = store.report("本月", start, end)
        slices = ([{"name": b["name"], "kwh": b["kwh"], "share": b["share"]} for b in bands]
                  if month.kwh >= 0.0005 else [])
        self.donut.set_data(slices, f"{month.kwh:.2f}", "kWh", format_yuan(month.cost_cents))
        plan = store.active_plan()
        if plan and plan.is_tou:
            prices = " · ".join(f"{b.name} {b.price:.{PRICE_DECIMALS}f}" for b in plan.bands)
            if plan.season is not None:
                winter = " · ".join(
                    f"{b.name} {b.price:.{PRICE_DECIMALS}f}" for b in plan.season.bands
                )
                self.band_note.setText(
                    f"当前电价（元/kWh）非采暖期：{prices}；{plan.season.label}：{winter}"
                )
            else:
                self.band_note.setText(f"当前电价（元/kWh）：{prices}")
        elif plan:
            self.band_note.setText(f"当前电价：统一 {plan.price:.{PRICE_DECIMALS}f} 元/kWh")

    def _calendar_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Navigable range: a year back (or the first recorded month) .. this month."""
        today = date.today()
        current = (today.year, today.month)
        year, month = current
        for _ in range(12):  # always allow looking a year back, even before data
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        floor = (year, month)
        first_ts = self._store.first_ts() if self._store is not None else None
        if not first_ts:
            return floor, current
        first_day = datetime.fromtimestamp(first_ts).date()
        return min((first_day.year, first_day.month), floor), current

    def _refresh_calendar(self) -> None:
        assert self._store is not None
        values = self._store.calendar(self._cal_year, self._cal_month)
        self.calendar.set_month(self._cal_year, self._cal_month, values)
        self.cal_month_label.setText(f"{self._cal_year} 年 {self._cal_month} 月")
        self._month_values = values
        self._refresh_calendar_note()

    def _refresh_calendar_note(self) -> None:
        """Month summary, or the clicked day's numbers."""
        assert self._store is not None
        day = self._selected_day
        if day is not None and (day.year, day.month) == (self._cal_year, self._cal_month):
            start = local_day_start(day)
            end = min(local_day_end(day), int(time.time()))
            report = self._store.report("day", start, end)
            peak = f" · 峰值 {report.peak_w:.0f} W" if report.peak_w else ""
            self.cal_note.setText(
                f"{day:%Y-%m-%d} · {format_kwh(report.kwh)} kWh · {format_yuan(report.cost_cents)}{peak}"
                " · 再点一次取消"
            )
            return
        month_total = sum(self._month_values.values())
        self.cal_note.setText(f"{self._cal_year}-{self._cal_month:02d} 已记录 {len(self._month_values)} 天 · "
                              f"合计 {format_kwh(month_total)} kWh · 点某一天看当日曲线")
        earliest, latest = self._calendar_bounds()
        here = (self._cal_year, self._cal_month)
        self.cal_prev.setEnabled(here > earliest)
        self.cal_next.setEnabled(here < latest)
        # Always clickable: on the current month it clears a selected day.
        self.cal_today.setEnabled(True)

    def _refresh_load(self) -> None:
        assert self._store is not None
        start = local_day_start(date.today().replace(day=1))
        end = min(local_day_end(date.today()), int(time.time()))
        rows = self._store.load_distribution(start, end, self._rated_w)
        self.load_bars.set_data(rows, f"额定 {self._rated_w:.0f} W · 按每分钟平均功率分档")

    def _refresh_sessions(self) -> None:
        assert self._store is not None
        sessions = self._store.recent_sessions(6)
        self.sessions.setRowCount(len(sessions))
        for row, item in enumerate(sessions):
            start = datetime.fromtimestamp(item["start_ts"])
            end = datetime.fromtimestamp(item["end_ts"])
            span = f"{start:%m-%d %H:%M}"
            report = self._store.report("会话", item["start_ts"], item["end_ts"] + 1)
            values = [span, format_duration(item["duration_s"]), f"{item['kwh']:.2f} kWh",
                      format_yuan(report.cost_cents)]
            for col, value in enumerate(values):
                cell = QtWidgets.QTableWidgetItem(value)
                cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if col == 0:
                    cell.setToolTip(f"{start:%Y-%m-%d %H:%M} → {end:%H:%M}")
                self.sessions.setItem(row, col, cell)

    def _refresh_status(self) -> None:
        assert self._store is not None
        store = self._store
        last = store.last_sample_ts()
        when = datetime.fromtimestamp(last).strftime("%H:%M:%S") if last else "--"
        size_mb = store.db_size() / 1024 / 1024
        self.status_label.setText(f"采集：{self._state} · 最后样本 {when} · "
                                  f"样本 {store.sample_count():,} 条 · 数据库 {size_mb:.1f} MB")

    # -- actions -----------------------------------------------------------
    def _on_range_changed(self, _index: int) -> None:
        self._range = self.range_combo.currentData()
        if self._range != "day" and self._selected_day is not None:
            self._selected_day = None
            self.calendar.set_selected(None)
            self._refresh_calendar_note()
        self._refresh_chart()

    def _select_range(self, key: str) -> None:
        """Switch the chart range without re-entering _on_range_changed."""
        index = self.range_combo.findData(key)
        self._range = key
        if index >= 0 and index != self.range_combo.currentIndex():
            self.range_combo.blockSignals(True)
            self.range_combo.setCurrentIndex(index)
            self.range_combo.blockSignals(False)
        self._refresh_chart()

    @QtCore.Slot(object)
    def _on_day_clicked(self, day: date) -> None:
        """Clicking a calendar day shows that day's hourly curve; again cancels."""
        if self._selected_day == day:
            self._selected_day = None
            self.calendar.set_selected(None)
            self._select_range("today")
        else:
            self._selected_day = day
            self.calendar.set_selected(day)
            self._select_range("day")
        self._refresh_calendar_note()

    def _shift_month(self, delta: int) -> None:
        self._clear_day_selection()
        month = self._cal_month + delta
        year = self._cal_year
        if month < 1:
            year, month = year - 1, 12
        elif month > 12:
            year, month = year + 1, 1
        earliest, latest = self._calendar_bounds()
        self._cal_year, self._cal_month = min(max((year, month), earliest), latest)
        self._refresh_calendar()

    def _goto_current_month(self) -> None:
        self._clear_day_selection()
        today = date.today()
        self._cal_year, self._cal_month = today.year, today.month
        self._refresh_calendar()

    def _clear_day_selection(self) -> None:
        if self._selected_day is not None:
            self._selected_day = None
            self.calendar.set_selected(None)
            self._select_range("today")

    def _export(self, kind: str) -> None:
        default = f"himos-energy-{kind}-{date.today():%Y%m%d}.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 CSV", str(Path.home() / default),
                                                        "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            assert self._store is not None
            rows = self._store.export_csv(kind, path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.status_label.setText(f"已导出 {rows} 行 → {path}")

    def _open_tariff(self) -> None:
        try:
            store = EnergyStore(self.db_path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "无法打开数据库", str(exc))
            return
        try:
            dialog = TariffDialog(store, self)
            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                if self._store is not None:
                    self._store.invalidate_plan()
                self.refresh()
        finally:
            store.close()


# --------------------------------------------------------------------------- settings tab
class SettingsTab(QtWidgets.QWidget):
    auto_restore_changed = QtCore.Signal(bool)

    def __init__(
        self,
        db_path: str | Path,
        parent: QtWidgets.QWidget | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.db_path = Path(db_path)
        self.config_path = Path(config_path) if config_path is not None else default_config_path()
        self._state = "未启动"
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        restore_box = QtWidgets.QGroupBox("风扇配置持久化与启动恢复")
        restore_layout = QtWidgets.QVBoxLayout(restore_box)
        self.auto_restore_check = QtWidgets.QCheckBox("连接电源时自动恢复上次保存的风扇模式与自定义曲线")
        self.auto_restore_check.toggled.connect(self._on_auto_restore_toggled)
        restore_layout.addWidget(self.auto_restore_check)
        self.restore_summary_label = QtWidgets.QLabel("--")
        self.restore_summary_label.setWordWrap(True)
        self.restore_summary_label.setObjectName("cardUnit")
        restore_layout.addWidget(self.restore_summary_label)
        root.addWidget(restore_box)

        plan_box = QtWidgets.QGroupBox("电价方案")
        plan_layout = QtWidgets.QVBoxLayout(plan_box)
        self.plan_label = QtWidgets.QLabel("--")
        self.plan_label.setWordWrap(True)
        plan_layout.addWidget(self.plan_label)
        plan_buttons = QtWidgets.QHBoxLayout()
        self.edit_plan_btn = QtWidgets.QPushButton("编辑电价方案")
        self.recalc_btn = QtWidgets.QPushButton("重算历史费用")
        self.edit_plan_btn.clicked.connect(self._edit_plan)
        self.recalc_btn.clicked.connect(self._recalc)
        plan_buttons.addWidget(self.edit_plan_btn)
        plan_buttons.addWidget(self.recalc_btn)
        plan_buttons.addStretch(1)
        plan_layout.addLayout(plan_buttons)
        root.addWidget(plan_box)

        carbon_box = QtWidgets.QGroupBox("碳排估算（可选）")
        carbon_layout = QtWidgets.QHBoxLayout(carbon_box)
        carbon_layout.addWidget(QtWidgets.QLabel("排放因子"))
        self.carbon_spin = QtWidgets.QDoubleSpinBox()
        self.carbon_spin.setDecimals(3)
        self.carbon_spin.setRange(0.0, 2.0)
        self.carbon_spin.setSingleStep(0.01)
        self.carbon_spin.setSuffix(" kgCO₂/kWh")
        self.carbon_spin.setToolTip("0 表示不显示碳排；中国区域电网平均值常被引用为 0.57 左右，请自行核实")
        carbon_layout.addWidget(self.carbon_spin)
        self.carbon_save = QtWidgets.QPushButton("保存")
        self.carbon_save.clicked.connect(self._save_carbon)
        carbon_layout.addWidget(self.carbon_save)
        carbon_layout.addStretch(1)
        root.addWidget(carbon_box)

        data_box = QtWidgets.QGroupBox("数据管理")
        data_layout = QtWidgets.QVBoxLayout(data_box)
        self.data_label = QtWidgets.QLabel("--")
        self.data_label.setWordWrap(True)
        data_layout.addWidget(self.data_label)
        data_buttons = QtWidgets.QHBoxLayout()
        self.open_dir_btn = QtWidgets.QPushButton("打开数据目录")
        self.prune_btn = QtWidgets.QPushButton("清理 14 天前原始样本")
        self.export_all_btn = QtWidgets.QPushButton("导出每日汇总 CSV")
        self.open_dir_btn.clicked.connect(self._open_dir)
        self.prune_btn.clicked.connect(self._prune)
        self.export_all_btn.clicked.connect(self._export_all)
        for btn in (self.open_dir_btn, self.prune_btn, self.export_all_btn):
            data_buttons.addWidget(btn)
        data_buttons.addStretch(1)
        data_layout.addLayout(data_buttons)
        root.addWidget(data_box)

        note = QtWidgets.QLabel(
            "口径说明：统计的是电源输入端功率（不含显示器与外设）；固件不提供输出电流，因此本工具不计算转换效率。"
            "采样由 PSU 自身的推送间隔决定（1–3 s），断档按缺口记录、不按 0 W 补算。"
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        root.addWidget(note)

        self.state_label = QtWidgets.QLabel("")
        self.state_label.setObjectName("hint")
        root.addWidget(self.state_label)
        root.addStretch(1)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(10000)
        self.refresh()

    def set_collector_state(self, text: str) -> None:
        self._state = text
        self.refresh()

    def refresh_restore_summary(self) -> None:
        cfg = load_config(self.config_path, strict=False)
        enabled = bool(cfg.get("auto_restore", True))
        self.auto_restore_check.blockSignals(True)
        self.auto_restore_check.setChecked(enabled)
        self.auto_restore_check.blockSignals(False)
        try:
            profile = parse_saved_profile(cfg)
        except ValueError as exc:
            self.restore_summary_label.setText(f"已保存配置校验异常（已禁止自动下发）：{exc}")
            return
        parts: list[str] = []
        if profile.fan_mode is not None:
            parts.append(f"模式={profile.fan_mode.label}")
            if profile.fan_mode == FanMode.CUSTOM and profile.custom_curve is not None:
                parts.append(f"曲线={[list(pt) for pt in profile.custom_curve]}")
        if profile.refresh_mode is not None:
            parts.append(f"刷新={profile.refresh_mode.interval_ms} ms")
        if parts:
            self.restore_summary_label.setText(
                f"已保存配置：{' · '.join(parts)}\n"
                "电源断电/重启后固件会自动复位为 Auto；开启此项后，每次启动软件或开机服务都会在确认遥测正常后自动恢复。"
            )
        else:
            self.restore_summary_label.setText(
                "已保存配置：尚未记录风扇模式（切换风扇模式或点击「应用自定义曲线」后将自动保存；"
                "一次性清灰 Clean 不会被记录）。"
            )

    def _on_auto_restore_toggled(self, checked: bool) -> None:
        try:
            save_config({"auto_restore": bool(checked)}, path=self.config_path)
        except OSError as exc:
            self.state_label.setText(f"保存设置失败：{exc}")
        self.auto_restore_changed.emit(bool(checked))
        self.refresh_restore_summary()

    def refresh(self) -> None:
        self.refresh_restore_summary()
        store = None
        try:
            store = EnergyStore(self.db_path)
        except Exception as exc:
            self.plan_label.setText(f"无法打开数据库：{exc}")
            return
        try:
            plan = store.active_plan()
            if plan is None:
                self.plan_label.setText("尚未设置")
            else:
                self.plan_label.setWordWrap(True)
                # Rich text: escape the plan text, then join the seasons with <br>.
                body = "<br>".join(escape(line) for line in plan.describe_lines())
                self.plan_label.setText(f"{escape(plan.name)}<br>{body}")
            self.carbon_spin.setValue(store.carbon_factor)
            first = store.first_ts()
            size_mb = store.db_size() / 1024 / 1024
            self.data_label.setText(
                f"数据库：{store.path}\n大小 {size_mb:.1f} MB · 原始样本 {store.sample_count():,} 条"
                f" · 首采 {datetime.fromtimestamp(first).strftime('%Y-%m-%d %H:%M') if first else '尚未开始'}"
            )
            self.state_label.setText(f"采集状态：{self._state}")
        finally:
            store.close()

    def _edit_plan(self) -> None:
        store = EnergyStore(self.db_path)
        try:
            dialog = TariffDialog(store, self)
            dialog.exec()
        finally:
            store.close()
        self.refresh()

    def _recalc(self) -> None:
        store = EnergyStore(self.db_path)
        try:
            rows = store.recalc_costs()
        finally:
            store.close()
        QtWidgets.QMessageBox.information(self, "重算完成", f"已按当前电价重算 {rows} 个分钟桶的费用。")
        self.refresh()

    def _save_carbon(self) -> None:
        store = EnergyStore(self.db_path)
        try:
            store.set_carbon_factor(self.carbon_spin.value())
        finally:
            store.close()
        self.state_label.setText("碳排因子已保存。")

    def _open_dir(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.db_path.parent)))

    def _prune(self) -> None:
        store = EnergyStore(self.db_path)
        try:
            removed = store.prune_raw(14)
        finally:
            store.close()
        QtWidgets.QMessageBox.information(self, "清理完成", f"已删除 {removed} 条原始样本，分钟与日汇总保留。")
        self.refresh()

    def _export_all(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出每日汇总", str(Path.home() / "himos-energy-daily.csv"),
                                                        "CSV 文件 (*.csv)")
        if not path:
            return
        store = EnergyStore(self.db_path, create=False)
        try:
            rows = store.export_csv("daily", path)
        finally:
            store.close()
        QtWidgets.QMessageBox.information(self, "导出完成", f"已导出 {rows} 行 → {path}")
