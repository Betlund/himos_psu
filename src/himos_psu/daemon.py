"""Background collector daemon: owns the PSU serial port and serves clients over IPC.

Why a daemon at all: the transport opens the port with ``TIOCEXCL`` and takes a
flock, so exactly one process can talk to the PSU. Running collection 24/7 means
that process must be a daemon, and everything else (GUI, CLI) has to go through it.

The daemon reuses the normal :class:`~himos_psu.client.PowerSupplyClient` and
:class:`~himos_psu.collector.Collector`; the only new part is forwarding raw frames
to subscribers and executing their control commands.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .client import PowerSupplyClient
from .collector import Collector, CollectorBusy, StaleError
from .config import apply_profile_to_client, load_saved_profile
from .energy import DEFAULT_DB, EnergyStore
from .ipc import IpcServer, default_socket_path
from .protocol import CmdId, FanMode, PowerInfo, PowerStatus, RawFrame, RefreshMode

MAX_CACHED_FRAMES = 12

# Static frames describe the unit itself and never change during a session, so they
# must survive a late subscriber even after thousands of telemetry frames. Volatile
# ones (status/telemetry) only need their latest copy.
PINNED_FRAME_KEYS = frozenset({"info_vendor", "info_date", "sn"})


def frame_cache_key(raw: RawFrame) -> str:
    """Category key used by the daemon's snapshot cache.

    Keying by command id is wrong (0x03 answers both sub=0 and sub=53) and keying by
    raw bytes loses the static frames to LRU churn, so classify by kind instead.
    """
    if raw.cmd_id == CmdId.POWER_INFO_RESPONSE:
        payload = raw.payload
        # Same discriminator as protocol.parse_command_content: the build-date block
        # stores year/month/day in bytes 22-24, the vendor block keeps ASCII there.
        is_vendor = len(payload) >= 25 and (payload[23] > 12 or payload[24] > 31)
        return "info_vendor" if is_vendor else "info_date"
    if raw.cmd_id == CmdId.POWER_SN_RESPONSE:
        return "sn"
    if raw.cmd_id == CmdId.POWER_STATUS_RESPONSE:
        return "status"
    if raw.cmd_id == CmdId.POWER_INFO:
        return "telemetry"
    return raw.raw_bytes.hex()


class Daemon:
    """Own the PSU and expose it over a Unix socket."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        socket_path: str | Path | None = None,
        port: str | None = None,
        mock: bool = False,
        restore: bool = True,
        wait_port: float = 0.0,
        stale_timeout: float = 0.0,
        config_path: str | Path | None = None,
        quiet: bool = False,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_DB)
        self.socket_path = Path(socket_path) if socket_path is not None else default_socket_path()
        self.port = port
        self.mock = mock
        self.restore = restore
        self.wait_port = float(wait_port)
        self.stale_timeout = float(stale_timeout)
        self.config_path = Path(config_path) if config_path is not None else None
        self.quiet = quiet
        self._log = on_log or (lambda text: None)

        self.client: PowerSupplyClient | None = None
        self.store: EnergyStore | None = None
        self.server: IpcServer | None = None
        self.collector: Collector | None = None

        self._stop = threading.Event()
        self._failed = False
        self._thread: threading.Thread | None = None
        self._state = "未启动"
        # Keyed by frame kind (see frame_cache_key): the static specs must be replayed
        # to any late subscriber, the volatile telemetry only in its latest form.
        self._frames: dict[str, dict[str, Any]] = {}
        self._restored = False

    # -- logging -----------------------------------------------------------
    def _emit(self, text: str) -> None:
        self._state = text
        self._log(text)
        self._broadcast({"event": "state", "text": text})

    def _broadcast(self, message: dict[str, Any]) -> None:
        if self.server is not None:
            self.server.broadcast(message)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Acquire the PSU, start the IPC server and begin collecting (non-blocking)."""
        target_port = self.port
        if not self.mock and self.wait_port > 0:
            target_port = self._wait_for_port(self.port, self.wait_port)

        self.client = PowerSupplyClient(port=target_port, mock=self.mock)
        self.store = EnergyStore(self.db_path)
        self.collector = Collector(
            self.client,
            self.store,
            on_state=self._emit,
            on_status=lambda status: None,
            on_frame=self._on_frame,
            on_specs=self._on_specs,
            on_first_status=self._maybe_restore,
            stale_timeout=self.stale_timeout,
        )
        # Take the collector lock eagerly so a second daemon fails fast.
        self.collector.acquire()

        try:
            self.server = IpcServer(
                self.socket_path,
                handler=self._handle_command,
                on_subscribe=self._send_snapshot,
            )
            self.server.start()
        except Exception:
            self.collector.release()
            if self.store is not None:
                self.store.close()
                self.store = None
            raise

        self._emit("守护进程启动，正在连接电源…")
        self._thread = threading.Thread(target=self._run_collector, name="himos-collector", daemon=True)
        self._thread.start()

    def _run_collector(self) -> None:
        assert self.collector is not None
        try:
            self.collector.run(should_stop=self._stop.is_set)
        except CollectorBusy as exc:
            self._emit(f"串口被其他进程占用: {exc}")
        except StaleError as exc:
            self._emit(f"采集超时，退出等待重启: {exc}")
            self._failed = True
            self._stop.set()
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._emit(f"采集异常退出: {exc}")
            self._failed = True
            self._stop.set()
        finally:
            self._emit("采集已停止")

    def run(self) -> int:
        """Block until stopped; returns a process exit code."""
        self.start()
        self._install_signals()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            self._stop.set()
        finally:
            self.stop()
        return 1 if self._failed else 0

    def stop(self) -> None:
        self._stop.set()
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self.collector is not None:
            self.collector.release()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        if self.store is not None:
            self.store.close()
            self.store = None

    @property
    def state(self) -> str:
        return self._state

    def _install_signals(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except (ValueError, OSError):  # not in the main thread / unsupported
                pass

    def _wait_for_port(self, port: str | None, wait_s: float) -> str:
        from .transport import resolve_default_port

        deadline = time.monotonic() + max(0.0, wait_s)
        last_error: Exception | None = None
        while True:
            try:
                resolved = resolve_default_port(port)
                if os.path.exists(resolved) and os.access(resolved, os.R_OK | os.W_OK):
                    return resolved
                last_error = PermissionError(f"串口尚未就绪: {resolved}")
            except FileNotFoundError as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                raise last_error or TimeoutError("等待串口超时")
            time.sleep(0.5)

    # -- collector hooks ---------------------------------------------------
    def _on_frame(self, raw: RawFrame, parsed: Any) -> None:
        payload = {
            "event": "frame",
            "cmd": int(raw.cmd_id),
            "raw": raw.raw_bytes.hex(),
            "crc_ok": bool(raw.crc_valid),
        }
        key = frame_cache_key(raw)
        self._frames.pop(key, None)
        self._frames[key] = payload
        while len(self._frames) > MAX_CACHED_FRAMES:
            victim = next(
                (k for k in self._frames if k not in PINNED_FRAME_KEYS and k != key), None
            )
            if victim is None:
                break
            self._frames.pop(victim, None)
        self._broadcast(payload)

    def _on_specs(self, info: PowerInfo | None, sn: str) -> None:
        if info is not None:
            self._log(
                f"电源: {info.model} / {info.firmware_display} / {info.manufacture or '未知厂商'}"
            )

    def _maybe_restore(self, client: PowerSupplyClient, status: PowerStatus) -> None:
        if not self.restore or self._restored:
            return
        self._restored = True
        try:
            profile = load_saved_profile(self.config_path)
        except Exception as exc:
            self._emit(f"读取保存的配置失败: {exc}")
            return
        if profile is None or not profile.has_device_settings or not profile.auto_restore:
            return
        try:
            result = apply_profile_to_client(
                client, profile, initial_status=status, if_needed=True, confirm_timeout=0.0
            )
        except Exception as exc:
            self._emit(f"恢复保存的配置失败: {exc}")
            return
        if result.applied:
            parts = []
            if result.sent_mode and profile.fan_mode is not None:
                parts.append(f"风扇模式 {profile.fan_mode.label}")
            if result.sent_curve:
                parts.append("自定义曲线")
            if result.sent_refresh and profile.refresh_mode is not None:
                parts.append(f"刷新 {profile.refresh_mode.interval_ms} ms")
            if parts:
                self._emit("已恢复保存的配置: " + " + ".join(parts))

    # -- IPC ---------------------------------------------------------------
    def _send_snapshot(self, write: Callable[[dict[str, Any]], bool]) -> None:
        write({"event": "state", "text": self._state})
        for payload in list(self._frames.values()):
            write(payload)

    def _handle_command(self, message: dict[str, Any]) -> dict[str, Any] | None:
        client = self.client
        if client is None:
            raise RuntimeError("电源尚未连接")
        cmd = str(message.get("cmd", ""))

        if cmd in ("snapshot", "status"):
            return None
        if cmd == "set_fan_mode":
            mode = FanMode(int(message["mode"]))
            points = message.get("points")
            client.set_fan_mode(mode, custom_points=points)
            return None
        if cmd == "set_curve":
            points = message.get("points") or []
            client.set_fan_curve(points, switch_to_custom=bool(message.get("switch", True)))
            return None
        if cmd == "set_refresh":
            client.set_refresh_mode(RefreshMode(int(message["index"])))
            return None
        if cmd == "clear_fault":
            client.clear_fault_code()
            return None
        if cmd == "request_info":
            client.request_info(int(message.get("sub", 0)))
            return None
        if cmd == "ping":
            return {"state": self._state}
        raise ValueError(f"未知命令: {cmd!r}")


__all__ = ["Daemon"]
