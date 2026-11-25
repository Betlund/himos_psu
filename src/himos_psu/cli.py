"""Command-line interface for Segotep / Kunlun HiMOS PSUs."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from .client import PowerSupplyClient
from .collector import Collector, CollectorBusy, StaleError, collector_lock
from .daemon import Daemon
from .config import (
    PERSISTABLE_FAN_MODES,
    apply_profile_to_client,
    default_config_path,
    load_saved_profile,
    persist_device_settings,
    plan_profile_frames,
)
from .energy import (
    DEFAULT_DB,
    EnergyStore,
    TariffPlan,
    fixed_plan,
    format_kwh,
    format_yuan,
    local_day_end,
    local_day_start,
    parse_band_spec,
    preset_plan,
)
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
from .transport import discover_serial_ports, resolve_default_port

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
    if args.save and mode not in PERSISTABLE_FAN_MODES:
        raise ValueError(f"模式 {mode.label} 为一次性模式，不允许持久化保存")
    points = args.curve if mode == FanMode.CUSTOM else None
    if mode == FanMode.CUSTOM and points is None:
        points = list(DEFAULT_CUSTOM_POINTS)
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        client.set_fan_mode(mode, custom_points=points)
        print(f"已发送风扇模式指令: {mode.label} (id={int(mode)})")
    if args.save:
        if mode == FanMode.CUSTOM and points is not None:
            saved = persist_device_settings(fan_mode=mode, custom_curve=points, path=args.config)
        else:
            saved = persist_device_settings(fan_mode=mode, path=args.config)
        print(f"已保存配置到: {saved}")
    return 0


def _cmd_fan_curve(args: argparse.Namespace) -> int:
    curve = args.points
    raw_27 = resample_fan_curve(curve, validate=True)
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        frame = client.set_fan_curve(curve, switch_to_custom=not args.no_mode_switch)
        print(f"已下发自定义风扇曲线控制点: {curve}")
        print(f"21 档 PWM 占空比(%): {list(raw_27[:21])}")
        print(f"TX 帧 ({len(frame)}B): {frame.hex(' ')}")
    if args.save:
        if args.no_mode_switch:
            saved = persist_device_settings(custom_curve=curve, path=args.config)
        else:
            saved = persist_device_settings(fan_mode=FanMode.CUSTOM, custom_curve=curve, path=args.config)
        print(f"已保存配置到: {saved}")
    return 0


def _cmd_refresh_rate(args: argparse.Namespace) -> int:
    refresh = RefreshMode.from_value(args.rate)
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        frame = client.set_refresh_mode(refresh)
        print(f"已设置刷新速率: {refresh.interval_ms} ms (index={int(refresh)}), TX={frame.hex(' ')}")
    if args.save:
        saved = persist_device_settings(refresh_mode=refresh, path=args.config)
        print(f"已保存配置到: {saved}")
    return 0


def _wait_for_port(port: str | None, wait_s: float) -> str:
    deadline = time.monotonic() + max(0.0, wait_s)
    last_err: Exception | None = None
    while True:
        try:
            resolved = resolve_default_port(port)
            if os.path.exists(resolved) and os.access(resolved, os.R_OK | os.W_OK):
                return resolved
            last_err = PermissionError(f"串口不可访问或尚未就绪: {resolved}")
        except FileNotFoundError as exc:
            last_err = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_err is not None:
                raise last_err
            raise TimeoutError(f"等待串口就绪超时 ({wait_s:.1f}s)")
        time.sleep(min(0.5, remaining))


def _cmd_apply_saved(args: argparse.Namespace) -> int:
    cfg_path = args.config if args.config is not None else default_config_path()
    profile = load_saved_profile(cfg_path)
    if profile is None or not profile.has_device_settings:
        if not args.quiet:
            print(f"未发现已保存的风扇/刷新配置 ({cfg_path})，跳过下发。")
        return 0
    if not profile.auto_restore and not args.force:
        if not args.quiet:
            print("auto_restore 已禁用，跳过下发（可加 --force 强制应用）。")
        return 0

    plan = plan_profile_frames(profile)
    if args.dry_run:
        print(f"配置文件: {cfg_path}")
        if profile.fan_mode is not None:
            print(f"计划恢复模式: {profile.fan_mode.label} (id={int(profile.fan_mode)})")
        if profile.custom_curve is not None and profile.fan_mode == FanMode.CUSTOM:
            print(f"计划恢复曲线: {list(profile.custom_curve)}")
            if plan.curve_frame is not None:
                print(f"21 档 PWM 占空比(%): {list(plan.curve_frame[4:25])}")
        if profile.refresh_mode is not None:
            print(f"计划刷新间隔: {profile.refresh_mode.interval_ms} ms")
        if plan.refresh_frame is not None:
            print(f"TX 刷新周期帧 (8B): {plan.refresh_frame.hex(' ')}")
        if plan.mode_frame is not None:
            print(f"TX 风扇模式帧 (8B): {plan.mode_frame.hex(' ')}")
        if plan.curve_frame is not None:
            print(f"TX 自定义曲线帧 ({len(plan.curve_frame)}B): {plan.curve_frame.hex(' ')}")
        print("dry-run 完成：未打开串口，未向电源发送任何数据。")
        return 0

    db_path = Path(args.db or os.environ.get("HIMOS_PSU_DB") or DEFAULT_DB)
    lock_path = db_path.parent / "collector.lock"
    use_lock = (not args.mock) or (args.db is not None)

    def _run_apply() -> int:
        target_port = args.port
        if not args.mock and args.wait_port > 0:
            target_port = _wait_for_port(args.port, args.wait_port)
        with PowerSupplyClient(port=target_port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
            initial_status = client.wait_for_status(timeout=args.timeout)
            res = apply_profile_to_client(
                client,
                profile,
                initial_status=initial_status,
                force=args.force,
                if_needed=args.if_needed,
                confirm_timeout=args.timeout,
            )
        if res.skipped_reason.startswith("fault_code:"):
            code = res.skipped_reason.split(":", 1)[1]
            print(f"警告: 电源存在锁存故障码 ({code})，出于安全已跳过自动恢复（可用 --force 强制覆盖）。", file=sys.stderr)
            return 1
        if not res.applied:
            if not args.quiet:
                mode_str = res.initial_mode.label if res.initial_mode is not None else "unknown"
                print(f"跳过下发（原因: {res.skipped_reason or '无需变更'}，当前模式: {mode_str}）。")
            return 0
        if not args.quiet:
            actions: list[str] = []
            if res.sent_refresh and profile.refresh_mode is not None:
                actions.append(f"刷新={profile.refresh_mode.interval_ms}ms")
            if res.sent_mode and profile.fan_mode is not None:
                actions.append(f"模式={profile.fan_mode.label}")
            if res.sent_curve:
                actions.append("自定义曲线")
            print(f"已恢复已保存配置 ({', '.join(actions)})。")
        if profile.fan_mode is not None and res.confirmed_mode != profile.fan_mode:
            seen = res.confirmed_mode.label if res.confirmed_mode is not None else "无"
            print(
                f"警告: 指令已发送，但在 {args.timeout:.1f}s 内未收到电源切至 {profile.fan_mode.label} 的遥测确认（最后回报: {seen}）。",
                file=sys.stderr,
            )
        elif not args.quiet and res.confirmed_mode is not None:
            print(f"已确认电源当前模式: {res.confirmed_mode.label}")
        return 0

    if not use_lock:
        return _run_apply()
    try:
        with collector_lock(lock_path):
            return _run_apply()
    except CollectorBusy as exc:
        if args.skip_if_busy:
            if not args.quiet:
                print(f"串口已被其他采集进程占用，跳过本次下发: {exc}")
            return 0
        raise


def _default_systemd_exec() -> str:
    repo_bin = Path(__file__).resolve().parents[2] / "bin" / "himos-psu"
    if repo_bin.is_file():
        candidate = str(repo_bin)
    else:
        which_bin = shutil.which("himos-psu")
        candidate = which_bin if which_bin else f"{sys.executable} -m himos_psu"
    home_str = str(Path.home())
    if candidate == home_str or candidate.startswith(home_str + os.sep):
        candidate = "%h" + candidate[len(home_str):]
    return candidate


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Run the background collector daemon (owns the serial port, serves IPC)."""
    from .collector import CollectorBusy as _Busy
    from .ipc import default_socket_path

    socket_path = Path(args.socket) if args.socket else default_socket_path()

    def log(text: str) -> None:
        if not args.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    daemon = Daemon(
        db_path=args.db,
        socket_path=socket_path,
        port=args.port,
        mock=args.mock,
        restore=not args.no_restore,
        wait_port=args.wait_port,
        stale_timeout=args.stale_timeout,
        config_path=args.config,
        quiet=args.quiet,
        on_log=log,
    )
    try:
        return daemon.run()
    except _Busy as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


DAEMON_UNIT_NAME = "himos-psu-collect.service"
LEGACY_UNIT_NAME = "himos-psu-restore.service"


def _daemon_unit_text(args: argparse.Namespace) -> str:
    exec_bin = args.exec.strip() if args.exec else _default_systemd_exec()
    home_str = str(Path.home())
    if exec_bin == home_str or exec_bin.startswith(home_str + os.sep):
        exec_bin = "%h" + exec_bin[len(home_str):]
    # StartLimit* are [Unit] keys since systemd 230; in [Service] they are silently
    # ignored ("Unknown key" in the journal) and the rate limit stays at the default.
    return (
        "[Unit]\n"
        "Description=HiMOS PSU background collector (serial owner + IPC daemon)\n"
        "After=default.target\n"
        "StartLimitIntervalSec=300\n"
        "StartLimitBurst=10\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        # No --quiet: under systemd the journal is the only place the daemon can
        # explain itself, and "exited status=1" without a reason is undebuggable.
        f"ExecStart={exec_bin} daemon --wait-port {max(0.0, args.wait_port):g}"
        f" --stale-timeout {max(0.0, args.stale_timeout):g}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _run_systemctl(argv: Sequence[str], *, dry_run: bool) -> int:
    printable = " ".join(argv)
    if dry_run:
        print(f"$ {printable}")
        return 0
    if shutil.which(argv[0]) is None:
        print(f"错误: 找不到 {argv[0]}，请手动执行: {printable}", file=sys.stderr)
        return 1
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"错误: 执行 {printable} 失败: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        print(f"错误: {printable} 返回 {proc.returncode}: {detail}", file=sys.stderr)
        if argv[0] == "systemctl" and "--user" in argv:
            print(
                "提示: systemctl --user 需要登录会话的 D-Bus；在 SSH、nspawn 或容器里会连不上，"
                "请在桌面终端里重试。",
                file=sys.stderr,
            )
        return 1
    return 0


def _cmd_systemd_daemon(args: argparse.Namespace) -> int:
    unit = _daemon_unit_text(args)
    if not args.install:
        sys.stdout.write(unit)
        return 0
    return _install_daemon_unit(unit, args)


def _collector_busy() -> bool:
    """True when any process holds the default collector lock."""
    from .energy import DEFAULT_DB

    lock_path = Path(DEFAULT_DB).parent / "collector.lock"
    if not lock_path.exists():
        return False
    try:
        with collector_lock(lock_path):
            return False
    except CollectorBusy:
        return True
    except OSError:
        # Cannot open the lock file (permissions, read-only mount): let systemctl
        # report the real failure instead of blocking installation here.
        return False


def _user_unit_active(unit: str) -> bool | None:
    """True when ``unit`` is active, None when systemd cannot be asked."""
    if shutil.which("systemctl") is None:
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode in (1, 3, 4):  # failed / inactive / no such unit
        return False
    return None


def _install_daemon_unit(unit: str, args: argparse.Namespace) -> int:
    from .ipc import default_socket_path, probe_socket

    dest_dir = _systemd_user_dir()
    dest = dest_dir / DAEMON_UNIT_NAME
    socket_path = default_socket_path()

    # The serial port is exclusive: enabling the unit while a hand-started daemon
    # (or `collect`, or a GUI collecting) still owns it only produces a crash loop.
    # Our own running service is the one exception — it only needs a restart.
    restart_needed = False
    if args.enable:
        daemon_up = probe_socket(socket_path)
        lock_held = not daemon_up and _collector_busy()
        if daemon_up or lock_held:
            ours = None if args.dry_run else _user_unit_active(DAEMON_UNIT_NAME)
            where = f"{socket_path}" if daemon_up else "默认采集锁"
            if ours:
                restart_needed = True
                print(f"服务已在运行（{where}），安装后会自动重启以应用新单元。")
            elif args.dry_run:
                print(f"注意: 已有采集进程在运行（{where}），实际执行时需要先停掉它。")
            else:
                print(
                    f"检测到已有采集进程在运行（{where}）。\n"
                    "请先在那个终端按 Ctrl+C 停掉它（手跑的 `himos-psu daemon` 或 GUI 采集），"
                    "然后重新执行本命令；否则新服务会因为串口被独占而反复启动失败。",
                    file=sys.stderr,
                )
                return 1

    if args.dry_run:
        print(f"将写入 {dest}")
    else:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_text(unit, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        except OSError as exc:
            print(f"错误: 无法写入 {dest}: {exc}", file=sys.stderr)
            return 1
        print(f"已写入 {dest}")

    legacy = dest_dir / LEGACY_UNIT_NAME
    if legacy.exists():
        print(
            f"提示: 旧的 {LEGACY_UNIT_NAME} 仍在，守护进程已包含启动恢复功能，建议退役它：\n"
            f"  systemctl --user disable --now {LEGACY_UNIT_NAME}\n"
            f"  rm {legacy}"
        )

    rc = _run_systemctl(["systemctl", "--user", "daemon-reload"], dry_run=args.dry_run)
    if rc != 0:
        return rc
    if args.enable:
        rc = _run_systemctl(
            ["systemctl", "--user", "enable", "--now", DAEMON_UNIT_NAME], dry_run=args.dry_run
        )
        if rc != 0:
            return rc
        if restart_needed:
            rc = _run_systemctl(
                ["systemctl", "--user", "restart", DAEMON_UNIT_NAME], dry_run=args.dry_run
            )
            if rc != 0:
                return rc
    if args.linger:
        user = os.environ.get("USER") or getpass.getuser()
        rc = _run_systemctl(["loginctl", "enable-linger", user], dry_run=args.dry_run)
        if rc != 0:
            return rc

    print(f"查看状态: systemctl --user status {DAEMON_UNIT_NAME}")
    print(f"查看日志: journalctl --user -u {DAEMON_UNIT_NAME} -f")
    return 0


def _cmd_systemd_unit(args: argparse.Namespace) -> int:
    exec_bin = args.exec.strip() if args.exec else _default_systemd_exec()
    home_str = str(Path.home())
    if exec_bin == home_str or exec_bin.startswith(home_str + os.sep):
        exec_bin = "%h" + exec_bin[len(home_str):]
    wait_arg = f"{max(0.0, args.wait_port):g}"
    unit = (
        "[Unit]\n"
        "Description=Restore saved HiMOS PSU fan curve and mode on boot\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_bin} apply-saved --wait-port {wait_arg} --skip-if-busy\n"
        "TimeoutStartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    sys.stdout.write(unit)
    return 0


def _cmd_clear_fault(args: argparse.Namespace) -> int:
    with PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check) as client:
        frame = client.clear_fault_code()
        print(f"已发送清除故障码指令 (0x1E), TX={frame.hex(' ')}")
    return 0


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    return launch_gui(port=args.port, mock=args.mock)



def _period_bounds(period: str) -> tuple[str, int, int]:
    """Return (label, start_ts, end_ts) for a named reporting period."""
    today = date.today()
    if period == "yesterday":
        start_day = end_day = today - timedelta(days=1)
        return "昨日", local_day_start(start_day), local_day_end(end_day)
    if period == "week":
        start_day = today - timedelta(days=today.weekday())
        end_day = today
    elif period == "month":
        start_day, end_day = today.replace(day=1), today
    elif period == "year":
        start_day, end_day = date(today.year, 1, 1), today
    else:
        start_day = end_day = today
    label = {"today": "今日", "week": "本周", "month": "本月", "year": "今年"}[period]
    end_ts = min(int(time.time()), local_day_end(end_day))
    return label, local_day_start(start_day), max(end_ts, local_day_start(start_day))


def _print_energy_report(store: EnergyStore, label: str, start_ts: int, end_ts: int,
                         as_json: bool, rated_w: float = 0.0) -> int:
    report = store.report(label, start_ts, end_ts)
    bands = store.band_breakdown(start_ts, end_ts)
    if as_json:
        payload = report.as_dict()
        payload["bands"] = bands
        payload["db"] = str(store.path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if report.samples == 0:
        print(f"{label}：该区间没有采集数据。")
        print("能耗统计依赖采集进程：运行 `himos-psu collect`（或打开 GUI）以开始记录。")
        return 1
    span = f"{datetime.fromtimestamp(start_ts):%Y-%m-%d}"
    if end_ts - start_ts > 86400:
        span += f" → {datetime.fromtimestamp(end_ts - 1):%Y-%m-%d}"
    print(f"{label}  {span}")
    print(f"  用电量    {format_kwh(report.kwh)} kWh")
    print(f"  电费      {format_yuan(report.cost_cents)}")
    print(f"  平均功率  {report.avg_w:.0f} W        峰值 {report.peak_w:.0f} W")
    print(f"  覆盖率    {report.coverage * 100:.1f}%（缺口 {report.gap_s:.0f} s）")
    if bands:
        parts = []
        for item in bands:
            parts.append(f"{item['name']} {format_kwh(item['kwh'])}kWh {format_yuan(item['cost_yuan'] * 100)} "
                         f"{item['share'] * 100:.0f}%")
        print("  分时段    " + " | ".join(parts))
    if report.carbon_kg:
        print(f"  碳排      {report.carbon_kg:.2f} kgCO₂")
    print(f"  数据源    {store.path}")
    return 0


def _cmd_energy_period(args: argparse.Namespace) -> int:
    label, start_ts, end_ts = _period_bounds(args.period)
    with EnergyStore(args.db, create=False) as store:
        return _print_energy_report(store, label, start_ts, end_ts, args.json)


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _cmd_energy_report(args: argparse.Namespace) -> int:
    start_day = _parse_date(args.from_date)
    end_day = _parse_date(args.to_date)
    if end_day < start_day:
        raise ValueError("--to 不能早于 --from")
    with EnergyStore(args.db, create=False) as store:
        return _print_energy_report(store, "区间", local_day_start(start_day),
                                    local_day_end(end_day), args.json)


def _cmd_energy_export(args: argparse.Namespace) -> int:
    start_ts = local_day_start(_parse_date(args.from_date)) if args.from_date else 0
    end_ts = local_day_end(_parse_date(args.to_date)) if args.to_date else int(time.time()) + 86400
    with EnergyStore(args.db, create=False) as store:
        rows = store.export_csv(args.kind, args.out, start_ts, end_ts)
    if args.out != "-":
        print(f"已导出 {rows} 行 → {args.out}")
    return 0


def _cmd_energy_prune(args: argparse.Namespace) -> int:
    with EnergyStore(args.db) as store:
        removed = store.prune_raw(args.days)
        print(f"已删除 {removed} 条 {args.days} 天前的原始样本（分钟聚合与日汇总保留）。")
    return 0


def _cmd_tariff_list(args: argparse.Namespace) -> int:
    with EnergyStore(args.db, create=False) as store:
        active = store.active_plan()
        print(f"当前生效: {active.describe() if active else '(无方案)'}")
        for plan in store.plans()[:12]:
            marker = "*" if active and plan.plan_id == active.plan_id else " "
            print(f" {marker} #{plan.plan_id:<3} {plan.describe()}  生效自 {plan.effective_from}")
        print("\n提示：内置方案价格为示例值，请按当地电价修改：himos-psu tariff set ...")
    return 0


def _cmd_tariff_set(args: argparse.Namespace) -> int:
    choices = [bool(args.price is not None), bool(args.bands), bool(args.preset)]
    if sum(choices) != 1:
        raise ValueError("请指定 --price（固定电价）或 --bands（峰谷时段）或 --preset cn 三者之一")
    if args.price is not None:
        plan = fixed_plan(args.name, args.price)
    elif args.preset:
        plan = preset_plan(args.name)
    else:
        plan = TariffPlan(name=args.name, kind="tou", bands=parse_band_spec(args.bands),
                          effective_from=date.today().isoformat())
    with EnergyStore(args.db) as store:
        stored = store.set_plan(plan)
        print(f"已保存电价方案: {stored.describe()}")
        if args.recalc:
            rows = store.recalc_costs()
            print(f"已按新电价重算 {rows} 个分钟桶的历史费用。")
        else:
            print("历史费用未重算；如需应用新电价请加 --recalc 或运行 `himos-psu tariff recalc`。")
    return 0


def _cmd_tariff_recalc(args: argparse.Namespace) -> int:
    with EnergyStore(args.db) as store:
        rows = store.recalc_costs()
        print(f"已重算 {rows} 个分钟桶的费用（使用当前生效方案）。")
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    store = EnergyStore(args.db)
    client = PowerSupplyClient(port=args.port, mock=args.mock, verify_crc=not args.no_crc_check)
    last_report = [time.monotonic()]

    def on_state(text: str) -> None:
        if not args.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def on_status(status) -> None:
        if args.quiet or time.monotonic() - last_report[0] < 15:
            return
        last_report[0] = time.monotonic()
        print(f"[{time.strftime('%H:%M:%S')}] {status.power:.1f} W · 累计 "
              f"{format_kwh(collector.stats.kwh)} kWh · {collector.stats.samples} 样本"
              + (f" · 缺口 {collector.stats.gaps} 次" if collector.stats.gaps else ""), flush=True)

    collector = Collector(
        client, store, on_state=on_state, on_status=on_status,
        stale_timeout=getattr(args, "stale_timeout", 0.0),
    )
    try:
        collector.run()
    except KeyboardInterrupt:
        pass
    except CollectorBusy as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except StaleError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if not args.quiet:
        print(f"采集结束：{collector.stats.samples} 样本，{format_kwh(collector.stats.kwh)} kWh。")
    return 0


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

    # Shared connection flags accepted both before and after subcommands like gui / apply-saved.
    conn_flags = argparse.ArgumentParser(add_help=False)
    conn_flags.add_argument("-p", "--port", default=argparse.SUPPRESS, help="串口路径")
    conn_flags.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="离线模拟设备")
    conn_flags.add_argument("--no-crc-check", action="store_true", default=argparse.SUPPRESS, help="不校验 CRC")

    p_mode = sub.add_parser("fan-mode", help="设置风扇模式: auto/silent/performance/custom/clean")
    p_mode.add_argument("mode", help="目标风扇模式")
    p_mode.add_argument("--curve", nargs=5, type=_parse_point, metavar="T,D", help="当 mode=custom 时一并下发 5 个点")
    p_mode.add_argument("--save", action="store_true", help="同时保存到 settings.json（不支持 clean）")
    p_mode.add_argument("--config", type=Path, help="配置文件路径（默认 ~/.config/himos-psu/settings.json）")
    p_mode.set_defaults(func=_cmd_fan_mode)

    p_curve = sub.add_parser("fan-curve", help="设置自定义 5 点风扇曲线 (例如: 0,0 40,20 60,30 80,70 100,100)")
    p_curve.add_argument("points", nargs=5, type=_parse_point, metavar="T,D")
    p_curve.add_argument("--no-mode-switch", action="store_true", help="只发送 0x1B 曲线而不预先发送 Custom 模式")
    p_curve.add_argument("--save", action="store_true", help="同时保存曲线与模式到 settings.json")
    p_curve.add_argument("--config", type=Path, help="配置文件路径（默认 ~/.config/himos-psu/settings.json）")
    p_curve.set_defaults(func=_cmd_fan_curve)

    p_rate = sub.add_parser("refresh-rate", help="设置电源遥测主动推送间隔 (1000/1500/2000/2500/3000ms)")
    p_rate.add_argument("rate")
    p_rate.add_argument("--save", action="store_true", help="同时保存到 settings.json")
    p_rate.add_argument("--config", type=Path, help="配置文件路径（默认 ~/.config/himos-psu/settings.json）")
    p_rate.set_defaults(func=_cmd_refresh_rate)

    p_apply = sub.add_parser("apply-saved", parents=[conn_flags], help="从本地配置安全恢复上次保存的风扇模式/曲线")
    p_apply.add_argument("--config", type=Path, help="配置文件路径（默认 ~/.config/himos-psu/settings.json）")
    p_apply.add_argument("--db", help="能耗数据库路径（用于定位 collector.lock）")
    p_apply.add_argument("--dry-run", action="store_true", help="仅校验并打印计划下发的帧，不打开串口")
    p_apply.add_argument("--if-needed", action="store_true", help="仅当电源当前模式与保存值不一致时才下发")
    p_apply.add_argument("--force", action="store_true", help="忽略 auto_restore=false、同模式跳过或锁存故障码保护")
    p_apply.add_argument("--wait-port", type=float, default=0.0, help="等待串口就绪的最大秒数（开机服务可用 15）")
    p_apply.add_argument("--skip-if-busy", action="store_true", help="若串口已被 GUI 或 collect 占用则静默退出 0")
    p_apply.add_argument("--timeout", type=float, default=4.0, help="等待初始状态与切换确认的超时秒数")
    p_apply.add_argument("--quiet", action="store_true", help="成功或跳过时减少输出")
    p_apply.set_defaults(func=_cmd_apply_saved)

    p_clear = sub.add_parser("clear-fault", help="清除电源锁存故障码 (0x1E)")
    p_clear.set_defaults(func=_cmd_clear_fault)

    p_energy = sub.add_parser("energy", help="查看能耗统计（今日/本周/本月/今年/自定义区间）")
    p_energy.add_argument("--db", help="能耗数据库路径（默认 ~/.local/share/himos-psu/energy.db）")
    esub = p_energy.add_subparsers(dest="energy_cmd", required=True)
    for name, text in (("today", "今日"), ("yesterday", "昨日"), ("week", "本周"),
                       ("month", "本月"), ("year", "今年")):
        p_period = esub.add_parser(name, help=f"{text}能耗报告")
        p_period.add_argument("--json", action="store_true")
        p_period.set_defaults(func=_cmd_energy_period, period=name)
    p_report = esub.add_parser("report", help="自定义日期区间的能耗报告")
    p_report.add_argument("--from", dest="from_date", required=True, metavar="YYYY-MM-DD")
    p_report.add_argument("--to", dest="to_date", required=True, metavar="YYYY-MM-DD")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=_cmd_energy_report)
    p_export = esub.add_parser("export", help="导出 CSV（分钟明细/每日汇总/原始样本/供电会话）")
    p_export.add_argument("--kind", choices=["minute", "daily", "samples", "sessions"], default="daily")
    p_export.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
    p_export.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
    p_export.add_argument("--out", default="-", help="输出文件，- 表示标准输出")
    p_export.set_defaults(func=_cmd_energy_export)
    p_prune = esub.add_parser("prune", help="清理超过保留期的原始样本（分钟/日汇总保留）")
    p_prune.add_argument("--days", type=int, default=14)
    p_prune.set_defaults(func=_cmd_energy_prune)

    p_tariff = sub.add_parser("tariff", help="管理电价方案（固定电价 / 峰谷分时）")
    p_tariff.add_argument("--db", help="能耗数据库路径")
    tsub = p_tariff.add_subparsers(dest="tariff_cmd", required=True)
    p_tl = tsub.add_parser("list", help="列出生效方案与历史方案")
    p_tl.set_defaults(func=_cmd_tariff_list)
    p_ts = tsub.add_parser("set", help="新增并启用一个电价方案")
    p_ts.add_argument("--name", default="自定义方案")
    p_ts.add_argument("--price", type=float, help="固定电价（元/kWh）")
    p_ts.add_argument("--bands", help="峰谷时段，如 0-7:谷:0.32,7-10:平:0.58,10-24:峰:0.92")
    p_ts.add_argument("--preset", choices=["cn"], help="使用内置示例峰谷方案")
    p_ts.add_argument("--recalc", action="store_true", help="保存后按新电价重算历史费用")
    p_ts.set_defaults(func=_cmd_tariff_set)
    p_tr = tsub.add_parser("recalc", help="按当前生效方案重算历史费用")
    p_tr.set_defaults(func=_cmd_tariff_recalc)

    p_collect = sub.add_parser("collect", help="前台采集能耗数据（Ctrl+C 停止；与 GUI/守护进程互斥）")
    p_collect.add_argument("--db", help="能耗数据库路径")
    p_collect.add_argument("--quiet", action="store_true", help="不打印进度")
    p_collect.add_argument("--stale-timeout", type=float, default=0.0,
                           help="超过 N 秒收不到帧就退出（供 systemd 重启；0=不检查）")
    p_collect.set_defaults(func=_cmd_collect)

    p_daemon = sub.add_parser("daemon", help="后台采集守护进程（独占串口，GUI 通过 IPC 连它）")
    p_daemon.add_argument("--db", help="能耗数据库路径")
    p_daemon.add_argument("--socket", help="IPC 套接字路径（默认 $XDG_RUNTIME_DIR/himos-psu/daemon.sock）")
    p_daemon.add_argument("--config", type=Path, help="配置文件路径")
    p_daemon.add_argument("-p", "--port", help="串口路径")
    p_daemon.add_argument("--mock", action="store_true", help="离线模拟设备")
    p_daemon.add_argument("--no-restore", action="store_true", help="连接后不恢复保存的风扇配置")
    p_daemon.add_argument("--wait-port", type=float, default=0.0, help="等待串口就绪的最大秒数")
    p_daemon.add_argument("--stale-timeout", type=float, default=0.0,
                          help="超过 N 秒收不到帧就退出（生产环境建议 180）")
    p_daemon.add_argument("--quiet", action="store_true", help="不打印进度")
    p_daemon.set_defaults(func=_cmd_daemon)

    p_sysd = sub.add_parser(
        "systemd-daemon",
        help="打印或安装后台采集守护进程的 systemd --user 单元",
        description="默认打印单元内容；加 --install 则写入 ~/.config/systemd/user/ 并可选立即启用。",
    )
    p_sysd.add_argument("--exec", help="自定义 himos-psu 可执行路径")
    p_sysd.add_argument("--wait-port", type=float, default=30.0, help="启动时等待串口就绪的秒数")
    p_sysd.add_argument("--stale-timeout", type=float, default=180.0, help="无数据多久后重启")
    p_sysd.add_argument("--install", action="store_true", help="写入 user 单元目录而不是打印到 stdout")
    p_sysd.add_argument("--enable", action="store_true", help="安装后执行 systemctl --user enable --now")
    p_sysd.add_argument("--linger", action="store_true", help="同时开启 linger（未登录也采集）")
    p_sysd.add_argument("--dry-run", action="store_true", help="只打印将要执行的 systemctl 命令")
    p_sysd.set_defaults(func=_cmd_systemd_daemon)

    p_udev = sub.add_parser("udev-rule", help="打印无需 root 访问 CH340 串口的 udev 规则示例")
    p_udev.set_defaults(func=lambda _: (sys.stdout.write(UDEV_RULE), 0)[1])

    p_systemd = sub.add_parser("systemd-unit", help="打印开机自动恢复电源配置的 systemd --user 服务单元")
    p_systemd.add_argument("--exec", help="自定义 himos-psu 可执行路径")
    p_systemd.add_argument("--wait-port", type=float, default=15.0, help="启动时等待串口就绪的最大秒数（默认 15）")
    p_systemd.set_defaults(func=_cmd_systemd_unit)

    p_gui = sub.add_parser("gui", parents=[conn_flags], help="启动 PySide6 桌面控制面板")
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
