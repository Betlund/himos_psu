"""Persistent user configuration and safe on-connect PSU profile restoration."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .fan_curve import Point, validate_fan_curve
from .protocol import (
    FanMode,
    PowerStatus,
    RefreshMode,
    build_fan_curve_frame,
    build_fan_mode_frame,
    build_refresh_mode_frame,
)

if TYPE_CHECKING:
    from .client import PowerSupplyClient

# Only non-transient modes may be persisted and restored automatically.
# CLEAN (4) is a hardware one-shot dedust cycle; UNKNOWN (5) is read-only.
PERSISTABLE_FAN_MODES: frozenset[FanMode] = frozenset(
    {
        FanMode.AUTO,
        FanMode.SILENT,
        FanMode.PERFORMANCE,
        FanMode.CUSTOM,
    }
)

INTER_CMD_DELAY_S: float = 0.15
_UNSET = object()


def default_config_path() -> Path:
    """Return default settings.json path respecting XDG_CONFIG_HOME."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_cfg) if xdg_cfg else Path.home() / ".config"
    return base / "himos-psu" / "settings.json"


CONFIG_PATH: Path = default_config_path()


def load_config(path: Path | None = None, *, strict: bool = False) -> dict[str, Any]:
    """Load settings JSON as a dict; optionally raise ValueError on corruption."""
    target = Path(path) if path is not None else default_config_path()
    if not target.exists():
        return {}
    try:
        raw_text = target.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise ValueError(f"无法读取或解析配置文件 {target}: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"配置文件 {target} 顶层必须是 JSON 对象")
        return {}
    return data


def save_config(updates: dict[str, Any], path: Path | None = None) -> Path:
    """Merge updates into settings.json using an atomic sibling temp-file replace."""
    target = Path(path) if path is not None else default_config_path()
    existing = load_config(target, strict=False)
    merged = {**existing, **updates}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    payload = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


@dataclass(frozen=True, slots=True)
class SavedPsuProfile:
    """Validated PSU control settings stored locally on disk."""

    fan_mode: FanMode | None = None
    custom_curve: tuple[Point, ...] | None = None
    refresh_mode: RefreshMode | None = None
    auto_restore: bool = True

    @property
    def has_device_settings(self) -> bool:
        return self.fan_mode is not None or self.refresh_mode is not None


def _parse_fan_mode_value(raw: Any) -> FanMode:
    if isinstance(raw, bool):
        raise ValueError(f"Invalid fan_mode boolean: {raw!r}")
    if isinstance(raw, int):
        mode = FanMode(raw)
    elif isinstance(raw, str):
        mode = FanMode.from_name(raw)
    else:
        raise ValueError(f"Unsupported fan_mode type {type(raw).__name__}: {raw!r}")
    if mode not in PERSISTABLE_FAN_MODES:
        raise ValueError(f"Fan mode {mode.label!r} (id={int(mode)}) cannot be persisted or auto-restored")
    return mode


def _parse_curve_value(raw: Any) -> tuple[Point, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("custom_curve must be a list of 5 [temp, duty] points")
    cleaned: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"Invalid curve point {item!r}; expected [temp, duty]")
        x, y = item[0], item[1]
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise ValueError(f"Curve coordinates must be integers, got {item!r}")
        cleaned.append((x, y))
    return tuple(validate_fan_curve(cleaned))


def parse_saved_profile(data: dict[str, Any]) -> SavedPsuProfile:
    """Validate and convert raw settings dict into a safe SavedPsuProfile."""
    auto_restore = bool(data.get("auto_restore", True))

    fan_mode: FanMode | None = None
    if data.get("fan_mode") is not None:
        fan_mode = _parse_fan_mode_value(data["fan_mode"])

    custom_curve: tuple[Point, ...] | None = None
    if data.get("custom_curve") is not None:
        custom_curve = _parse_curve_value(data["custom_curve"])

    if fan_mode == FanMode.CUSTOM and custom_curve is None:
        raise ValueError("Custom fan mode requires a valid 5-point custom_curve")

    refresh_mode: RefreshMode | None = None
    if data.get("refresh_mode") is not None:
        refresh_raw = data["refresh_mode"]
        if isinstance(refresh_raw, bool):
            raise ValueError(f"Invalid refresh_mode boolean: {refresh_raw!r}")
        refresh_mode = RefreshMode.from_value(refresh_raw)

    return SavedPsuProfile(
        fan_mode=fan_mode,
        custom_curve=custom_curve,
        refresh_mode=refresh_mode,
        auto_restore=auto_restore,
    )


def load_saved_profile(path: Path | None = None) -> SavedPsuProfile | None:
    """Load and strictly validate saved PSU settings, or return None if unconfigured."""
    target = Path(path) if path is not None else default_config_path()
    if not target.exists():
        return None
    data = load_config(target, strict=True)
    return parse_saved_profile(data)


def persist_device_settings(
    *,
    fan_mode: FanMode | None | object = _UNSET,
    custom_curve: Sequence[Sequence[int]] | object = _UNSET,
    refresh_mode: RefreshMode | None | object = _UNSET,
    auto_restore: bool | object = _UNSET,
    path: Path | None = None,
) -> Path:
    """Validate and atomically save PSU control settings to settings.json."""
    updates: dict[str, Any] = {}
    if fan_mode is not _UNSET:
        if fan_mode is None:
            updates["fan_mode"] = None
        else:
            mode_enum = FanMode(fan_mode) if isinstance(fan_mode, FanMode) else _parse_fan_mode_value(fan_mode)
            if mode_enum not in PERSISTABLE_FAN_MODES:
                raise ValueError(f"Fan mode {mode_enum.label!r} cannot be persisted or auto-restored")
            updates["fan_mode"] = mode_enum.name.lower()
    if custom_curve is not _UNSET:
        pts = validate_fan_curve(custom_curve)  # type: ignore[arg-type]
        updates["custom_curve"] = [list(pt) for pt in pts]
    if refresh_mode is not _UNSET:
        if refresh_mode is None:
            updates["refresh_mode"] = None
        else:
            rm = RefreshMode(refresh_mode) if isinstance(refresh_mode, RefreshMode) else RefreshMode.from_value(refresh_mode)  # type: ignore[arg-type]
            updates["refresh_mode"] = rm.interval_ms
    if auto_restore is not _UNSET:
        updates["auto_restore"] = bool(auto_restore)
    return save_config(updates, path=path)


@dataclass(frozen=True, slots=True)
class PlannedRestore:
    """Validated wire frames pre-built before touching the serial port."""

    profile: SavedPsuProfile
    refresh_frame: bytes | None
    mode_frame: bytes | None
    curve_frame: bytes | None


def plan_profile_frames(profile: SavedPsuProfile) -> PlannedRestore:
    """Pre-build and verify exact wire frame lengths prior to any serial write."""
    refresh_frame: bytes | None = None
    if profile.refresh_mode is not None:
        refresh_frame = build_refresh_mode_frame(profile.refresh_mode)
        if len(refresh_frame) != 8:
            raise ValueError(f"Unexpected refresh frame length {len(refresh_frame)}")

    mode_frame: bytes | None = None
    curve_frame: bytes | None = None
    if profile.fan_mode is not None:
        if profile.fan_mode not in PERSISTABLE_FAN_MODES:
            raise ValueError(f"Refusing to build non-persistable fan mode {profile.fan_mode.label!r}")
        mode_frame = build_fan_mode_frame(profile.fan_mode)
        if len(mode_frame) != 8:
            raise ValueError(f"Unexpected fan mode frame length {len(mode_frame)}")
        if profile.fan_mode == FanMode.CUSTOM:
            if profile.custom_curve is None:
                raise ValueError("Custom fan mode requires a valid 5-point custom_curve")
            curve_frame = build_fan_curve_frame(profile.custom_curve, validate=True)
            if len(curve_frame) != 33:
                raise ValueError(f"Unexpected fan curve frame length {len(curve_frame)}")

    return PlannedRestore(
        profile=profile,
        refresh_frame=refresh_frame,
        mode_frame=mode_frame,
        curve_frame=curve_frame,
    )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Outcome summary of an on-connect or CLI profile restore attempt."""

    applied: bool
    skipped_reason: str = ""
    sent_refresh: bool = False
    sent_mode: bool = False
    sent_curve: bool = False
    confirmed_mode: FanMode | None = None
    initial_mode: FanMode | None = None
    final_status: PowerStatus | None = None


def apply_profile_to_client(
    client: "PowerSupplyClient",
    profile: SavedPsuProfile,
    *,
    initial_status: PowerStatus,
    force: bool = False,
    if_needed: bool = False,
    confirm_timeout: float = 0.0,
    delay_s: float = INTER_CMD_DELAY_S,
) -> RestoreResult:
    """Safely apply a validated profile to an already-verified PSU connection.

    Requires an observed ``initial_status`` (CRC-verified 0x02 frame) before writing,
    aborts on active fault codes unless ``force=True``, validates and pre-builds all
    frames before sending byte 0, and sends at most once without retry loops.
    """
    plan = plan_profile_frames(profile)

    if not profile.has_device_settings:
        return RestoreResult(
            applied=False,
            skipped_reason="no_settings",
            initial_mode=initial_status.mode,
            final_status=initial_status,
        )

    if initial_status.malfunction_code != 0 and not force:
        return RestoreResult(
            applied=False,
            skipped_reason=f"fault_code:0x{initial_status.malfunction_code:04x}",
            initial_mode=initial_status.mode,
            final_status=initial_status,
        )

    if profile.fan_mode is None:
        need_fan = False
    elif force:
        need_fan = True
    elif profile.fan_mode == FanMode.CUSTOM:
        need_fan = (initial_status.mode != FanMode.CUSTOM) if if_needed else True
    else:
        need_fan = initial_status.mode != profile.fan_mode

    need_refresh = plan.refresh_frame is not None

    if not need_fan and not need_refresh:
        return RestoreResult(
            applied=False,
            skipped_reason="already_applied",
            confirmed_mode=initial_status.mode,
            initial_mode=initial_status.mode,
            final_status=initial_status,
        )

    sent_refresh = False
    sent_mode = False
    sent_curve = False

    if need_refresh and profile.refresh_mode is not None:
        client.set_refresh_mode(profile.refresh_mode)
        sent_refresh = True
        if need_fan:
            time.sleep(delay_s)

    if need_fan and profile.fan_mode is not None:
        if profile.fan_mode == FanMode.CUSTOM:
            client.set_fan_mode(FanMode.CUSTOM, custom_points=profile.custom_curve, delay_s=delay_s)
            sent_mode = True
            sent_curve = True
        else:
            client.set_fan_mode(profile.fan_mode, delay_s=delay_s)
            sent_mode = True

    confirmed_mode: FanMode | None = initial_status.mode if not need_fan else None
    final_status: PowerStatus | None = initial_status if not need_fan else None

    if confirm_timeout > 0 and need_fan and profile.fan_mode is not None:
        deadline = time.monotonic() + confirm_timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            for _raw, parsed in client.poll_frames(timeout=min(0.2, remaining)):
                if isinstance(parsed, PowerStatus):
                    final_status = parsed
                    confirmed_mode = parsed.mode
            if confirmed_mode == profile.fan_mode:
                break

    return RestoreResult(
        applied=True,
        sent_refresh=sent_refresh,
        sent_mode=sent_mode,
        sent_curve=sent_curve,
        confirmed_mode=confirmed_mode,
        initial_mode=initial_status.mode,
        final_status=final_status,
    )
