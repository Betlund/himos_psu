"""Read-only HTTP/JSON API served by the collector daemon.

The daemon owns the serial port, so everything that wants live numbers has to ask
it. Until now that meant either the CLI (which fights for the port) or the raw
frame IPC (which every consumer would have to decode). This module exposes the
decoded values over loopback HTTP for optimizers, dashboards and shell scripts:

    GET /v1/status    latest power/voltage/temperature/fan snapshot + today totals
    GET /v1/history   power series, default 24 h at 5 minute steps
    GET /v1/energy    today/yesterday/week/month/year totals and cost
    GET /v1/meta      model, firmware, rated power (serial number is opt-in)
    GET /v1/health    liveness + data staleness
    GET /metrics      Prometheus text exposition

Design rules:

* Read-only. No endpoint mutates the PSU, the database or the daemon.
* Loopback only. The socket binds 127.0.0.1; an optional bearer token can be
  required for hostile multi-user machines.
* Stale data is labelled, never silently served as current: every status carries
  ``age_s`` and ``stale``.
* ``/v1`` is frozen: fields may be added, never renamed or removed.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .energy import (
    EnergyStore,
    local_day_end,
    local_day_start,
    format_kwh,
)
from .protocol import PowerInfo, PowerStatus

SCHEMA_VERSION = 1
DEFAULT_HTTP_PORT = 8790
ALLOWED_STEPS = (60, 300, 900, 3600)
MAX_HISTORY_HOURS = 24 * 14  # raw samples are kept 14 days; minute buckets are older
STALE_AFTER_S = 5.0
ENERGY_CACHE_S = 2.0

RANGE_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "week": "本周",
    "month": "本月",
    "year": "今年",
}


def range_bounds(name: str, today: date | None = None) -> tuple[int, int]:
    """Start/end epoch seconds for a named range (local time)."""
    key = (name or "today").lower()
    if key not in RANGE_LABELS:
        raise ValueError(f"未知区间 {name!r}，可选 {', '.join(RANGE_LABELS)}")
    current = today or date.today()
    if key == "today":
        start = local_day_start(current)
    elif key == "yesterday":
        start = local_day_start(current - timedelta(days=1))
    elif key == "week":
        start = local_day_start(current - timedelta(days=current.weekday()))
    elif key == "month":
        start = local_day_start(current.replace(day=1))
    else:
        start = local_day_start(current.replace(month=1, day=1))
    end = min(local_day_end(current), int(time.time()))
    return start, end


def build_status_payload(
    status: PowerStatus | None,
    received_at: float | None,
    state: str,
    info: PowerInfo | None,
    energy: dict[str, Any] | None,
) -> dict[str, Any]:
    now = time.time()
    age = (now - received_at) if received_at else None
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ts": round(received_at, 3) if received_at else None,
        "age_s": round(age, 3) if age is not None else None,
        "stale": (age is None) or (age > STALE_AFTER_S),
        "state": state,
    }
    if status is None:
        payload["power_w"] = None
        return payload
    payload.update(
        {
            "power_w": status.power,
            "input_voltage_v": status.input_voltage,
            "frequency_hz": status.frequency,
            "rails_v": {
                "12": status.voltage_12v,
                "5": status.voltage_5v,
                "3.3": status.voltage_3v3,
                "5vsb": status.voltage_5v_sb,
            },
            "temps_c": {
                "hv": status.high_voltage_temperature,
                "lv": status.low_voltage_temperature,
                "env": status.environment_temperature,
            },
            "fan_rpm": status.fan_speed,
            "fan_mode": status.mode.name.lower(),
            "fan_mode_id": int(status.mode),
            "fault_code": status.malfunction_code,
        }
    )
    if info is not None:
        payload["unit"] = {
            "model": info.model,
            "firmware": info.firmware_display,
            "vendor": info.manufacture or None,
            "rated_power_w": info.rated_power,
        }
    if energy is not None:
        payload["energy"] = energy
    return payload


def energy_payload(store: EnergyStore, range_name: str, with_bands: bool = False) -> dict[str, Any]:
    """Totals for a named range, straight from the energy database."""
    key = (range_name or "today").lower()
    start, end = range_bounds(key)
    report = store.report(RANGE_LABELS[key], start, end)
    payload = _energy_payload(report, key)
    payload["schema"] = SCHEMA_VERSION
    if with_bands:
        payload["bands"] = store.band_breakdown(start, end)
    return payload


def history_payload(store: EnergyStore, hours: float, step: int) -> dict[str, Any]:
    """Power series over the last ``hours`` in fixed ``step`` buckets."""
    if step not in ALLOWED_STEPS:
        raise ValueError(f"step 必须是 {', '.join(str(s) for s in ALLOWED_STEPS)} 之一")
    if hours <= 0 or hours > MAX_HISTORY_HOURS:
        raise ValueError(f"hours 必须在 0–{MAX_HISTORY_HOURS} 之间")
    end = int(time.time())
    start = end - int(hours * 3600)
    series = store.power_series(start, end, step)
    return {
        "schema": SCHEMA_VERSION,
        "step_s": step,
        "unit": "W",
        "start": start,
        "end": end,
        "points": [
            {"ts": int(item["ts"]), "avg": _round(item["avg"]), "min": _round(item["min"]),
             "max": _round(item["max"])}
            for item in series
        ],
    }


def _energy_payload(report: Any, label: str) -> dict[str, Any]:
    return {
        "range": label,
        "kwh": round(report.kwh, 4),
        "cost_yuan": round(report.cost_yuan, 3),
        "avg_w": round(report.avg_w, 1),
        "peak_w": round(report.peak_w, 1),
        "coverage": round(report.coverage, 4),
        "gap_s": round(report.gap_s, 1),
        "runtime_s": None,
        "start": datetime.fromtimestamp(report.start_ts).isoformat(timespec="seconds"),
        "end": datetime.fromtimestamp(report.end_ts).isoformat(timespec="seconds"),
        "first_sample_at": (
            datetime.fromtimestamp(report.first_ts).isoformat(timespec="seconds")
            if report.first_ts
            else None
        ),
    }


class HttpApi:
    """Loopback HTTP server over the daemon's live state plus the energy database."""

    def __init__(
        self,
        db_path: str | Path,
        status_provider: Callable[[], tuple[PowerStatus | None, float | None]],
        state_provider: Callable[[], str],
        info_provider: Callable[[], PowerInfo | None] | None = None,
        sn_provider: Callable[[], str] | None = None,
        *,
        port: int = DEFAULT_HTTP_PORT,
        token: str | None = None,
        host: str = "127.0.0.1",
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._status_provider = status_provider
        self._state_provider = state_provider
        self._info_provider = info_provider or (lambda: None)
        self._sn_provider = sn_provider or (lambda: "")
        self.host = host
        self.requested_port = int(port)
        self.token = token or None
        self._log = on_log or (lambda text: None)
        self._store: EnergyStore | None = None
        self._lock = threading.Lock()
        self._energy_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = self.requested_port

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        try:
            self._store = EnergyStore(self.db_path, create=False)
        except Exception:
            self._store = None  # live status still works, history answers 503
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.requested_port), handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="himos-http", daemon=True
        )
        self._thread.start()
        self._log(f"HTTP API 监听 http://{self.host}:{self.port}/v1/status")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._store is not None:
            self._store.close()
            self._store = None

    # -- data --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        status, received_at = self._status_provider()
        energy = self._energy_summary()
        return build_status_payload(status, received_at, self._state_provider(), self._info_provider(), energy)

    def _energy_summary(self) -> dict[str, Any] | None:
        if self._store is None:
            return None
        cached = self._energy_cache.get("status")
        now = time.time()
        if cached and now - cached[0] < ENERGY_CACHE_S:
            return cached[1]
        try:
            with self._lock:
                start, end = range_bounds("today")
                today = self._store.report("今日", start, end)
                month_start, _ = range_bounds("month")
                month = self._store.report("本月", month_start, end)
                first = self._store.recording_start_ts()
                total_start = local_day_start(datetime.fromtimestamp(first).date()) if first else start
                total = self._store.report("累计", total_start, end)
        except Exception:
            return None
        summary = {
            "today_kwh": round(today.kwh, 4),
            "today_cost_yuan": round(today.cost_yuan, 3),
            "month_kwh": round(month.kwh, 4),
            "total_kwh": round(total.kwh, 4),
            "recorded_days": self._store.recorded_days() if self._store else None,
        }
        self._energy_cache["status"] = (now, summary)
        return summary

    def history(self, hours: float, step: int) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("能耗数据库不可用")
        with self._lock:
            return history_payload(self._store, hours, step)

    def energy(self, range_name: str, with_bands: bool = False) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("能耗数据库不可用")
        with self._lock:
            return energy_payload(self._store, range_name, with_bands)

    def meta(self, include_sn: bool = False) -> dict[str, Any]:
        info = self._info_provider()
        status, received_at = self._status_provider()
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "state": self._state_provider(),
            "stale": (time.time() - received_at) > STALE_AFTER_S if received_at else True,
            "http": {"host": self.host, "port": self.port, "auth": bool(self.token)},
            "units": {"power": "W", "energy": "kWh", "cost": "CNY", "temperature": "°C"},
        }
        if info is not None:
            payload.update(
                {
                    "model": info.model,
                    "firmware": info.firmware_display,
                    "vendor": info.manufacture or None,
                    "rated_power_w": info.rated_power,
                    "certification_80plus": info.certification_80plus,
                }
            )
        if include_sn:
            payload["sn"] = self._sn_provider() or None
        if status is not None:
            payload["fan_mode"] = status.mode.name.lower()
        return payload

    def health(self) -> dict[str, Any]:
        status, received_at = self._status_provider()
        age = (time.time() - received_at) if received_at else None
        return {
            "schema": SCHEMA_VERSION,
            "ok": status is not None,
            "state": self._state_provider(),
            "age_s": round(age, 3) if age is not None else None,
            "stale": (age is None) or (age > STALE_AFTER_S),
            "database": self._store is not None,
        }

    def metrics(self) -> str:
        status, received_at = self._status_provider()
        lines = [
            "# HELP himos_psu_stale 1 when the latest sample is older than the stale window",
            "# TYPE himos_psu_stale gauge",
        ]
        age = (time.time() - received_at) if received_at else None
        stale = 1 if (age is None or age > STALE_AFTER_S) else 0
        lines.append(f"himos_psu_stale {stale}")
        if status is not None:
            lines += [
                "# HELP himos_psu_power_watts PSU input power",
                "# TYPE himos_psu_power_watts gauge",
                f"himos_psu_power_watts {status.power}",
                "# TYPE himos_psu_input_voltage_volts gauge",
                f"himos_psu_input_voltage_volts {status.input_voltage}",
                "# TYPE himos_psu_frequency_hertz gauge",
                f"himos_psu_frequency_hertz {status.frequency}",
                "# TYPE himos_psu_fan_rpm gauge",
                f"himos_psu_fan_rpm {status.fan_speed}",
                "# TYPE himos_psu_temp_celsius gauge",
                f'himos_psu_temp_celsius{{sensor="hv"}} {status.high_voltage_temperature}',
                f'himos_psu_temp_celsius{{sensor="lv"}} {status.low_voltage_temperature}',
                f'himos_psu_temp_celsius{{sensor="env"}} {status.environment_temperature}',
                "# TYPE himos_psu_fault_code gauge",
                f"himos_psu_fault_code {status.malfunction_code}",
            ]
        summary = self._energy_summary()
        if summary:
            lines += [
                "# TYPE himos_psu_energy_kwh gauge",
                f'himos_psu_energy_kwh{{range="today"}} {summary["today_kwh"]}',
                f'himos_psu_energy_kwh{{range="month"}} {summary["month_kwh"]}',
                f'himos_psu_energy_kwh{{range="total"}} {summary["total_kwh"]}',
                "# TYPE himos_psu_energy_cost_yuan gauge",
                f'himos_psu_energy_cost_yuan{{range="today"}} {summary["today_cost_yuan"]}',
            ]
        return "\n".join(lines) + "\n"

    # -- helpers -----------------------------------------------------------
    def authorized(self, header: str | None) -> bool:
        if not self.token:
            return True
        if not header:
            return False
        header = header.strip()
        if header.lower().startswith("bearer "):
            header = header[7:].strip()
        return header == self.token


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _make_handler(api: HttpApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "himos-psu-api"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the journal quiet
            del fmt, args

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", status)

        def _fail(self, message: str, status: int = 400) -> None:
            self._json({"error": message, "status": status}, status)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            if not api.authorized(self.headers.get("Authorization")):
                self._fail("需要 Authorization: Bearer <token>", 401)
                return
            try:
                if path == "/v1/status":
                    self._json(api.status())
                elif path == "/v1/history":
                    hours = float(query.get("hours", ["24"])[0])
                    step = int(query.get("step", ["300"])[0])
                    self._json(api.history(hours, step))
                elif path == "/v1/energy":
                    name = query.get("range", ["today"])[0]
                    bands = query.get("bands", ["0"])[0] in ("1", "true", "yes")
                    self._json(api.energy(name, bands))
                elif path == "/v1/meta":
                    sn = query.get("sn", ["0"])[0] in ("1", "true", "yes")
                    self._json(api.meta(sn))
                elif path == "/v1/health":
                    self._json(api.health())
                elif path == "/metrics":
                    self._send(api.metrics().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8")
                elif path == "/":
                    self._json({
                        "service": "himos-psu-api",
                        "schema": SCHEMA_VERSION,
                        "endpoints": ["/v1/status", "/v1/history", "/v1/energy",
                                      "/v1/meta", "/v1/health", "/metrics"],
                    })
                else:
                    self._fail(f"未知端点 {path}", 404)
            except ValueError as exc:
                self._fail(str(exc), 400)
            except RuntimeError as exc:
                self._fail(str(exc), 503)
            except Exception as exc:  # never let a request kill the server thread
                self._fail(f"内部错误: {exc}", 500)

    return Handler


def format_summary_line(status: dict[str, Any]) -> str:
    """One-line rendering used by the CLI and docs examples."""
    if status.get("power_w") is None:
        return "无数据"
    parts = [f"{status['power_w']:.1f} W", f"{status['input_voltage_v']:.1f} V"]
    energy = status.get("energy") or {}
    if "today_kwh" in energy:
        parts.append(f"今日 {format_kwh(energy['today_kwh'])} kWh")
    if status.get("stale"):
        parts.append("(数据过期)")
    return " · ".join(parts)
