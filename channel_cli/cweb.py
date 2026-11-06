"""Detached cweb maintenance with a transactional queue drain."""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .store import Store

ACTIVE = ('starting', 'waiting', 'restarting')


def status(store):
    row = store.db.execute('SELECT * FROM cweb_restart WHERE id=1').fetchone()
    return dict(row) if row else {'state': 'idle'}


def command(store, action):
    if action == 'status':
        return status(store)
    if action == 'recover':
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            row = status(store)
            if row['state'] not in ACTIVE or time.time() - row['updated'] < 30:
                raise ValueError('no stale restart request to recover')
            try:
                probe = subprocess.run(['tmux', 'has-session', '-t', '=cweb-restart-' + row['token'][:12]], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError('cannot check restart supervisor') from exc
            if probe.returncode == 0:
                raise ValueError('restart supervisor is still running')
            if probe.returncode != 1:
                raise ValueError('cannot check restart supervisor')
            store.db.execute("UPDATE cweb_restart SET state='failed',updated=?,error='supervisor missing; recovered manually' WHERE id=1 AND token=?", (time.time(), row['token']))
        return status(store)
    if action == 'cancel':
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            cur = store.db.execute("UPDATE cweb_restart SET state='cancelled',updated=? WHERE id=1 AND state IN ('starting','waiting')", (time.time(),))
            if not cur.rowcount:
                raise ValueError('no cancellable restart request')
        return status(store)
    if store.home != (Path.home() / '.local/state/channel').resolve():
        raise ValueError('cweb restart requires the host queue home')
    token = uuid.uuid4().hex
    now = time.time()
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if status(store)['state'] in ACTIVE:
            raise ValueError('cweb restart already in progress')
        store.db.execute('INSERT OR REPLACE INTO cweb_restart VALUES(1,?,?,?,?,NULL)', (token, 'starting', now, now))
    argv = [sys.executable, '-m', 'channel_cli.cweb', str(store.home), token]
    try:
        subprocess.run(['tmux', 'new-session', '-d', '-s', 'cweb-restart-' + token[:12], shlex.join(argv)], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        finish(store, token, 'failed', 'could not start supervisor: ' + str(exc))
        raise ValueError('could not start cweb restart supervisor') from exc
    return status(store)


def finish(store, token, state, error=None):
    with store.db:
        store.db.execute('UPDATE cweb_restart SET state=?,updated=?,error=? WHERE id=1 AND token=? AND state IN (\'starting\',\'waiting\',\'restarting\')', (state, time.time(), error, token))


def notify_atrium(payload, timeout=60):
    try:
        auth_token = os.environ.get('ATRIUM_TOKEN') or os.environ.get('ARTRIUM_TOKEN')
        if not auth_token:
            try:
                auth_token = (Path.home() / '.claude/channels/corpus-web/notify-token').read_text().strip()
            except OSError:
                auth_token = None
        if not auth_token:
            return

        base = os.environ.get('CORPUS_WEB_URL', 'http://127.0.0.1:8790').rstrip('/')
        url = f"{base}/notify"
        data = json.dumps(payload).encode('utf-8')
        headers = {
            'Authorization': f'Bearer {auth_token}',
            'Content-Type': 'application/json',
        }
        deadline = time.monotonic() + timeout
        while True:
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method='POST')
                timeout_sec = min(5, max(1, deadline - time.monotonic()))
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    status_code = getattr(resp, 'status', getattr(resp, 'code', 200))
                    if 200 <= status_code < 300:
                        return
                    if 400 <= status_code < 500:
                        return
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
            except Exception:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
    except Exception:
        pass


def notify_restart(token, elapsed, timeout=60):
    payload = {
        "topic": "dev",
        "from": "system",
        "text": f"✅ cweb 안전 재시작이 완료되었습니다. ({elapsed:.1f}초 소요)",
        "key": f"cweb-restart:{token[:12]}",
    }
    notify_atrium(payload, timeout=timeout)


def supervise(home, token, timeout=600, notify_timeout=60, warn_delay=30):
    start_time = time.monotonic()
    store = Store(home)
    warned = False
    try:
        with store.db:
            store.db.execute("UPDATE cweb_restart SET state='waiting',updated=? WHERE id=1 AND token=? AND state='starting'", (time.time(), token))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                row = status(store)
                if row.get('token') != token or row['state'] != 'waiting':
                    return
                count = store.db.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0]
                if count == 0:
                    store.db.execute("UPDATE cweb_restart SET state='restarting',updated=? WHERE id=1 AND token=? AND state='waiting'", (time.time(), token))
                    break
            if not warned and (time.monotonic() - start_time) >= warn_delay:
                warned = True
                notify_atrium({
                    "topic": "dev",
                    "from": "system",
                    "text": f"⏳ 진행 중인 작업({count}개)이 완료되기를 기다리고 있습니다...",
                    "key": f"cweb-restart-wait:{token[:12]}",
                }, timeout=10)
            remaining = deadline - time.monotonic()
            if not warned and (start_time + warn_delay) > time.monotonic():
                next_interval = max(0.01, (start_time + warn_delay) - time.monotonic())
            else:
                next_interval = 1.0
            time.sleep(min(next_interval, max(0.01, remaining)))
        else:
            finish(store, token, 'failed', 'timed out waiting for running jobs')
            notify_atrium({
                "topic": "dev",
                "from": "system",
                "text": f"⚠️ 진행 중인 작업이 끝나지 않아 cweb 재시작이 취소되었습니다. (타임아웃 {timeout}초 초과)",
                "key": f"cweb-restart-timeout:{token[:12]}",
            }, timeout=10)
            return
        directory = Path.home() / 'Corpus' / 'Atrium'
        kill = subprocess.run(['tmux', 'kill-session', '-t', '=cweb'], capture_output=True, text=True, timeout=10)
        if kill.returncode != 0:
            err = (kill.stderr or '').strip()
            if "can't find session" not in err and "no server running" not in err:
                raise RuntimeError(f'failed to kill existing cweb: {err}')
        subprocess.run(['sh', str(directory / 'start.sh')], check=True, timeout=30)
        subprocess.run(['tmux', 'has-session', '-t', '=cweb'], check=True, capture_output=True, timeout=10)
        finish(store, token, 'done')
        elapsed = time.monotonic() - start_time
        try:
            notify_restart(token, elapsed, timeout=notify_timeout)
        except Exception:
            pass
    except Exception as exc:
        finish(store, token, 'failed', str(exc)[:500])
    finally:
        store.close()


if __name__ == '__main__':
    supervise(sys.argv[1], sys.argv[2])
