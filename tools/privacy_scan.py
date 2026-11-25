#!/usr/bin/env python3
"""Fail the build when personal or secret material would be published.

Scan targets
------------
--worktree   every file tracked by git in the working tree
--staged     the content staged for the next commit
--history    every blob reachable from any ref (catches rewritten-but-lingering objects)
--messages   commit messages
--all        worktree + history + messages (default)

The scanner is written so that it never contains a real secret itself: the
serial pattern is matched by shape (vendor prefix + 11 chars), not by value.
Matches are printed masked, so a failing run never copies the secret into CI logs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Shape-based rules. `allow` lists the sanitized values that are safe to publish.
RULES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    ("serial-number", re.compile(r"\bSBRC[A-Z0-9]{11}\b"), frozenset({"SBRC00000000123"})),
    ("edition-badge", re.compile(r"No\.\s?\d{1,4}\s?/\s?1000"), frozenset({"No.123/1000"})),
    ("home-path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+"), frozenset()),
    ("private-ip", re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|"
                              r"\b192\.168\.\d{1,3}\.\d{1,3}\b|"
                              r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"), frozenset()),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     frozenset({"BetLund@proton.me"})),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), frozenset()),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), frozenset()),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), frozenset()),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), frozenset()),
)

TRACKED_BYTECODE = (".pyc", ".pyo")


@dataclass(frozen=True, slots=True)
class Finding:
    location: str
    rule: str
    masked: str

    def render(self) -> str:
        return f"  FAIL [{self.rule}] {self.location}  {self.masked}"


def mask(text: str) -> str:
    text = text.strip()
    if len(text) <= 6:
        return text[:2] + "*" * max(1, len(text) - 2)
    return f"{text[:4]}{'*' * (len(text) - 6)}{text[-2:]}"


def is_probably_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def scan_text(text: str, location: str) -> list[Finding]:
    findings: list[Finding] = []
    for rule, pattern, allow in RULES:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value in allow:
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(f"{location}:{line}", rule, mask(value)))
    return findings


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def scan_worktree() -> list[Finding]:
    findings: list[Finding] = []
    for name in git("ls-files", "-z").split("\0"):
        if not name:
            continue
        path = REPO_ROOT / name
        if path.suffix.lower() in TRACKED_BYTECODE:
            findings.append(Finding(name, "tracked-bytecode", "-"))
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(Finding(name, "unreadable", str(exc)[:40]))
            continue
        if is_probably_binary(data):
            continue
        findings.extend(scan_text(data.decode("utf-8", "replace"), name))
    return findings


def scan_staged() -> list[Finding]:
    findings: list[Finding] = []
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    for name in names:
        if not name:
            continue
        if Path(name).suffix.lower() in TRACKED_BYTECODE:
            findings.append(Finding(name, "tracked-bytecode", "-"))
            continue
        try:
            blob = subprocess.run(
                ["git", "show", f":{name}"], cwd=REPO_ROOT, capture_output=True, check=True
            ).stdout
        except subprocess.CalledProcessError:
            continue
        if is_probably_binary(blob):
            continue
        findings.extend(scan_text(blob.decode("utf-8", "replace"), f"staged:{name}"))
    return findings


def scan_history() -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for commit in git("rev-list", "--all").split():
        for entry in git("ls-tree", "-r", "-z", commit).split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            blob = parts[2]
            key = (blob, path)
            if key in seen:
                continue
            seen.add(key)
            data = subprocess.run(
                ["git", "cat-file", "blob", blob], cwd=REPO_ROOT, capture_output=True, check=True
            ).stdout
            if is_probably_binary(data):
                continue
            findings.extend(scan_text(data.decode("utf-8", "replace"), f"{commit[:7]}:{path}"))
    return findings


def scan_messages() -> list[Finding]:
    log = git("log", "--all", "--format=%H%x1f%B%x1e")
    findings: list[Finding] = []
    for record in log.split("\x1e"):
        record = record.strip()
        if not record or "\x1f" not in record:
            continue
        commit, _, body = record.partition("\x1f")
        findings.extend(scan_text(body, f"commit:{commit[:7]}"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描仓库中的个人信息与密钥，命中即失败")
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--messages", action="store_true")
    parser.add_argument("--all", action="store_true", help="worktree + history + messages（默认）")
    args = parser.parse_args(argv)

    explicit = any((args.worktree, args.staged, args.history, args.messages, args.all))
    if not explicit or args.all:
        do = dict(worktree=True, staged=False, history=True, messages=True)
    else:
        do = dict(
            worktree=args.worktree, staged=args.staged,
            history=args.history, messages=args.messages,
        )

    stages: list[str] = []
    findings: list[Finding] = []
    if do["worktree"]:
        stages.append("worktree")
        findings += scan_worktree()
    if do["staged"]:
        stages.append("staged")
        findings += scan_staged()
    if do["history"]:
        stages.append("history")
        findings += scan_history()
    if do["messages"]:
        stages.append("messages")
        findings += scan_messages()

    print(f"privacy-scan: {' + '.join(stages)}")
    if not findings:
        print("  OK 未发现个人信息或密钥")
        return 0

    print(f"  发现 {len(findings)} 处可疑内容：")
    for finding in findings:
        print(finding.render())

    serial_hits = [f for f in findings if f.rule in ("serial-number", "edition-badge")]
    if serial_hits:
        print("\n提示：序列号/编号命中时，请改用合成值 SBRC00000000123 与 No.123/1000，")
        print("      并删除 __pycache__；若已进入历史，需重写历史后再 gc（见 tools/sanitize_private_copy.py）。")
    print("\n阻断提交：请先清理上述内容。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
