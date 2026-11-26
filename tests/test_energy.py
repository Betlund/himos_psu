"""Tests for energy accounting, tariff handling, export and the collector lock."""

from __future__ import annotations

import contextlib
import csv
import io
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from himos_psu import cli
from himos_psu.client import PowerSupplyClient
from himos_psu.collector import Collector, CollectorBusy
from himos_psu.energy import (
    Band,
    EnergyStore,
    SeasonWindow,
    TariffPlan,
    fixed_plan,
    format_duration,
    local_day_start,
    parse_band_spec,
    preset_plan,
    seasonal_preset,
    validate_bands,
)
from himos_psu.protocol import FanMode, PowerStatus

DAY = date(2026, 11, 15)
BASE = local_day_start(DAY)


def sample(power: float, ts_offset: float = 0.0) -> PowerStatus:
    return PowerStatus(
        voltage_3v3=3.3,
        voltage_5v=5.0,
        voltage_12v=12.1,
        voltage_5v_sb=5.0,
        mode=FanMode.AUTO,
        reserved=b"\x00\x00\x00",
        power=power,
        frequency=50.0,
        high_voltage_temperature=60.0,
        low_voltage_temperature=50.0,
        environment_temperature=30.0,
        input_voltage=228.0,
        fan_speed=0,
        malfunction_code=0,
    )


class EnergyStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "energy.db"
        self.store = EnergyStore(self.db)

    def tearDown(self) -> None:
        try:
            self.store.close()
        finally:
            self._tmp.cleanup()

    # -- integration ------------------------------------------------------
    def test_trapezoid_integration_and_report(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        self.store.add_sample(BASE + 9 * 3600, sample(100.0))
        self.store.add_sample(BASE + 9 * 3600 + 2, sample(200.0))  # 2 s <= gap_max

        report = self.store.report("t", BASE, BASE + 86400)
        expected_ws = (100.0 + 200.0) / 2.0 * 2.0
        self.assertAlmostEqual(report.kwh, expected_ws / 3_600_000, places=9)
        self.assertAlmostEqual(report.cost_cents, expected_ws / 3_600_000 * 100 * 0.5, places=6)
        self.assertAlmostEqual(report.avg_w, 150.0, places=6)
        self.assertEqual(report.peak_w, 200.0)
        self.assertEqual(report.samples, 1)  # one integrated interval (not raw sample count)
        # 2 s of samples inside a 60 s wall window
        self.assertAlmostEqual(report.coverage, 2.0 / 60.0, places=3)

    def test_report_exposes_measured_span(self) -> None:
        """Coverage is relative to the recorded span, which must be visible."""
        self.store.set_plan(fixed_plan("测试", 0.5))
        t0 = BASE + 9 * 3600
        for offset in (0, 2, 60):  # 09:00:00, 09:00:02, 09:01:00 (58 s gap)
            self.store.add_sample(t0 + offset, sample(100.0))

        data = self.store.report("t", BASE, BASE + 86400).as_dict()
        self.assertTrue(data["first_sample_at"].startswith("2026-11-15T09:00"))
        self.assertTrue(data["last_sample_at"].startswith("2026-11-15T09:01"))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(
                [
                    "energy",
                    "--db",
                    str(self.store.path),
                    "report",
                    "--from",
                    "2026-11-15",
                    "--to",
                    "2026-11-16",
                ]
            )
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("记录时段 09:00–09:01", text)
        self.assertIn("缺口 58 s", text)

    def test_gap_is_recorded_not_counted(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        self.store.add_sample(BASE + 9 * 3600, sample(100.0))
        self.store.add_sample(BASE + 9 * 3600 + 30, sample(100.0))  # 30 s > gap_max

        report = self.store.report("t", BASE, BASE + 86400)
        self.assertEqual(report.kwh, 0.0)
        self.assertAlmostEqual(report.gap_s, 30.0, places=3)
        self.assertEqual(report.covered_s, 0.0)

    def test_session_split_on_long_gap(self) -> None:
        self.store.add_sample(BASE, sample(100.0))
        self.store.add_sample(BASE + 5, sample(100.0))
        self.store.add_sample(BASE + 200, sample(100.0))  # > session_gap
        sessions = self.store.recent_sessions(10)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["start_ts"], BASE + 200)

    def test_active_seconds_uses_threshold(self) -> None:
        for i in range(61):
            self.store.add_sample(BASE + i, sample(100.0))
        for i in range(61):
            self.store.add_sample(BASE + 60 + i, sample(10.0))
        active = self.store.active_seconds(BASE, BASE + 86400, threshold_w=20.0)
        self.assertGreaterEqual(active, 59.0)
        self.assertLess(active, 61.0)

    # -- pricing ----------------------------------------------------------
    def test_season_window_switches_bands_and_prices(self) -> None:
        plan = seasonal_preset()
        # 非采暖期：峰 08:00-22:00；采暖期：峰 08:00-20:00，21:00 已经回落
        summer_peak = int(datetime(2026, 7, 15, 21, 0).timestamp())
        winter_valley = int(datetime(2026, 12, 15, 21, 0).timestamp())
        self.assertAlmostEqual(plan.price_at(summer_peak), 0.5769, places=6)
        self.assertAlmostEqual(plan.price_at(winter_valley), 0.3469, places=6)
        self.assertEqual(plan.band_name_at(summer_peak), "峰")
        self.assertEqual(plan.band_name_at(winter_valley), "谷")

    def test_season_window_wraps_the_new_year(self) -> None:
        plan = seasonal_preset()
        window = plan.season
        self.assertIsNotNone(window)
        assert window is not None
        self.assertTrue(window.covers(datetime(2026, 11, 1, 0, 0)))
        self.assertTrue(window.covers(datetime(2027, 1, 15, 12, 0)))
        self.assertTrue(window.covers(datetime(2027, 3, 31, 23, 59)))
        self.assertFalse(window.covers(datetime(2027, 4, 1, 0, 0)))
        self.assertFalse(window.covers(datetime(2026, 10, 31, 23, 59)))
        self.assertFalse(window.covers(datetime(2026, 7, 1, 12, 0)))

    def test_seasonal_plan_round_trips_through_the_store(self) -> None:
        stored = self.store.set_plan(seasonal_preset("采暖方案"))
        self.assertIsNotNone(stored.season)
        active = self.store.active_plan()
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.name, "采暖方案")
        self.assertIsNotNone(active.season)
        assert active.season is not None
        self.assertEqual(active.season.label, "采暖期")
        self.assertEqual(active.season.range_label, "11-01..03-31")
        self.assertEqual([b.name for b in active.season.bands], ["谷", "峰", "谷"])
        self.assertAlmostEqual(active.season.bands[0].price, 0.3469, places=6)
        self.assertAlmostEqual(active.bands[0].price, 0.3769, places=6)

    def test_costs_follow_the_heating_season(self) -> None:
        self.store.set_plan(seasonal_preset())
        start = int(datetime(2026, 12, 15, 21, 0).timestamp())  # 采暖期 谷段
        for offset in (0, 2, 4):
            self.store.add_sample(start + offset, sample(100.0))
        report = self.store.report("t", start - 60, start + 120)
        kwh = (100.0 * 2 + 100.0 * 2) / 3_600_000
        self.assertAlmostEqual(report.kwh, kwh, places=9)
        self.assertAlmostEqual(report.cost_cents, kwh * 100 * 0.3469, places=4)

    def test_describe_lines_split_the_two_seasons(self) -> None:
        lines = seasonal_preset().describe_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("非采暖期", lines[0])
        self.assertIn("0.3769", lines[0])
        self.assertIn("采暖期 11-01..03-31", lines[1])
        self.assertIn("0.3469", lines[1])
        self.assertEqual(fixed_plan("x", 0.5469).describe_lines(), ["统一 0.5469 元/kWh"])

    def test_prices_keep_four_decimals(self) -> None:
        self.assertIn("0.5550", fixed_plan("x", 0.555).describe())
        plan = seasonal_preset()
        self.assertIn("0.5769", plan.describe())
        self.assertIn("0.3469", plan.describe())

    def test_season_needs_a_full_day_of_bands(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_plan(
                TariffPlan(
                    name="坏方案",
                    kind="tou",
                    bands=parse_band_spec("0-24:谷:0.3"),
                    season=SeasonWindow(
                        label="采暖期",
                        start=(11, 1),
                        end=(3, 31),
                        bands=parse_band_spec("0-8:谷:0.3"),
                    ),
                )
            )

    def test_fixed_plan_rejects_a_season(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_plan(
                TariffPlan(
                    name="固定",
                    kind="fixed",
                    price=0.555,
                    season=SeasonWindow(
                        label="采暖期",
                        start=(11, 1),
                        end=(3, 31),
                        bands=parse_band_spec("0-24:谷:0.3"),
                    ),
                )
            )

    def test_tou_price_boundaries(self) -> None:
        plan = preset_plan()
        self.assertEqual(plan.price_at(BASE + 6 * 3600 + 59 * 60), 0.32)   # 谷
        self.assertEqual(plan.price_at(BASE + 7 * 3600), 0.58)            # 平
        self.assertEqual(plan.price_at(BASE + 10 * 3600), 0.92)           # 峰
        self.assertEqual(plan.price_at(BASE + 15 * 3600), 0.58)           # 平
        self.assertEqual(plan.price_at(BASE + 18 * 3600), 1.15)           # 尖
        self.assertEqual(plan.price_at(BASE + 23 * 3600), 0.32)           # 谷

    def test_band_validation(self) -> None:
        with self.assertRaises(ValueError):
            parse_band_spec("0-7:谷:0.3,8-24:平:0.5")  # gap between 7 and 8
        with self.assertRaises(ValueError):
            parse_band_spec("1-24:谷:0.3")
        with self.assertRaises(ValueError):
            validate_bands([Band(0, 1440, "平", -1.0)])

    def test_recalc_uses_new_plan(self) -> None:
        preset = self.store.active_plan()
        self.assertIsNotNone(preset)
        for i in range(61):
            self.store.add_sample(BASE + 10 * 3600 + i, sample(100.0))
        before = self.store.report("t", BASE, BASE + 86400).cost_cents
        self.assertAlmostEqual(before, 6000 / 3_600_000 * 100 * 0.92, places=5)

        self.store.set_plan(fixed_plan("统一", 0.5))
        self.store.recalc_costs()
        after = self.store.report("t", BASE, BASE + 86400).cost_cents
        self.assertAlmostEqual(after, 6000 / 3_600_000 * 100 * 0.5, places=5)

    # -- aggregates -------------------------------------------------------
    def test_calendar_and_daily_series(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        for offset, power in ((0, 100.0), (1, 200.0)):
            day_start = BASE + offset * 86400
            for i in range(61):
                self.store.add_sample(day_start + 12 * 3600 + i, sample(power))

        cal = self.store.calendar(2026, 11)
        self.assertAlmostEqual(cal[15], 6000 / 3_600_000, places=6)
        self.assertAlmostEqual(cal[16], 12000 / 3_600_000, places=6)

        series = self.store.daily_series(DAY, DAY + timedelta(days=1))
        self.assertEqual(len(series), 2)
        self.assertAlmostEqual(series[1]["kwh"], 12000 / 3_600_000, places=6)

    def test_hourly_profile(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        for i in range(61):
            self.store.add_sample(BASE + 9 * 3600 + i, sample(120.0))
        values = self.store.hourly_watts(DAY)
        self.assertEqual(len(values), 24)
        self.assertAlmostEqual(values[9] or 0.0, 120.0, places=1)
        self.assertIsNone(values[10])

    def test_load_distribution_bands(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        for i in range(61):
            self.store.add_sample(BASE + i, sample(100.0))       # 7.7 % -> <20%
        for i in range(61):
            self.store.add_sample(BASE + 120 + i, sample(260.0))  # 20 % -> 20-40%
        rows = self.store.load_distribution(BASE, BASE + 86400, rated_w=1300.0)
        shares = {row["label"]: row["share"] for row in rows}
        self.assertGreater(shares["<20%"], 0.4)
        self.assertGreater(shares["20-40%"], 0.4)

    # -- export / retention ------------------------------------------------
    def test_export_csv(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        for i in range(61):
            self.store.add_sample(BASE + 12 * 3600 + i, sample(100.0))
        target = Path(self._tmp.name) / "out.csv"
        rows = self.store.export_csv("daily", str(target), BASE, BASE + 86400)
        self.assertEqual(rows, 1)
        with target.open(encoding="utf-8") as handle:
            data = list(csv.reader(handle))
        self.assertEqual(data[0], ["date", "kwh", "cost_yuan"])
        self.assertEqual(data[1][0], "2026-11-15")

    def test_prune_keeps_minute_rows(self) -> None:
        self.store.set_plan(fixed_plan("测试", 0.5))
        for i in range(61):
            self.store.add_sample(BASE + i, sample(100.0))
        self.assertGreater(self.store.sample_count(), 0)
        removed = self.store.prune_raw(days=0, now=BASE + 100)
        self.assertGreater(removed, 0)
        self.assertEqual(self.store.sample_count(), 0)
        self.assertGreater(self.store.report("t", BASE, BASE + 86400).kwh, 0.0)

    def test_readonly_connection(self) -> None:
        self.store.close()
        readonly = EnergyStore(self.db, create=False)
        try:
            self.assertGreater(readonly.report("t", BASE, BASE + 86400).samples, -1)
            with self.assertRaises(Exception):
                readonly.set_meta("x", "1")
        finally:
            readonly.close()
        self.store = EnergyStore(self.db, create=False)

    def test_missing_database_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                EnergyStore(Path(tmp) / "nope.db", create=False)

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(42), "42s")
        self.assertEqual(format_duration(90), "1m")
        self.assertEqual(format_duration(3 * 3600 + 5 * 60), "3h 05m")


class CollectorTestCase(unittest.TestCase):
    def test_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            store = EnergyStore(db)
            first = Collector(PowerSupplyClient(mock=True), store, lock_path=Path(tmp) / "c.lock")
            second = Collector(PowerSupplyClient(mock=True), store, lock_path=Path(tmp) / "c.lock")
            first.acquire()
            try:
                with self.assertRaises(CollectorBusy):
                    second.acquire()
            finally:
                first.release()
                store.close()

    def test_collector_collects_from_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            store = EnergyStore(db)
            client = PowerSupplyClient(mock=True)
            collector = Collector(client, store, lock_path=Path(tmp) / "c.lock")
            started = time.monotonic()
            try:
                collector.run(should_stop=lambda: time.monotonic() - started > 2.2)
            finally:
                store.close()

            readonly = EnergyStore(db, create=False)
            try:
                self.assertGreaterEqual(collector.stats.samples, 1)
                self.assertGreater(collector.stats.kwh, 0.0)
                sessions = readonly.recent_sessions(5)
                self.assertTrue(sessions)
                self.assertGreater(sessions[0]["end_ts"], sessions[0]["start_ts"])
            finally:
                readonly.close()


if __name__ == "__main__":
    unittest.main()
