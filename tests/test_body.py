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

    def __init__(self, path, answer="answer", status="completed", loaded=("thread-1",), ephemeral=(), busy=0, hold=False, steer_turn=False):
        self.path, self.answer, self.status, self.loaded = path, answer, status, list(loaded)
        self.ephemeral, self.busy, self.hold, self.steer_turn = set(ephemeral), busy, hold, steer_turn
        self.cwds, self.updated = {}, {}
        self.release, self.steers, self.started = threading.Event(), [], threading.Event()
        self.prompts, self.methods = [], []
        self.server = socket.socket(socket.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(os.path.dirname(path))  # bind long paths relative to their directory
        try:
            self.server.bind(os.path.basename(path))
        finally:
            os.chdir(cwd)
        self.server.listen(4)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def close(self):
        self.server.close()

    def accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
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
                elif method == "thread/read":
                    tid = message["params"]["threadId"]
                    active = bool(self.busy) and tid not in self.ephemeral and "thread/resume" in self.methods
                    if active:
                        self.busy -= 1
                    result = {"thread": {"id": tid, "ephemeral": tid in self.ephemeral, "cwd": self.cwds.get(tid), "updatedAt": self.updated.get(tid, 0),
                                         "status": {"type": "active" if active else "idle"}}}
                elif method == "turn/interrupt":
                    self.status = "interrupted"
                    self.release.set()
                elif method == "turn/steer":
                    assert message["params"]["expectedTurnId"] == "turn-1"
                    self.steers.append(message["params"]["input"][0]["text"])
                    self.answer += "+" + self.steers[-1]
                    self.release.set()
                elif method == "turn/start":
                    self.prompts.append(message["params"]["input"][0]["text"])
                    frame({"jsonrpc": "2.0", "id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    self.started.set()
                    if self.hold:
                        self.release.wait(10)
                    owner = "turn-2" if self.steer_turn and self.steers else "turn-1"
                    frame({"jsonrpc": "2.0", "method": "item/completed",
                           "params": {"turnId": "other", "item": {"type": "agentMessage", "text": "not mine"}}})
                    frame({"jsonrpc": "2.0", "method": "item/completed",
                           "params": {"turnId": owner, "item": {"type": "agentMessage", "text": self.answer}}})
                    completed = {"turn": {"id": owner, "status": self.status}}
                    if self.steer_turn:
                        completed["threadId"] = "thread-1"
                    frame({"jsonrpc": "2.0", "method": "turn/completed", "params": completed})
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
        self.assertEqual(d.methods[:5], ["initialize", "initialized", "thread/loaded/list", "thread/read", "thread/resume"])

    def test_long_socket_path(self):
        deep = self.base / ("d" * 60) / ("e" * 60)
        d = self.daemon(deep)
        self.assertGreater(len(d.path), 108)
        self.assertEqual(body.run("hi", 5, d.path).text, "answer")

    def test_failed_turn_is_an_error(self):
        d = self.daemon(status="failed")
        with self.assertRaisesRegex(ValueError, "failed"):
            body.run("hi", 5, d.path)

    def test_ephemeral_helper_thread_ignored(self):
        d = self.daemon(loaded=("live", "helper"), ephemeral=("helper",))
        self.assertEqual(body.run("hi", 5, d.path).session, "live")

    def test_waits_while_session_busy(self):
        d = self.daemon(busy=2)
        self.assertEqual(body.run("hi", 10, d.path).text, "answer")
        self.assertEqual(d.methods.count("thread/read"), 1 + 3)
        self.assertEqual(d.prompts, ["hi"])

    def test_interrupt_stops_held_turn(self):
        d = self.daemon(hold=True)
        seen = []
        worker = threading.Thread(target=lambda: seen.append(self.catch(lambda: body.run("long", 10, d.path, on_turn=lambda *t: seen.append(t)))))
        worker.start()
        self.assertTrue(d.started.wait(5))
        body.interrupt("thread-1", "turn-1", d.path)
        worker.join(5)
        self.assertEqual(seen[0], ("thread-1", "turn-1"))
        self.assertIsInstance(seen[1], body.Interrupted)

    def test_steer_adds_to_running_turn(self):
        d = self.daemon(hold=True)
        out = []
        worker = threading.Thread(target=lambda: out.append(body.run("long", 10, d.path)))
        worker.start()
        self.assertTrue(d.started.wait(5))
        body.steer("thread-1", "turn-1", "also this", d.path)
        worker.join(5)
        self.assertEqual(out[0].text, "answer+also this")

    def test_steer_follows_unannounced_successor_turn_for_same_thread(self):
        d = self.daemon(hold=True, steer_turn=True)
        out = []
        worker = threading.Thread(target=lambda: out.append(body.run("long", 10, d.path)))
        worker.start()
        self.assertTrue(d.started.wait(5))
        body.steer("thread-1", "turn-1", "also this", d.path)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(out[0].text, "answer+also this")

    @staticmethod
    def catch(fn):
        try:
            return fn()
        except Exception as exc:
            return exc

    def test_cwd_picks_newest_body_thread(self):
        d = self.daemon(loaded=("mac", "old", "new"))
        d.cwds = {"mac": "/elsewhere", "old": str(self.base), "new": str(self.base)}
        d.updated = {"old": 1, "new": 2}
        with patch.dict(os.environ, {"CHANNEL_CODEX_CWD": str(self.base)}):
            self.assertEqual(body.run("hi", 5, d.path).session, "new")

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


class BodyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.d = FakeDaemon(str(self.base / "app.sock"), hold=True)
        self.addCleanup(self.d.close)
        self.env = {**os.environ, "CHANNEL_HOME": str(self.base / "state"), "CHANNEL_CODEX_SOCKET": self.d.path,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

    def cli(self, *args):
        import subprocess, sys
        p = subprocess.run([sys.executable, "-m", "channel_cli", "--json", *args], env=self.env,
                           capture_output=True, text=True, timeout=15)
        return json.loads(p.stdout or p.stderr)

    def start_worker(self):
        import subprocess, sys
        first = self.cli("send", "--agent", "codex-body", "long job")
        proc = subprocess.Popen([sys.executable, "-m", "channel_cli", "--json", "worker", "--once", "--timeout", "10"],
                                env=self.env, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.assertTrue(self.d.started.wait(5))
        return first, proc

    def test_steer_queued_job_into_running_turn(self):
        first, proc = self.start_worker()
        second = self.cli("send", "--agent", "codex-body", "now please")
        steered = self.cli("steer", second["id"][:8])
        self.assertEqual(steered["state"], "steered")
        done = json.loads(proc.communicate(timeout=10)[0])
        self.assertEqual((done["id"], done["state"], done["answer"]), (first["id"], "done", "answer+now please"))
        self.assertIn("steered", [e["type"] for e in self.cli("events", "--after", "0")])

    def test_interrupt_running_job(self):
        first, proc = self.start_worker()
        self.cli("interrupt", first["id"])
        done = json.loads(proc.communicate(timeout=10)[0])
        self.assertEqual((done["state"], done["error"]), ("interrupted", "stopped by interrupt"))

    def test_steer_without_running_turn_keeps_job_queued(self):
        job = self.cli("send", "--agent", "codex-body", "later")
        self.assertIn("no running turn", self.cli("steer", job["id"])["error"])
        self.assertEqual(self.cli("result", job["id"])["state"], "queued")
