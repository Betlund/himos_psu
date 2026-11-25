"""Serial transports and CH340 (1A86:7523) port auto-discovery for Linux."""

from __future__ import annotations

import glob
import os
import select
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from .protocol import (
    BAUDRATE,
    CH340_PID,
    CH340_VID,
    CmdId,
    FanMode,
    RefreshMode,
    parse_frame,
)

if sys.platform.startswith("linux"):
    import fcntl
    import termios
else:  # pragma: no cover - non-Linux fallback uses pyserial
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    """Detected serial candidate for a HiMOS digital power supply."""

    device: str
    vendor_id: str = ""
    product_id: str = ""
    product: str = ""
    by_id: str = ""

    @property
    def is_ch340(self) -> bool:
        vid_ok = self.vendor_id.upper() == CH340_VID
        pid_ok = self.product_id.upper() == CH340_PID
        return (vid_ok and pid_ok) or ("1a86" in self.by_id.lower())


class SerialTransport(Protocol):
    """Minimal synchronous byte-stream interface used by PowerSupplyClient."""

    port_name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 4096, timeout: float = 0.2) -> bytes: ...
    def flush_input(self) -> None: ...


def _read_sysfs_attr(start_dir: str, attr: str) -> str:
    """Walk up to 4 USB sysfs parent directories to find idVendor/idProduct/product."""
    cur = os.path.realpath(start_dir)
    for _ in range(4):
        candidate = os.path.join(cur, attr)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8", errors="ignore") as handle:
                    return handle.read().strip()
            except OSError:
                return ""
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def discover_serial_ports() -> list[SerialPortInfo]:
    """Enumerate candidate serial devices on Linux with CH340 (1A86:7523) prioritized."""
    if not sys.platform.startswith("linux"):
        return _discover_via_pyserial()

    by_id_map: dict[str, str] = {}
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        target = os.path.realpath(link)
        by_id_map.setdefault(target, link)

    ports: list[SerialPortInfo] = []
    seen: set[str] = set()
    for dev_path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        real_dev = os.path.realpath(dev_path)
        if real_dev in seen:
            continue
        seen.add(real_dev)
        tty_name = os.path.basename(dev_path)
        sysfs_dev = f"/sys/class/tty/{tty_name}/device"
        vid = _read_sysfs_attr(sysfs_dev, "idVendor").upper()
        pid = _read_sysfs_attr(sysfs_dev, "idProduct").upper()
        product = _read_sysfs_attr(sysfs_dev, "product")
        by_id = by_id_map.get(real_dev, "")
        if not vid and "1a86" in by_id.lower():
            vid = CH340_VID
            if "usb_serial" in by_id.lower():
                pid = pid or CH340_PID
        ports.append(
            SerialPortInfo(
                device=dev_path,
                vendor_id=vid,
                product_id=pid,
                product=product,
                by_id=by_id,
            )
        )

    ports.sort(key=lambda item: (not item.is_ch340, item.device))
    return ports


def _discover_via_pyserial() -> list[SerialPortInfo]:  # pragma: no cover
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except ImportError:
        return []
    result: list[SerialPortInfo] = []
    for port in list_ports.comports():
        vid = f"{port.vid:04X}" if port.vid is not None else ""
        pid = f"{port.pid:04X}" if port.pid is not None else ""
        result.append(
            SerialPortInfo(
                device=port.device,
                vendor_id=vid,
                product_id=pid,
                product=port.product or "",
            )
        )
    result.sort(key=lambda item: (not item.is_ch340, item.device))
    return result


def resolve_default_port(explicit_port: str | None = None) -> str:
    """Resolve user-supplied port or auto-select the connected CH340 PSU serial port."""
    if explicit_port:
        return explicit_port
    ports = discover_serial_ports()
    ch340_ports = [p for p in ports if p.is_ch340]
    if ch340_ports:
        return ch340_ports[0].device
    if ports:
        return ports[0].device
    raise FileNotFoundError(
        "No USB serial port found (/dev/ttyUSB* or CH340 1A86:7523). Pass --port explicitly or use --mock."
    )


class PosixSerialTransport:
    """Zero-dependency Linux termios/select raw 8N1 serial transport."""

    def __init__(self, port: str, baudrate: int = BAUDRATE) -> None:
        self.port_name = port
        self.baudrate = baudrate
        self._fd: int | None = None

    def open(self) -> None:
        if self._fd is not None:
            return
        if termios is None:  # pragma: no cover
            raise RuntimeError("PosixSerialTransport requires Linux termios; install pyserial on this OS")

        baud_flag = getattr(termios, f"B{self.baudrate}", None)
        if baud_flag is None:
            raise ValueError(f"Unsupported baudrate {self.baudrate}")

        try:
            fd = os.open(self.port_name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError as exc:
            raise PermissionError(
                f"Permission denied opening {self.port_name}. "
                "Add user to serial group (uucp/dialout) or install the udev rule "
                "(`himos-psu udev-rule`)."
            ) from exc

        try:
            try:
                if hasattr(termios, "TIOCEXCL"):
                    fcntl.ioctl(fd, termios.TIOCEXCL)
            except OSError:
                pass
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag: disable XON/XOFF, CR->NL translation, parity checks
            attrs[1] = 0  # oflag: raw output
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # lflag: non-canonical, no echo/signals
            if hasattr(attrs, "__setitem__"):
                attrs[4] = baud_flag
                attrs[5] = baud_flag
                attrs[6][termios.VMIN] = 0
                attrs[6][termios.VTIME] = 0
            if hasattr(termios, "cfsetspeed"):
                try:
                    termios.cfsetspeed(attrs, baud_flag)
                except OSError:
                    pass
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None and termios is not None and hasattr(termios, "TIOCNXCL"):
                    fcntl.ioctl(self._fd, termios.TIOCNXCL)
            except OSError:
                pass
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def flush_input(self) -> None:
        if self._fd is not None and termios is not None:
            termios.tcflush(self._fd, termios.TCIFLUSH)

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise RuntimeError("Serial port is not open")
        view = memoryview(data)
        total = 0
        deadline = time.monotonic() + 2.0
        while total < len(view):
            try:
                written = os.write(self._fd, view[total:])
            except BlockingIOError:
                remaining = max(0.01, deadline - time.monotonic())
                _, writable, _ = select.select([], [self._fd], [], remaining)
                if not writable:
                    raise TimeoutError(f"Serial write timed out on {self.port_name}")
                continue
            if written == 0:
                raise OSError(f"Short serial write on {self.port_name}")
            total += written
        return total

    def read(self, size: int = 4096, timeout: float = 0.2) -> bytes:
        if self._fd is None:
            raise RuntimeError("Serial port is not open")
        ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        try:
            return os.read(self._fd, size)
        except BlockingIOError:
            return b""


class PySerialTransport:  # pragma: no cover - optional cross-platform wrapper
    """Optional pyserial backend when running outside Linux or explicitly selected."""

    def __init__(self, port: str, baudrate: int = BAUDRATE) -> None:
        self.port_name = port
        self.baudrate = baudrate
        self._ser = None

    def open(self) -> None:
        if self._ser is not None:
            return
        import serial  # type: ignore[import-not-found]

        self._ser = serial.Serial(port=self.port_name, baudrate=self.baudrate, timeout=0.1)

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def flush_input(self) -> None:
        if self._ser is not None:
            self._ser.reset_input_buffer()

    def write(self, data: bytes) -> int:
        if self._ser is None:
            raise RuntimeError("Serial port is not open")
        return int(self._ser.write(data))

    def read(self, size: int = 4096, timeout: float = 0.2) -> bytes:
        if self._ser is None:
            raise RuntimeError("Serial port is not open")
        self._ser.timeout = timeout
        return bytes(self._ser.read(size))


class MockSerialTransport:
    """Offline KE-1300P wire-level simulator for testing CLI/GUI without hardware."""

    def __init__(self) -> None:
        self.port_name = "mock://KE-1300P"
        self._open = False
        self._rx_queue: deque[bytes] = deque()
        self.tx_history: list[bytes] = []
        self.mode = FanMode.AUTO
        self.refresh_mode = RefreshMode.MS_1000
        self.malfunction_code = 0x0000
        self.custom_curve = b""
        self._tick = 0
        self._last_emit = 0.0

    @staticmethod
    def _rx_wrap(command_content: bytes) -> bytes:
        """Simulate real KE-1300P MCU RX frame whose CRC8 covers header + commandContent."""
        from .crc8 import crc8

        body = b"\x55\x7E" + command_content
        return body + bytes([crc8(body), 0xAE])

    def open(self) -> None:
        self._open = True
        self._last_emit = time.monotonic() - 1.0

    def close(self) -> None:
        self._open = False

    def flush_input(self) -> None:
        self._rx_queue.clear()

    def write(self, data: bytes) -> int:
        if not self._open:
            raise RuntimeError("Mock serial port is not open")
        self.tx_history.append(data)
        frame = parse_frame(data)
        if frame.cmd_id == CmdId.POWER_INFO and len(frame.payload) >= 1:
            sub = frame.payload[0]
            self._enqueue_specs_and_sn(sub)
        elif frame.cmd_id == CmdId.POWER_FAN_MODE and len(frame.payload) >= 2:
            self.mode = FanMode(frame.payload[1])
            self._enqueue_status()
        elif frame.cmd_id == CmdId.POWER_REFRESH_MODE and len(frame.payload) >= 2:
            self.refresh_mode = RefreshMode(frame.payload[1])
        elif frame.cmd_id == CmdId.POWER_FAN_CUSTOM:
            self.custom_curve = frame.payload
        elif frame.cmd_id == CmdId.POWER_CLEAR_FAULT_CODE:
            self.malfunction_code = 0
            self._enqueue_status()
        return len(data)

    def _enqueue_specs_and_sn(self, sub: int) -> None:
        # Tail union matches observed KE-1300P hardware: sub=0 -> "segotep", sub=53 -> "sego\x18\x09\x14"
        tail = b"sego\x18\x09\x14" if sub == 53 else b"segotep"
        info_payload = (
            b"KE-1300P"
            + b"D"
            + bytes([4, 20, 110, 100, 240])
            + (1300).to_bytes(2, "big")
            + bytes([10])
            + b"P"
            + tail
        )
        self._rx_queue.append(self._rx_wrap(bytes([len(info_payload) + 2, CmdId.POWER_INFO_RESPONSE]) + info_payload))
        sn_payload = b"SBRC00000000123 "
        self._rx_queue.append(self._rx_wrap(bytes([len(sn_payload) + 2, CmdId.POWER_SN_RESPONSE]) + sn_payload))

    def _enqueue_status(self) -> None:
        self._tick += 1
        delta = (self._tick % 7) - 3
        rpm_raw = 0 if self.mode in (FanMode.AUTO, FanMode.SILENT) else (60 if self.mode == FanMode.CLEAN else 38 + (self._tick % 4))
        payload = bytearray()
        payload.extend((3307 + delta).to_bytes(2, "little"))
        payload.extend((5005 + delta).to_bytes(2, "little"))
        payload.extend((12129 + delta * 2).to_bytes(2, "little"))
        payload.extend((5010 + delta).to_bytes(2, "little"))
        payload.append(int(self.mode))
        payload.extend(b"\x0f\x0e\x01")
        payload.extend((4240 + delta * 35).to_bytes(2, "little"))  # ~212W
        payload.extend((501 + (self._tick % 2)).to_bytes(2, "little"))  # 50.1-50.2Hz
        payload.extend((int((62.5 + delta * 0.2) * 256)).to_bytes(2, "little"))
        payload.extend((int((51.8 + delta * 0.2) * 256)).to_bytes(2, "little"))
        payload.extend((int((31.2 + delta * 0.1) * 256)).to_bytes(2, "little"))
        payload.extend((2284 + delta).to_bytes(2, "little"))  # ~228.4V
        payload.extend(rpm_raw.to_bytes(2, "little"))
        payload.extend(self.malfunction_code.to_bytes(2, "little"))
        self._rx_queue.append(self._rx_wrap(bytes([len(payload) + 2, CmdId.POWER_STATUS_RESPONSE]) + bytes(payload)))

    def read(self, size: int = 4096, timeout: float = 0.2) -> bytes:
        if not self._open:
            raise RuntimeError("Mock serial port is not open")
        now = time.monotonic()
        interval = max(0.25, min(self.refresh_mode.interval_ms / 1000.0, 1.0))
        if not self._rx_queue and now - self._last_emit >= interval:
            self._enqueue_status()
            self._last_emit = now
        if not self._rx_queue and timeout > 0:
            time.sleep(min(timeout, 0.05))
        if not self._rx_queue:
            return b""
        return self._rx_queue.popleft()[:size]


def create_transport(port: str | None = None, mock: bool = False) -> SerialTransport:
    """Create the appropriate transport for the current OS or mock mode."""
    if mock or (port and port.startswith("mock://")):
        return MockSerialTransport()
    resolved = resolve_default_port(port)
    if sys.platform.startswith("linux"):
        return PosixSerialTransport(resolved)
    return PySerialTransport(resolved)
