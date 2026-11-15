"""Offscreen PySide6 GUI smoke tests (no serial port, no desktop capture)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6 import QtCore, QtGui, QtWidgets

from himos_psu.energy import EnergyStore, fixed_plan, local_day_start
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
                base = local_day_start(date.today())
                with EnergyStore(tmp / "energy.db") as store:
                    store.set_plan(fixed_plan("测试电价", 0.6))
                    for i in range(61):
                        store.add_sample(base + 10 * 3600 + i, status(150.0))
                win.energy_tab.refresh()
                self.assertNotEqual(win.energy_tab.kpi_today.value.text(), "--")
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


if __name__ == "__main__":
    unittest.main()
