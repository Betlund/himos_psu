#!/usr/bin/env python3
"""Sanitize a *private* checkout so it can be published.

Written for the reverse-engineering copy this public repo was derived from
(``~/Project/鑫谷电源上位机逆向/himos_psu``), but it works on any directory.

It never needs the real serial number as an argument:
  * any ``SBRC`` + 11 chars serial -> synthetic ``SBRC00000000123``
  * ``LIVE_SN_HEX`` frames are rebuilt so payload and CRC stay consistent
  * ``No.<digits>/1000`` edition badges -> ``No.123/1000``
  * ``__pycache__`` / ``*.pyc`` are deleted (bytecode embeds the same strings)

Dry-run by default; ``--apply`` writes.  ``--git`` additionally rewrites the
history of a git checkout so the old objects stop being reachable.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SYNTHETIC_SN = "SBRC00000000123"
SYNTHETIC_BADGE = "No.123/1000"

SERIAL_RE = re.compile(r"\bSBRC[A-Z0-9]{11}\b")
BADGE_RE = re.compile(r"No\.\s?\d{1,4}\s?/\s?1000")
HEX_FRAME_RE = re.compile(r'((?:LIVE_SN_HEX|SN_FRAME)\s*=\s*")([0-9a-fA-F ]+)(")')
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".json", ".yml", ".yaml", ".sh"}

TOOLS_DIR = Path(__file__).resolve().parent


def crc8(data: bytes) -> int:
    """CRC-8 poly 0x07 init 0x00, matching himos_psu.crc8."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def rewrite_frame(hex_text: str) -> str:
    """Replace the ASCII serial inside a 0x05 frame and recompute its CRC."""
    try:
        raw = bytes.fromhex(hex_text.replace(" ", ""))
    except ValueError:
        return hex_text
    if len(raw) < 8 or raw[:2] != b"\x55\x7e" or raw[3] != 0x05:
        return hex_text
    body = raw[:-2]
    text = body[4:].decode("latin-1")
    if not SERIAL_RE.search(text):
        return hex_text
    new_body = body[:4] + SERIAL_RE.sub(SYNTHETIC_SN, text).encode("latin-1")
    frame = new_body + bytes([crc8(new_body), 0xAE])
    return frame.hex(" ")


def sanitize_text(text: str) -> str:
    text = HEX_FRAME_RE.sub(lambda m: m.group(1) + rewrite_frame(m.group(2)) + m.group(3), text)
    text = SERIAL_RE.sub(SYNTHETIC_SN, text)
    text = BADGE_RE.sub(SYNTHETIC_BADGE, text)
    return text


def sanitize_directory(root: Path, drop_usb_node: bool = False) -> tuple[list[str], list[str]]:
    """Rewrite text files in place and drop bytecode.  Returns (changed, removed)."""
    changed: list[str] = []
    removed: list[str] = []

    pycs = [p for p in root.rglob("*.pyc") if ".git" not in p.parts]
    removed.extend(str(p.relative_to(root)) for p in pycs)
    for cache in [p for p in root.rglob("__pycache__") if ".git" not in p.parts]:
        shutil.rmtree(cache, ignore_errors=True)

    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        updated = sanitize_text(original)
        if drop_usb_node:
            updated = updated.replace("/dev/ttyUSB0", "/dev/ttyUSB*")
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(str(path.relative_to(root)))
    return changed, removed


def preview(root: Path, drop_usb_node: bool) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    removed: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() == ".pyc":
            removed.append(str(path.relative_to(root)))
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        updated = sanitize_text(original)
        if drop_usb_node:
            updated = updated.replace("/dev/ttyUSB0", "/dev/ttyUSB*")
        if updated != original:
            changed.append(str(path.relative_to(root)))
    return sorted(set(changed)), sorted(set(removed))


def rewrite_history(root: Path) -> None:
    """filter-branch every commit through the same sanitizer, then gc."""
    helper = root / ".himos_sanitize_tmp.py"
    helper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
        "import sanitize_private_copy as m\n"
        "m.sanitize_directory(Path('.'), False)\n",
        encoding="utf-8",
    )
    try:
        env = {**os.environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"}
        subprocess.run(
            ["git", "filter-branch", "-f", "--tree-filter", f"/usr/bin/python3 {helper} .", "--", "--all"],
            cwd=root, env=env, check=False,
        )
        originals = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/original"],
            cwd=root, capture_output=True, text=True,
        ).stdout.split()
        for ref in originals:
            subprocess.run(["git", "update-ref", "-d", ref], cwd=root, check=False)
        subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=root, check=False)
        subprocess.run(["git", "gc", "--prune=now", "--quiet"], cwd=root, check=False)
    finally:
        helper.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理私有副本中的序列号等个人信息")
    parser.add_argument("directory", help="要清理的目录")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    parser.add_argument("--drop-usb-node", action="store_true",
                        help="把 /dev/ttyUSB0 泛化为 /dev/ttyUSB*")
    parser.add_argument("--git", action="store_true", help="同时重写该目录的 git 历史")
    args = parser.parse_args(argv)

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：{root} 不是目录", file=sys.stderr)
        return 2

    print(f"目录: {root}")
    if args.apply:
        changed, removed = sanitize_directory(root, args.drop_usb_node)
        print(f"  已删除字节码 {len(removed)} 项")
        print(f"  已改写文件 {len(changed)} 个")
        for item in changed:
            print(f"    ~ {item}")
        if args.git and (root / ".git").exists():
            print("  重写 git 历史…")
            rewrite_history(root)
        print("  完成。")
        return 0

    changed, removed = preview(root, args.drop_usb_node)
    print(f"  待删除字节码 {len(removed)} 项")
    print(f"  待改写文件 {len(changed)} 个")
    for item in changed:
        print(f"    ~ {item}")
    print("\n以上为预演；加 --apply 才会写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
