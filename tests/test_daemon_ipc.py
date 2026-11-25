"""Tests for the IPC layer, the background daemon and GUI remote mode."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from himos_psu import ipc
from himos_psu.collector import Collector, StaleError
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
                window.grab()
            finally:
                if window is not None:
                    window.close()
                daemon.stop()
                if previous is None:
                    os.environ.pop(ipc.SOCKET_ENV, None)
                else:
                    os.environ[ipc.SOCKET_ENV] = previous


if __name__ == "__main__":
    unittest.main()
