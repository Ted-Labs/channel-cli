import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from channel_cli import adapters, body


class FakeDaemon:
    """Minimal app-server: one loaded thread, answers turn/start with canned events."""

    def __init__(self, path, answer="answer", status="completed", loaded=("thread-1",)):
        self.path, self.answer, self.status, self.loaded = path, answer, status, list(loaded)
        self.prompts, self.methods = [], []
        self.server = socket.socket(socket.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(os.path.dirname(path))  # bind long paths relative to their directory
        try:
            self.server.bind(os.path.basename(path))
        finally:
            os.chdir(cwd)
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.server.close()

    def serve(self):
        conn, _ = self.server.accept()
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            buf = buf.split(b"\r\n\r\n", 1)[1]
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")

            def frame(message):
                data = json.dumps(message).encode()
                head = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(data))
                conn.sendall(head + data)

            while True:
                while len(buf) < 2:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                n, offset = buf[1] & 0x7F, 2
                if n == 126:
                    n, offset = struct.unpack(">H", buf[2:4])[0], 4
                while len(buf) < offset + 4 + n:
                    buf += conn.recv(65536)
                mask = buf[offset:offset + 4]
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[offset + 4:offset + 4 + n]))
                buf = buf[offset + 4 + n:]
                message = json.loads(data)
                method = message.get("method")
                self.methods.append(method)
                if "id" not in message:
                    continue
                result = {}
                if method == "thread/loaded/list":
                    result = {"data": self.loaded}
                elif method == "turn/start":
                    self.prompts.append(message["params"]["input"][0]["text"])
                    frame({"jsonrpc": "2.0", "id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    frame({"jsonrpc": "2.0", "method": "item/completed",
                           "params": {"turnId": "other", "item": {"type": "agentMessage", "text": "not mine"}}})
                    frame({"jsonrpc": "2.0", "method": "item/completed",
                           "params": {"turnId": "turn-1", "item": {"type": "agentMessage", "text": self.answer}}})
                    frame({"jsonrpc": "2.0", "method": "turn/completed",
                           "params": {"turn": {"id": "turn-1", "status": self.status}}})
                    continue
                frame({"jsonrpc": "2.0", "id": message["id"], "result": result})


class BodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def daemon(self, directory=None, **kw):
        directory = directory or self.base
        directory.mkdir(parents=True, exist_ok=True)
        d = FakeDaemon(str(directory / "app-server-control.sock"), **kw)
        self.addCleanup(d.close)
        return d

    def test_turn_goes_into_loaded_thread(self):
        d = self.daemon(answer="사과")
        reply = body.run("처음 암호?", 5, d.path)
        self.assertEqual((reply.text, reply.session), ("사과", "thread-1"))
        self.assertEqual(d.prompts, ["처음 암호?"])
        self.assertEqual(d.methods[:4], ["initialize", "initialized", "thread/loaded/list", "thread/resume"])

    def test_long_socket_path(self):
        deep = self.base / ("d" * 60) / ("e" * 60)
        d = self.daemon(deep)
        self.assertGreater(len(d.path), 108)
        self.assertEqual(body.run("hi", 5, d.path).text, "answer")

    def test_failed_turn_is_an_error(self):
        d = self.daemon(status="failed")
        with self.assertRaisesRegex(ValueError, "failed"):
            body.run("hi", 5, d.path)

    def test_ambiguous_threads_rejected(self):
        d = self.daemon(loaded=("a", "b"))
        with self.assertRaisesRegex(ValueError, "CHANNEL_CODEX_THREAD"):
            body.run("hi", 5, d.path)

    def test_explicit_thread(self):
        d = self.daemon(loaded=("a", "b"))
        with patch.dict(os.environ, {"CHANNEL_CODEX_THREAD": "b"}):
            self.assertEqual(body.run("hi", 5, d.path).session, "b")
        self.assertNotIn("thread/loaded/list", d.methods)

    def test_missing_daemon(self):
        with self.assertRaisesRegex(ValueError, "not reachable"):
            body.run("hi", 1, str(self.base / "none.sock"))

    def test_adapter_dispatch_and_discovery(self):
        d = self.daemon()
        with patch.dict(os.environ, {"CHANNEL_CODEX_SOCKET": d.path}):
            self.assertEqual(adapters.run("codex-body", "hi", None, None, 5, self.base / "logs").text, "answer")
            self.assertTrue(next(a for a in adapters.discover() if a["id"] == "codex-body")["installed"])
