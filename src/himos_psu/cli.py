"""Command-line interface for Segotep / Kunlun HiMOS PSUs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Sequence

from .client import PowerSupplyClient
from .fan_curve import DEFAULT_CUSTOM_POINTS, resample_fan_curve
from .protocol import (
    FanMode,
    PowerExtraTelemetry,
    PowerInfo,
    PowerStatus,
    RefreshMode,
    format_warranty_badge,
    summary_dict,
)
from .transport import discover_serial_ports

UDEV_RULE = 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0666"\n'


def _parse_point(token: str) -> tuple[int, int]:
    parts = token.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Point must look like temp,duty (e.g. 40,20); got {token!r}")
    return int(parts[0]), int(parts[1])


def _print_info(info: PowerInfo | None, sn: str, as_json: bool) -> None:
    if as_json:
        payload = summary_dict(info) if info else {"type": "info"}
        payload["sn"] = sn
        if info:
            warranty, badge = format_warranty_badge(info, sn)
            payload["warranty_label"] = warranty
            payload["edition_badge"] = badge
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if info is None:
        print(f"SN: {sn or '(unknown)'} (no POWER_INFO_RESPONSE received)")
        return

    warranty, badge = format_warranty_badge(info, sn)
    print(f"型号 Model        : {info.model} ({info.brand_family})")
    print(f"序列号 SN         : {sn or '(unknown)'}")
    print(f"固件版本 Firmware : {info.firmware_display}")
    print(f"80 PLUS 认证      : {info.certification_label}")
    print(f"额定功率 Rated    : {info.rated_power} W")
    print(f"额定电流 I/O Max  : {info.rated_input_current} A / {info.rated_output_current} A")
    print(f"输入电压范围      : {info.rated_input_low_voltage}V ~ {info.rated_input_high_voltage}V")
    print(f"代工/厂商 Vendor  : {info.manufacture or '(unknown)'}")
    badge_suffix = f"  [{badge}]" if badge else ""
    print(f"质保 Warranty     : {warranty}{badge_suffix}")


def _format_status_lines(st: PowerStatus, extra: PowerExtraTelemetry | None = None) -> str:
    fault_str = f"0x{st.malfunction_code:04x}" + (" (OK)" if st.malfunction_code == 0 else " (FAULT!)")
    lines = [
        f"输入功率: {st.power:6.2f} W  | 输入电压: {st.input_voltage:5.1f} V  | 输入频率: {st.frequency:4.1f} Hz",
        f"直流输出: +12V={st.voltage_12v:5.2f}V | +5V={st.voltage_5v:4.2f}V | +3.3V={st.voltage_3v3:4.2f}V | +5VSB={st.voltage_5v_sb:4.2f}V",
        f"电源温度: 高压={st.high_voltage_temperature:5.2f}°C | 低压={st.low_voltage_temperature:5.2f}°C | 环境={st.environment_temperature:5.2f}°C",
        f"风扇状态: {st.fan_speed:4d} RPM | 模式={st.mode.label} ({int(st.mode)}) | 故障码={fault_str}",
    ]
    if extra is not None:
        words_hex = " ".join(f"{w}" for w in extra.u16_words)
        lines.append(f"扩展帧(0x04 raw u16LE): [{words_hex}]")
    return "\n".join(lines)


def _cmd_list(args: argparse.Namespace) -> int:
    ports = discover_serial_ports()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "device": p.device,
                        "vendor_id": p.vendor_id,
                        "product_id": p.product_id,
                        "product": p.product,
                        "by_id": p.by_id,
                        "is_ch340": p.is_ch340,
                    }
                    for p in ports
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not ports:
        print("未检测到 /dev/ttyUSB* 或 /dev/ttyACM* 串口设备。")
        return 1
    for p in ports:
        tag = "[CH340 PSU]" if p.is_ch340 else "[Other]"
        vid_pid = f"{p.vendor_id or '????'}:{p.product_id or '????'}"
        print(f"{tag:11s} {p.device}  VID:PID={vid_pid}  {p.product or p.by_id}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        info, sn = client.query_specs_and_sn(timeout=args.timeout)
        _print_info(info, sn, args.json)
    return 0 if (info is not None or sn) else 1


def _cmd_status(args: argparse.Namespace) -> int:
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        status = client.wait_for_status(timeout=args.timeout)
        if args.json:
            data = summary_dict(status)
            if args.extra and client.last_extra:
                data["extra_0x04"] = summary_dict(client.last_extra)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(_format_status_lines(status, client.last_extra if args.extra else None))
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        info, sn = client.query_specs_and_sn(timeout=2.0)
        count = 0
        header = f"{info.model} ({sn})" if info else (sn or client.port_name)
        while args.count <= 0 or count < args.count:
            events = client.poll_frames(timeout=0.5)
            for raw, parsed in events:
                if args.raw:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] cmd=0x{raw.cmd_id:02x} crc_ok={raw.crc_valid}: {raw.raw_bytes.hex(' ')}", flush=True)
                    if isinstance(parsed, PowerStatus):
                        count += 1
                    continue
                if isinstance(parsed, PowerStatus):
                    count += 1
                    if args.json:
                        obj = summary_dict(parsed)
                        obj["ts"] = time.time()
                        obj["sn"] = sn
                        print(json.dumps(obj, ensure_ascii=False), flush=True)
                    else:
                        ts = time.strftime("%H:%M:%S")
                        if count > 1:
                            print("-" * 72)
                        print(f"[{ts}] {header} (#{count})")
                        print(_format_status_lines(parsed, client.last_extra if args.extra else None), flush=True)
    return 0


def _cmd_fan_mode(args: argparse.Namespace) -> int:
    mode = FanMode.from_name(args.mode)
    points = args.curve if mode == FanMode.CUSTOM else None
    if mode == FanMode.CUSTOM and points is None:
        points = list(DEFAULT_CUSTOM_POINTS)
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        client.set_fan_mode(mode, custom_points=points)
        print(f"已发送风扇模式指令: {mode.label} (id={int(mode)})")
    return 0


def _cmd_fan_curve(args: argparse.Namespace) -> int:
    curve = args.points
    raw_27 = resample_fan_curve(curve, validate=True)
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        frame = client.set_fan_curve(curve, switch_to_custom=not args.no_mode_switch)
        print(f"已下发自定义风扇曲线控制点: {curve}")
        print(f"21 档 PWM 占空比(%): {list(raw_27[:21])}")
        print(f"TX 帧 ({len(frame)}B): {frame.hex(' ')}")
    return 0


def _cmd_refresh_rate(args: argparse.Namespace) -> int:
    refresh = RefreshMode.from_value(args.rate)
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        frame = client.set_refresh_mode(refresh)
        print(f"已设置刷新速率: {refresh.interval_ms} ms (index={int(refresh)}), TX={frame.hex(' ')}")
    return 0


def _cmd_clear_fault(args: argparse.Namespace) -> int:
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        frame = client.clear_fault_code()
        print(f"已发送清除故障码指令 (0x1E), TX={frame.hex(' ')}")
    return 0


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    return launch_gui(port=args.port, mock=args.mock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="himos-psu",
        description="开源 Linux 鑫谷/昆仑数字电源上位机 (HiMOS CH340 1A86:7523 协议)",
    )
    parser.add_argument("-p", "--port", help="串口路径 (默认自动探测 CH340 1A86:7523)")
    parser.add_argument("--mock", action="store_true", help="使用离线模拟 KE-1300P 设备调试")
    parser.add_argument("--no-crc-check", action="store_true", help="允许接收 CRC 不匹配帧（仅调试用）")

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出检测到的串口与 CH340 电源设备")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_info = sub.add_parser("info", help="读取电源规格、版本号、出厂日期、厂商与序列号")
    p_info.add_argument("--timeout", type=float, default=2.5)
    p_info.add_argument("--json", action="store_true")
    p_info.set_defaults(func=_cmd_info)

    p_status = sub.add_parser("status", help="读取一帧实时功率、各路电压、温度与风扇转速")
    p_status.add_argument("--timeout", type=float, default=3.5)
    p_status.add_argument("--extra", action="store_true", help="同时显示未映射的 0x04 扩展遥测字")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_status)

    p_watch = sub.add_parser("watch", help="持续监控实时遥测或原始帧")
    p_watch.add_argument("-n", "--count", type=int, default=0, help="接收状态帧数量 (0=持续)")
    p_watch.add_argument("--raw", action="store_true", help="以十六进制打印全部原始帧")
    p_watch.add_argument("--extra", action="store_true")
    p_watch.add_argument("--json", action="store_true")
    p_watch.set_defaults(func=_cmd_watch)

    p_mode = sub.add_parser("fan-mode", help="设置风扇模式: auto/silent/performance/custom/clean")
    p_mode.add_argument("mode", help="目标风扇模式")
    p_mode.add_argument("--curve", nargs=5, type=_parse_point, metavar="T,D", help="当 mode=custom 时一并下发 5 个点")
    p_mode.set_defaults(func=_cmd_fan_mode)

    p_curve = sub.add_parser("fan-curve", help="设置自定义 5 点风扇曲线 (例如: 0,0 40,20 60,30 80,70 100,100)")
    p_curve.add_argument("points", nargs=5, type=_parse_point, metavar="T,D")
    p_curve.add_argument("--no-mode-switch", action="store_true", help="只发送 0x1B 曲线而不预先发送 Custom 模式")
    p_curve.set_defaults(func=_cmd_fan_curve)

    p_rate = sub.add_parser("refresh-rate", help="设置电源遥测主动推送间隔 (1000/1500/2000/2500/3000ms)")
    p_rate.add_argument("rate")
    p_rate.set_defaults(func=_cmd_refresh_rate)

    p_clear = sub.add_parser("clear-fault", help="清除电源锁存故障码 (0x1E)")
    p_clear.set_defaults(func=_cmd_clear_fault)

    p_udev = sub.add_parser("udev-rule", help="打印无需 root 访问 CH340 串口的 udev 规则示例")
    p_udev.set_defaults(func=lambda _: (sys.stdout.write(UDEV_RULE), 0)[1])

    # `himos-psu-gui --mock` expands to `himos-psu gui --mock`, so the gui subcommand must
    # accept the global connection flags too; SUPPRESS keeps values given before "gui" intact.
    gui_flags = argparse.ArgumentParser(add_help=False)
    gui_flags.add_argument("-p", "--port", default=argparse.SUPPRESS, help="串口路径")
    gui_flags.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="离线模拟设备")
    gui_flags.add_argument("--no-crc-check", action="store_true", default=argparse.SUPPRESS, help="不校验 CRC")

    p_gui = sub.add_parser("gui", parents=[gui_flags], help="启动 PySide6 桌面控制面板")
    p_gui.set_defaults(func=_cmd_gui)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
