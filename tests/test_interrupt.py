import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from channel_cli import adapters, agy_body, claude_body
from channel_cli.store import Store


class StopStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'queue')
        self.addCleanup(self.store.close)

    def job(self):
        return self.store.enqueue('web', 'dev', 'agy-body', 'request', 'request')

    def test_only_running_jobs_accept_stop_and_duplicate_request_is_idempotent(self):
        job = self.job()
        with self.assertRaisesRegex(ValueError, 'queued'):
            self.store.request_interrupt(job['id'])
        claimed = self.store.claim()
        self.assertTrue(self.store.request_interrupt(job['id'])['interrupt_requested'])
        self.store.request_interrupt(job['id'])
        self.assertEqual([e['type'] for e in self.store.events(0, 100)].count('interrupt_requested'), 1)
        self.store.finish(claimed, 'interrupted', error='stopped by interrupt')
        with self.assertRaisesRegex(ValueError, 'interrupted'):
            self.store.request_interrupt(job['id'])

    def test_stop_wins_done_race_and_does_not_leak_to_retry_or_another_job(self):
        first = self.job()
        running = self.store.claim()
        self.store.set_turn(first['id'], 'old-session', 'old-turn')
        self.store.request_interrupt(first['id'])
        self.store.part(first['id'], 'late part')
        self.assertFalse(any(e['type'] == 'part' for e in self.store.events(0, 100)))
        result = self.store.finish(running, 'done', answer='late final')
        self.assertEqual((result['state'], result['answer'], result['error']), ('interrupted', None, 'stopped by interrupt'))
        self.store.change(first['id'], 'retry')
        self.assertEqual(self.store.finish(running, 'done', answer='stale final')['state'], 'queued')
        retry = self.store.claim()
        self.assertEqual(retry['attempts'], 2)
        self.assertEqual(self.store.finish(running, 'done', answer='stale final')['state'], 'running')
        self.assertNotIn('interrupt_requested', retry)
        self.assertIsNone(self.store.turn(first['id']))
        self.assertEqual(self.store.finish(retry, 'done', answer='retry final')['state'], 'done')
        self.job()
        other = self.store.claim()
        self.assertNotIn('interrupt_requested', other)

    def test_completion_wins_race_when_already_committed(self):
        job = self.job(); running = self.store.claim()
        self.store.finish(running, 'done', answer='already done')
        with self.assertRaisesRegex(ValueError, 'done'):
            self.store.request_interrupt(job['id'])
        self.assertEqual(self.store.get(job['id'])['answer'], 'already done')

    def test_cli_records_attempt_bound_request_without_sending_blind_escape(self):
        job = self.job(); running = self.store.claim()
        env = {**os.environ, 'CHANNEL_HOME': str(self.store.home)}
        result = subprocess.run([sys.executable, '-m', 'channel_cli', 'interrupt', job['id'], '--json'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['interrupt_requested'])
        self.assertTrue(self.store.interrupt_requested(job['id'], running['attempts']))


class CoraStopTests(unittest.TestCase):
    def test_cancel_before_delivery_never_touches_the_native_body(self):
        with patch('urllib.request.urlopen') as request:
            with self.assertRaises(adapters.Interrupted):
                claude_body.run('request', should_interrupt=lambda: True)
        request.assert_not_called()

    def test_cora_waits_for_actual_interrupted_state_and_cleans_up(self):
        calls = []
        states = iter([{'state': 'accepted'}, {'state': 'stopping'}, {'state': 'interrupted'}])
        created = False
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps(self.data).encode()
        def request(req, **kwargs):
            nonlocal created
            calls.append((req.method, req.full_url))
            if req.method == 'POST' and req.full_url.endswith('/body-runs'):
                created = True
            return Response(next(states) if req.method == 'GET' else {})
        with patch.dict(os.environ, {'ATRIUM_TOKEN': 'test'}), patch('urllib.request.urlopen', side_effect=request), patch('time.sleep'):
            with self.assertRaises(adapters.Interrupted):
                claude_body.run('request', should_interrupt=lambda: created)
        self.assertEqual(sum(path.endswith('/interrupt') for _, path in calls), 1)
        self.assertEqual(calls[-1][0], 'DELETE')


    def test_cora_completion_before_stop_poll_returns_without_interrupting_idle_body(self):
        calls = []
        created = False
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps(self.data).encode()
        def request(req, **kwargs):
            nonlocal created
            calls.append(req.full_url)
            if req.method == 'POST': created = True
            return Response({'state': 'done', 'answer': 'already completed'})
        with patch.dict(os.environ, {'ATRIUM_TOKEN': 'test'}), patch('urllib.request.urlopen', side_effect=request):
            result = claude_body.run('request', should_interrupt=lambda: created)
        self.assertEqual(result.text, 'already completed')
        self.assertFalse(any(path.endswith('/interrupt') for path in calls))


class GeminiStopTests(unittest.TestCase):
    def test_stop_terminates_owned_native_process_and_never_returns_partial_as_done(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'GEMINI_HOME': home}):
            transcript = Path(home) / '.gemini/antigravity-cli/brain/own/.system_generated/logs/transcript.jsonl'
            transcript.parent.mkdir(parents=True)
            late = Path(home) / 'late-result'
            state = {'prompt': '', 'child': None, 'escape': 0}
            requested = threading.Event()
            def tmux(args, **kwargs):
                if 'load-buffer' in args:
                    state['prompt'] = kwargs['input'].decode()
                if 'send-keys' in args and args[-1] == 'Enter':
                    transcript.write_text(json.dumps({'type': 'USER_INPUT', 'content': state['prompt']})+'\n'+json.dumps({'source':'MODEL','type':'PLANNER_RESPONSE','content':'partial answer'})+'\n')
                    state['child'] = subprocess.Popen([sys.executable, '-c', 'import time,pathlib,sys;time.sleep(3);pathlib.Path(sys.argv[1]).write_text("late")', str(late)])
                if args[-1] == 'Escape':
                    state['escape'] += 1
                    state['child'].terminate(); state['child'].wait(timeout=3)
                return Mock(returncode=0)
            def busy(*args, **kwargs):
                return state['child'] is not None and state['child'].poll() is None
            try:
                with patch.object(agy_body, 'is_online', return_value=True), patch.object(agy_body, 'is_busy', side_effect=busy), patch('subprocess.run', side_effect=tmux):
                    with self.assertRaises(adapters.Interrupted):
                        agy_body.run('request', timeout=5, on_turn=lambda *_: requested.set(), should_interrupt=requested.is_set)
                self.assertEqual(state['escape'], 1)
                self.assertIsNotNone(state['child'].poll())
                self.assertFalse(late.exists())
            finally:
                if state['child'] and state['child'].poll() is None:
                    state['child'].kill(); state['child'].wait()

    def test_cancel_while_waiting_does_not_interrupt_someone_elses_turn(self):
        with patch.object(agy_body, 'is_online', return_value=True), patch.object(agy_body, 'is_busy', return_value=True), patch.object(agy_body, 'interrupt') as interrupt:
            with self.assertRaises(adapters.Interrupted):
                agy_body.run('request', should_interrupt=lambda: True)
        interrupt.assert_not_called()
