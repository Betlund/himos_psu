"""Tests for the read-only HTTP API served by the daemon."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from himos_psu.api import HttpApi, build_status_payload, energy_payload, history_payload
from himos_psu.energy import EnergyStore, fixed_plan
from himos_psu.protocol import FanMode, PowerInfo, PowerStatus


def status(power: float = 212.4) -> PowerStatus:
    return PowerStatus(
        voltage_3v3=3.31,
        voltage_5v=5.01,
        voltage_12v=12.13,
        voltage_5v_sb=5.01,
        mode=FanMode.CUSTOM,
        reserved=b"\x0f\x0e\x01",
        power=power,
        frequency=50.0,
        high_voltage_temperature=34.5,
        low_voltage_temperature=36.1,
        environment_temperature=24.4,
        input_voltage=228.4,
        fan_speed=480,
        malfunction_code=0,
    )


INFO = PowerInfo(
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


def seed_database(path: Path) -> None:
    store = EnergyStore(path)
    store.set_plan(fixed_plan("测试电价", 0.5))
    now = int(time.time())
    for offset in range(600, 0, -2):  # 2 s apart so intervals are integrated
        store.add_sample(now - offset, status())
    store.close()


class HttpApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "energy.db"
        seed_database(self.db)
        self.received_at = time.time()
        self.api = HttpApi(
            self.db,
            status_provider=lambda: (status(), self.received_at),
            state_provider=lambda: "采集中",
            info_provider=lambda: INFO,
            sn_provider=lambda: "SBRC00000000123",
            port=0,
        )
        self.api.start()
        self.base = f"http://127.0.0.1:{self.api.port}"

    def tearDown(self) -> None:
        self.api.stop()
        self._tmp.cleanup()

    def get(self, path: str, token: str | None = None) -> tuple[int, dict | str]:
        request = urllib.request.Request(self.base + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
                if "json" in response.headers.get("Content-Type", ""):
                    return response.status, json.loads(body)
                return response.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, body

    def test_status_carries_readings_and_today_totals(self) -> None:
        code, payload = self.get("/v1/status")
        self.assertEqual(code, 200)
        self.assertEqual(payload["schema"], 1)
        self.assertAlmostEqual(payload["power_w"], 212.4, places=2)
        self.assertFalse(payload["stale"])
        self.assertLess(payload["age_s"], 5)
        self.assertEqual(payload["rails_v"]["12"], 12.13)
        self.assertEqual(payload["temps_c"]["hv"], 34.5)
        self.assertEqual(payload["fan_mode"], "custom")
        self.assertEqual(payload["unit"]["model"], "KE-1300P")
        self.assertGreater(payload["energy"]["today_kwh"], 0.0)
        self.assertIn("today_cost_yuan", payload["energy"])

    def test_history_uses_five_minute_buckets_by_default(self) -> None:
        code, payload = self.get("/v1/history?hours=1")
        self.assertEqual(code, 200)
        self.assertEqual(payload["step_s"], 300)
        self.assertEqual(payload["unit"], "W")
        expected = len(range(payload["start"] // 300 * 300, payload["end"], 300))
        self.assertEqual(len(payload["points"]), expected)
        self.assertTrue(any(point["avg"] is not None for point in payload["points"]))
        for point in payload["points"]:
            self.assertIn("min", point)
            self.assertIn("max", point)

    def test_history_rejects_unknown_step(self) -> None:
        code, payload = self.get("/v1/history?step=120")
        self.assertEqual(code, 400)
        self.assertIn("step", payload["error"])

    def test_history_rejects_absurd_windows(self) -> None:
        code, payload = self.get("/v1/history?hours=400")
        self.assertEqual(code, 400)
        self.assertIn("hours", payload["error"])
        code, _payload = self.get("/v1/history?hours=0")
        self.assertEqual(code, 400)

    def test_energy_range_and_bands(self) -> None:
        code, payload = self.get("/v1/energy?range=today")
        self.assertEqual(code, 200)
        self.assertGreater(payload["kwh"], 0.0)
        self.assertGreater(payload["cost_yuan"], 0.0)
        self.assertEqual(payload["range"], "today")
        self.assertIn("active_s", payload)
        self.assertGreaterEqual(payload["active_s"], 0.0)

        code, payload = self.get("/v1/energy?range=today&bands=1")
        self.assertEqual(code, 200)
        self.assertEqual(payload["bands"][0]["name"], "统一电价")

    def test_meta_hides_serial_number_unless_asked(self) -> None:
        code, payload = self.get("/v1/meta")
        self.assertEqual(code, 200)
        self.assertEqual(payload["model"], "KE-1300P")
        self.assertNotIn("sn", payload)
        code, payload = self.get("/v1/meta?sn=1")
        self.assertEqual(payload["sn"], "SBRC00000000123")

    def test_health_and_metrics(self) -> None:
        code, payload = self.get("/v1/health")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["stale"])

        code, body = self.get("/metrics")
        self.assertEqual(code, 200)
        self.assertIn("himos_psu_power_watts 212.4", body)
        self.assertIn("himos_psu_stale 0", body)
        self.assertIn('himos_psu_energy_kwh{range="today"}', body)

    def test_unknown_endpoint_is_404(self) -> None:
        code, _payload = self.get("/v1/nope")
        self.assertEqual(code, 404)

    def test_token_is_enforced_when_configured(self) -> None:
        api = HttpApi(
            self.db,
            status_provider=lambda: (status(), time.time()),
            state_provider=lambda: "采集中",
            port=0,
            token="s3cret",
        )
        api.start()
        try:
            base = f"http://127.0.0.1:{api.port}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/v1/status", timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            request = urllib.request.Request(base + "/v1/status")
            request.add_header("Authorization", "Bearer s3cret")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            api.stop()


class ApiPayloadTestCase(unittest.TestCase):
    """The payload builders are also used by the socket ``query`` command."""

    def test_stale_status_is_labelled(self) -> None:
        payload = build_status_payload(status(), time.time() - 60, "采集中", INFO, None)
        self.assertTrue(payload["stale"])
        self.assertGreater(payload["age_s"], 50)

    def test_empty_status_still_answers(self) -> None:
        payload = build_status_payload(None, None, "未启动", None, None)
        self.assertIsNone(payload["power_w"])
        self.assertTrue(payload["stale"])

    def test_energy_and_history_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "energy.db"
            seed_database(db)
            store = EnergyStore(db, create=False)
            try:
                payload = energy_payload(store, "today")
                self.assertEqual(payload["range"], "today")
                self.assertGreater(payload["kwh"], 0.0)
                series = history_payload(store, 1, 300)
                expected = len(range(series["start"] // 300 * 300, series["end"], 300))
                self.assertEqual(len(series["points"]), expected)
                with self.assertRaises(ValueError):
                    history_payload(store, 1, 120)
                with self.assertRaises(ValueError):
                    energy_payload(store, "nonsense")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
