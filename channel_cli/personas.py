"""Persona registry shared with Atrium, with legacy defaults when unavailable."""
import json
import os
import re
from pathlib import Path

_DEFAULTS = [
    {'id': 'cora', 'name': '코라', 'cwd': '~/Corpus', 'role': 'master'},
    {'id': 'pixie', 'name': '픽시', 'cwd': '~/codex-jobs'},
    {'id': 'gemini', 'name': 'Gemini', 'cwd': '~/agy-jobs'},
]

def registry():
    path = Path(os.environ.get('ATRIUM_AGENTS_PATH', '~/Corpus/Atrium/web/atrium/agents.json')).expanduser()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, list) or not data:
            raise ValueError('empty persona registry')
        ids = [x.get('id') for x in data if isinstance(x, dict)]
        if len(ids) != len(data) or len(set(ids)) != len(ids) or any(not isinstance(x, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', x) for x in ids):
            raise ValueError('invalid persona registry')
        return data
    except (OSError, ValueError, TypeError):
        return [item.copy() for item in _DEFAULTS]

def ids():
    return tuple(item['id'] for item in registry())

def config(persona):
    return next((item for item in registry() if item['id'] == persona), {})

def name(persona):
    return config(persona).get('name') or persona

def cwd(persona):
    legacy = {'cora': '~/Corpus', 'pixie': '~/codex-jobs', 'gemini': '~/agy-jobs'}
    path = config(persona).get('cwd') or legacy.get(persona)
    if path:
        return str(Path(path).expanduser())
    candidate = Path.home() / f'{persona}-jobs'
    return str(candidate if candidate.is_dir() else Path.home() / 'Corpus')

def session(persona):
    key = f"AGENT_{persona.upper().replace('-', '_')}_TMUX"
    legacy = {'cora': 'CHANNEL_CORA_SESSION', 'gemini': 'CHANNEL_AGY_SESSION'}
    return os.environ.get(key) or (os.environ.get(legacy[persona]) if persona in legacy else None) or config(persona).get('tmuxSession') or persona

def master():
    entries = registry()
    return next((item['id'] for item in entries if item.get('role') == 'master'), entries[0]['id'])
