"""High-level thread-safe client for Segotep / Kunlun HiMOS power supplies."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence

from .protocol import (
    FanMode,
    PowerExtraTelemetry,
    PowerInfo,
    PowerSerial,
    PowerStatus,
    RawFrame,
    RefreshMode,
    SpecsMerger,
    StreamFrameParser,
    build_clear_fault_frame,
    build_fan_curve_frame,
    build_fan_mode_frame,
    build_info_request_frame,
    build_refresh_mode_frame,
    parse_command_content,
)
from .transport import SerialTransport, create_transport


class PowerSupplyClient:
    """Stateful protocol session handling handshake, telemetry parsing, and control commands."""

    def __init__(
        self,
        port: str | None = None,
        mock: bool = False,
        transport: SerialTransport | None = None,
        verify_crc: bool = True,
    ) -> None:
        self.transport: SerialTransport = transport or create_transport(port=port, mock=mock)
        self._parser = StreamFrameParser(verify_crc=verify_crc)
        self._lock = threading.Lock()
        self.specs_merger = SpecsMerger()
        self.sn: str = ""
        self.last_status: PowerStatus | None = None
        self.last_extra: PowerExtraTelemetry | None = None

    @property
    def port_name(self) -> str:
        return self.transport.port_name

    @property
    def info(self) -> PowerInfo | None:
        return self.specs_merger.current

    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "PowerSupplyClient":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _send_bytes(self, frame_bytes: bytes) -> None:
        with self._lock:
            self.transport.write(frame_bytes)

    def _poll_once_unlocked(self, timeout: float = 0.2) -> list[tuple[RawFrame, Any]]:
        chunk = self.transport.read(4096, timeout=timeout)
        if not chunk:
            return []
        decoded: list[tuple[RawFrame, Any]] = []
        for raw in self._parser.feed(chunk):
            parsed = parse_command_content(raw.cmd_id, raw.payload)
            if isinstance(parsed, PowerStatus):
                self.last_status = parsed
            elif isinstance(parsed, PowerInfo):
                parsed = self.specs_merger.update(parsed)
            elif isinstance(parsed, PowerSerial):
                self.sn = parsed.sn
            elif isinstance(parsed, PowerExtraTelemetry):
                self.last_extra = parsed
            decoded.append((raw, parsed))
        return decoded

    def poll_frames(self, timeout: float = 0.2) -> list[tuple[RawFrame, Any]]:
        """Read available serial bytes, decode complete frames, and update client state."""
        with self._lock:
            return self._poll_once_unlocked(timeout=timeout)

    def collect_until(
        self,
        predicate: Callable[[], bool],
        timeout: float = 2.0,
        step_timeout: float = 0.15,
    ) -> list[tuple[RawFrame, Any]]:
        """Pump incoming frames until predicate returns True or timeout expires."""
        collected: list[tuple[RawFrame, Any]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            events = self.poll_frames(timeout=min(step_timeout, max(0.01, remaining)))
            collected.extend(events)
            if predicate():
                break
        return collected

    def query_specs_and_sn(
        self,
        timeout: float = 2.0,
        on_frame: Callable[[RawFrame, Any], None] | None = None,
    ) -> tuple[PowerInfo | None, str]:
        """Run both POWER_INFO(sub=0) and POWER_INFO(sub=53) matching HiMOS startup.

        sub=0 populates manufacture (e.g. 'segotep') + SN; sub=53 populates build date (year/month/day).
        """
        with self._lock:
            self.transport.flush_input()
            self.transport.write(build_info_request_frame(0))
        for raw, parsed in self.collect_until(
            lambda: bool(self.sn and self.info and self.info.manufacture),
            timeout=timeout / 2,
        ):
            if on_frame is not None:
                on_frame(raw, parsed)

        with self._lock:
            self.transport.write(build_info_request_frame(53))
        for raw, parsed in self.collect_until(
            lambda: bool(self.info and (self.info.year or self.info.month or self.info.day)),
            timeout=timeout / 2,
        ):
            if on_frame is not None:
                on_frame(raw, parsed)
        return self.info, self.sn

    def wait_for_status(self, timeout: float = 3.5) -> PowerStatus:
        """Wait for the next POWER_STATUS_RESPONSE telemetry frame."""
        target = self.last_status
        seen_new = False

        def got_new() -> bool:
            return seen_new

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for _, parsed in self.poll_frames(timeout=min(0.2, max(0.01, deadline - time.monotonic()))):
                if isinstance(parsed, PowerStatus):
                    seen_new = True
                    target = parsed
            if got_new() and target is not None:
                return target
        raise TimeoutError(f"Timed out after {timeout:.1f}s waiting for POWER_STATUS_RESPONSE (0x02)")

    def set_fan_mode(
        self,
        mode: FanMode,
        custom_points: Sequence[Sequence[int]] | None = None,
        delay_s: float = 0.15,
    ) -> bytes:
        """Set active fan mode; pre-validate and push custom curve after switching to CUSTOM."""
        frame = build_fan_mode_frame(mode)
        curve_frame = (
            build_fan_curve_frame(custom_points, validate=True)
            if (mode == FanMode.CUSTOM and custom_points is not None)
            else None
        )
        self._send_bytes(frame)
        if curve_frame is not None:
            time.sleep(delay_s)
            self._send_bytes(curve_frame)
            return curve_frame
        return frame

    def set_fan_curve(
        self,
        points: Sequence[Sequence[int]],
        switch_to_custom: bool = True,
        delay_s: float = 0.15,
    ) -> bytes:
        """Validate, resample (x9), and upload a 5-point custom fan curve."""
        curve_frame = build_fan_curve_frame(points, validate=True)
        if switch_to_custom:
            self._send_bytes(build_fan_mode_frame(FanMode.CUSTOM))
            time.sleep(delay_s)
        self._send_bytes(curve_frame)
        return curve_frame

    def set_refresh_mode(self, refresh: RefreshMode) -> bytes:
        """Set active telemetry push interval on the PSU MCU (1000..3000ms)."""
        frame = build_refresh_mode_frame(refresh)
        self._send_bytes(frame)
        return frame

    def clear_fault_code(self) -> bytes:
        """Clear latched hardware malfunction code on the PSU."""
        frame = build_clear_fault_frame()
        self._send_bytes(frame)
        return frame

    def request_info(self, sub: int = 0) -> bytes:
        """Send raw POWER_INFO(sub) frame."""
        frame = build_info_request_frame(sub)
        self._send_bytes(frame)
        return frame
