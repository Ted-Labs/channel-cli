"""Bind queue jobs to their canonical live body, independent of transport."""
import json
import socket
import struct
import subprocess
from pathlib import Path
from . import personas

class FollowupError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


ENGINES = ('claude-body', 'codex-body', 'agy-body')


def process(pid):
    stat = Path(f'/proc/{pid}/stat').read_text()
    fields = stat[stat.rfind(')') + 2:].split()
    return {'pid': int(pid), 'parent': int(fields[1]), 'start': fields[19],
            'args': Path(f'/proc/{pid}/cmdline').read_bytes().decode(errors='replace').split('\0')}


def pane(persona):
    from .agy_body import session_for_channel, tmux_bin
    session = session_for_channel(f'atrium-web:{persona}')
    result = subprocess.run([tmux_bin(), 'list-panes', '-s', '-t', f'={session}', '-F', '#{pane_pid}'], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise ValueError('본체 세션을 확인하지 못했어요.')
    return process(int(result.stdout.strip().splitlines()[0]))


def daemon():
    from .body import default_socket, Connection
    connection = Connection(default_socket(), 3)
    try:
        pid, _, _ = struct.unpack('3i', connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return process(pid)
    finally:
        connection.close()


def capture(job):
    if job.get('persona') not in personas.ids() or job['agent'] not in ENGINES:
        return None
    try:
        current = pane(job['persona'])
        expected = {'claude-body': 'claude', 'codex-body': 'codex', 'agy-body': 'agy'}[job['agent']]
        if not any(Path(a).name == expected for a in current['args']):
            return None
        if any(a.startswith(('--fork-session', '--bg-spare', '--bg-pty-host')) for a in current['args']):
            return None
        identity = {'persona': job['persona'], 'engine': job['agent'], 'pane': {'pid': current['pid'], 'start': current['start']}}
        if job['agent'] == 'codex-body':
            peer = daemon()
            identity['daemon'] = {'pid': peer['pid'], 'start': peer['start']}
        return identity
    except (OSError, ValueError, subprocess.SubprocessError):
        # Lack of followup identity must not prevent legacy queue execution.
        return None


def authorized(identity, pid):
    try:
        current = pane(identity['persona'])
        if any(current[k] != identity['pane'][k] for k in ('pid', 'start')):
            return False
        if any(a.startswith(('--fork-session', '--bg-spare', '--bg-pty-host')) for a in current['args']):
            return False
        root = identity['pane']['pid']
        if identity['engine'] == 'codex-body':
            peer = daemon()
            if any(peer[k] != identity.get('daemon', {}).get(k) for k in ('pid', 'start')):
                return False
            env = dict(part.split(b'=', 1) for part in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in part)
            if not identity.get('session') or env.get(b'CODEX_THREAD_ID', b'').decode() != identity['session']:
                return False
            root = peer['pid']
        if identity['engine'] == 'agy-body':
            env = dict(part.split(b'=', 1) for part in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in part)
            if not identity.get('conversation') or env.get(b'ANTIGRAVITY_CONVERSATION_ID', b'').decode() != identity['conversation']:
                return False
        seen = set()
        while pid > 1 and pid not in seen and len(seen) < 64:
            if pid == root:
                return True
            seen.add(pid)
            row = process(pid)
            if any(Path(arg).name in ('claude', 'codex', 'agy', 'gemini') or arg.startswith(('--fork-session', '--bg-spare', '--bg-pty-host')) for arg in row['args'][:2]):
                return False
            pid = row['parent']
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return False


def caller_persona(pid):
    """Return the live body persona whose process tree started ``pid``, or None.

    A clone (persona-less engine run) may only be given work and read back by
    the body that owns it, so the caller must descend from that body's pane
    (or, for Codex, its app-server daemon) without crossing another engine
    process or a forked/background session.
    """
    for persona in personas.ids():
        try:
            current = pane(persona)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if any(a.startswith(('--fork-session', '--bg-spare', '--bg-pty-host')) for a in current['args']):
            continue
        roots = {current['pid']}
        if any(Path(a).name == 'codex' for a in current['args']):
            try:
                roots.add(daemon()['pid'])
            except (OSError, ValueError):
                pass
        walk, seen = int(pid), set()
        try:
            while walk > 1 and walk not in seen and len(seen) < 64:
                if walk in roots:
                    return persona
                seen.add(walk)
                row = process(walk)
                if any(Path(arg).name in ('claude', 'codex', 'agy', 'gemini') or arg.startswith(('--fork-session', '--bg-spare', '--bg-pty-host')) for arg in row['args'][:2]):
                    break
                walk = row['parent']
        except (OSError, ValueError):
            continue
    return None


def prompt(job, text):
    lines = text.splitlines(keepends=True)
    index = 1 if lines and lines[0].startswith('[Atrium metadata] ') else 0
    prefix = '[Atrium] '
    if len(lines) > index and lines[index].startswith(prefix):
        try:
            header = json.loads(lines[index][len(prefix):])
            if not isinstance(header, dict): raise ValueError('invalid header')
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid Atrium header") from exc
        lines[index] = prefix + json.dumps({**header, 'followup': job['id']}, ensure_ascii=False, separators=(',', ':')) + '\n'
    else:
        persona = job['persona']
        entry = personas.config(persona)
        header = {'persona': entry.get('name', persona), 'lang': 'ko', 'rules': '~/Corpus/agents/ATRIUM_RULES.md', 'read': ['identity', 'rules'], 'followup': job['id']}
        if entry.get('instructionsFile'): header['identity'] = '~/Corpus/' + entry['instructionsFile']
        lines.insert(index, prefix + json.dumps(header, ensure_ascii=False, separators=(',', ':')) + '\n')
    return ''.join(lines)
