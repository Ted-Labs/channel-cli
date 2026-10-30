"""Send a prompt into a live agy (Gemini) session running in tmux.

Pastes into the tmux session using bracketed paste and monitors the transcript
and tmux pane until the model completes its turn.
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .adapters import Reply, Interrupted


def default_session():
    return os.environ.get("CHANNEL_AGY_SESSION", "gemini")


def tmux_bin():
    return shutil.which("tmux") or "tmux"


def is_online(session=None):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "has-session", "-t", session],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def capture_pane(session=None, lines=8):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "capture-pane", "-pt", session, "-S", f"-{lines}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def is_busy(session=None):
    pane = capture_pane(session, lines=8)
    return bool(re.search(r"esc to (interrupt|cancel)|Press up to edit queued|Working\.\.\.|Running command|Loading\.\.\.", pane, re.I))


def extract_latest_answer(transcript_path, min_line=0):
    if not transcript_path or not transcript_path.exists():
        return ""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for i in range(len(lines) - 1, min_line - 1, -1):
            try:
                row = json.loads(lines[i])
            except Exception:
                continue
            if row.get("source") == "MODEL" and row.get("type") == "PLANNER_RESPONSE":
                content = row.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    except Exception:
        pass
    return ""


def request_transcript(marker):
    """Find the transcript containing this injection, never a merely recent session."""
    home = Path(os.environ.get("GEMINI_HOME") or Path.home())
    for path in home.glob(".gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            found = None
            for index, line in enumerate(lines):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "USER_INPUT":
                    if marker in str(row.get("content", "")):
                        found = index + 1
                    elif found is not None:
                        raise ValueError("Gemini received another input before this request completed")
            if found is not None:
                return path, found
        except OSError:
            continue
    return None


def interrupt(session=None):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "send-keys", "-t", session, "Escape"], check=False)
    if proc.returncode:
        raise ValueError("failed to send interruption to Gemini")


def clear_session(session, on_session=None):
    proc = subprocess.run([tmux_bin(), "send-keys", "-t", session, "/clear", "Enter"], check=False)
    if proc.returncode:
        raise ValueError("failed to start a fresh Gemini conversation")
    time.sleep(0.4)
    if on_session:
        on_session(session)


def run(prompt, timeout=300, session=None, fresh=False, on_session=None,
        on_turn=None, on_part=None, should_interrupt=None):
    session = session or default_session()
    if not is_online(session):
        raise ValueError(f"tmux session '{session}' is not online")

    deadline = time.monotonic() + timeout

    # Wait if session is busy; cancellation here must not stop that other work.
    if should_interrupt and should_interrupt():
        raise Interrupted("stopped before delivering to Gemini")
    while is_busy(session):
        if should_interrupt and should_interrupt():
            raise Interrupted("stopped while waiting for Gemini")
        if time.monotonic() > deadline:
            raise TimeoutError(f"session '{session}' remained busy for {timeout}s")
        time.sleep(0.5)

    if fresh:
        clear_session(session, on_session)
    elif on_session:
        on_session(session)

    marker = '[Atrium request ' + str(uuid.uuid4()) + ']'
    prompt = marker + '\n' + prompt
    transcript = None
    min_line = 0

    # Paste prompt into tmux session
    buffer_name = f"channel-agy-{os.getpid()}"
    load_proc = subprocess.run([tmux_bin(), "load-buffer", "-b", buffer_name, "-"],
                               input=prompt.replace("\r\n", "\n").encode("utf-8"), check=False)
    if load_proc.returncode != 0:
        raise ValueError("failed to load prompt into tmux buffer")

    paste_proc = subprocess.run([tmux_bin(), "paste-buffer", "-p", "-b", buffer_name, "-t", session, "-d"], check=False)
    if paste_proc.returncode != 0:
        raise ValueError("failed to paste buffer into tmux session")

    time.sleep(0.4)
    send_proc = subprocess.run([tmux_bin(), "send-keys", "-t", session, "Enter"], check=False)
    if send_proc.returncode != 0:
        raise ValueError("failed to send Enter to tmux session")

    if on_turn:
        on_turn(session, f"turn-{int(time.time())}")

    stop_at = None
    idle_hits = 0
    def stopping():
        nonlocal stop_at, idle_hits
        if not should_interrupt or not should_interrupt():
            return False
        # A unique marker proves the request belongs to this worker before Escape.
        if stop_at is None:
            if not request_transcript(marker):
                return True
            interrupt(session)
            stop_at = time.monotonic()
        if is_busy(session):
            idle_hits = 0
        else:
            idle_hits += 1
            if idle_hits >= 2 and time.monotonic() - stop_at >= 0.5:
                raise Interrupted("Gemini confirmed interruption")
        if time.monotonic() - stop_at > 15:
            raise ValueError("Gemini did not confirm interruption")
        return True

    # Wait for turn to start (becomes busy)
    started = False
    for _ in range(20):
        if stopping():
            time.sleep(0.1)
            continue
        time.sleep(0.3)
        if is_busy(session):
            started = True
            break

    last_answer = ""
    # Monitor until completion
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"turn in '{session}' did not finish within {timeout}s")

        if stopping():
            time.sleep(0.1)
            continue
        match = request_transcript(marker)
        if match:
            transcript, min_line = match
        cur_answer = extract_latest_answer(transcript, min_line) if transcript else ""
        if cur_answer and cur_answer != last_answer:
            last_answer = cur_answer
            if on_part:
                on_part(cur_answer)

        # Check if idle again after started
        if (started or last_answer) and not is_busy(session):
            # Extra settling pause to make sure transcript flushed
            time.sleep(0.5)
            final_answer = extract_latest_answer(transcript, min_line) or last_answer
            if not final_answer or is_busy(session):
                continue
            return Reply(final_answer, session)

        time.sleep(0.5)
