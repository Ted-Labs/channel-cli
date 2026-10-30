"""Transport to the existing Cora MCP session, returning through the common queue."""
import json
import os
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from .adapters import Reply


def run(prompt, timeout=300):
    token = os.environ.get('ATRIUM_TOKEN') or os.environ.get('ARTRIUM_TOKEN')
    if not token:
        try:
            token = (Path.home() / '.claude/channels/corpus-web/notify-token').read_text().strip()
        except OSError as exc:
            raise ValueError('claude-body needs ATRIUM_TOKEN or notify-token') from exc
    base = os.environ.get('CORPUS_WEB_URL', 'http://127.0.0.1:8790').rstrip('/')
    meta = {}
    if prompt.startswith('[Atrium metadata] '):
        header, _, prompt = prompt.partition('\n')
        try:
            meta = json.loads(header[len('[Atrium metadata] '):])
        except ValueError as exc:
            raise ValueError('Invalid Atrium message metadata') from exc
    run_id = str(uuid.uuid4())
    deadline = time.monotonic() + timeout

    def request(method, path, data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Cora body did not complete before its deadline')
        req = urllib.request.Request(base + path, method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=min(10, remaining)) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError(f'Cora body transport HTTP {exc.code}') from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ValueError('Cora body transport unavailable') from exc

    path = '/body-runs/' + run_id
    created = False
    try:
        request('POST', '/body-runs', {'id': run_id, 'agent': 'cora', 'prompt': prompt, 'timeout': timeout, 'meta': meta})
        created = True
        ack_deadline = min(deadline, time.monotonic() + 10)
        while True:
            state = request('GET', path)
            if state.get('state') == 'done':
                answer = state.get('answer')
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError('Cora body returned an empty answer')
                return Reply(answer, 'cora')
            if state.get('state') == 'failed':
                raise ValueError(state.get('error') or 'Cora body failed')
            if state.get('state') not in ('pending', 'accepted'):
                raise ValueError('Cora body returned an invalid state')
            if state['state'] == 'pending' and time.monotonic() >= ack_deadline:
                raise ValueError('Cora MCP bridge did not acknowledge delivery')
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    finally:
        if created:
            try:
                # Cleanup is bounded even after the main request deadline expired.
                req = urllib.request.Request(base + path, method='DELETE',
                    headers={'Authorization': 'Bearer ' + token})
                with urllib.request.urlopen(req, timeout=2):
                    pass
            except (OSError, ValueError):
                pass
