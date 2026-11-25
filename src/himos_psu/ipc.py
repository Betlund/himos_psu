"""Unix-socket IPC between the background collector daemon and GUI/CLI clients.

The serial port can only be owned by one process (``TIOCEXCL`` + flock), so the
daemon owns it and everything else talks to the daemon.

Wire format is newline-delimited JSON, one object per line::

    client -> daemon   {"cmd": "subscribe"}
                       {"cmd": "set_fan_mode", "mode": 1}
                       {"cmd": "set_curve", "points": [[0,0], ...], "switch": true}
                       {"cmd": "set_refresh", "index": 2}
                       {"cmd": "clear_fault"}
                       {"cmd": "request_info", "sub": 0}
                       {"cmd": "snapshot"}

    daemon -> client   {"event": "frame", "cmd": 2, "payload": "<hex>", "crc_ok": true}
                       {"event": "state", "text": "采集中"}
                       {"event": "info",  "model": ..., "sn": ...}   (specs snapshot)
                       {"event": "error", "message": "..."}
                       {"event": "ack",   "cmd": "set_fan_mode", "ok": true}

Telemetry is forwarded as raw frames rather than decoded structs so both sides
reuse :mod:`himos_psu.protocol` for parsing and stay in sync automatically.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

SOCKET_ENV = "HIMOS_PSU_SOCKET"
DEFAULT_TIMEOUT = 5.0


def default_socket_path() -> Path:
    """Return the daemon socket path, honouring XDG_RUNTIME_DIR and env override."""
    override = os.environ.get(SOCKET_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        runtime = f"/tmp/himos-psu-{os.getuid()}"
    return Path(runtime) / "himos-psu" / "daemon.sock"


def encode_line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode_line(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("IPC message must be a JSON object")
    return data


class IpcError(RuntimeError):
    """Raised when the daemon is unreachable or the connection drops."""


class DaemonUnavailable(IpcError):
    """The socket does not exist or nobody is listening."""


def probe_socket(path: str | Path | None = None, timeout: float = 1.0) -> bool:
    """Return True when a daemon is accepting connections on ``path``."""
    target = Path(path) if path is not None else default_socket_path()
    if not target.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(target))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class SocketClient:
    """Blocking client: one reader thread dispatches events to a callback."""

    def __init__(
        self,
        path: str | Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.path = Path(path) if path is not None else default_socket_path()
        self._on_event = on_event
        self._send_lock = threading.Lock()
        self._closed = False
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(str(self.path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            self._sock.close()
            raise DaemonUnavailable(f"后台采集守护进程未运行（{self.path}）") from exc
        except OSError as exc:
            self._sock.close()
            raise IpcError(f"无法连接守护进程 {self.path}: {exc}") from exc
        self._sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, name="himos-ipc-reader", daemon=True)
        self._reader.start()

    # -- sending -----------------------------------------------------------
    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise IpcError("连接已关闭")
        payload = encode_line(message)
        with self._send_lock:
            try:
                self._sock.sendall(payload)
            except OSError as exc:
                self._closed = True
                raise IpcError(f"发送失败: {exc}") from exc

    def subscribe(self) -> None:
        self.send({"cmd": "subscribe"})

    def command(self, **payload: Any) -> None:
        self.send(payload)

    # -- receiving ---------------------------------------------------------
    def _read_loop(self) -> None:
        buffer = b""
        try:
            while not self._closed:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = decode_line(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if self._on_event is not None:
                        try:
                            self._on_event(message)
                        except Exception:
                            pass
        except OSError:
            pass
        finally:
            self._closed = True
            if self._on_event is not None:
                try:
                    self._on_event({"event": "closed"})
                except Exception:
                    pass
            try:
                self._sock.close()
            except OSError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class _Connection:
    def __init__(self, sock: socket.socket, address: object) -> None:
        self.sock = sock
        self.address = address
        self._lock = threading.Lock()
        self.closed = False

    def send(self, message: dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            with self._lock:
                self.sock.sendall(encode_line(message))
            return True
        except OSError:
            self.closed = True
            return False

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class IpcServer:
    """Accept clients, dispatch commands to a handler and broadcast events."""

    def __init__(
        self,
        path: str | Path | None = None,
        handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        on_subscribe: Callable[[Callable[[dict[str, Any]], None]], None] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_socket_path()
        self._handler = handler
        self._on_subscribe = on_subscribe
        self._subscribers: set[_Connection] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.settimeout(0.5)
        self._prepare_path()
        self._server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._server.listen(8)
        self._accept_thread = threading.Thread(target=self._accept_loop, name="himos-ipc-accept", daemon=True)
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------
    def _prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        if self.path.exists():
            # A socket file left behind by a crashed daemon: only remove if dead.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
                probe.close()
                self._server.close()
                raise RuntimeError(f"已有守护进程在 {self.path} 上监听")
            except (ConnectionRefusedError, FileNotFoundError):
                probe.close()
                self.path.unlink(missing_ok=True)
            except OSError:
                probe.close()
                self.path.unlink(missing_ok=True)

    def start(self) -> None:
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        with self._lock:
            connections = list(self._subscribers)
            self._subscribers.clear()
        for conn in connections:
            conn.close()
        self._accept_thread.join(timeout=2.0)
        for thread in list(self._threads):
            thread.join(timeout=1.0)
        self.path.unlink(missing_ok=True)

    # -- serving -----------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._client_loop, args=(sock, address), name="himos-ipc-client", daemon=True
            )
            self._threads = [t for t in self._threads if t.is_alive()] + [thread]
            thread.start()

    def _client_loop(self, sock: socket.socket, address: object) -> None:
        conn = _Connection(sock, address)
        buffer = b""
        try:
            while not self._stop.is_set() and not conn.closed:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = decode_line(line)
                    except (ValueError, json.JSONDecodeError):
                        conn.send({"event": "error", "message": "无法解析的请求"})
                        continue
                    self._dispatch(conn, message)
        finally:
            with self._lock:
                self._subscribers.discard(conn)
            conn.close()

    def _dispatch(self, conn: _Connection, message: dict[str, Any]) -> None:
        cmd = str(message.get("cmd", ""))
        if cmd == "subscribe":
            with self._lock:
                self._subscribers.add(conn)
            if self._on_subscribe is not None:
                try:
                    self._on_subscribe(conn.send)
                except Exception as exc:
                    conn.send({"event": "error", "message": f"订阅快照失败: {exc}"})
            return
        if self._handler is None:
            conn.send({"event": "error", "message": "守护进程未就绪"})
            return
        try:
            reply = self._handler(message)
        except Exception as exc:
            conn.send({"event": "ack", "cmd": cmd, "ok": False, "error": str(exc)})
            return
        ack = {"event": "ack", "cmd": cmd, "ok": True}
        if reply:
            ack.update(reply)
        conn.send(ack)

    # -- broadcasting ------------------------------------------------------
    def broadcast(self, message: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers)
        dead = [conn for conn in targets if not conn.send(message)]
        if dead:
            with self._lock:
                for conn in dead:
                    self._subscribers.discard(conn)
                    conn.close()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
