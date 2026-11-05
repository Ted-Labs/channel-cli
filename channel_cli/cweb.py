"""Detached cweb maintenance with a transactional queue drain."""
import shlex
import subprocess
import sys
import time
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


def supervise(home, token, timeout=600):
    store = Store(home)
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
            time.sleep(1)
        else:
            finish(store, token, 'failed', 'timed out waiting for running jobs')
            return
        directory = Path.home() / 'Corpus' / 'Atrium'
        subprocess.run(['tmux', 'kill-session', '-t', '=cweb'], check=False, capture_output=True, timeout=10)
        subprocess.run(['sh', str(directory / 'start.sh')], check=True, timeout=30)
        subprocess.run(['tmux', 'has-session', '-t', '=cweb'], check=True, capture_output=True, timeout=10)
        finish(store, token, 'done')
    except Exception as exc:
        finish(store, token, 'failed', str(exc)[:500])
    finally:
        store.close()


if __name__ == '__main__':
    supervise(sys.argv[1], sys.argv[2])
