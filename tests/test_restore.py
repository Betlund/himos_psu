"""Unit and offscreen GUI tests for fan-mode persistence, safe restore, and systemd unit generation."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6 import QtCore, QtWidgets

from himos_psu import cli
from himos_psu.client import PowerSupplyClient
from himos_psu.collector import collector_lock
from himos_psu.config import (
    SavedPsuProfile,
    apply_profile_to_client,
    load_config,
    load_saved_profile,
    parse_saved_profile,
    save_config,
)
from himos_psu.fan_curve import DEFAULT_CUSTOM_POINTS, resample_fan_curve
from himos_psu.gui import MainWindow
from himos_psu.protocol import CmdId, FanMode, RefreshMode, build_fan_curve_frame
from himos_psu.transport import MockSerialTransport

CUSTOM_POINTS = ((0, 0), (35, 25), (55, 45), (75, 75), (100, 100))


class RestoreConfigAndCliTestCase(unittest.TestCase):
    def test_profile_validation_accepts_and_rejects_safely(self) -> None:
        for name, expected in (
            ("auto", FanMode.AUTO),
            ("silent", FanMode.SILENT),
            ("performance", FanMode.PERFORMANCE),
        ):
            prof = parse_saved_profile({"fan_mode": name, "refresh_mode": 2000})
            self.assertEqual(prof.fan_mode, expected)
            self.assertEqual(prof.refresh_mode, RefreshMode.MS_2000)
            self.assertTrue(prof.auto_restore)

        prof_custom = parse_saved_profile(
            {
                "fan_mode": "custom",
                "custom_curve": [list(pt) for pt in CUSTOM_POINTS],
                "auto_restore": False,
            }
        )
        self.assertEqual(prof_custom.fan_mode, FanMode.CUSTOM)
        self.assertEqual(prof_custom.custom_curve, CUSTOM_POINTS)
        self.assertFalse(prof_custom.auto_restore)

        for bad in (
            {"fan_mode": "clean"},
            {"fan_mode": 4},
            {"fan_mode": "unknown"},
            {"fan_mode": 5},
            {"fan_mode": True},
            {"fan_mode": "custom"},  # missing curve
            {"fan_mode": "custom", "custom_curve": [[0, 0], [20, 10], [60, 30], [80, 70], [100, 100]]},
            {"fan_mode": "custom", "custom_curve": [[0, 0], [40, 60], [60, 40], [80, 70], [100, 100]]},
        ):
            with self.assertRaises(ValueError):
                parse_saved_profile(bad)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "settings.json"
            save_config({"fahrenheit": True, "sensor": 2}, path=cfg_path)
            save_config({"fan_mode": "silent"}, path=cfg_path)
            loaded = load_config(cfg_path, strict=True)
            self.assertTrue(loaded["fahrenheit"])
            self.assertEqual(loaded["sensor"], 2)
            self.assertEqual(loaded["fan_mode"], "silent")

            cfg_path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_saved_profile(cfg_path)

    def test_client_prevalidates_curve_before_sending_mode(self) -> None:
        transport = MockSerialTransport()
        with PowerSupplyClient(transport=transport) as client:
            invalid_curve = [(0, 0), (20, 10), (60, 30), (80, 70), (100, 100)]
            with self.assertRaises(ValueError):
                client.set_fan_mode(FanMode.CUSTOM, custom_points=invalid_curve)
            self.assertEqual(transport.tx_history, [])
            self.assertEqual(transport.mode, FanMode.AUTO)

    def test_apply_profile_and_cli_restore_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = tmp / "settings.json"
            db_path = tmp / "energy.db"

            # Unconfigured settings exits 0 without touching serial port.
            save_config({"fahrenheit": False, "sensor": 1}, path=cfg_path)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["apply-saved", "--config", str(cfg_path), "--db", str(db_path)])
            self.assertEqual(rc, 0)
            self.assertIn("跳过", out.getvalue())

            # Persist curve + custom mode via CLI --mock --save.
            args = [
                "--mock",
                "fan-curve",
                *(f"{x},{y}" for x, y in CUSTOM_POINTS),
                "--save",
                "--config",
                str(cfg_path),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            saved = load_config(cfg_path, strict=True)
            self.assertEqual(saved["fan_mode"], "custom")
            self.assertEqual(saved["custom_curve"], [list(pt) for pt in CUSTOM_POINTS])

            # Reject saving CLEAN mode before touching PSU or config.
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc_clean = cli.main(["--mock", "fan-mode", "clean", "--save", "--config", str(cfg_path)])
            self.assertEqual(rc_clean, 1)
            self.assertEqual(load_config(cfg_path, strict=True)["fan_mode"], "custom")

            # --dry-run does not open serial port (even without --mock).
            dry_out = io.StringIO()
            with contextlib.redirect_stdout(dry_out):
                rc_dry = cli.main(["apply-saved", "--config", str(cfg_path), "--dry-run"])
            self.assertEqual(rc_dry, 0)
            self.assertIn("dry-run", dry_out.getvalue())
            self.assertIn(build_fan_curve_frame(CUSTOM_POINTS).hex(" "), dry_out.getvalue())

            # Mock PSU starts in AUTO; apply-saved transitions it to CUSTOM and confirms.
            apply_out = io.StringIO()
            with contextlib.redirect_stdout(apply_out):
                rc_apply = cli.main(
                    ["apply-saved", "--mock", "--config", str(cfg_path), "--db", str(db_path)]
                )
            self.assertEqual(rc_apply, 0)
            self.assertIn("已确认电源当前模式: Custom", apply_out.getvalue())

            # auto_restore=false skips unless --force.
            save_config({"auto_restore": False}, path=cfg_path)
            skip_out = io.StringIO()
            with contextlib.redirect_stdout(skip_out):
                self.assertEqual(
                    cli.main(["apply-saved", "--mock", "--config", str(cfg_path), "--db", str(db_path)]),
                    0,
                )
            self.assertIn("auto_restore 已禁用", skip_out.getvalue())

            force_out = io.StringIO()
            with contextlib.redirect_stdout(force_out):
                self.assertEqual(
                    cli.main(
                        ["apply-saved", "--mock", "--force", "--config", str(cfg_path), "--db", str(db_path)]
                    ),
                    0,
                )
            self.assertIn("已确认电源当前模式: Custom", force_out.getvalue())

            # --skip-if-busy exits 0 cleanly while collector.lock is held.
            save_config({"auto_restore": True}, path=cfg_path)
            lock_path = tmp / "collector.lock"
            with collector_lock(lock_path):
                busy_out = io.StringIO()
                with contextlib.redirect_stdout(busy_out):
                    rc_busy = cli.main(
                        [
                            "apply-saved",
                            "--mock",
                            "--config",
                            str(cfg_path),
                            "--db",
                            str(db_path),
                            "--skip-if-busy",
                        ]
                    )
                self.assertEqual(rc_busy, 0)
                self.assertIn("占用", busy_out.getvalue())

    def test_apply_profile_if_needed_and_fault_guard(self) -> None:
        profile = SavedPsuProfile(
            fan_mode=FanMode.CUSTOM,
            custom_curve=CUSTOM_POINTS,
            refresh_mode=None,
            auto_restore=True,
        )

        # Already in CUSTOM with --if-needed -> zero control writes.
        t_already = MockSerialTransport()
        t_already.mode = FanMode.CUSTOM
        with PowerSupplyClient(transport=t_already) as client:
            initial = client.wait_for_status(timeout=1.5)
            t_already.tx_history.clear()
            res = apply_profile_to_client(
                client, profile, initial_status=initial, if_needed=True, delay_s=0.01
            )
            self.assertFalse(res.applied)
            self.assertEqual(res.skipped_reason, "already_applied")
            self.assertEqual(t_already.tx_history, [])

        # Active fault code blocks auto-restore unless force=True.
        t_fault = MockSerialTransport()
        t_fault.malfunction_code = 0x0010
        with PowerSupplyClient(transport=t_fault) as client:
            initial = client.wait_for_status(timeout=1.5)
            t_fault.tx_history.clear()
            res_blocked = apply_profile_to_client(
                client, profile, initial_status=initial, delay_s=0.01
            )
            self.assertFalse(res_blocked.applied)
            self.assertEqual(res_blocked.skipped_reason, "fault_code:0x0010")
            self.assertEqual(t_fault.tx_history, [])

            res_forced = apply_profile_to_client(
                client, profile, initial_status=initial, force=True, confirm_timeout=1.5, delay_s=0.01
            )
            self.assertTrue(res_forced.applied)
            self.assertEqual(res_forced.confirmed_mode, FanMode.CUSTOM)
            self.assertEqual(t_fault.custom_curve, resample_fan_curve(CUSTOM_POINTS))

        # Normal AUTO -> CUSTOM sends 0x13 then 0x1B and confirms.
        t_auto = MockSerialTransport()
        with PowerSupplyClient(transport=t_auto) as client:
            initial = client.wait_for_status(timeout=1.5)
            self.assertEqual(initial.mode, FanMode.AUTO)
            t_auto.tx_history.clear()
            res_ok = apply_profile_to_client(
                client, profile, initial_status=initial, confirm_timeout=1.5, delay_s=0.01
            )
            self.assertTrue(res_ok.applied)
            self.assertTrue(res_ok.sent_mode)
            self.assertTrue(res_ok.sent_curve)
            self.assertEqual(res_ok.confirmed_mode, FanMode.CUSTOM)
            self.assertEqual(t_auto.mode, FanMode.CUSTOM)
            self.assertEqual(t_auto.custom_curve, resample_fan_curve(CUSTOM_POINTS))
            self.assertEqual([f[3] for f in t_auto.tx_history], [CmdId.POWER_FAN_MODE, CmdId.POWER_FAN_CUSTOM])

    def test_systemd_unit_generation_is_clean(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["systemd-unit", "--exec", "/usr/local/bin/himos-psu", "--wait-port", "15"])
        self.assertEqual(rc, 0)
        unit = out.getvalue()
        self.assertIn("[Unit]", unit)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ExecStart=/usr/local/bin/himos-psu apply-saved --wait-port 15 --skip-if-busy", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn("After=default.target", unit)
        self.assertNotIn("/home/", unit)


class GuiRestoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_gui_persists_mode_without_clean_overwrite_and_toggles_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = tmp / "settings.json"
            transport = MockSerialTransport()
            client = PowerSupplyClient(transport=transport)
            client.open()
            win = MainWindow(
                client=client,
                config_path=cfg_path,
                db_path=tmp / "energy.db",
                start_collector=False,
            )
            try:
                win._on_mode_clicked(int(FanMode.SILENT))
                self.assertEqual(load_config(cfg_path, strict=True)["fan_mode"], "silent")

                # CLEAN must not overwrite the persisted mode.
                win._on_mode_clicked(int(FanMode.CLEAN))
                self.assertEqual(load_config(cfg_path, strict=True)["fan_mode"], "silent")

                # Applying custom curve saves custom mode + curve points.
                win.curve_widget.set_points(CUSTOM_POINTS)
                win._on_apply_curve()
                cfg = load_config(cfg_path, strict=True)
                self.assertEqual(cfg["fan_mode"], "custom")
                self.assertEqual(cfg["custom_curve"], [list(pt) for pt in CUSTOM_POINTS])

                # SettingsTab checkbox toggles auto_restore immediately.
                win.settings_tab.auto_restore_check.setChecked(False)
                self.assertFalse(load_config(cfg_path, strict=True)["auto_restore"])
                self.assertFalse(win._auto_restore)
            finally:
                win.close()

    def test_gui_auto_restores_on_connect_with_mock_collector(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = tmp / "settings.json"
            save_config(
                {
                    "fan_mode": "custom",
                    "custom_curve": [list(pt) for pt in CUSTOM_POINTS],
                    "auto_restore": True,
                },
                path=cfg_path,
            )
            transport = MockSerialTransport()
            self.assertEqual(transport.mode, FanMode.AUTO)
            client = PowerSupplyClient(transport=transport)
            win = MainWindow(
                client=client,
                config_path=cfg_path,
                db_path=tmp / "energy.db",
                start_collector=True,
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    QtCore.QCoreApplication.processEvents()
                    if (
                        transport.mode == FanMode.CUSTOM
                        and transport.custom_curve == resample_fan_curve(CUSTOM_POINTS)
                        and win.mode_buttons[FanMode.CUSTOM].isChecked()
                    ):
                        break
                    time.sleep(0.02)

                self.assertEqual(transport.mode, FanMode.CUSTOM)
                self.assertEqual(transport.custom_curve, resample_fan_curve(CUSTOM_POINTS))
                self.assertTrue(win.mode_buttons[FanMode.CUSTOM].isChecked())
                self.assertEqual(win.curve_widget.points(), list(CUSTOM_POINTS))
                self.assertNotEqual(win.curve_widget.points(), list(DEFAULT_CUSTOM_POINTS))
            finally:
                win.close()


if __name__ == "__main__":
    unittest.main()
