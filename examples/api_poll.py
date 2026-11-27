#!/usr/bin/env python3
"""Poll the himos-psu read-only API and print (or log) the input power.

    ./examples/api_poll.py                      # 1 Hz, human readable
    ./examples/api_poll.py --csv power.csv      # also append "ts,power_w,today_kwh"
    ./examples/api_poll.py --interval 5 --url http://127.0.0.1:8790

Standard library only, so it runs with the system Python. The daemon has to be
started with ``--http-port 8790`` first.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def fetch(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="轮询 himos-psu 的只读 API")
    parser.add_argument("--url", default="http://127.0.0.1:8790/v1/status")
    parser.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒")
    parser.add_argument("--csv", type=Path, help="把样本追加写入 CSV")
    parser.add_argument("--token", help="Authorization: Bearer <token>")
    args = parser.parse_args()

    writer = None
    if args.csv:
        new_file = not args.csv.exists()
        handle = args.csv.open("a", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["ts", "power_w", "today_kwh", "today_cost_yuan"])
    try:
        while True:
            try:
                payload = fetch(args.url)
            except urllib.error.HTTPError as exc:
                print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}")
                return 1
            except urllib.error.URLError as exc:
                print(f"连不上守护进程（是否加了 --http-port？）: {exc.reason}")
                return 1
            if payload.get("power_w") is None:
                print("还没有数据（守护进程可能仍在连接电源）")
            else:
                energy = payload.get("energy") or {}
                line = (
                    f"{time.strftime('%H:%M:%S')}  {payload['power_w']:6.1f} W"
                    f"  {payload['input_voltage_v']:5.1f} V"
                    f"  风扇 {payload['fan_rpm']} RPM"
                    f"  今日 {energy.get('today_kwh', 0):.3f} kWh"
                )
                if payload.get("stale"):
                    line += "  (数据过期)"
                print(line)
                if writer is not None:
                    writer.writerow([payload["ts"], payload["power_w"],
                                     energy.get("today_kwh"), energy.get("today_cost_yuan")])
                    handle.flush()
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        if args.csv:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
