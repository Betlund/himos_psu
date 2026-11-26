"""Offscreen PySide6 GUI smoke tests (no serial port, no desktop capture)."""

from __future__ import annotations

import contextlib
import gc
import os
import sys
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6 import QtCore, QtGui, QtWidgets

from himos_psu.energy import EnergyStore, fixed_plan, local_day_start, seasonal_preset


from himos_psu.energy import EnergyStore, fixed_plan, local_day_start, seasonal_preset


# PySide6 finalizes every remaining Qt wrapper while destroying QCoreApplication, and
# with Python 3.14 that segfaults once a test has painted custom widgets (the process
# prints OK and then crashes, which would fail CI). Widgets are disposed inside the
# tests; this just skips the interpreter-side Qt teardown and keeps unittest's status.
_ORIGINAL_EXIT = sys.exit


def _exit_without_qt_teardown(code: object = 0) -> "None":  # noqa: ANN401
    if isinstance(code, int):
        os._exit(code)
    os._exit(0 if not code else 1)


if os.environ.get("HIMOS_PSU_QT_TEARDOWN") != "1":
    sys.exit = _exit_without_qt_teardown  # type: ignore[assignment]
from himos_psu.gui_energy import TariffDialog
from himos_psu.fan_curve import DEFAULT_CUSTOM_POINTS
from himos_psu.gui import MainWindow
from himos_psu.protocol import FanMode, PowerInfo, PowerStatus


def status(power: float) -> PowerStatus:
    return PowerStatus(
        voltage_3v3=3.31,
        voltage_5v=5.01,
        voltage_12v=12.13,
        voltage_5v_sb=5.01,
        mode=FanMode.CUSTOM,
        reserved=b"\x0f\x0e\x01",
        power=power,
        frequency=50.2,
        high_voltage_temperature=63.48,
        low_voltage_temperature=52.07,
        environment_temperature=31.33,
        input_voltage=228.6,
        fan_speed=1140,
        malfunction_code=0,
    )


class GuiSmokeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_main_window_renders_and_enforces_curve(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            win = MainWindow(mock=True, config_path=tmp / "settings.json",
                             db_path=tmp / "energy.db", start_collector=False)
            try:
                win.resize(1180, 800)
                info = PowerInfo(
                    model="KE-1300P",
                    version=4,
                    rated_input_current=20,
                    rated_output_current=110,
                    rated_input_low_voltage=100,
                    rated_input_high_voltage=240,
                    rated_power=1300,
                    warranty=10,
                    certification_80plus="P",
                    manufacture="segotep",
                    year=24,
                    month=9,
                    day=20,
                )
                win._on_info_updated(info, "SBRC00000000123")
                win._on_status_updated(status(212.35))
                win._on_status_updated(status(212.35))

                self.assertIn("KE-1300P", win.title_label.text())
                self.assertEqual(win.edition_badge.text(), "No.123/1000")
                self.assertIn("212.35 W", win.metric_labels["power"].text())
                self.assertTrue(win.mode_buttons[FanMode.CUSTOM].isChecked())
                self.assertEqual(win.tabs.count(), 3)
                self.assertEqual([win.tabs.tabText(i) for i in range(3)],
                                 ["实时监控", "能耗统计", "设置"])

                win.curve_widget.set_points(DEFAULT_CUSTOM_POINTS)
                win.curve_widget.drag_point_to(1, 10, 80)  # clamps x>=30, propagates y>=80 to pt2..pt3
                pts = win.curve_widget.points()
                self.assertEqual(pts[1], (30, 80))
                self.assertEqual(pts[2], (60, 80))
                self.assertEqual(pts[3], (80, 80))

                # Seed a little history so the energy tab has something to show.
                # Timestamps must sit inside today *and* before "now", otherwise the
                # tab's today range [day start, now] excludes them and the KPI stays
                # zero — which is exactly what happened on a CI runner running UTC.
                now = int(time.time())
                day_start = local_day_start(date.today())
                first_ts = max(day_start, now - 120)
                seeded = 0
                with EnergyStore(tmp / "energy.db") as store:
                    store.set_plan(fixed_plan("测试电价", 0.6))
                    for offset in range(61):
                        ts = first_ts + offset
                        if ts >= now:
                            break
                        store.add_sample(ts, status(150.0))
                        seeded += 1
                win.energy_tab.refresh()
                self.assertNotEqual(win.energy_tab.kpi_today.value.text(), "--")
                if seeded:  # 整数秒内恰好跨零点的极端情况不强行断言
                    self.assertGreater(float(win.energy_tab.kpi_today.value.text()), 0.0)
                self.assertIn("费用", win.energy_tab.kpi_today.sub.text())

                win.tabs.setCurrentIndex(1)
                QtCore.QCoreApplication.processEvents()
                pixmap = QtGui.QPixmap(win.size())
                win.render(pixmap)
                self.assertFalse(pixmap.isNull())

                win.tabs.setCurrentIndex(2)
                QtCore.QCoreApplication.processEvents()
                settings_pixmap = QtGui.QPixmap(win.size())
                win.render(settings_pixmap)
                self.assertFalse(settings_pixmap.isNull())
            finally:
                win.close()
                win.deleteLater()
                QtWidgets.QApplication.processEvents()
                del win


class TariffDialogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_four_decimals_and_empty_table_seeding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EnergyStore(Path(tmp) / "energy.db")
            try:
                store.set_plan(fixed_plan("家用电", 0.5469))
                dialog = TariffDialog(store)
                try:
                    self.assertEqual(dialog.price_spin.decimals(), 4)
                    self.assertEqual(dialog.price_spin.text(), "0.5469")
                    self.assertEqual(dialog.table.rowCount(), 0)  # fixed plan has no bands
                    dialog.kind_combo.setCurrentIndex(1)          # 切到峰谷分时
                    self.app.processEvents()
                    self.assertEqual(dialog.table.rowCount(), 7)
                    self.assertEqual(dialog.table.item(0, 3).text(), "0.3200")
                finally:
                    dialog.close()
                    dialog.deleteLater()
                    QtWidgets.QApplication.processEvents()
                    del dialog
            finally:
                store.close()

    def test_seasonal_plan_round_trips_through_the_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EnergyStore(Path(tmp) / "energy.db")
            try:
                store.set_plan(seasonal_preset("采暖"))
                dialog = TariffDialog(store)
                try:
                    self.assertTrue(dialog.season_check.isChecked())
                    self.assertEqual(dialog.season_start_edit.text(), "11-01")
                    self.assertEqual(dialog.table.item(0, 3).text(), "0.3769")
                    dialog.season_combo.setCurrentIndex(1)
                    self.app.processEvents()
                    self.assertEqual(dialog.table.rowCount(), 3)
                    self.assertEqual(dialog.table.item(1, 2).text(), "峰")
                    self.assertEqual(dialog.table.item(1, 1).text(), "20:00")
                finally:
                    dialog.close()
                    dialog.deleteLater()
                    QtWidgets.QApplication.processEvents()
                    del dialog
            finally:
                store.close()


class ThemeAndCalendarTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self) -> None:
        from himos_psu import theme

        os.environ.pop("HIMOS_PSU_THEME", None)
        theme.apply(theme.DARK)

    def test_env_override_switches_every_painter_alias(self) -> None:
        from himos_psu import gui_energy, theme

        os.environ["HIMOS_PSU_THEME"] = "dark"
        self.assertEqual(theme.detect().name, "dark")
        theme.apply(theme.detect())
        self.assertEqual(gui_energy.BG, theme.DARK.bg)
        self.assertEqual(gui_energy.BAND_COLORS, theme.DARK.band_colors)

        os.environ["HIMOS_PSU_THEME"] = "light"
        theme.apply(theme.detect())
        self.assertEqual(gui_energy.BG, theme.LIGHT.bg)
        self.assertEqual(gui_energy.BORDER, theme.LIGHT.border)

        sheet = theme.stylesheet()
        self.assertIn(theme.LIGHT.bg, sheet)
        self.assertIn(theme.LIGHT.accent, sheet)
        self.assertIn("QPushButton#navBtn", sheet)

    def test_calendar_header_shows_month_and_navigates(self) -> None:
        from himos_psu.gui_energy import EnergyTab

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            store = EnergyStore(db)
            now = int(time.time())
            for offset in range(0, 300, 5):
                store.add_sample(now - offset, status(180.0))
            # Two older samples 40 days back so that month owns a real minute bucket
            # (a lone sample never integrates, so it would not appear in the calendar).
            store.add_sample(now - 40 * 86400, status(180.0))
            store.add_sample(now - 40 * 86400 + 2, status(180.0))
            store.close()

            tab = EnergyTab(db)
            try:
                tab.refresh()
                today = date.today()
                self.assertEqual(tab.cal_month_label.text(), f"{today.year} 年 {today.month} 月")
                self.assertFalse(tab.cal_next.isEnabled(), "不该翻到未来月份")

                tab._shift_month(-1)
                prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
                self.assertEqual(tab.cal_month_label.text(), f"{prev_year} 年 {prev_month} 月")
                self.assertTrue(tab.cal_next.isEnabled())
                # 没有数据的月份也要能翻到，否则按钮看起来像坏的
                for _ in range(3):
                    tab._shift_month(-1)
                self.assertIn("年", tab.cal_month_label.text())
                self.assertTrue(tab.cal_prev.isEnabled())

                tab._goto_current_month()
                self.assertEqual(tab.cal_month_label.text(), f"{today.year} 年 {today.month} 月")
                self.assertTrue(tab.cal_today.isEnabled(), "今天按钮应始终可点")
                self.assertTrue(tab.cal_prev.isEnabled())
            finally:
                tab._timer.stop()
                tab.close()
                tab.deleteLater()
                QtWidgets.QApplication.processEvents()
                del tab


class DaySelectionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_clicking_a_day_switches_the_chart_and_back(self) -> None:
        from himos_psu.energy import preset_plan
        from himos_psu.gui_energy import EnergyTab

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            store = EnergyStore(db)
            store.set_plan(preset_plan())
            now = int(time.time())
            for offset in range(600, 0, -3):  # oldest first: integration needs order
                store.add_sample(now - offset, status(200.0))
            store.close()

            tab = EnergyTab(db)
            try:
                tab.refresh()
                day = date.today()
                self.assertIsNone(tab._selected_day)
                tab._on_day_clicked(day)
                self.assertEqual(tab._selected_day, day)
                self.assertEqual(tab._range, "day")
                self.assertEqual(tab.range_combo.currentData(), "day")
                self.assertIn(day.strftime("%Y-%m-%d"), tab.chart._caption)
                self.assertIn(day.strftime("%Y-%m-%d"), tab.cal_note.text())
                self.assertIn("再点一次取消", tab.cal_note.text())

                tab._on_day_clicked(day)  # 再点一次取消
                self.assertIsNone(tab._selected_day)
                self.assertEqual(tab._range, "today")
                self.assertNotIn("再点一次取消", tab.cal_note.text())

                tab._on_day_clicked(day)
                tab._shift_month(-1)      # 翻月应清掉选中
                self.assertIsNone(tab._selected_day)
                self.assertEqual(tab._range, "today")

                tab._on_day_clicked(day)
                tab._goto_current_month()  # 「今天」也应清掉选中
                self.assertIsNone(tab._selected_day)
                self.assertEqual(tab._range, "today")
            finally:
                tab._timer.stop()
                tab.close()
                tab.deleteLater()
                QtWidgets.QApplication.processEvents()
                del tab

    def test_donut_refreshes_after_a_tariff_edit(self) -> None:
        from himos_psu.energy import fixed_plan, preset_plan
        from himos_psu.gui_energy import EnergyTab

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            store = EnergyStore(db)
            store.set_plan(preset_plan())
            now = int(time.time())
            for offset in range(600, 0, -3):
                store.add_sample(now - offset, status(200.0))
            store.close()

            tab = EnergyTab(db)
            try:
                tab.refresh()
                self.assertTrue(tab.donut._slices)
                self.assertNotIn("统一电价", [s["name"] for s in tab.donut._slices])

                other = EnergyStore(db)
                try:
                    other.set_plan(fixed_plan("固定电价", 0.555))
                    other.recalc_costs()
                finally:
                    other.close()
                tab._store.invalidate_plan()
                tab.refresh()
                self.assertEqual([s["name"] for s in tab.donut._slices], ["统一电价"])
            finally:
                tab._timer.stop()
                tab.close()
                tab.deleteLater()
                QtWidgets.QApplication.processEvents()
                del tab


class ChartHoverTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_hover_maps_x_to_the_nearest_point(self) -> None:
        from himos_psu.gui_energy import PowerChart

        chart = PowerChart()
        chart.resize(600, 220)
        values = [100.0 + i for i in range(24)]
        chart.set_data(values, [f"{h:02d}" for h in range(24)], "W",
                       details=[f"{h:02d}:00 · {100 + h:.0f} W" for h in range(24)])
        plot = chart._plot_rect()
        width = plot.width() / (len(values) - 1)
        chart.mouseMoveEvent(QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove, QtCore.QPointF(plot.left() + width * 5, plot.center().y()),
            QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier))
        self.assertEqual(chart._hover, 5)
        self.assertIn("105 W", chart._details[5])
        chart.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
        self.assertIsNone(chart._hover)
        chart.deleteLater()
        QtWidgets.QApplication.processEvents()

    def test_fine_range_uses_five_minute_buckets(self) -> None:
        from himos_psu.energy import fixed_plan
        from himos_psu.gui_energy import EnergyTab

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            store = EnergyStore(db)
            store.set_plan(fixed_plan("测试", 0.5))
            now = int(time.time())
            for offset in range(1200, 0, -2):  # 2 s apart: wider gaps are not integrated
                store.add_sample(now - offset, status(180.0))
            store.close()

            tab = EnergyTab(db)
            try:
                tab.range_combo.setCurrentIndex(1)   # 今日（每 5 分钟）
                tab.refresh()
                self.assertEqual(tab._range, "fine")
                expected = max(1, len(tab.chart._values))
                self.assertGreaterEqual(expected, 270)  # 一天 288 格，今天还没过完
                self.assertLessEqual(expected, 288)
                self.assertTrue(tab.chart._details)
                self.assertTrue(any("平均" in d for d in tab.chart._details),
                                "有数据的格子应带平均值")
            finally:
                tab._timer.stop()
                tab.close()
                tab.deleteLater()
                QtWidgets.QApplication.processEvents()
                del tab


if __name__ == "__main__":
    unittest.main()
