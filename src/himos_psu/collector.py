"""Single-owner telemetry collector.

The serial port is opened with ``TIOCEXCL`` by the transport, so only one process
may poll the PSU at a time.  This collector takes an exclusive ``flock`` next to
the database and refuses to start when another collector already owns the port,
which is what lets the GUI and ``himos-psu collect`` share one history without
fighting over ``/dev/ttyUSB0``.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .client import PowerSupplyClient
from .energy import SESSION_GAP_S, WS_PER_KWH, EnergyStore
from .protocol import PowerInfo, PowerStatus

try:  # pragma: no cover - non-POSIX platforms simply skip locking
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class CollectorBusy(RuntimeError):
    """Another process already owns the PSU / energy database."""


class StaleError(RuntimeError):
    """No telemetry for ``stale_timeout`` seconds; the caller should restart."""


def _acquire_lock_fd(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = ""
            try:
                holder = os.read(fd, 32).decode("ascii", "ignore").strip()
            except OSError:
                pass
            os.close(fd)
            detail = f"（PID {holder}）" if holder else ""
            raise CollectorBusy(
                f"另一个采集进程正在运行{detail}。串口被独占，同一时间只能有一个采集者；"
                f"请先停止它，或用 `himos-psu energy` 只读查看历史。"
            ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    return fd


def _release_lock_fd(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def collector_lock(lock_path: str | Path) -> Iterator[None]:
    """Acquire the exclusive non-blocking collector flock for a serial session."""
    fd = _acquire_lock_fd(Path(lock_path))
    try:
        yield
    finally:
        _release_lock_fd(fd)


@dataclass(slots=True)
class CollectorStats:
    samples: int = 0
    ws: float = 0.0
    gaps: int = 0
    started_ts: float = 0.0

    @property
    def kwh(self) -> float:
        return self.ws / WS_PER_KWH


class Collector:
    """Poll one PSU and append integrated energy to an :class:`EnergyStore`."""

    def __init__(
        self,
        client: PowerSupplyClient,
        store: EnergyStore,
        *,
        lock_path: str | Path | None = None,
        on_status: Callable[[PowerStatus], None] | None = None,
        on_frame: Callable[[object, object], None] | None = None,
        on_first_status: Callable[[PowerSupplyClient, PowerStatus], None] | None = None,
        on_specs: Callable[[PowerInfo | None, str], None] | None = None,
        on_state: Callable[[str], None] | None = None,
        poll_timeout: float = 0.2,
        prune_interval: float = 6 * 3600.0,
        session_gap: float = SESSION_GAP_S,
        stale_timeout: float = 0.0,
    ) -> None:
        self.client = client
        self.store = store
        self.lock_path = Path(lock_path) if lock_path else store.path.parent / "collector.lock"
        self.on_status = on_status
        self.on_frame = on_frame
        self.on_first_status = on_first_status
        self.on_specs = on_specs
        self.on_state = on_state
        self.poll_timeout = poll_timeout
        self.prune_interval = prune_interval
        self.session_gap = session_gap
        self.stale_timeout = float(stale_timeout)
        self.stats = CollectorStats()
        self._lock_fd: int | None = None

    # -- locking -----------------------------------------------------------
    def acquire(self) -> None:
        if self._lock_fd is not None:
            return
        self._lock_fd = _acquire_lock_fd(self.lock_path)

    def release(self) -> None:
        if self._lock_fd is None:
            return
        fd = self._lock_fd
        self._lock_fd = None
        _release_lock_fd(fd)

    # -- callbacks ---------------------------------------------------------
    def _emit_state(self, text: str) -> None:
        if self.on_state is not None:
            self.on_state(text)

    # -- main loop ---------------------------------------------------------
    def run(self, should_stop: Callable[[], bool] | None = None) -> None:
        """Block until ``should_stop`` returns True; raises on serial errors."""
        stop = should_stop or (lambda: False)
        self.acquire()
        self.stats.started_ts = time.time()
        try:
            self._emit_state("正在连接电源…")
            self.store.prune_raw()
            self.client.open()
            self._emit_state(f"已连接 {self.client.port_name}")
            try:
                info, sn = self.client.query_specs_and_sn(timeout=2.0, on_frame=self.on_frame)
                if self.on_specs is not None and (info is not None or sn):
                    self.on_specs(info, sn)
            except Exception:
                pass  # specs are nice-to-have; telemetry still works
            self._emit_state("采集中")
            next_prune = time.monotonic() + self.prune_interval
            first_status_seen = False
            last_frame = time.monotonic()
            while not stop():
                events = self.client.poll_frames(timeout=self.poll_timeout)
                if events:
                    last_frame = time.monotonic()
                elif self.stale_timeout > 0 and time.monotonic() - last_frame > self.stale_timeout:
                    raise StaleError(
                        f"{self.stale_timeout:.0f}s 内没有收到任何遥测帧，串口可能已失效（休眠/重新枚举）"
                    )
                for raw, parsed in events:
                    if self.on_frame is not None:
                        try:
                            self.on_frame(raw, parsed)
                        except Exception:
                            pass
                    if isinstance(parsed, PowerStatus):
                        stamp = time.time()
                        ws, gap = self.store.add_sample(stamp, parsed)
                        self.stats.samples += 1
                        self.stats.ws += ws
                        if gap:
                            self.stats.gaps += 1
                        if not first_status_seen:
                            first_status_seen = True
                            if self.on_first_status is not None:
                                try:
                                    self.on_first_status(self.client, parsed)
                                except Exception:
                                    pass
                        if self.on_status is not None:
                            self.on_status(parsed)
                if time.monotonic() >= next_prune:
                    self.store.prune_raw()
                    next_prune = time.monotonic() + self.prune_interval
        finally:
            self.store.close_session()
            try:
                self.client.close()
            except Exception:
                pass
            self.release()
            self._emit_state("已停止")
