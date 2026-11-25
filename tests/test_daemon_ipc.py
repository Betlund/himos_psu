"""Tests for the IPC layer, the background daemon and GUI remote mode."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from himos_psu import cli, ipc, protocol
from himos_psu.collector import Collector, StaleError, collector_lock
from himos_psu.daemon import Daemon
from himos_psu.energy import EnergyStore
from himos_psu.ipc import IpcServer, SocketClient, default_socket_path, probe_socket


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_status_frame(power_raw: int) -> protocol.RawFrame:
    """Build one synthetic POWER_STATUS_RESPONSE frame (unique bytes per call)."""
    payload = bytearray(28)
    payload[0:2] = (3300).to_bytes(2, "little")
    payload[2:4] = (5000).to_bytes(2, "little")
    payload[4:6] = (12000).to_bytes(2, "little")
    payload[6:8] = (5000).to_bytes(2, "little")
    payload[12:14] = power_raw.to_bytes(2, "little")
    payload[24:26] = (16).to_bytes(2, "little")
    body = bytes([len(payload) + 2, protocol.CmdId.POWER_STATUS_RESPONSE]) + bytes(payload)
    return protocol.parse_frame(protocol.encode_frame(body))


class IpcTestCase(unittest.TestCase):
    def test_roundtrip_broadcast_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "daemon.sock"
            received: list[dict] = []

            def handler(message: dict) -> dict | None:
                if message.get("cmd") == "fail":
                    raise RuntimeError("boom")
                return {"echo": message.get("value")}

            server = IpcServer(
                sock_path,
                handler=handler,
                on_subscribe=lambda write: write({"event": "state", "text": "snapshot"}),
            )
            server.start()
            client = SocketClient(sock_path, on_event=received.append)
            try:
                self.assertTrue(probe_socket(sock_path))
                client.subscribe()
                self.assertTrue(wait_for(lambda: any(e.get("text") == "snapshot" for e in received)))

                client.command(cmd="ping", value=42)
                self.assertTrue(wait_for(lambda: any(e.get("event") == "ack" for e in received)))
                ack = [e for e in received if e.get("event") == "ack"][-1]
                self.assertTrue(ack["ok"])
                self.assertEqual(ack["echo"], 42)

                server.broadcast({"event": "frame", "cmd": 2, "raw": "00"})
                self.assertTrue(wait_for(lambda: any(e.get("event") == "frame" for e in received)))

                client.command(cmd="fail")
                self.assertTrue(
                    wait_for(lambda: any(e.get("event") == "ack" and not e["ok"] for e in received))
                )
                failed = [e for e in received if e.get("event") == "ack" and not e["ok"]][-1]
                self.assertIn("boom", failed["error"])
                self.assertEqual(server.subscriber_count, 1)
            finally:
                client.close()
                server.stop()
            self.assertFalse(sock_path.exists())

    def test_unreachable_socket_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.sock"
            with self.assertRaises(ipc.DaemonUnavailable):
                SocketClient(missing)
            self.assertFalse(probe_socket(missing))

    def test_default_socket_path_honours_env(self) -> None:
        previous = os.environ.get(ipc.SOCKET_ENV)
        os.environ[ipc.SOCKET_ENV] = "/run/example/himos.sock"
        try:
            self.assertEqual(str(default_socket_path()), "/run/example/himos.sock")
        finally:
            if previous is None:
                os.environ.pop(ipc.SOCKET_ENV, None)
            else:
                os.environ[ipc.SOCKET_ENV] = previous


class DaemonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.daemon = Daemon(
            db_path=self.dir / "energy.db",
            socket_path=self.dir / "daemon.sock",
            mock=True,
            restore=False,
            quiet=True,
        )

    def tearDown(self) -> None:
        try:
            self.daemon.stop()
        finally:
            self._tmp.cleanup()

    def test_daemon_collects_and_accepts_commands(self) -> None:
        self.daemon.start()
        self.assertTrue(wait_for(lambda: probe_socket(self.daemon.socket_path)))

        events: list[dict] = []
        client = SocketClient(self.daemon.socket_path, on_event=events.append)
        try:
            client.subscribe()
            self.assertTrue(
                wait_for(lambda: any(e.get("event") == "frame" and e.get("cmd") == 2 for e in events))
            )
            # Specs are queried at connect time, so the snapshot must carry them.
            self.assertTrue(
                wait_for(lambda: any(e.get("event") == "frame" and e.get("cmd") in (3, 5) for e in events))
            )
            # Both POWER_INFO variants (sub=0 vendor, sub=53 date) must survive the
            # snapshot cache, otherwise a late-joining GUI cannot merge the specs.
            info_raw = {e["raw"] for e in events if e.get("event") == "frame" and e.get("cmd") == 3}
            self.assertGreaterEqual(len(info_raw), 2)

            before = len(events)
            client.command(cmd="set_fan_mode", mode=1)
            self.assertTrue(
                wait_for(
                    lambda: any(
                        e.get("event") == "ack" and e.get("cmd") == "set_fan_mode"
                        for e in events[before:]
                    )
                )
            )
            ack = [e for e in events[before:] if e.get("event") == "ack"][-1]
            self.assertTrue(ack["ok"])

            client.command(cmd="set_refresh", index=2)
            client.command(cmd="clear_fault")
            client.command(cmd="request_info", sub=53)
            client.command(cmd="bogus")
            self.assertTrue(
                wait_for(lambda: any(e.get("event") == "ack" and not e["ok"] for e in events))
            )
        finally:
            client.close()

        store = EnergyStore(self.daemon.db_path, create=False)
        try:
            self.assertGreater(store.sample_count(), 0)
        finally:
            store.close()

    def test_snapshot_keeps_static_frames_after_telemetry_churn(self) -> None:
        """A subscriber joining hours later must still receive the specs.

        Every status/telemetry frame is unique on the wire, so a cache keyed by raw
        bytes gets flushed within seconds and late GUIs showed "--" for the vendor.
        """
        self.daemon.start()
        self.assertTrue(wait_for(lambda: probe_socket(self.daemon.socket_path)))
        self.assertTrue(
            wait_for(
                lambda: "info_vendor" in self.daemon._frames
                and "info_date" in self.daemon._frames
                and "sn" in self.daemon._frames
            ),
            "handshake frames were not cached",
        )

        # Simulate a long-running session: all of these are distinct wire frames.
        for tick in range(200):
            self.daemon._on_frame(make_status_frame(4000 + tick), None)

        events: list[dict] = []
        client = SocketClient(self.daemon.socket_path, on_event=events.append)
        try:
            client.subscribe()
            self.assertTrue(
                wait_for(lambda: any(e.get("event") == "frame" for e in events)),
                "no snapshot received",
            )
            frames = [e for e in events if e.get("event") == "frame"]
            info_raw = {e["raw"] for e in frames if e.get("cmd") == 3}
            self.assertGreaterEqual(len(info_raw), 2, "both POWER_INFO variants must survive")
            self.assertTrue(any(e.get("cmd") == 5 for e in frames), "SN frame must survive")

            merged = protocol.SpecsMerger()
            for payload_hex in info_raw:
                raw = protocol.parse_frame(bytes.fromhex(payload_hex))
                merged.update(protocol.parse_command_content(raw.cmd_id, raw.payload))
            self.assertIsNotNone(merged.current)
            assert merged.current is not None
            self.assertEqual(merged.current.model, "KE-1300P")
            self.assertEqual(merged.current.manufacture, "segotep")
            self.assertGreater(
                merged.current.year + merged.current.month + merged.current.day, 0
            )
        finally:
            client.close()

    def test_second_daemon_cannot_take_the_port(self) -> None:
        self.daemon.start()
        other = Daemon(
            db_path=self.dir / "energy2.db",
            socket_path=self.dir / "other.sock",
            mock=True,
            restore=False,
            quiet=True,
        )
        try:
            with self.assertRaises(Exception):
                other.start()
        finally:
            other.stop()

    def test_socket_is_removed_on_stop(self) -> None:
        self.daemon.start()
        path = self.daemon.socket_path
        self.assertTrue(path.exists())
        self.daemon.stop()
        self.assertFalse(path.exists())


class _SilentClient:
    """Stands in for PowerSupplyClient when nothing ever arrives on the wire."""

    port_name = "silent://test"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def query_specs_and_sn(self, timeout: float = 2.0, on_frame=None):
        return None, ""

    def poll_frames(self, timeout: float = 0.2):
        time.sleep(min(timeout, 0.05))
        return []


class StaleDetectionTestCase(unittest.TestCase):
    def test_collector_raises_when_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EnergyStore(Path(tmp) / "energy.db")
            collector = Collector(_SilentClient(), store, stale_timeout=0.2, poll_timeout=0.05)
            try:
                with self.assertRaises(StaleError):
                    collector.run()
            finally:
                store.close()

    def test_no_stale_check_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EnergyStore(Path(tmp) / "energy.db")
            collector = Collector(_SilentClient(), store, stale_timeout=0.0, poll_timeout=0.02)
            stop = threading.Event()
            thread = threading.Thread(target=lambda: collector.run(should_stop=stop.is_set))
            thread.start()
            time.sleep(0.3)
            stop.set()
            thread.join(timeout=3.0)
            store.close()
            self.assertFalse(thread.is_alive())


class GuiRemoteTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6 import QtWidgets

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_gui_follows_daemon_over_ipc(self) -> None:
        from PySide6 import QtCore, QtWidgets

        from himos_psu.gui import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = Daemon(
                db_path=root / "energy.db",
                socket_path=root / "daemon.sock",
                mock=True,
                restore=False,
                quiet=True,
            )
            daemon.start()
            previous = os.environ.get(ipc.SOCKET_ENV)
            os.environ[ipc.SOCKET_ENV] = str(daemon.socket_path)
            window = None
            try:
                self.assertTrue(wait_for(lambda: probe_socket(daemon.socket_path)))
                # Let the daemon finish its startup handshake, then bury the specs under
                # a long session's worth of unique telemetry frames, like a daemon that
                # has been collecting for hours when the user finally opens the GUI.
                self.assertTrue(
                    wait_for(lambda: "info_vendor" in daemon._frames and "sn" in daemon._frames)
                )
                for tick in range(100):
                    daemon._on_frame(make_status_frame(4000 + tick), None)
                window = MainWindow(
                    mock=False,
                    config_path=root / "settings.json",
                    db_path=root / "energy.db",
                )
                window.resize(1180, 800)
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.03)
                self.assertTrue(window._remote)
                self.assertIsNone(window.client)  # GUI must not open the serial port
                self.assertNotEqual(window.metric_labels["power"].text(), "--")
                self.assertIn("KE-1300P", window.title_label.text())
                # Vendor and build date come from two different 0x03 frames; a
                # snapshot that drops either one leaves this at "--".
                self.assertEqual(window.spec_labels["vendor"].text(), "segotep")
                window.grab()
            finally:
                if window is not None:
                    window.close()
                daemon.stop()
                if previous is None:
                    os.environ.pop(ipc.SOCKET_ENV, None)
                else:
                    os.environ[ipc.SOCKET_ENV] = previous


class DaemonUnitInstallTestCase(unittest.TestCase):
    """`systemd-daemon --install` turns the manual daemon into a user service."""

    @contextlib.contextmanager
    def _sandbox_env(self, tmp: str, socket: str | None = None):
        keys = ("XDG_CONFIG_HOME", ipc.SOCKET_ENV)
        previous = {key: os.environ.get(key) for key in keys}
        os.environ["XDG_CONFIG_HOME"] = tmp
        # Never touch the real daemon socket: point at a path that cannot exist.
        os.environ[ipc.SOCKET_ENV] = socket or str(Path(tmp) / "no-daemon.sock")
        try:
            # Keep the collector-lock probe away from the real data directory.
            with mock.patch("himos_psu.energy.DEFAULT_DB", str(Path(tmp) / "energy.db")):
                yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_print_mode_still_writes_unit_to_stdout(self) -> None:
        rc, out, _err = self._run(["systemd-daemon", "--exec", "/usr/local/bin/himos-psu"])
        self.assertEqual(rc, 0)
        self.assertIn("[Unit]", out)
        self.assertIn("Restart=always", out)
        self.assertIn("ExecStart=/usr/local/bin/himos-psu daemon", out)
        # StartLimit* moved to [Unit] in systemd 230; in [Service] they are ignored.
        unit_section, service_section = out.split("[Service]")
        self.assertIn("StartLimitIntervalSec=300", unit_section)
        self.assertIn("StartLimitBurst=10", unit_section)
        self.assertNotIn("StartLimit", service_section)

    def test_dry_run_previews_commands_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "systemd" / "user" / "himos-psu-collect.service"
            with self._sandbox_env(tmp):
                rc, out, _err = self._run(
                    ["systemd-daemon", "--install", "--enable", "--linger", "--dry-run"]
                )
            self.assertEqual(rc, 0)
            self.assertIn(str(dest), out)
            self.assertIn("$ systemctl --user daemon-reload", out)
            self.assertIn("$ systemctl --user enable --now himos-psu-collect.service", out)
            self.assertIn("$ loginctl enable-linger", out)
            self.assertFalse(dest.exists(), "dry run must not touch the filesystem")

    def test_install_writes_unit_and_enables_it(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "systemd" / "user" / "himos-psu-collect.service"
            with self._sandbox_env(tmp), mock.patch(
                "himos_psu.cli.shutil.which", return_value="/usr/bin/systemctl"
            ), mock.patch("himos_psu.cli.subprocess.run", side_effect=fake_run):
                rc, out, err = self._run(
                    [
                        "systemd-daemon",
                        "--install",
                        "--enable",
                        "--linger",
                        "--exec",
                        "/usr/local/bin/himos-psu",
                    ]
                )
            self.assertEqual(rc, 0, err)
            self.assertIn(str(dest), out)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o644)
            unit = dest.read_text(encoding="utf-8")
            self.assertIn("ExecStart=/usr/local/bin/himos-psu daemon", unit)
            self.assertIn("--stale-timeout 180", unit)
            self.assertEqual(
                calls,
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", "himos-psu-collect.service"],
                    ["loginctl", "enable-linger", calls[2][2]],
                ],
            )
            self.assertTrue(calls[2][2])

    def test_reinstall_restarts_its_own_running_service(self) -> None:
        """Re-applying a new unit must not be blocked by the service itself."""
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._sandbox_env(tmp), mock.patch(
                "himos_psu.energy.DEFAULT_DB", str(root / "energy.db")
            ), mock.patch("himos_psu.cli.shutil.which", return_value="/usr/bin/systemctl"), \
                    mock.patch("himos_psu.cli._user_unit_active", return_value=True), \
                    mock.patch("himos_psu.cli.subprocess.run", side_effect=fake_run):
                with collector_lock(root / "collector.lock"):
                    rc, out, err = self._run(["systemd-daemon", "--install", "--enable"])
            self.assertEqual(rc, 0, err)
            self.assertIn("会自动重启", out)
            self.assertEqual(
                calls,
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", "himos-psu-collect.service"],
                    ["systemctl", "--user", "restart", "himos-psu-collect.service"],
                ],
            )

    def test_install_refuses_while_a_collector_holds_the_lock(self) -> None:
        """`collect` and GUI collection hold the lock but expose no socket."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "systemd" / "user" / "himos-psu-collect.service"
            with self._sandbox_env(tmp), mock.patch(
                "himos_psu.energy.DEFAULT_DB", str(root / "energy.db")
            ):
                with collector_lock(root / "collector.lock"):
                    rc, _out, err = self._run(["systemd-daemon", "--install", "--enable"])
            self.assertEqual(rc, 1)
            self.assertIn("Ctrl+C", err)
            self.assertIn("采集锁", err)
            self.assertFalse(dest.exists())

    def test_install_refuses_while_a_manual_daemon_holds_the_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "systemd" / "user" / "himos-psu-collect.service"
            daemon = Daemon(
                db_path=root / "energy.db",
                socket_path=root / "daemon.sock",
                mock=True,
                restore=False,
                quiet=True,
            )
            daemon.start()
            try:
                self.assertTrue(wait_for(lambda: probe_socket(root / "daemon.sock")))
                with self._sandbox_env(tmp, socket=str(root / "daemon.sock")):
                    rc, _out, err = self._run(["systemd-daemon", "--install", "--enable"])
                self.assertEqual(rc, 1)
                self.assertIn("Ctrl+C", err)
                self.assertIn("串口被独占", err)
                self.assertFalse(dest.exists(), "refusal must not leave a half-installed unit")
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
