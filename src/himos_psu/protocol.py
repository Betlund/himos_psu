"""Binary wire protocol encoder/decoder for HiMOS CH340 PowerSupply frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Sequence

from .crc8 import crc8, verify_rx_crc
from .fan_curve import resample_fan_curve

START_FLAG = b"\x55\x7E"
END_FLAG = 0xAE
CH340_VID = "1A86"
CH340_PID = "7523"
BAUDRATE = 115200


class CmdId(IntEnum):
    """PowerSupply command IDs from main_disasm slot [102] + observed 0x04 telemetry."""

    POWER_STATUS_RESPONSE = 2
    POWER_INFO_RESPONSE = 3
    POWER_INFO = 4  # TX query (sub=0 for vendor+SN, sub=53 for date); also seen as RX 0x04 extended frame
    POWER_SN_RESPONSE = 5
    POWER_FAN_MODE = 19
    POWER_REFRESH_MODE = 22
    POWER_FAN_CUSTOM = 27
    POWER_CLEAR_FAULT_CODE = 30


class FanMode(IntEnum):
    """PSU fan preset modes matching HiMOS Ra enum."""

    AUTO = 0
    SILENT = 1
    PERFORMANCE = 2
    CUSTOM = 3
    CLEAN = 4
    UNKNOWN = 5

    @classmethod
    def from_name(cls, name: str) -> "FanMode":
        key = name.strip().upper()
        aliases = {
            "MUTE": cls.SILENT,
            "OVERCLOCK": cls.PERFORMANCE,
            "OC": cls.PERFORMANCE,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls[key]
        except KeyError as exc:
            valid = ", ".join(m.name.lower() for m in cls if m != cls.UNKNOWN)
            raise ValueError(f"Unknown fan mode {name!r}; expected one of: {valid}") from exc

    @property
    def label(self) -> str:
        return self.name.capitalize()


class RefreshMode(IntEnum):
    """PSU telemetry refresh interval options matching HiMOS index [258]."""

    MS_1000 = 0
    MS_1500 = 1
    MS_2000 = 2
    MS_2500 = 3
    MS_3000 = 4

    @property
    def interval_ms(self) -> int:
        return 1000 + int(self) * 500

    @classmethod
    def from_value(cls, value: int | str) -> "RefreshMode":
        text = str(value).strip().lower().removesuffix("ms")
        num = int(text)
        if 0 <= num <= 4:
            return cls(num)
        for member in cls:
            if member.interval_ms == num:
                return member
        raise ValueError(f"Unsupported refresh rate {value!r}; expected 0..4 or 1000..3000ms")


class TempSensor(IntEnum):
    """Selectable thermal probe in the UI gauge."""

    HIGH_VOLTAGE = 0
    LOW_VOLTAGE = 1
    ENVIRONMENT = 2

    @property
    def himos_key(self) -> str:
        return {
            TempSensor.HIGH_VOLTAGE: "highVoltage",
            TempSensor.LOW_VOLTAGE: "lowVoltage",
            TempSensor.ENVIRONMENT: "envVoltage",
        }[self]


CERT_80PLUS_MAP: dict[str, str] = {
    "S": "80 PLUS Silver",
    "G": "80 PLUS Gold",
    "P": "80 PLUS Platinum",
    "T": "80 PLUS Titanium",
    "B": "80 PLUS Bronze",
}


def js_round_2(value: float) -> float:
    """Match HiMOS helper it(val, 2) = Math.round(val * 100) / 100."""
    return int(value * 100.0 + 0.5) / 100.0


@dataclass(slots=True)
class RawFrame:
    """Decoded wire-level frame before payload interpretation."""

    length_byte: int
    cmd_id: int
    payload: bytes
    wire_crc: int
    raw_bytes: bytes
    crc_valid: bool

    @property
    def command_content(self) -> bytes:
        return self.raw_bytes[2:-2]


@dataclass(slots=True)
class PowerStatus:
    """Real-time PSU telemetry from POWER_STATUS_RESPONSE (0x02)."""

    voltage_3v3: float
    voltage_5v: float
    voltage_12v: float
    voltage_5v_sb: float
    mode: FanMode
    reserved: bytes
    power: float
    frequency: float
    high_voltage_temperature: float
    low_voltage_temperature: float
    environment_temperature: float
    input_voltage: float
    fan_speed: int
    malfunction_code: int

    def selected_temperature(self, sensor: TempSensor) -> float:
        if sensor == TempSensor.HIGH_VOLTAGE:
            return self.high_voltage_temperature
        if sensor == TempSensor.LOW_VOLTAGE:
            return self.low_voltage_temperature
        return self.environment_temperature


@dataclass(slots=True)
class PowerInfo:
    """PSU specification / version / manufacturer frame POWER_INFO_RESPONSE (0x03)."""

    model: str
    version: int
    rated_input_current: int
    rated_output_current: int
    rated_input_low_voltage: int
    rated_input_high_voltage: int
    rated_power: int
    warranty: int
    certification_80plus: str
    manufacture: str = ""
    year: int = 0
    month: int = 0
    day: int = 0

    @property
    def brand_family(self) -> str:
        if self.model.startswith("KE"):
            return "kunlun"
        if self.model.startswith("BTF"):
            return "colorful"
        if self.model.startswith("DM"):
            return "segotep"
        return "segotep"

    @property
    def certification_label(self) -> str:
        return CERT_80PLUS_MAP.get(self.certification_80plus, self.certification_80plus or "Unknown")

    @property
    def firmware_display(self) -> str:
        if self.year or self.month or self.day:
            return f"V{self.version}.{self.year}.{self.month:02d}.{self.day:02d}"
        return f"V{self.version}"


@dataclass(slots=True)
class PowerSerial:
    """PSU serial number response POWER_SN_RESPONSE (0x05)."""

    sn: str
    raw_bytes: bytes


@dataclass(slots=True)
class PowerExtraTelemetry:
    """Unmapped 28-byte periodic frame cmd=0x04 observed on live KE-1300P hardware.

    HiMOS.exe skips cmd=0x04 in BC#5142; kept as raw 14 x u16LE words for inspection
    without claiming unverified engineering units.
    """

    u16_words: tuple[int, ...]
    raw_payload: bytes


class SpecsMerger:
    """Reimplements HiMOS Pt.updateSpecsInfo to merge sub=0 (vendor) + sub=53 (date)."""

    def __init__(self) -> None:
        self.current: PowerInfo | None = None

    def update(self, incoming: PowerInfo) -> PowerInfo:
        if self.current is None:
            self.current = incoming
            return incoming

        manufacture = incoming.manufacture if incoming.manufacture else self.current.manufacture
        if incoming.year != 0 or incoming.month != 0 or incoming.day != 0:
            year, month, day = incoming.year, incoming.month, incoming.day
        else:
            year, month, day = self.current.year, self.current.month, self.current.day

        merged = PowerInfo(
            model=incoming.model.strip(),
            version=incoming.version,
            rated_input_current=incoming.rated_input_current,
            rated_output_current=incoming.rated_output_current,
            rated_input_low_voltage=incoming.rated_input_low_voltage,
            rated_input_high_voltage=incoming.rated_input_high_voltage,
            rated_power=incoming.rated_power,
            warranty=incoming.warranty,
            certification_80plus=incoming.certification_80plus,
            manufacture=manufacture,
            year=year,
            month=month,
            day=day,
        )
        self.current = merged
        return merged


def encode_frame(command_content: bytes | bytearray, use_crc: bool = True) -> bytes:
    """Wrap commandContent in [0x55, 0x7E] + content + [crc8(content)] + [0xAE]."""
    chk = crc8(command_content) if use_crc else 0
    return START_FLAG + bytes(command_content) + bytes([chk, END_FLAG])


def build_info_request_frame(sub: int = 0) -> bytes:
    """Build POWER_INFO (0x04) query. sub=0 returns vendor/SN; sub=53 returns build date."""
    return encode_frame(bytes([4, CmdId.POWER_INFO, sub & 0xFF, 0]))


def build_fan_mode_frame(mode: FanMode | int) -> bytes:
    """Build POWER_FAN_MODE (0x13) command."""
    return encode_frame(bytes([4, CmdId.POWER_FAN_MODE, 0, int(mode) & 0xFF]))


def build_refresh_mode_frame(mode: RefreshMode | int) -> bytes:
    """Build POWER_REFRESH_MODE (0x16) command."""
    return encode_frame(bytes([4, CmdId.POWER_REFRESH_MODE, 0, int(mode) & 0xFF]))


def build_fan_curve_frame(points: Sequence[Sequence[int]], validate: bool = True) -> bytes:
    """Build POWER_FAN_CUSTOM (0x1B) command from 5 [temp, duty] control points."""
    curve_bytes = resample_fan_curve(points, validate=validate)
    return encode_frame(bytes([len(curve_bytes) + 2, CmdId.POWER_FAN_CUSTOM]) + curve_bytes)


def build_clear_fault_frame() -> bytes:
    """Build POWER_CLEAR_FAULT_CODE (0x1E) command."""
    return encode_frame(bytes([4, CmdId.POWER_CLEAR_FAULT_CODE, 0, 0]))


def parse_frame(frame: bytes | bytearray) -> RawFrame:
    """Parse a complete single wire frame into RawFrame."""
    data = bytes(frame)
    if len(data) < 6:
        raise ValueError(f"Frame too short ({len(data)} bytes)")
    if data[:2] != START_FLAG or data[-1] != END_FLAG:
        raise ValueError(f"Invalid frame delimiters: {data.hex(' ')}")
    length_byte = data[2]
    expected_len = 4 + length_byte
    if len(data) != expected_len:
        raise ValueError(f"Frame length mismatch: header declares {expected_len}, got {len(data)}")
    return RawFrame(
        length_byte=length_byte,
        cmd_id=data[3],
        payload=data[4:-2],
        wire_crc=data[-2],
        raw_bytes=data,
        crc_valid=verify_rx_crc(data),
    )


def parse_command_content(
    cmd_id: int, payload: bytes
) -> PowerStatus | PowerInfo | PowerSerial | PowerExtraTelemetry | bytes:
    """Decode command payload matching HiMOS parseCommandContent."""
    if cmd_id == CmdId.POWER_STATUS_RESPONSE:
        if len(payload) < 28:
            raise ValueError(f"POWER_STATUS_RESPONSE too short: {len(payload)} < 28")
        mode_raw = payload[8]
        try:
            mode = FanMode(mode_raw)
        except ValueError:
            mode = FanMode.UNKNOWN
        return PowerStatus(
            voltage_3v3=js_round_2(int.from_bytes(payload[0:2], "little") / 1000.0),
            voltage_5v=js_round_2(int.from_bytes(payload[2:4], "little") / 1000.0),
            voltage_12v=js_round_2(int.from_bytes(payload[4:6], "little") / 1000.0),
            voltage_5v_sb=js_round_2(int.from_bytes(payload[6:8], "little") / 1000.0),
            mode=mode,
            reserved=payload[9:12],
            power=js_round_2(int.from_bytes(payload[12:14], "little") / 20.0),
            frequency=js_round_2(int.from_bytes(payload[14:16], "little") * 0.1),
            high_voltage_temperature=js_round_2(int.from_bytes(payload[16:18], "little") / 256.0),
            low_voltage_temperature=js_round_2(int.from_bytes(payload[18:20], "little") / 256.0),
            environment_temperature=js_round_2(int.from_bytes(payload[20:22], "little") / 256.0),
            input_voltage=js_round_2(int.from_bytes(payload[22:24], "little") * 0.1),
            fan_speed=int.from_bytes(payload[24:26], "little") * 30,
            malfunction_code=int.from_bytes(payload[26:28], "little"),
        )

    if cmd_id == CmdId.POWER_INFO_RESPONSE:
        if len(payload) < 25:
            raise ValueError(f"POWER_INFO_RESPONSE too short: {len(payload)} < 25")
        is_vendor_info = payload[23] > 12 or payload[24] > 31
        manufacture = payload[18:25].decode("utf-8", errors="replace").rstrip("\x00").strip() if is_vendor_info else ""
        return PowerInfo(
            model=payload[0:8].decode("utf-8", errors="replace").rstrip("\x00").strip(),
            version=payload[9],
            rated_input_current=payload[10],
            rated_output_current=payload[11],
            rated_input_low_voltage=payload[12],
            rated_input_high_voltage=payload[13],
            rated_power=int.from_bytes(payload[14:16], "big"),  # Big-Endian per HiMOS readUInt16BE!
            warranty=payload[16],
            certification_80plus=payload[17:18].decode("utf-8", errors="replace").strip(),
            manufacture=manufacture,
            year=0 if is_vendor_info else payload[22],
            month=0 if is_vendor_info else payload[23],
            day=0 if is_vendor_info else payload[24],
        )

    if cmd_id == CmdId.POWER_SN_RESPONSE:
        if len(payload) < 15:
            raise ValueError(f"POWER_SN_RESPONSE too short: {len(payload)} < 15")
        sn = payload[0:15].decode("utf-8", errors="replace").rstrip("\x00 ")
        return PowerSerial(sn=sn, raw_bytes=payload)

    if cmd_id == CmdId.POWER_INFO and len(payload) >= 28:
        words = tuple(int.from_bytes(payload[i : i + 2], "little") for i in range(0, 28, 2))
        return PowerExtraTelemetry(u16_words=words, raw_payload=payload)

    return payload


@dataclass(slots=True)
class StreamFrameParser:
    """Resynchronizing byte-stream extractor for HiMOS frames."""

    verify_crc: bool = True
    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes | bytearray) -> list[RawFrame]:
        """Append incoming bytes and extract all valid complete frames."""
        if chunk:
            self._buf.extend(chunk)
        frames: list[RawFrame] = []

        while True:
            start = self._buf.find(START_FLAG)
            if start == -1:
                # Keep at most a trailing 0x55 in case START_FLAG spans two USB reads.
                if self._buf.endswith(b"\x55"):
                    self._buf = bytearray(b"\x55")
                else:
                    self._buf.clear()
                break

            if start > 0:
                del self._buf[:start]

            if len(self._buf) < 4:
                break

            length_byte = self._buf[2]
            total_len = 4 + length_byte
            if length_byte < 2 or total_len > 260:
                # False header sync; advance past the first 0x55 and resync.
                del self._buf[:1]
                continue

            if len(self._buf) < total_len:
                break

            candidate = bytes(self._buf[:total_len])
            if candidate[-1] != END_FLAG:
                del self._buf[:1]
                continue

            raw_frame = parse_frame(candidate)
            if self.verify_crc and not raw_frame.crc_valid:
                del self._buf[:1]
                continue

            del self._buf[:total_len]
            frames.append(raw_frame)

        return frames

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)


def format_warranty_badge(info: PowerInfo, sn: str) -> tuple[str, str | None]:
    """Return HiMOS warranty label and limited-edition No.X/1000 badge if applicable."""
    if info.version < 12 and len(sn) >= 4:
        tail = sn[-4:]
        if tail.isdigit() and int(tail) < 1001:
            trimmed = tail.lstrip("0") or "0"
            return "终身质保 (Lifetime)", f"No.{trimmed}/1000"
    return f"{info.warranty} 年", None


def summary_dict(obj: Any) -> dict[str, Any]:
    """Serialize dataclass response cleanly for JSON CLI output."""
    if isinstance(obj, PowerStatus):
        return {
            "type": "status",
            "voltage_3v3": obj.voltage_3v3,
            "voltage_5v": obj.voltage_5v,
            "voltage_12v": obj.voltage_12v,
            "voltage_5v_sb": obj.voltage_5v_sb,
            "mode": obj.mode.label,
            "mode_id": int(obj.mode),
            "power_w": obj.power,
            "frequency_hz": obj.frequency,
            "high_voltage_temp_c": obj.high_voltage_temperature,
            "low_voltage_temp_c": obj.low_voltage_temperature,
            "environment_temp_c": obj.environment_temperature,
            "input_voltage_v": obj.input_voltage,
            "fan_speed_rpm": obj.fan_speed,
            "malfunction_code": f"0x{obj.malfunction_code:x}",
            "reserved_hex": obj.reserved.hex(),
        }
    if isinstance(obj, PowerInfo):
        return {
            "type": "info",
            "model": obj.model,
            "brand_family": obj.brand_family,
            "version": obj.version,
            "firmware_display": obj.firmware_display,
            "rated_power_w": obj.rated_power,
            "rated_input_current_a": obj.rated_input_current,
            "rated_output_current_a": obj.rated_output_current,
            "rated_input_low_v": obj.rated_input_low_voltage,
            "rated_input_high_v": obj.rated_input_high_voltage,
            "warranty_years": obj.warranty,
            "certification_80plus": obj.certification_80plus,
            "certification_label": obj.certification_label,
            "manufacture": obj.manufacture,
            "build_date": {"year": obj.year, "month": obj.month, "day": obj.day},
        }
    if isinstance(obj, PowerSerial):
        return {"type": "sn", "sn": obj.sn}
    if isinstance(obj, PowerExtraTelemetry):
        return {
            "type": "extra_0x04",
            "u16_words": list(obj.u16_words),
            "raw_hex": obj.raw_payload.hex(),
        }
    return {"type": "raw", "hex": obj.hex() if isinstance(obj, (bytes, bytearray)) else str(obj)}
