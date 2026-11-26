"""Native PySide6 desktop monitor and fan controller for Segotep / Kunlun PSUs."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from .client import PowerSupplyClient
from .collector import Collector, CollectorBusy
from .config import (
    CONFIG_PATH,
    PERSISTABLE_FAN_MODES,
    SavedPsuProfile,
    apply_profile_to_client,
    load_config,
    parse_saved_profile,
    save_config,
)
from .energy import DEFAULT_DB, EnergyStore
from .fan_curve import (
    DEFAULT_CUSTOM_POINTS,
    Point,
    _js_round,
    clamp_dragged_point,
    resample_fan_curve,
    validate_fan_curve,
)
from . import theme
from .gui_energy import EnergyTab, SettingsTab
from .ipc import DaemonUnavailable, IpcError, SocketClient, default_socket_path, probe_socket
from .protocol import (
    FanMode,
    PowerInfo,
    PowerSerial,
    PowerStatus,
    RefreshMode,
    SpecsMerger,
    TempSensor,
    format_warranty_badge,
    parse_command_content,
    parse_frame,
)


def _to_display_temp(celsius: float, fahrenheit: bool) -> float:
    return (1.8 * celsius + 32.0) if fahrenheit else celsius


class FanCurveWidget(QtWidgets.QWidget):
    """Interactive 5-point ControlChart widget enforcing HiMOS drag constraints."""

    points_changed = QtCore.Signal(list)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(400, 250)
        self.setMouseTracking(True)
        self._points: list[Point] = list(DEFAULT_CUSTOM_POINTS)
        self._editable: bool = False
        self._fahrenheit: bool = False
        self._dragging_idx: int | None = None
        self._hover_idx: int | None = None
        self._pad_left = 48
        self._pad_right = 20
        self._pad_top = 18
        self._pad_bottom = 34

    def points(self) -> list[Point]:
        return list(self._points)

    def set_points(self, points: Sequence[Sequence[int]]) -> None:
        self._points = [(int(p[0]), int(p[1])) for p in points]
        self.update()

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        self._dragging_idx = None
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.update()

    def set_fahrenheit(self, fahrenheit: bool) -> None:
        self._fahrenheit = fahrenheit
        self.update()

    def _plot_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(
            self._pad_left,
            self._pad_top,
            max(80.0, self.width() - self._pad_left - self._pad_right),
            max(80.0, self.height() - self._pad_top - self._pad_bottom),
        )

    def _to_pixel(self, pt: Point) -> QtCore.QPointF:
        rect = self._plot_rect()
        px = rect.left() + (pt[0] / 100.0) * rect.width()
        py = rect.bottom() - (pt[1] / 100.0) * rect.height()
        return QtCore.QPointF(px, py)

    def _from_pixel(self, pos: QtCore.QPointF) -> tuple[int, int]:
        rect = self._plot_rect()
        x = ((pos.x() - rect.left()) / rect.width()) * 100.0
        y = ((rect.bottom() - pos.y()) / rect.height()) * 100.0
        return int(x), int(y)

    def _hit_test(self, pos: QtCore.QPointF) -> int | None:
        for idx in (1, 2, 3):
            px = self._to_pixel(self._points[idx])
            if QtCore.QLineF(pos, px).length() <= 13.0:
                return idx
        return None

    def drag_point_to(self, index: int, temp_pct: int, duty_pct: int) -> None:
        """Move control point programmatically or from pointer drag."""
        self._points = clamp_dragged_point(self._points, index, temp_pct, duty_pct)
        self.points_changed.emit([list(p) for p in self._points])
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._editable and event.button() == QtCore.Qt.MouseButton.LeftButton:
            idx = self._hit_test(event.position())
            if idx is not None:
                self._dragging_idx = idx
                self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging_idx is not None and self._editable:
            gx, gy = self._from_pixel(event.position())
            self.drag_point_to(self._dragging_idx, gx, gy)
            event.accept()
            return
        if self._editable:
            hit = self._hit_test(event.position())
            if hit != self._hover_idx:
                self._hover_idx = hit
                self.setCursor(
                    QtCore.Qt.CursorShape.OpenHandCursor if hit is not None else QtCore.Qt.CursorShape.ArrowCursor
                )
                self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging_idx is not None:
            self._dragging_idx = None
            self.setCursor(
                QtCore.Qt.CursorShape.OpenHandCursor
                if (self._editable and self._hit_test(event.position()) is not None)
                else QtCore.Qt.CursorShape.ArrowCursor
            )
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()

        painter.fillRect(self.rect(), QtGui.QColor(theme.P.panel))
        painter.fillRect(rect, QtGui.QColor(theme.P.panel2))

        grid_pen = QtGui.QPen(QtGui.QColor(theme.P.gridline), 1, QtCore.Qt.PenStyle.DashLine)
        text_pen = QtGui.QPen(QtGui.QColor(theme.P.muted))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        for tick in range(0, 101, 20):
            x = rect.left() + (tick / 100.0) * rect.width()
            y = rect.bottom() - (tick / 100.0) * rect.height()
            painter.setPen(grid_pen)
            painter.drawLine(QtCore.QPointF(x, rect.top()), QtCore.QPointF(x, rect.bottom()))
            painter.drawLine(QtCore.QPointF(rect.left(), y), QtCore.QPointF(rect.right(), y))

            painter.setPen(text_pen)
            temp_val = _to_display_temp(float(tick), self._fahrenheit)
            unit = "°F" if self._fahrenheit else "°C"
            x_label = f"{temp_val:.0f}{unit}" if tick in (0, 100) else f"{temp_val:.0f}"
            painter.drawText(
                QtCore.QRectF(x - 24, rect.bottom() + 4, 48, 20),
                QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop,
                x_label,
            )
            y_label = "%" if tick == 0 else str(tick)
            painter.drawText(
                QtCore.QRectF(4, y - 9, self._pad_left - 10, 18),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                y_label,
            )

        # Draw 21 firmware resampled points subtly so users can verify the x9 wire output.
        if self._editable:
            raw = resample_fan_curve(self._points, validate=False)
            step_color = QtGui.QColor(theme.P.accent)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(step_color)
            for idx, duty in enumerate(raw[:21]):
                tx = _js_round(self._points[0][0] + (self._points[-1][0] - self._points[0][0]) * (idx / 20))
                pt_px = self._to_pixel((tx, duty))
                painter.drawEllipse(pt_px, 2.3, 2.3)

        line_color = QtGui.QColor(theme.P.text) if self._editable else QtGui.QColor(theme.P.chart_line_dim)
        line_width = 3.0 if self._editable else 2.0
        painter.setPen(QtGui.QPen(line_color, line_width))
        poly = QtGui.QPolygonF([self._to_pixel(pt) for pt in self._points])
        painter.drawPolyline(poly)

        for idx, pt in enumerate(self._points):
            px = self._to_pixel(pt)
            is_movable = self._editable and idx in (1, 2, 3)
            radius = 6.5 if is_movable else 4.0
            if idx == self._dragging_idx or idx == self._hover_idx:
                radius = 8.0
            fill = QtGui.QColor(theme.P.accent) if is_movable else line_color
            painter.setPen(QtGui.QPen(QtGui.QColor(theme.P.bg), 1.5))
            painter.setBrush(fill)
            painter.drawEllipse(px, radius, radius)

            if self._editable and idx in (1, 2, 3):
                painter.setPen(QtGui.QColor(theme.P.text))
                painter.drawText(
                    QtCore.QRectF(px.x() - 30, px.y() - 24, 60, 16),
                    QtCore.Qt.AlignmentFlag.AlignHCenter,
                    f"{pt[0]}%/{pt[1]}%",
                )


class HistoryChartWidget(QtWidgets.QWidget):
    """Lightweight scrolling chart for real-time input power (W) and selected temperature."""

    def __init__(self, max_points: int = 120, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(130)
        self._powers: deque[float] = deque(maxlen=max_points)
        self._temps: deque[tuple[float, float, float]] = deque(maxlen=max_points)
        self._sensor = TempSensor.LOW_VOLTAGE
        self._fahrenheit = False

    def append_sample(self, status: PowerStatus) -> None:
        self._powers.append(status.power)
        self._temps.append(
            (
                status.high_voltage_temperature,
                status.low_voltage_temperature,
                status.environment_temperature,
            )
        )
        self.update()

    def set_sensor(self, sensor: TempSensor) -> None:
        self._sensor = sensor
        self.update()

    def set_fahrenheit(self, fahrenheit: bool) -> None:
        self._fahrenheit = fahrenheit
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(theme.P.panel))

        rect = self.rect().adjusted(48, 22, -48, -16)
        painter.fillRect(rect, QtGui.QColor(theme.P.panel2))

        if len(self._powers) < 2:
            painter.setPen(QtGui.QColor(theme.P.muted))
            painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, "等待连续遥测数据...")
            return

        sensor_idx = int(self._sensor)
        p_max = max(300.0, max(self._powers) * 1.15)
        t_vals = [_to_display_temp(sample[sensor_idx], self._fahrenheit) for sample in self._temps]
        t_min = 0.0 if not self._fahrenheit else 32.0
        t_max = max(100.0 if not self._fahrenheit else 212.0, max(t_vals) + 10.0)

        def draw_series(vals: Sequence[float], v_min: float, v_max: float, color: str) -> None:
            pts = []
            span = max(1.0, v_max - v_min)
            n = len(vals)
            for idx, v in enumerate(vals):
                x = rect.left() + (idx / max(1, n - 1)) * rect.width()
                norm = max(0.0, min(1.0, (v - v_min) / span))
                y = rect.bottom() - norm * rect.height()
                pts.append(QtCore.QPointF(x, y))
            painter.setPen(QtGui.QPen(QtGui.QColor(color), 2.0))
            painter.drawPolyline(QtGui.QPolygonF(pts))

        draw_series(list(self._powers), 0.0, p_max, theme.P.accent)
        draw_series(t_vals, t_min, t_max, theme.P.band_colors[2])

        unit = "°F" if self._fahrenheit else "°C"
        painter.setPen(QtGui.QColor(theme.P.accent))
        painter.drawText(8, 15, f"功率 W (0~{p_max:.0f}W): {self._powers[-1]:.1f}W")
        painter.setPen(QtGui.QColor(theme.P.band_colors[2]))
        painter.drawText(rect.right() - 150, 15, f"温度 {unit}: {t_vals[-1]:.1f}{unit}")


class RemoteCommander:
    """Fire-and-forget command proxy used when a background daemon owns the PSU.

    It mirrors the subset of :class:`PowerSupplyClient` that the GUI calls, so the
    window code does not need to know who actually holds the serial port.
    """

    def __init__(self, worker: "DaemonWorker") -> None:
        self._worker = worker

    def _send(self, **payload: object) -> bytes:
        self._worker.send(payload)
        return b""

    def set_fan_mode(self, mode: FanMode, custom_points: list | None = None) -> bytes:
        message: dict[str, object] = {"cmd": "set_fan_mode", "mode": int(mode)}
        if custom_points is not None:
            message["points"] = [list(p) for p in custom_points]
        return self._send(**message)

    def set_fan_curve(self, points, switch_to_custom: bool = True) -> bytes:
        return self._send(cmd="set_curve", points=[list(p) for p in points], switch=switch_to_custom)

    def set_refresh_mode(self, refresh: RefreshMode) -> bytes:
        return self._send(cmd="set_refresh", index=int(refresh))

    def clear_fault_code(self) -> bytes:
        return self._send(cmd="clear_fault")

    def close(self) -> None:
        pass


class DaemonWorker(QtCore.QThread):
    """Talks to the background daemon instead of opening the serial port."""

    info_updated = QtCore.Signal(object, str)
    status_updated = QtCore.Signal(object)
    error_occurred = QtCore.Signal(str)
    state_changed = QtCore.Signal(str)

    def __init__(self, socket_path: Path, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.socket_path = Path(socket_path)
        self._sock: SocketClient | None = None
        self._running = True
        self._stop_event = threading.Event()
        self._merger = SpecsMerger()
        self._sn = ""

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        try:
            self._sock = SocketClient(self.socket_path, on_event=self._on_event)
        except DaemonUnavailable as exc:
            self.error_occurred.emit(str(exc))
            self.state_changed.emit("插件：后台守护进程不可用")
            return
        except Exception as exc:
            self.error_occurred.emit(f"连接守护进程失败: {exc}")
            return
        self.state_changed.emit("已连接后台采集守护进程")
        try:
            self._sock.subscribe()
        except IpcError as exc:
            self.error_occurred.emit(str(exc))
        while self._running:
            self._stop_event.wait(0.2)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    def send(self, payload: dict[str, object]) -> None:
        sock = self._sock
        if sock is None:
            raise IpcError("尚未连上后台守护进程")
        sock.send(payload)

    # -- events (reader thread) -------------------------------------------
    def _on_event(self, message: dict) -> None:
        kind = message.get("event")
        if kind == "state":
            self.state_changed.emit(str(message.get("text", "")))
            return
        if kind == "error":
            self.error_occurred.emit(str(message.get("message", "守护进程错误")))
            return
        if kind == "closed":
            if self._running:
                self.error_occurred.emit("与后台守护进程的连接已断开")
            return
        if kind != "frame":
            return
        raw_hex = message.get("raw")
        if not isinstance(raw_hex, str):
            return
        try:
            raw = parse_frame(bytes.fromhex(raw_hex))
            parsed = parse_command_content(raw.cmd_id, raw.payload)
        except Exception:
            return
        if isinstance(parsed, PowerStatus):
            self.status_updated.emit(parsed)
        elif isinstance(parsed, PowerInfo):
            merged = self._merger.update(parsed)
            self.info_updated.emit(merged, self._sn)
        elif isinstance(parsed, PowerSerial):
            self._sn = parsed.sn
            self.info_updated.emit(self._merger.current, self._sn)


class TelemetryWorker(QtCore.QThread):
    """Background collector thread keeping the UI responsive.

    Runs the same :class:`Collector` as ``himos-psu collect``: it takes the
    single-collector lock, opens the PSU, integrates energy into SQLite and
    forwards telemetry to the widgets.
    """

    info_updated = QtCore.Signal(object, str)
    status_updated = QtCore.Signal(object)
    error_occurred = QtCore.Signal(str)
    state_changed = QtCore.Signal(str)
    restoring_mode = QtCore.Signal(object)
    profile_restored = QtCore.Signal(str)

    def __init__(
        self,
        client: PowerSupplyClient,
        db_path: Path,
        parent: QtCore.QObject | None = None,
        *,
        restore_profile: SavedPsuProfile | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.db_path = Path(db_path)
        self.restore_profile = restore_profile
        self._running = True

    def _restore_on_connect(self, client: PowerSupplyClient, initial_status: PowerStatus) -> None:
        profile = self.restore_profile
        if profile is None or not profile.auto_restore or not profile.has_device_settings:
            return
        if initial_status.malfunction_code != 0:
            self.profile_restored.emit(
                f"检测到电源故障码 0x{initial_status.malfunction_code:04x}，已跳过启动自动恢复"
            )
            return
        if profile.fan_mode is not None and initial_status.mode != profile.fan_mode:
            self.restoring_mode.emit(profile.fan_mode)
        try:
            res = apply_profile_to_client(
                client,
                profile,
                initial_status=initial_status,
                if_needed=True,
                confirm_timeout=0.0,
            )
        except Exception as exc:
            self.profile_restored.emit(f"启动自动恢复配置失败: {exc}")
            return
        if res.applied:
            parts: list[str] = []
            if res.sent_mode and profile.fan_mode is not None:
                parts.append(f"风扇模式 {profile.fan_mode.label}")
            if res.sent_curve:
                parts.append("自定义曲线")
            if res.sent_refresh and profile.refresh_mode is not None:
                parts.append(f"刷新 {profile.refresh_mode.interval_ms} ms")
            if parts:
                self.profile_restored.emit("已自动恢复上次保存的配置: " + " + ".join(parts))

    def run(self) -> None:
        store: EnergyStore | None = None
        try:
            store = EnergyStore(self.db_path)
            collector = Collector(
                self.client,
                store,
                on_state=self.state_changed.emit,
                on_specs=lambda info, sn: self.info_updated.emit(info, sn),
                on_first_status=self._restore_on_connect,
                on_status=self.status_updated.emit,
            )
            collector.run(should_stop=lambda: not self._running)
        except CollectorBusy as exc:
            self.error_occurred.emit(str(exc))
            self.state_changed.emit("串口被其他采集进程占用（历史数据仍可查看）")
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.state_changed.emit("采集已中断")
        finally:
            if store is not None:
                store.close()

    def stop(self) -> None:
        self._running = False


class MainWindow(QtWidgets.QMainWindow):
    """Main desktop dashboard for HiMOS / Kunlun KE-1300P power supplies."""

    def __init__(
        self,
        port: str | None = None,
        mock: bool = False,
        client: PowerSupplyClient | None = None,
        config_path: Path | None = None,
        db_path: str | Path | None = None,
        start_collector: bool = True,
    ) -> None:
        super().__init__()
        self._port = port
        self._mock = mock
        self._config_path = config_path or CONFIG_PATH
        self._db_path = Path(db_path or os.environ.get("HIMOS_PSU_DB") or DEFAULT_DB)
        self._start_collector = start_collector
        try:  # make sure the energy tab has a database even before collection starts
            EnergyStore(self._db_path).close()
        except Exception:
            pass
        self._socket_path = default_socket_path()
        self._remote = False
        # When a daemon is already running there is no point opening the serial port
        # here: it would fail with TIOCEXCL anyway. Defer client creation in that case.
        remote_preferred = client is None and not mock and probe_socket(self._socket_path)
        self.client = client
        if self.client is None and not remote_preferred:
            self.client = PowerSupplyClient(port=port, mock=mock)
        self._commander = self.client
        self._worker: QtCore.QThread | None = None
        self._cfg = load_config(self._config_path)
        self._fahrenheit = bool(self._cfg.get("fahrenheit", False))
        self._sensor = TempSensor(int(self._cfg.get("sensor", int(TempSensor.LOW_VOLTAGE))))
        self._saved_fan_mode: FanMode | None = None
        self._saved_refresh_mode: RefreshMode | None = None
        self._auto_restore: bool = bool(self._cfg.get("auto_restore", True))
        try:
            saved_profile = parse_saved_profile(self._cfg)
            self._saved_fan_mode = saved_profile.fan_mode
            self._saved_refresh_mode = saved_profile.refresh_mode
            self._auto_restore = saved_profile.auto_restore
        except ValueError:
            pass
        self._waiting_clean_exit = False
        self._pending_mode: FanMode | None = None
        self._mode_grace_until = 0.0
        self._last_status: PowerStatus | None = None
        self._current_sn = ""
        self._current_info: PowerInfo | None = None

        self.setWindowTitle("HiMOS PSU Linux 监控控制面板")
        self.resize(1020, 680)
        theme.apply(theme.detect(QtWidgets.QApplication.instance()))
        self._apply_theme()
        self._watch_color_scheme()
        self._build_ui()
        self._load_saved_curve()
        self._start_worker()

    def _apply_theme(self) -> None:
        """Adopt the desktop's colour scheme (called at startup and on change)."""
        self.setStyleSheet(theme.stylesheet())

    def _watch_color_scheme(self) -> None:
        """Follow a live light/dark switch; painters read ``theme.P`` each frame."""
        app = QtWidgets.QApplication.instance()
        hints = app.styleHints() if app is not None else None
        signal = getattr(hints, "colorSchemeChanged", None)
        if signal is None:
            return
        try:
            signal.connect(self._on_color_scheme_changed)
        except Exception:
            pass

    @QtCore.Slot()
    def _on_color_scheme_changed(self, *_args: object) -> None:
        theme.apply(theme.detect(QtWidgets.QApplication.instance()))
        self._apply_theme()
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.update()

    def _apply_dark_style(self) -> None:
        """Kept as an alias: tests and older call sites used this name."""
        self._apply_theme()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        main_layout = QtWidgets.QVBoxLayout(root)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(10)

        # Header bar
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel("KUNLUN / HiMOS 数字电源")
        font = self.title_label.font()
        font.setPointSize(14)
        font.setBold(True)
        self.title_label.setFont(font)
        header.addWidget(self.title_label)

        self.edition_badge = QtWidgets.QLabel("")
        self.edition_badge.setObjectName("badge")
        self.edition_badge.setVisible(False)
        header.addWidget(self.edition_badge)
        header.addStretch(1)

        self.port_label = QtWidgets.QLabel("状态: 正在连接...")
        header.addWidget(self.port_label)

        self.unit_btn = QtWidgets.QPushButton("°F" if self._fahrenheit else "°C")
        self.unit_btn.setCheckable(True)
        self.unit_btn.setChecked(self._fahrenheit)
        self.unit_btn.clicked.connect(self._toggle_temp_unit)
        header.addWidget(self.unit_btn)
        main_layout.addLayout(header)

        # Body: left spec column + right live dashboard/control column, inside a tab
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.realtime_tab = QtWidgets.QWidget()
        body = QtWidgets.QHBoxLayout(self.realtime_tab)
        body.setContentsMargins(0, 6, 0, 0)
        body.setSpacing(10)

        # Left column: hardware specs + rails + fault clear
        left_col = QtWidgets.QVBoxLayout()
        body.addLayout(left_col, 35)

        spec_box = QtWidgets.QGroupBox("电源规格信息")
        spec_form = QtWidgets.QFormLayout(spec_box)
        self.spec_labels: dict[str, QtWidgets.QLabel] = {}
        for key, title in (
            ("model", "电源型号:"),
            ("rated", "额定功率:"),
            ("cert", "80 PLUS:"),
            ("io_max", "I/O 最大电流:"),
            ("ac_range", "输入电压范围:"),
            ("fw", "固件版本:"),
            ("vendor", "代工/厂商:"),
            ("sn", "序列号:"),
            ("warranty", "质保:"),
        ):
            lbl = QtWidgets.QLabel("--")
            lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.spec_labels[key] = lbl
            spec_form.addRow(title, lbl)
        left_col.addWidget(spec_box)

        rails_box = QtWidgets.QGroupBox("直流各路输出")
        rails_form = QtWidgets.QFormLayout(rails_box)
        self.rail_labels: dict[str, QtWidgets.QLabel] = {}
        for key, title in (
            ("v12", "+12V:"),
            ("v5", "+5V:"),
            ("v33", "+3.3V:"),
            ("v5sb", "+5V SB:"),
        ):
            lbl = QtWidgets.QLabel("-- V")
            self.rail_labels[key] = lbl
            rails_form.addRow(title, lbl)
        left_col.addWidget(rails_box)

        fault_box = QtWidgets.QGroupBox("故障保护")
        fault_layout = QtWidgets.QHBoxLayout(fault_box)
        self.fault_label = QtWidgets.QLabel("故障码: 0x0000")
        fault_layout.addWidget(self.fault_label, 1)
        self.clear_fault_btn = QtWidgets.QPushButton("清除故障码")
        self.clear_fault_btn.setObjectName("danger")
        self.clear_fault_btn.clicked.connect(self._on_clear_fault_clicked)
        fault_layout.addWidget(self.clear_fault_btn)
        left_col.addWidget(fault_box)
        left_col.addStretch(1)

        # Right column: live telemetry cards + history + fan controls + interactive curve
        right_col = QtWidgets.QVBoxLayout()
        body.addLayout(right_col, 65)

        metrics_row = QtWidgets.QHBoxLayout()
        self.metric_labels: dict[str, QtWidgets.QLabel] = {}
        for key, title in (
            ("power", "输入功率"),
            ("temp", "电源温度"),
            ("rpm", "风扇转速"),
            ("ac_v", "输入电压"),
            ("freq", "输入频率"),
        ):
            card = QtWidgets.QGroupBox(title)
            v = QtWidgets.QVBoxLayout(card)
            lbl = QtWidgets.QLabel("--")
            lbl.setObjectName("metricValue")
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            v.addWidget(lbl)
            self.metric_labels[key] = lbl
            metrics_row.addWidget(card)
        right_col.addLayout(metrics_row)

        self.history_chart = HistoryChartWidget()
        self.history_chart.set_sensor(self._sensor)
        self.history_chart.set_fahrenheit(self._fahrenheit)
        right_col.addWidget(self.history_chart)

        # Thermal probe + refresh selector row
        param_row = QtWidgets.QHBoxLayout()
        param_row.addWidget(QtWidgets.QLabel("温度显示传感器:"))
        self.sensor_group = QtWidgets.QButtonGroup(self)
        for sensor, text in (
            (TempSensor.HIGH_VOLTAGE, "高压区"),
            (TempSensor.LOW_VOLTAGE, "低压区"),
            (TempSensor.ENVIRONMENT, "环境"),
        ):
            rb = QtWidgets.QRadioButton(text)
            rb.setChecked(sensor == self._sensor)
            self.sensor_group.addButton(rb, int(sensor))
            param_row.addWidget(rb)
        self.sensor_group.idClicked.connect(self._on_sensor_changed)

        param_row.addStretch(1)
        param_row.addWidget(QtWidgets.QLabel("刷新周期:"))
        self.refresh_combo = QtWidgets.QComboBox()
        for rm in RefreshMode:
            self.refresh_combo.addItem(f"{rm.interval_ms} ms", int(rm))
        if self._saved_refresh_mode is not None:
            idx = self.refresh_combo.findData(int(self._saved_refresh_mode))
            if idx >= 0:
                self.refresh_combo.blockSignals(True)
                self.refresh_combo.setCurrentIndex(idx)
                self.refresh_combo.blockSignals(False)
        self.refresh_combo.activated.connect(self._on_refresh_selected)
        param_row.addWidget(self.refresh_combo)
        right_col.addLayout(param_row)

        # Fan control group
        fan_box = QtWidgets.QGroupBox("风扇调速控制")
        fan_layout = QtWidgets.QVBoxLayout(fan_box)

        mode_row = QtWidgets.QHBoxLayout()
        self.mode_buttons: dict[FanMode, QtWidgets.QPushButton] = {}
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for mode, text in (
            (FanMode.SILENT, "静音 Silent"),
            (FanMode.AUTO, "自动 Auto"),
            (FanMode.PERFORMANCE, "超频 Perf"),
            (FanMode.CUSTOM, "自定义 Custom"),
            (FanMode.CLEAN, "清灰 Clean"),
        ):
            btn = QtWidgets.QPushButton(text)
            btn.setCheckable(True)
            self.mode_group.addButton(btn, int(mode))
            self.mode_buttons[mode] = btn
            mode_row.addWidget(btn)
        self.mode_group.idClicked.connect(self._on_mode_clicked)
        fan_layout.addLayout(mode_row)

        self.curve_widget = FanCurveWidget()
        self.curve_widget.set_fahrenheit(self._fahrenheit)
        self.curve_widget.points_changed.connect(self._on_curve_dragged)
        fan_layout.addWidget(self.curve_widget, 1)

        curve_btn_row = QtWidgets.QHBoxLayout()
        self.curve_info_label = QtWidgets.QLabel(f"控制点: {list(DEFAULT_CUSTOM_POINTS)}")
        curve_btn_row.addWidget(self.curve_info_label, 1)
        self.reset_curve_btn = QtWidgets.QPushButton("恢复默认曲线")
        self.reset_curve_btn.clicked.connect(self._on_reset_curve)
        curve_btn_row.addWidget(self.reset_curve_btn)
        self.apply_curve_btn = QtWidgets.QPushButton("应用自定义曲线")
        self.apply_curve_btn.clicked.connect(self._on_apply_curve)
        curve_btn_row.addWidget(self.apply_curve_btn)
        fan_layout.addLayout(curve_btn_row)

        right_col.addWidget(fan_box, 1)

        self.energy_tab = EnergyTab(self._db_path)
        self.settings_tab = SettingsTab(self._db_path, config_path=self._config_path)
        self.settings_tab.auto_restore_changed.connect(self._on_auto_restore_changed)
        self.tabs.addTab(self.realtime_tab, "实时监控")
        self.tabs.addTab(self.energy_tab, "能耗统计")
        self.tabs.addTab(self.settings_tab, "设置")
        main_layout.addWidget(self.tabs, 1)

    def _load_saved_curve(self) -> None:
        raw_pts = self._cfg.get("custom_curve")
        if isinstance(raw_pts, list) and len(raw_pts) == 5:
            try:
                pts = validate_fan_curve(raw_pts)
                self.curve_widget.set_points(pts)
                self.curve_info_label.setText(f"控制点: {pts}")
                return
            except (TypeError, ValueError, IndexError):
                pass
        self.curve_widget.set_points(DEFAULT_CUSTOM_POINTS)

    def _persist_config(self) -> None:
        updates: dict[str, object] = {
            "fahrenheit": self._fahrenheit,
            "sensor": int(self._sensor),
            "custom_curve": [list(p) for p in self.curve_widget.points()],
            "auto_restore": self._auto_restore,
        }
        if self._saved_fan_mode in PERSISTABLE_FAN_MODES:
            assert self._saved_fan_mode is not None
            updates["fan_mode"] = self._saved_fan_mode.name.lower()
        if self._saved_refresh_mode is not None:
            updates["refresh_mode"] = self._saved_refresh_mode.interval_ms
        try:
            save_config(updates, path=self._config_path)
        except OSError:
            pass
        if hasattr(self, "settings_tab"):
            self.settings_tab.refresh_restore_summary()

    def _build_restore_profile(self) -> SavedPsuProfile | None:
        if not self._auto_restore:
            return None
        cfg = load_config(self._config_path, strict=False)
        try:
            profile = parse_saved_profile(cfg)
        except ValueError:
            return None
        if not profile.auto_restore or not profile.has_device_settings:
            return None
        return profile

    def _start_worker(self) -> None:
        if not self._start_collector:
            self.port_label.setText("状态: 仅查看（未启动采集）")
            return
        # A background daemon owns the serial port 24/7; when it is running, follow it
        # over IPC instead of fighting for the port (which would fail with TIOCEXCL).
        # Only follow a daemon when no client was injected (tests/embedding pass one)
        # and this window is not in mock mode.
        if self.client is None and not self._mock and probe_socket(self._socket_path):
            worker = DaemonWorker(self._socket_path, self)
            worker.state_changed.connect(self._on_collector_state)
            worker.info_updated.connect(self._on_info_updated)
            worker.status_updated.connect(self._on_status_updated)
            worker.error_occurred.connect(self._on_worker_error)
            self._commander = RemoteCommander(worker)
            self._remote = True
            self._worker = worker
            worker.start()
            return
        if self.client is None:
            self.client = PowerSupplyClient(port=self._port, mock=self._mock)
            self._commander = self.client
        restore_profile = self._build_restore_profile()
        self._worker = TelemetryWorker(
            self.client,
            self._db_path,
            self,
            restore_profile=restore_profile,
        )
        self._worker.state_changed.connect(self._on_collector_state)
        self._worker.info_updated.connect(self._on_info_updated)
        self._worker.status_updated.connect(self._on_status_updated)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.restoring_mode.connect(self._on_restoring_mode)
        self._worker.profile_restored.connect(self._on_profile_restored)
        self._worker.start()

    @QtCore.Slot(bool)
    def _on_auto_restore_changed(self, enabled: bool) -> None:
        self._auto_restore = bool(enabled)

    @QtCore.Slot(object)
    def _on_restoring_mode(self, mode: FanMode) -> None:
        self._pending_mode = mode
        self._mode_grace_until = time.monotonic() + 3.5
        if mode in self.mode_buttons:
            self.mode_buttons[mode].setChecked(True)
        self.curve_widget.set_editable(mode == FanMode.CUSTOM)

    @QtCore.Slot(str)
    def _on_profile_restored(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)

    @QtCore.Slot(str)
    def _on_collector_state(self, text: str) -> None:
        self.port_label.setText(f"状态: {text}")
        self.energy_tab.set_collector_state(text)
        self.settings_tab.set_collector_state(text)

    @QtCore.Slot(str)
    def _on_worker_error(self, message: str) -> None:
        self.port_label.setText(f"连接异常: {message}")
        self.statusBar().showMessage(message, 8000)

    @QtCore.Slot(object, str)
    def _on_info_updated(self, info: PowerInfo | None, sn: str) -> None:
        self._current_sn = sn
        if info is None:
            self.spec_labels["sn"].setText(sn or "--")
            return
        self._current_info = info
        self.energy_tab.set_info(info)
        brand = "昆仑 (Kunlun)" if info.brand_family == "kunlun" else info.brand_family.capitalize()
        self.title_label.setText(f"{brand} {info.model} 数字电源")
        self.spec_labels["model"].setText(info.model)
        self.spec_labels["rated"].setText(f"{info.rated_power} W")
        self.spec_labels["cert"].setText(info.certification_label)
        self.spec_labels["io_max"].setText(f"{info.rated_input_current} A / {info.rated_output_current} A")
        self.spec_labels["ac_range"].setText(f"{info.rated_input_low_voltage}V ~ {info.rated_input_high_voltage}V")
        self.spec_labels["fw"].setText(info.firmware_display)
        self.spec_labels["vendor"].setText(info.manufacture or "--")
        self.spec_labels["sn"].setText(sn or "--")

        warranty, badge = format_warranty_badge(info, sn)
        self.spec_labels["warranty"].setText(warranty)
        if badge:
            self.edition_badge.setText(badge)
            self.edition_badge.setVisible(True)
        else:
            self.edition_badge.setVisible(False)

    @QtCore.Slot(object)
    def _on_status_updated(self, status: PowerStatus) -> None:
        self._last_status = status
        self.metric_labels["power"].setText(f"{status.power:.2f} W")
        self.metric_labels["rpm"].setText(f"{status.fan_speed} RPM")
        self.metric_labels["ac_v"].setText(f"{status.input_voltage:.1f} V")
        self.metric_labels["freq"].setText(f"{status.frequency:.1f} Hz")
        self._update_temp_metric()

        self.rail_labels["v12"].setText(f"{status.voltage_12v:.2f} V")
        self.rail_labels["v5"].setText(f"{status.voltage_5v:.2f} V")
        self.rail_labels["v33"].setText(f"{status.voltage_3v3:.2f} V")
        self.rail_labels["v5sb"].setText(f"{status.voltage_5v_sb:.2f} V")

        fault_txt = f"0x{status.malfunction_code:04x}" + (" (正常)" if status.malfunction_code == 0 else " (异常!)")
        self.fault_label.setText(f"故障码: {fault_txt}")
        self.history_chart.append_sample(status)

        if self._pending_mode is not None:
            if status.mode == self._pending_mode or time.monotonic() > self._mode_grace_until:
                self._pending_mode = None

        # Match HiMOS auto-sync when hardware completes Clean cycle and exits mode 4.
        if self._waiting_clean_exit and self._pending_mode is None and status.mode != FanMode.CLEAN:
            self._waiting_clean_exit = False
            try:
                self._commander.set_fan_mode(status.mode)
            except Exception:
                pass

        if (
            self._pending_mode is None
            and status.mode in self.mode_buttons
            and self.mode_group.checkedId() != int(status.mode)
        ):
            self.mode_buttons[status.mode].setChecked(True)
            self.curve_widget.set_editable(status.mode == FanMode.CUSTOM)

    def _update_temp_metric(self) -> None:
        if self._last_status is None:
            return
        temp_c = self._last_status.selected_temperature(self._sensor)
        val = _to_display_temp(temp_c, self._fahrenheit)
        unit = "°F" if self._fahrenheit else "°C"
        self.metric_labels["temp"].setText(f"{val:.2f} {unit}")

    def _toggle_temp_unit(self) -> None:
        self._fahrenheit = self.unit_btn.isChecked()
        self.unit_btn.setText("°F" if self._fahrenheit else "°C")
        self.curve_widget.set_fahrenheit(self._fahrenheit)
        self.history_chart.set_fahrenheit(self._fahrenheit)
        self._update_temp_metric()
        self._persist_config()

    def _on_sensor_changed(self, sensor_id: int) -> None:
        self._sensor = TempSensor(sensor_id)
        self.history_chart.set_sensor(self._sensor)
        self._update_temp_metric()
        self._persist_config()

    def _on_refresh_selected(self, index: int) -> None:
        rate_val = self.refresh_combo.itemData(index)
        try:
            refresh = RefreshMode(int(rate_val))
            self._commander.set_refresh_mode(refresh)
            self._saved_refresh_mode = refresh
            self._persist_config()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "设置失败", str(exc))

    def _on_mode_clicked(self, mode_id: int) -> None:
        mode = FanMode(mode_id)
        self._pending_mode = mode
        self._mode_grace_until = time.monotonic() + 2.5
        self.curve_widget.set_editable(mode == FanMode.CUSTOM)
        if mode == FanMode.CLEAN:
            self._waiting_clean_exit = True
        try:
            pts = self.curve_widget.points() if mode == FanMode.CUSTOM else None
            self._commander.set_fan_mode(mode, custom_points=pts)
            if mode in PERSISTABLE_FAN_MODES:
                self._saved_fan_mode = mode
                self._persist_config()
        except Exception as exc:
            self._pending_mode = None
            QtWidgets.QMessageBox.warning(self, "风扇模式切换失败", str(exc))

    def _on_curve_dragged(self, pts: list) -> None:
        self.curve_info_label.setText(f"控制点: {pts}")
        self._persist_config()

    def _on_reset_curve(self) -> None:
        self.curve_widget.set_points(DEFAULT_CUSTOM_POINTS)
        self.curve_info_label.setText(f"控制点: {list(DEFAULT_CUSTOM_POINTS)}")
        self._persist_config()

    def _on_apply_curve(self) -> None:
        try:
            pts = self.curve_widget.points()
            self._pending_mode = FanMode.CUSTOM
            self._mode_grace_until = time.monotonic() + 2.5
            self._commander.set_fan_curve(pts, switch_to_custom=True)
            self.mode_buttons[FanMode.CUSTOM].setChecked(True)
            self.curve_widget.set_editable(True)
            self._saved_fan_mode = FanMode.CUSTOM
            self._persist_config()
            self.statusBar().showMessage(f"已应用并保存自定义曲线: {pts}", 3000)
        except Exception as exc:
            self._pending_mode = None
            QtWidgets.QMessageBox.warning(self, "下发曲线失败", str(exc))

    def _on_clear_fault_clicked(self) -> None:
        reply = QtWidgets.QMessageBox.question(
            self,
            "确认清除故障码",
            "确定向电源发送清除锁存故障码指令 (0x1E)？",
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self._commander.clear_fault_code()
            self.statusBar().showMessage("已发送清除故障码指令", 3000)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "指令发送失败", str(exc))

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(2000)
        if not self._remote and self.client is not None:
            self.client.close()
        # Tabs own refresh timers and sqlite handles; close them before teardown.
        for attr in ("energy_tab", "settings_tab"):
            tab = getattr(self, attr, None)
            if tab is not None:
                tab.close()
        super().closeEvent(event)


def launch_gui(port: str | None = None, mock: bool = False, db_path: str | Path | None = None,
               start_collector: bool = True) -> int:
    """Start the PySide6 desktop GUI application."""
    if os.environ.get("QT_QPA_PLATFORM") is None and os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setWindowIcon(QtGui.QIcon(str(Path(__file__).with_name("assets") / "himos-psu.svg")))
    window = MainWindow(port=port, mock=mock, db_path=db_path, start_collector=start_collector)
    window.show()
    return int(app.exec())


def main() -> int:
    port = os.environ.get("HIMOS_PSU_PORT")
    mock = os.environ.get("HIMOS_PSU_MOCK") == "1"
    return launch_gui(port=port, mock=mock, db_path=os.environ.get("HIMOS_PSU_DB"))
