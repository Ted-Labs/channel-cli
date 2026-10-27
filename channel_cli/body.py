"""Send a turn into a live Codex session through the local app-server daemon.

The daemon owns the thread, so a TUI attached with `codex --remote unix://<socket>`
shows the turn live and keeps the same memory. Speaks JSON-RPC over a WebSocket
on the daemon's Unix control socket using only the standard library.
"""
import base64
import json
import os
import socket
import struct
import time
from pathlib import Path


def default_socket():
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return os.environ.get("CHANNEL_CODEX_SOCKET") or str(Path(home) / "app-server-control" / "app-server-control.sock")


class Interrupted(Exception):
    """The turn was stopped with turn/interrupt."""


class Connection:
    def __init__(self, path, timeout):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout)
        self.dirfd = None
        try:
            self.sock.connect(self._address(path))
        except OSError as exc:
            self.close()
            raise ValueError(f"codex app-server daemon is not reachable at {path}: {exc}")
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            self.close()
            raise ValueError("codex app-server refused the WebSocket upgrade")
        self.next_id = 0

    def _address(self, path):
        # sun_path holds about 104-108 bytes; reach long paths through a directory descriptor.
        if len(os.fsencode(path)) < 100 or not Path("/proc/self/fd").exists():
            return path
        self.dirfd = os.open(os.path.dirname(path), os.O_RDONLY)
        return f"/proc/self/fd/{self.dirfd}/{os.path.basename(path)}"

    def close(self):
        self.sock.close()
        if self.dirfd is not None:
            os.close(self.dirfd)
            self.dirfd = None

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ValueError("codex app-server closed the connection")
        self.buf += chunk

    def _need(self, n):
        while len(self.buf) < n:
            self._fill()

    def send(self, message):
        data = json.dumps(message).encode()
        mask, n = os.urandom(4), len(data)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0xFE]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 0xFF]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def receive(self):
        while True:
            self._need(2)
            opcode, n, offset = self.buf[0] & 0x0F, self.buf[1] & 0x7F, 2
            if n == 126:
                self._need(4)
                n, offset = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                self._need(10)
                n, offset = struct.unpack(">Q", self.buf[2:10])[0], 10
            self._need(offset + n)
            payload, self.buf = self.buf[offset:offset + n], self.buf[offset + n:]
            if opcode == 0x8:
                raise ValueError("codex app-server closed the connection")
            if opcode == 0x1:
                return json.loads(payload)

    def request(self, method, params, notes):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise ValueError(f"{method} failed: {message['error'].get('message', message['error'])}")
                return message.get("result") or {}
            if "method" in message:
                notes.append(message)


def pick_thread(conn, notes):
    wanted = os.environ.get("CHANNEL_CODEX_THREAD")
    if wanted:
        return wanted
    loaded = conn.request("thread/loaded/list", {}, notes).get("data") or []
    # The TUI also loads ephemeral helper threads; only a persisted thread is the live session.
    live = [t for t in loaded if not conn.request("thread/read", {"threadId": t}, notes).get("thread", {}).get("ephemeral")]
    if len(live) != 1:
        raise ValueError(f"expected one persisted Codex thread on the daemon, found {len(live)}; "
                         "set CHANNEL_CODEX_THREAD")
    return live[0]


def wait_idle(conn, thread, deadline, notes):
    """Wait while the session is busy with a turn typed in the TUI or sent by someone else."""
    while True:
        status = (conn.request("thread/read", {"threadId": thread}, notes).get("thread", {}).get("status") or {}).get("type")
        if status != "active":
            return
        if time.monotonic() > deadline:
            raise TimeoutError("codex session stayed busy until the timeout")
        notes.clear()
        time.sleep(1)


def run(prompt, timeout, socket_path=None, on_turn=None):
    """Start a turn in the daemon's live thread and wait for it to finish."""
    from .adapters import Reply
    deadline = time.monotonic() + timeout
    conn = Connection(socket_path or default_socket(), timeout)
    notes = []
    try:
        conn.request("initialize", {"clientInfo": {"name": "channel-cli", "version": "0.1.0"}}, notes)
        conn.send({"jsonrpc": "2.0", "method": "initialized"})
        thread = pick_thread(conn, notes)
        conn.request("thread/resume", {"threadId": thread}, notes)
        wait_idle(conn, thread, deadline, notes)
        notes.clear()
        turn = conn.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}]},
                            notes).get("turn", {}).get("id")
        if on_turn:
            on_turn(thread, turn)
        parts = []
        while True:
            message = notes.pop(0) if notes else conn.receive()
            if time.monotonic() > deadline:
                raise TimeoutError(f"codex turn did not finish within {timeout:g}s")
            params = message.get("params") or {}
            owner = params.get("turnId") or (params.get("turn") or {}).get("id")
            if turn and owner and owner != turn:
                continue
            method = message.get("method")
            item = params.get("item") or {}
            if method == "item/completed" and item.get("type") == "agentMessage":
                parts.append(item.get("text") or "")
            elif method == "error":
                raise ValueError(f"codex turn failed: {params.get('error') or params}")
            elif method == "turn/completed":
                status = (params.get("turn") or {}).get("status")
                if status == "interrupted":
                    raise Interrupted("codex turn was interrupted")
                if status not in (None, "completed"):
                    raise ValueError(f"codex turn ended as {status}: {(params.get('turn') or {}).get('error')}")
                break
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            raise ValueError("codex turn finished without an answer")
        return Reply(text, thread)
    except socket.timeout:
        raise TimeoutError(f"codex turn did not finish within {timeout:g}s")
    finally:
        conn.close()


def _control(method, params, socket_path=None):
    conn = Connection(socket_path or default_socket(), 15)
    notes = []
    try:
        conn.request("initialize", {"clientInfo": {"name": "channel-cli", "version": "0.1.0"}}, notes)
        conn.send({"jsonrpc": "2.0", "method": "initialized"})
        conn.request("thread/resume", {"threadId": params["threadId"]}, notes)
        return conn.request(method, params, notes)
    finally:
        conn.close()


def interrupt(thread, turn, socket_path=None):
    return _control("turn/interrupt", {"threadId": thread, "turnId": turn}, socket_path)


def steer(thread, turn, text, socket_path=None):
    return _control("turn/steer", {"threadId": thread, "expectedTurnId": turn,
                                   "input": [{"type": "text", "text": text}]}, socket_path)
