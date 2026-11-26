"""Energy accounting: SQLite store, trapezoidal integration and tariff engine.

Design notes
------------
* The PSU reports only instantaneous input power (W) on its own 1-3 s push
  interval, so energy has to be integrated host-side with the trapezoidal rule
  over **measured** timestamps (never a nominal period).
* A gap longer than ``gap_max`` seconds is not interpolated: it is recorded as
  unknown and surfaces as reduced coverage, the same way UGREEN's UPS panel and
  Home Assistant treat missing samples.  Missing time is never counted as 0 W.
* Minute buckets keep watt-seconds *and* the cost computed at write time, so
  day/month/year queries stay cheap.  ``recalc_costs`` rebuilds costs from those
  buckets whenever the tariff changes; the energy history itself is untouched.
* Energy unit is watt-seconds; kWh = ws / 3_600_000.  Costs are CNY cents
  (float) to avoid binary-float drift while summing.
"""

from __future__ import annotations

import calendar
import csv
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .protocol import PowerStatus

SCHEMA_VERSION = 2
GAP_MAX_S = 5.0
SESSION_GAP_S = 60.0
RAW_RETENTION_DAYS = 14
WS_PER_KWH = 3_600_000.0

DEFAULT_DB = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "himos-psu" / "energy.db"

# Example residential peak/valley plan (CNY per kWh).  Prices are illustrative
# only; the GUI labels this plan as an example and the CLI prints a warning.
CN_TOU_PRESET = (
    "0-7:谷:0.32,7-10:平:0.58,10-15:峰:0.92,"
    "15-18:平:0.58,18-21:尖:1.15,21-23:峰:0.92,23-24:谷:0.32"
)
# 北方冬季（采暖期）峰段缩短到 20:00、谷价更低；这里给的是示例值。
CN_TOU_PRESET_NORMAL = "0-8:谷:0.3769,8-22:峰:0.5769,22-24:谷:0.3769"
CN_TOU_PRESET_HEATING = "0-8:谷:0.3469,8-20:峰:0.5769,20-24:谷:0.3469"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    ts    INTEGER PRIMARY KEY,
    w     REAL NOT NULL,
    v     REAL,
    hz    REAL,
    t_hv  REAL,
    t_lv  REAL,
    t_env REAL,
    rpm   INTEGER,
    mode  INTEGER,
    fault INTEGER
);
CREATE TABLE IF NOT EXISTS energy_minute (
    ts         INTEGER PRIMARY KEY,
    ws         REAL NOT NULL DEFAULT 0,
    cov_s      REAL NOT NULL DEFAULT 0,
    n          INTEGER NOT NULL DEFAULT 0,
    gap_s      REAL NOT NULL DEFAULT 0,
    cost_cents REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    ws       REAL NOT NULL DEFAULT 0,
    peak_w   REAL NOT NULL DEFAULT 0,
    n        INTEGER NOT NULL DEFAULT 0,
    gap_s    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tariffs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    config         TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_ts     INTEGER NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
"""


# --------------------------------------------------------------------------- time helpers
def local_day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day).timestamp())


def local_day_end(day: date) -> int:
    return local_day_start(day + timedelta(days=1))


def minute_ts(ts: float) -> int:
    return int(ts) // 60 * 60


def _hm_to_minutes(text: str) -> int:
    parts = text.strip().split(":")
    if len(parts) == 1:
        hour, minute = int(parts[0]), 0
    elif len(parts) == 2:
        hour, minute = int(parts[0]), int(parts[1])
    else:
        raise ValueError(f"Invalid time {text!r}; expected HH:MM")
    if hour == 24 and minute == 0:
        return 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time {text!r}")
    return hour * 60 + minute


def _minutes_to_hm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


# --------------------------------------------------------------------------- tariff
PRICE_DECIMALS = 4  # 元/kWh; 山东居民一档 0.5469 这种四位小数必须原样保留


def _parse_monthday(text: str) -> tuple[int, int]:
    """Parse ``11-01`` / ``11.1`` / ``3/31`` into ``(month, day)``."""
    raw = text.strip().replace(".", "-").replace("/", "-")
    parts = raw.split("-")
    if len(parts) != 2:
        raise ValueError(f"日期格式应为 MM-DD，收到 {text!r}")
    month, day = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"月份超出范围: {text!r}")
    max_day = calendar.monthrange(2024, month)[1]  # 2024 是闰年，允许 02-29
    if not 1 <= day <= max_day:
        raise ValueError(f"{month} 月没有 {day} 日")
    return month, day


def _format_monthday(value: tuple[int, int]) -> str:
    return f"{value[0]:02d}-{value[1]:02d}"


@dataclass(frozen=True, slots=True)
class Band:
    """One price band of a time-of-use plan, covering [start_min, end_min)."""

    start_min: int
    end_min: int
    name: str
    price: float  # CNY per kWh

    @property
    def label(self) -> str:
        return f"{_minutes_to_hm(self.start_min)}-{_minutes_to_hm(self.end_min)}"

    def to_dict(self) -> dict[str, Any]:
        return {"start": _minutes_to_hm(self.start_min), "end": _minutes_to_hm(self.end_min),
                "name": self.name, "price": self.price}


@dataclass(frozen=True, slots=True)
class SeasonWindow:
    """A date window (month-day, may wrap the year end) that prices hours differently.

    Divide-by-season is the norm in northern China: the same 08:00-20:00 block is
    peak during the heating season and valley pricing shifts with it, so bands alone
    cannot express a plan.
    """

    label: str
    start: tuple[int, int]  # (month, day), inclusive
    end: tuple[int, int]    # (month, day), inclusive
    bands: tuple[Band, ...]

    def covers(self, when: date | datetime) -> bool:
        point = (when.month, when.day)
        if self.start <= self.end:
            return self.start <= point <= self.end
        # e.g. 11-01 .. 03-31 crosses New Year
        return point >= self.start or point <= self.end

    @property
    def range_label(self) -> str:
        return f"{_format_monthday(self.start)}..{_format_monthday(self.end)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": _format_monthday(self.start),
            "end": _format_monthday(self.end),
            "bands": [band.to_dict() for band in self.bands],
        }


@dataclass(frozen=True, slots=True)
class TariffPlan:
    """A named pricing plan: flat price, time bands, and optional seasonal bands."""

    name: str
    kind: str  # "fixed" | "tou"
    price: float = 0.0
    bands: tuple[Band, ...] = ()
    currency: str = "CNY"
    effective_from: str = ""
    plan_id: int | None = None
    season: SeasonWindow | None = None  # 采暖期/非采暖期 两套时段

    @property
    def is_tou(self) -> bool:
        return self.kind == "tou"

    def bands_for(self, ts: float) -> tuple[Band, ...]:
        """Bands in force at ``ts`` (the season window wins inside its dates)."""
        if not self.is_tou:
            return ()
        if self.season is not None and self.season.covers(datetime.fromtimestamp(ts)):
            return self.season.bands
        return self.bands

    def in_season(self, ts: float) -> bool:
        return self.season is not None and self.season.covers(datetime.fromtimestamp(ts))

    def price_at(self, ts: float) -> float:
        if not self.is_tou:
            return self.price
        minute = datetime.fromtimestamp(ts).hour * 60 + datetime.fromtimestamp(ts).minute
        for band in self.bands_for(ts):
            if band.start_min <= minute < band.end_min:
                return band.price
        return 0.0

    def band_name_at(self, ts: float) -> str:
        if not self.is_tou:
            return "统一电价"
        minute = datetime.fromtimestamp(ts).hour * 60 + datetime.fromtimestamp(ts).minute
        for band in self.bands_for(ts):
            if band.start_min <= minute < band.end_min:
                return band.name
        return "未定义"

    def to_config(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "price": self.price,
            "currency": self.currency,
            "bands": [band.to_dict() for band in self.bands],
            "season": self.season.to_dict() if self.season is not None else None,
        }

    def describe_lines(self) -> list[str]:
        """One line per billing season, for terminals that wrap long lines badly."""
        if not self.is_tou:
            return [f"统一 {self.price:.{PRICE_DECIMALS}f} 元/kWh"]
        if self.season is None:
            return [_bands_text(self.bands)]
        return [
            f"其余日期（非采暖期）: {_bands_text(self.bands)}",
            f"{self.season.label} {self.season.range_label}: {_bands_text(self.season.bands)}",
        ]

    def describe(self) -> str:
        if not self.is_tou:
            return f"{self.name}（统一 {self.price:.{PRICE_DECIMALS}f} 元/kWh）"
        base = _bands_text(self.bands)
        if self.season is None:
            return f"{self.name}（{base}）"
        return (
            f"{self.name}（非采暖期 {base}；"
            f"{self.season.label} {self.season.range_label} {_bands_text(self.season.bands)}）"
        )


def _bands_text(bands: Iterable[Band]) -> str:
    return ", ".join(f"{band.name} {band.label} {band.price:.{PRICE_DECIMALS}f}" for band in bands)


def _bands_from_config(items: Iterable[dict[str, Any]]) -> tuple[Band, ...]:
    return tuple(
        Band(_hm_to_minutes(item["start"]), _hm_to_minutes(item["end"]), item["name"], float(item["price"]))
        for item in items
    )


def parse_band_spec(spec: str) -> tuple[Band, ...]:
    """Parse ``0-7:谷:0.32,7-10:平:0.58,...`` into contiguous bands."""
    bands: list[Band] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            rng, name, price = chunk.split(":", 2)
            start_text, end_text = rng.split("-", 1)
        except ValueError as exc:
            raise ValueError(f"Bad band {chunk!r}; expected HH:MM-HH:MM:名称:单价") from exc
        bands.append(
            Band(
                start_min=_hm_to_minutes(start_text),
                end_min=_hm_to_minutes(end_text),
                name=name.strip(),
                price=float(price),
            )
        )
    return validate_bands(bands)


def validate_bands(bands: Iterable[Band]) -> tuple[Band, ...]:
    ordered = sorted(bands, key=lambda band: band.start_min)
    if not ordered:
        raise ValueError("峰谷方案至少需要一个时段")
    if ordered[0].start_min != 0:
        raise ValueError("时段必须从 00:00 开始")
    if ordered[-1].end_min != 1440:
        raise ValueError("时段必须覆盖到 24:00")
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.end_min != nxt.start_min:
            raise ValueError(f"时段不连续：{prev.label} 之后是 {nxt.label}")
    for band in ordered:
        if band.end_min <= band.start_min:
            raise ValueError(f"时段 {band.label} 的结束时间必须晚于开始时间")
        if band.price < 0:
            raise ValueError("电价不能为负数")
    return tuple(ordered)


def validate_season(season: SeasonWindow) -> SeasonWindow:
    """Reject a season window or band set that cannot be billed correctly."""
    if not season.label.strip():
        raise ValueError("季节名称不能为空")
    return SeasonWindow(
        label=season.label.strip(),
        start=season.start,
        end=season.end,
        bands=validate_bands(season.bands),
    )


def parse_monthday(text: str) -> tuple[int, int]:
    """Public wrapper for CLI/GUI input (``11-01``)."""
    return _parse_monthday(text)


def preset_plan(name: str = "示例：居民峰谷") -> TariffPlan:
    return TariffPlan(
        name=name,
        kind="tou",
        bands=parse_band_spec(CN_TOU_PRESET),
        effective_from=date.today().isoformat(),
    )


def seasonal_preset(name: str = "示例：居民峰谷（含采暖期）") -> TariffPlan:
    """Example plan shaped like a northern-China residential tariff (single tier).

    Prices are placeholders for the single-tier residential rate; tiered billing
    (第一/二/三档) is not modelled, so treat them as one reference tier.
    """
    return TariffPlan(
        name=name,
        kind="tou",
        bands=parse_band_spec(CN_TOU_PRESET_NORMAL),
        season=SeasonWindow(
            label="采暖期",
            start=(11, 1),
            end=(3, 31),
            bands=parse_band_spec(CN_TOU_PRESET_HEATING),
        ),
        effective_from=date.today().isoformat(),
    )


def fixed_plan(name: str, price: float) -> TariffPlan:
    if price < 0:
        raise ValueError("电价不能为负数")
    return TariffPlan(name=name, kind="fixed", price=float(price),
                      effective_from=date.today().isoformat())


# --------------------------------------------------------------------------- reports
@dataclass(slots=True)
class EnergyReport:
    label: str
    start_ts: int
    end_ts: int
    kwh: float = 0.0
    cost_cents: float = 0.0
    peak_w: float = 0.0
    avg_w: float = 0.0
    covered_s: float = 0.0
    gap_s: float = 0.0
    wall_s: float = 0.0
    samples: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    carbon_kg: float = 0.0

    @property
    def cost_yuan(self) -> float:
        return self.cost_cents / 100.0

    @property
    def coverage(self) -> float:
        return (self.covered_s / self.wall_s) if self.wall_s > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": datetime.fromtimestamp(self.start_ts).isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(self.end_ts).isoformat(timespec="seconds"),
            "kwh": round(self.kwh, 4),
            "cost_yuan": round(self.cost_yuan, 3),
            "peak_w": round(self.peak_w, 1),
            "avg_w": round(self.avg_w, 1),
            "coverage": round(self.coverage, 4),
            "gap_s": round(self.gap_s, 1),
            # Coverage is measured between the first and last recorded minute, which
            # is narrower than the requested range whenever collection started late.
            "first_sample_at": (
                datetime.fromtimestamp(self.first_ts).isoformat(timespec="seconds")
                if self.first_ts
                else None
            ),
            "last_sample_at": (
                datetime.fromtimestamp(self.last_ts).isoformat(timespec="seconds")
                if self.last_ts
                else None
            ),
            "samples": self.samples,
            "carbon_kg": round(self.carbon_kg, 3),
        }


# --------------------------------------------------------------------------- store
class EnergyStore:
    """SQLite-backed energy history.  One connection per process/thread."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        create: bool = True,
        gap_max: float = GAP_MAX_S,
        session_gap: float = SESSION_GAP_S,
    ) -> None:
        self.path = Path(path or os.environ.get("HIMOS_PSU_DB") or DEFAULT_DB)
        self.gap_max = float(gap_max)
        self.session_gap = float(session_gap)
        self._last_ts: float | None = None
        self._last_w: float = 0.0
        self._session_id: int | None = None
        self._plan: TariffPlan | None = None
        self._plan_minute: int | None = None
        self._closed = False

        if not create:
            if not self.path.is_file():
                raise FileNotFoundError(f"能耗数据库不存在：{self.path}")
            uri = f"file:{self.path}?mode=ro"
            # check_same_thread=False: the daemon builds the store on its main thread
            # and the collector thread writes through it; SQLite serialises internally.
            self._conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            with self._conn:
                self._conn.executescript(SCHEMA)
                self._migrate()
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
            if self.active_plan() is None:
                self.set_plan(preset_plan())

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(energy_minute)")}
        if "cov_s" not in columns:  # v1 databases only stored ws/n/gap
            self._conn.execute("ALTER TABLE energy_minute ADD COLUMN cov_s REAL NOT NULL DEFAULT 0")

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if not self._closed:
            self.close_session()
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "EnergyStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- plan --------------------------------------------------------------
    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> TariffPlan:
        config = json.loads(row["config"])
        season = None
        raw_season = config.get("season")
        if raw_season:
            season = SeasonWindow(
                label=raw_season.get("label", "采暖期"),
                start=_parse_monthday(raw_season["start"]),
                end=_parse_monthday(raw_season["end"]),
                bands=_bands_from_config(raw_season.get("bands", [])),
            )
        return TariffPlan(
            name=row["name"],
            kind=config.get("kind", "fixed"),
            price=float(config.get("price", 0.0)),
            bands=_bands_from_config(config.get("bands", [])),
            currency=config.get("currency", "CNY"),
            effective_from=row["effective_from"],
            plan_id=int(row["id"]),
            season=season,
        )

    def active_plan(self) -> TariffPlan | None:
        row = self._conn.execute(
            "SELECT * FROM tariffs WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._plan_from_row(row)

    def set_plan(self, plan: TariffPlan) -> TariffPlan:
        if plan.kind not in ("fixed", "tou"):
            raise ValueError("kind 必须是 fixed 或 tou")
        if plan.kind == "tou":
            plan = TariffPlan(
                name=plan.name, kind=plan.kind, price=plan.price,
                bands=validate_bands(plan.bands), currency=plan.currency,
                effective_from=plan.effective_from or date.today().isoformat(),
                season=validate_season(plan.season) if plan.season is not None else None,
            )
        else:
            if plan.season is not None:
                raise ValueError("固定电价方案不支持采暖期时段")
            if plan.price < 0:
                raise ValueError("电价不能为负数")
            plan = TariffPlan(
                name=plan.name, kind=plan.kind, price=float(plan.price),
                currency=plan.currency,
                effective_from=plan.effective_from or date.today().isoformat(),
            )
        with self._conn:
            self._conn.execute("UPDATE tariffs SET active = 0 WHERE active = 1")
            cur = self._conn.execute(
                "INSERT INTO tariffs(name, kind, config, effective_from, created_ts, active) VALUES(?,?,?,?,?,1)",
                (
                    plan.name,
                    plan.kind,
                    json.dumps(plan.to_config(), ensure_ascii=False),
                    plan.effective_from or date.today().isoformat(),
                    int(time.time()),
                ),
            )
            plan_id = int(cur.lastrowid)
        stored = TariffPlan(
            name=plan.name, kind=plan.kind, price=plan.price, bands=plan.bands,
            currency=plan.currency, effective_from=plan.effective_from, plan_id=plan_id,
            season=plan.season,
        )
        self._plan = stored
        self._plan_minute = None
        return stored

    def plans(self) -> list[TariffPlan]:
        rows = self._conn.execute("SELECT * FROM tariffs ORDER BY id DESC").fetchall()
        return [self._plan_from_row(row) for row in rows]

    def _current_plan(self) -> TariffPlan:
        if self._plan is None:
            self._plan = self.active_plan() or preset_plan()
        return self._plan

    def invalidate_plan(self) -> None:
        """Drop the cached tariff so the next call re-reads the active row.

        The GUI edits tariffs through a second connection, so without this the
        tab kept billing with the previous plan (a fixed plan still showed 峰/谷).
        """
        self._plan = None
        self._plan_minute = None

    def _refresh_plan_if_new_minute(self, ts: float) -> None:
        bucket = minute_ts(ts)
        if self._plan_minute == bucket:
            return
        self._plan_minute = bucket
        fresh = self.active_plan()
        if fresh is not None:
            self._plan = fresh

    # -- meta --------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    @property
    def carbon_factor(self) -> float:
        raw = self.get_meta("carbon_factor", "0") or "0"
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def set_carbon_factor(self, value: float) -> None:
        self.set_meta("carbon_factor", f"{max(0.0, float(value)):.6f}")

    # -- ingestion ---------------------------------------------------------
    def add_sample(self, ts: float, status: PowerStatus) -> tuple[float, bool]:
        """Record one telemetry sample; return (watt_seconds added, gap detected)."""
        stamp = float(ts)
        ts_i = int(stamp)
        power = float(status.power)
        added = 0.0
        gap = False
        self._refresh_plan_if_new_minute(stamp)

        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO samples(ts, w, v, hz, t_hv, t_lv, t_env, rpm, mode, fault)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ts_i, power, status.input_voltage, status.frequency,
                    status.high_voltage_temperature, status.low_voltage_temperature,
                    status.environment_temperature, status.fan_speed,
                    int(status.mode), status.malfunction_code,
                ),
            )
            if self._last_ts is None:
                self._session_id = self._open_session(ts_i, power)
            else:
                dt = stamp - self._last_ts
                if dt <= 0:
                    pass
                elif dt <= self.gap_max:
                    ws = (self._last_w + power) / 2.0 * dt
                    price = self._current_plan().price_at(stamp)
                    cost = ws / WS_PER_KWH * 100.0 * price
                    self._conn.execute(
                        "INSERT INTO energy_minute(ts, ws, cov_s, n, gap_s, cost_cents) VALUES(?,?,?,1,0,?)"
                        " ON CONFLICT(ts) DO UPDATE SET"
                        " ws = ws + excluded.ws, cov_s = cov_s + excluded.cov_s, n = n + 1,"
                        " cost_cents = cost_cents + excluded.cost_cents",
                        (minute_ts(stamp), ws, dt, cost),
                    )
                    if self._session_id is not None:
                        self._conn.execute(
                            "UPDATE sessions SET end_ts = ?, ws = ws + ?, peak_w = MAX(peak_w, ?), n = n + 1"
                            " WHERE id = ?",
                            (ts_i, ws, power, self._session_id),
                        )
                    added = ws
                else:
                    self._conn.execute(
                        "INSERT INTO energy_minute(ts, ws, n, gap_s, cost_cents) VALUES(?,0,0,?,0)"
                        " ON CONFLICT(ts) DO UPDATE SET gap_s = gap_s + excluded.gap_s",
                        (minute_ts(stamp), dt),
                    )
                    gap = True
                    if dt > self.session_gap:
                        self._session_id = self._open_session(ts_i, power)

        self._last_ts = stamp
        self._last_w = power
        return added, gap

    def _open_session(self, ts_i: int, power: float) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions(start_ts, end_ts, ws, peak_w, n) VALUES(?,?,0,?,1)", (ts_i, ts_i, power)
        )
        return int(cur.lastrowid)

    def close_session(self) -> None:
        if self._session_id is None or self._closed:
            return
        end = int(self._last_ts or time.time())
        with self._conn:
            self._conn.execute("UPDATE sessions SET end_ts = MAX(end_ts, ?) WHERE id = ?", (end, self._session_id))
        self._session_id = None

    def prune_raw(self, days: int = RAW_RETENTION_DAYS, now: float | None = None) -> int:
        cutoff = int((now if now is not None else time.time()) - days * 86400)
        with self._conn:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        return int(cur.rowcount or 0)

    # -- queries -----------------------------------------------------------
    def _range_totals(self, start_ts: int, end_ts: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(ws), 0) AS ws, COALESCE(SUM(cov_s), 0) AS cov,"
            " COALESCE(SUM(cost_cents), 0) AS cents,"
            " COALESCE(SUM(gap_s), 0) AS gaps, COALESCE(SUM(n), 0) AS n,"
            " MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            " FROM energy_minute WHERE ts >= ? AND ts < ?",
            (int(start_ts), int(end_ts)),
        ).fetchone()
        return dict(row) if row else {}

    def report(self, label: str, start_ts: int, end_ts: int) -> EnergyReport:
        totals = self._range_totals(start_ts, end_ts)
        first = totals.get("first_ts")
        last = totals.get("last_ts")
        report = EnergyReport(label=label, start_ts=int(start_ts), end_ts=int(end_ts))
        if first is None or last is None:
            return report
        span_start = max(int(start_ts), int(first))
        span_end = min(int(end_ts), int(last) + 60)
        wall = max(0, span_end - span_start)
        gaps = float(totals.get("gaps") or 0.0)
        covered = min(float(wall), float(totals.get("cov") or 0.0))
        ws = float(totals.get("ws") or 0.0)
        session_peak = self._conn.execute(
            "SELECT COALESCE(MAX(peak_w), 0) AS peak FROM sessions WHERE end_ts > ? AND start_ts < ?",
            (int(start_ts), int(end_ts)),
        ).fetchone()["peak"]
        minute_peak = self._conn.execute(
            "SELECT COALESCE(MAX(CASE WHEN cov_s > 0 THEN ws / cov_s ELSE 0 END), 0) AS peak"
            " FROM energy_minute WHERE ts >= ? AND ts < ?",
            (int(start_ts), int(end_ts)),
        ).fetchone()["peak"]
        report.kwh = ws / WS_PER_KWH
        report.cost_cents = float(totals.get("cents") or 0.0)
        report.peak_w = max(float(session_peak or 0.0), float(minute_peak or 0.0))
        report.avg_w = (ws / covered) if covered > 0 else 0.0
        report.covered_s = covered
        report.gap_s = gaps
        report.wall_s = float(wall)
        report.samples = int(totals.get("n") or 0)
        report.first_ts = int(first)
        report.last_ts = int(last)
        factor = self.carbon_factor
        report.carbon_kg = report.kwh * factor if factor > 0 else 0.0
        return report

    def band_breakdown(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        # Read the active plan every time: a cached one survives a tariff edit made
        # in another connection and would keep splitting by stale 峰/谷 bands.
        plan = self.active_plan() or self._current_plan()
        buckets: dict[str, dict[str, float]] = {}
        for row in self._conn.execute(
            "SELECT ts, ws FROM energy_minute WHERE ts >= ? AND ts < ? AND ws > 0", (int(start_ts), int(end_ts))
        ):
            stamp = float(row["ts"])
            name = plan.band_name_at(stamp)
            price = plan.price_at(stamp)
            item = buckets.setdefault(name, {"ws": 0.0, "cents": 0.0, "price": price})
            item["ws"] += float(row["ws"])
            item["cents"] += float(row["ws"]) / WS_PER_KWH * 100.0 * price
        total_ws = sum(item["ws"] for item in buckets.values()) or 1.0
        order: list[str] = []
        if plan.is_tou:
            for band in plan.bands:
                if band.name not in order:
                    order.append(band.name)
        else:
            order = ["统一电价"]
        result: list[dict[str, Any]] = []
        for name in order + [n for n in buckets if n not in order]:
            item = buckets.get(name)
            if not item:
                continue
            result.append(
                {
                    "name": name,
                    "kwh": item["ws"] / WS_PER_KWH,
                    "cost_yuan": item["cents"] / 100.0,
                    "price": item["price"],
                    "share": item["ws"] / total_ws,
                }
            )
        return result

    def hourly_watts(self, day: date) -> list[float | None]:
        start, end = local_day_start(day), local_day_end(day)
        sums = [0.0] * 24
        covered = [0.0] * 24
        for row in self._conn.execute(
            "SELECT ts, ws, cov_s FROM energy_minute WHERE ts >= ? AND ts < ?", (start, end)
        ):
            index = min(23, (int(row["ts"]) - start) // 3600)
            sums[index] += float(row["ws"])
            covered[index] += float(row["cov_s"])
        values: list[float | None] = []
        for hour in range(24):
            if covered[hour] < 5.0:  # too little sampled time to average honestly
                values.append(None)
                continue
            values.append(sums[hour] / covered[hour])
        return values

    def daily_series(self, start_day: date, end_day: date) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS d, SUM(ws) AS ws, SUM(cost_cents) AS cents"
            " FROM energy_minute WHERE ts >= ? AND ts < ? GROUP BY d ORDER BY d",
            (local_day_start(start_day), local_day_end(end_day)),
        ).fetchall()
        found = {row["d"]: row for row in rows}
        series: list[dict[str, Any]] = []
        cursor = start_day
        while cursor <= end_day:
            key = cursor.isoformat()
            row = found.get(key)
            series.append(
                {
                    "date": cursor,
                    "kwh": (float(row["ws"]) / WS_PER_KWH) if row else 0.0,
                    "cost_yuan": (float(row["cents"]) / 100.0) if row else 0.0,
                }
            )
            cursor += timedelta(days=1)
        return series

    def calendar(self, year: int, month: int) -> dict[int, float]:
        first = date(year, month, 1)
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        rows = self._conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS d, SUM(ws) AS ws"
            " FROM energy_minute WHERE ts >= ? AND ts < ? GROUP BY d",
            (local_day_start(first), local_day_start(nxt)),
        ).fetchall()
        result: dict[int, float] = {}
        for row in rows:
            if not row["d"]:
                continue
            result[int(row["d"][-2:])] = float(row["ws"]) / WS_PER_KWH
        return result

    def active_seconds(self, start_ts: int, end_ts: int, threshold_w: float = 20.0) -> float:
        """Seconds whose average power was above ``threshold_w`` (i.e. machine really on)."""
        total = 0.0
        for row in self._conn.execute(
            "SELECT ws, cov_s FROM energy_minute WHERE ts >= ? AND ts < ?", (int(start_ts), int(end_ts))
        ):
            covered = float(row["cov_s"])
            if covered <= 0:
                continue
            if float(row["ws"]) / covered >= threshold_w:
                total += covered
        return total

    def load_distribution(self, start_ts: int, end_ts: int, rated_w: float) -> list[dict[str, Any]]:
        labels = ["<20%", "20-40%", "40-60%", "60-80%", ">80%"]
        seconds = [0.0] * 5
        rated = rated_w if rated_w > 0 else 1.0
        for row in self._conn.execute(
            "SELECT ts, ws, cov_s FROM energy_minute WHERE ts >= ? AND ts < ?", (int(start_ts), int(end_ts))
        ):
            effective = float(row["cov_s"])
            if effective <= 0:
                continue
            watts = float(row["ws"]) / effective
            ratio = watts / rated
            if ratio < 0.2:
                seconds[0] += effective
            elif ratio < 0.4:
                seconds[1] += effective
            elif ratio < 0.6:
                seconds[2] += effective
            elif ratio < 0.8:
                seconds[3] += effective
            else:
                seconds[4] += effective
        total = sum(seconds)
        return [
            {"label": label, "seconds": sec, "share": (sec / total) if total else 0.0}
            for label, sec in zip(labels, seconds)
        ]

    def recent_sessions(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY start_ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            duration = max(0, int(row["end_ts"]) - int(row["start_ts"]))
            out.append(
                {
                    "start_ts": int(row["start_ts"]),
                    "end_ts": int(row["end_ts"]),
                    "duration_s": duration,
                    "kwh": float(row["ws"]) / WS_PER_KWH,
                    "peak_w": float(row["peak_w"]),
                }
            )
        return out

    def first_ts(self) -> int | None:
        row = self._conn.execute("SELECT MIN(ts) AS ts FROM energy_minute").fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def recorded_days(self) -> int:
        """Distinct calendar days that actually contain energy data."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT date(ts, 'unixepoch', 'localtime')) AS n"
            " FROM energy_minute WHERE ws > 0"
        ).fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0

    def last_sample_ts(self) -> int | None:
        row = self._conn.execute("SELECT MAX(ts) AS ts FROM samples").fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def sample_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
        return int(row["n"])

    def db_size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def recalc_costs(self) -> int:
        plan = self._current_plan()
        rows = self._conn.execute("SELECT ts, ws FROM energy_minute").fetchall()
        updates = [
            (float(row["ws"]) / WS_PER_KWH * 100.0 * plan.price_at(float(row["ts"])), int(row["ts"]))
            for row in rows
        ]
        with self._conn:
            self._conn.executemany("UPDATE energy_minute SET cost_cents = ? WHERE ts = ?", updates)
        return len(updates)

    # -- export ------------------------------------------------------------
    def export_csv(self, kind: str, target: str, start_ts: int | None = None, end_ts: int | None = None) -> int:
        """Write CSV rows; ``target`` may be '-' for stdout.  Returns row count."""
        start_ts = int(start_ts if start_ts is not None else 0)
        end_ts = int(end_ts if end_ts is not None else time.time() + 86400)
        handle = None
        close_after = False
        if target == "-":
            import sys

            handle = sys.stdout
        else:
            handle = open(target, "w", newline="", encoding="utf-8")
            close_after = True
        try:
            writer = csv.writer(handle)
            count = 0
            if kind == "minute":
                writer.writerow(["local_time", "kwh", "cost_yuan", "covered_s", "gap_s"])
                for row in self._conn.execute(
                    "SELECT ts, ws, cost_cents, cov_s, gap_s FROM energy_minute WHERE ts >= ? AND ts < ? ORDER BY ts",
                    (start_ts, end_ts),
                ):
                    writer.writerow([
                        datetime.fromtimestamp(int(row["ts"])).strftime("%Y-%m-%d %H:%M"),
                        f"{float(row['ws']) / WS_PER_KWH:.6f}",
                        f"{float(row['cost_cents']) / 100.0:.4f}",
                        f"{float(row['cov_s']):.1f}",
                        f"{float(row['gap_s']):.1f}",
                    ])
                    count += 1
            elif kind == "daily":
                writer.writerow(["date", "kwh", "cost_yuan"])
                rows = self._conn.execute(
                    "SELECT date(ts, 'unixepoch', 'localtime') AS d, SUM(ws) AS ws, SUM(cost_cents) AS cents"
                    " FROM energy_minute WHERE ts >= ? AND ts < ? GROUP BY d ORDER BY d",
                    (start_ts, end_ts),
                ).fetchall()
                for row in rows:
                    writer.writerow([row["d"], f"{float(row['ws']) / WS_PER_KWH:.4f}",
                                     f"{float(row['cents']) / 100.0:.4f}"])
                    count += 1
            elif kind == "samples":
                writer.writerow(["local_time", "power_w", "voltage_v", "freq_hz", "t_hv_c", "t_lv_c",
                                 "t_env_c", "fan_rpm", "mode", "fault"])
                for row in self._conn.execute(
                    "SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ts, end_ts)
                ):
                    writer.writerow([
                        datetime.fromtimestamp(int(row["ts"])).strftime("%Y-%m-%d %H:%M:%S"),
                        f"{float(row['w']):.2f}", row["v"], row["hz"], row["t_hv"], row["t_lv"],
                        row["t_env"], row["rpm"], row["mode"], f"0x{int(row['fault'] or 0):04x}",
                    ])
                    count += 1
            elif kind == "sessions":
                writer.writerow(["start", "end", "duration_min", "kwh", "peak_w"])
                for row in self._conn.execute(
                    "SELECT * FROM sessions WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts", (start_ts, end_ts)
                ):
                    writer.writerow([
                        datetime.fromtimestamp(int(row["start_ts"])).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(int(row["end_ts"])).strftime("%Y-%m-%d %H:%M"),
                        f"{(int(row['end_ts']) - int(row['start_ts'])) / 60.0:.1f}",
                        f"{float(row['ws']) / WS_PER_KWH:.4f}",
                        f"{float(row['peak_w']):.0f}",
                    ])
                    count += 1
            else:
                raise ValueError(f"未知导出类型 {kind!r}（可选 minute/daily/samples/sessions）")
            return count
        finally:
            if close_after and handle is not None:
                handle.close()


# --------------------------------------------------------------------------- formatting
def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def format_kwh(value: float) -> str:
    if value >= 100:
        return f"{value:.1f}"
    if value >= 10:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.3f}"
    return f"{value:.4f}"


def format_yuan(cents: float) -> str:
    return f"¥{cents / 100.0:.2f}"
