import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from channel_cli import adapters
from channel_cli.cli import route
from channel_cli.store import Store

ROOT = Path(__file__).resolve().parents[1]

FAKE = '''#!PYTHON
import json, os, sys, time
from pathlib import Path
a = sys.argv[1:]
prompt = sys.stdin.read()
if prompt == 'timeout':
    time.sleep(20)
if prompt == 'error':
    print('simulated failure', file=sys.stderr)
    sys.exit(7)
agent = Path(sys.argv[0]).name
if agent == 'agy':
    prompt = next(x[len('--print='):] for x in a if x.startswith('--print='))
sid = 'native-session'
if '--resume' in a:
    assert a[a.index('--resume')+1] == sid
    text = 'resumed:' + prompt
elif 'resume' in a:
    assert sid in a
    text = 'resumed:' + prompt
elif '--session' in a:
    assert a[a.index('--session')+1] == sid
    text = 'resumed:' + prompt
elif '--conversation' in a:
    assert a[a.index('--conversation')+1] == sid
    text = 'resumed:' + prompt
else:
    text = 'first:' + prompt
if agent == 'claude':
    assert '--strict-mcp-config' in a
    print(json.dumps({'result':text,'session_id':sid}))
elif agent == 'codex':
    Path(a[a.index('-o')+1]).write_text(text)
    print(json.dumps({'type':'thread.started','thread_id':sid}))
elif agent == 'agy':
    print(json.dumps({'response':text,'conversation_id':sid,'status':'SUCCESS'}))
else:
    print(json.dumps({'type':'text','sessionID':sid,'part':{'text':text}}))
'''.replace('PYTHON', sys.executable)


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / 'state')
        self.addCleanup(self.store.close)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for agent in set(adapters.AGENTS) - set(adapters.BODIES):
            exe = self.bin / agent
            exe.write_text(FAKE)
            exe.chmod(0o700)
        self.env = {**os.environ, 'CHANNEL_HOME': str(self.store.home),
                    'PYTHONPATH': str(ROOT), 'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}
        self.env.pop('CHANNEL_DEFAULT_AGENT', None)
        # Never reach a real Codex daemon from tests; daemon bodies are covered by test_body.
        self.env['CHANNEL_CODEX_SOCKET'] = str(self.base / 'no-daemon.sock')
        patcher = patch.dict(os.environ, {'CHANNEL_CODEX_SOCKET': self.env['CHANNEL_CODEX_SOCKET']})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, '-m', 'channel_cli', '--json', *args], env=self.env,
                           capture_output=True, text=True, timeout=10, cwd=ROOT)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def test_part_appends_answer_and_records_event(self):
        job = self.enqueue()
        self.store.part(job['id'], 'early')  # queued: ignored
        claimed = self.store.claim()
        self.store.part(claimed['id'], 'one')
        self.store.part(claimed['id'], 'two')
        self.assertEqual(self.store.get(claimed['id'])['answer'], 'one\ntwo')
        kinds = [e['type'] for e in self.store.events(0, 100)]
        self.assertEqual(kinds.count('part'), 2)

    def enqueue(self, text='hello', agent='claude', room='r', channel='web', message_id=None):
        return self.store.enqueue(channel, room, agent, text, text, message_id=message_id)

    def test_route(self):
        self.assertEqual(route('@Claude hello'), ('claude', 'hello'))
        self.assertEqual(route('hello a@codex.com', default='claude'), ('claude', 'hello a@codex.com'))
        self.assertEqual(route('@codex hi @codex'), ('codex', 'hi'))
        self.assertEqual(route('@gemini hello'), ('agy', 'hello'))
        self.assertEqual(route('@agy hi @gemini'), ('agy', 'hi'))
        for text in ('@unknown hi', '@claude @codex hi', '@claude', 'hi', '@claudex hi'):
            with self.assertRaises(ValueError, msg=text):
                route(text)
        with self.assertRaises(ValueError):
            route('@claude hi', explicit='codex')

    def test_idempotency_scoped_to_channel_room(self):
        first = self.enqueue(message_id='m')
        self.assertEqual(first['id'], self.enqueue(message_id='m')['id'])
        self.assertNotEqual(first['id'], self.enqueue(channel='telegram', message_id='m')['id'])
        self.assertNotEqual(first['id'], self.enqueue(room='other', message_id='m')['id'])
        with self.assertRaises(ValueError):
            self.enqueue(text='different', message_id='m')

    def test_concurrent_enqueue_deduplicates(self):
        home = self.store.home
        def add(_):
            s = Store(home)
            try:
                return s.enqueue('web', 'r', 'claude', 'hi', 'hi', message_id='same')['id']
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(add, range(12)))
        self.assertEqual(len(set(ids)), 1)

    def test_simultaneous_first_open_of_new_queue(self):
        barrier = threading.Barrier(8)
        fresh = self.base / 'fresh-state'
        def start(_):
            barrier.wait()
            s = Store(fresh)
            try:
                return s.enqueue('web', 'r', 'agy', 'hi', 'hi', message_id='same')['id']
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(start, range(8)))
        self.assertEqual(len(set(ids)), 1)

    def test_cwd_binding_and_room_isolation(self):
        first = self.enqueue()
        self.assertEqual(first['cwd'], self.enqueue()['cwd'])
        self.assertNotEqual(first['cwd'], self.enqueue(channel='telegram')['cwd'])
        with self.assertRaises(ValueError):
            self.store.enqueue('web', 'r', 'claude', 'hi', 'hi', cwd=str(self.base))

    def test_fifo_and_recovery(self):
        a, b = self.enqueue(), self.enqueue(text='second')
        self.assertEqual(self.store.claim()['id'], a['id'])
        self.store.recover()
        self.assertEqual(self.store.get(a['id'])['state'], 'interrupted')
        self.assertEqual(self.store.claim()['id'], b['id'])
        self.store.change(a['id'], 'retry')
        self.assertEqual(self.store.get(a['id'])['state'], 'queued')

    def test_cancel_and_retry_guards(self):
        a = self.enqueue()
        with self.assertRaises(ValueError):
            self.store.change(a['id'], 'retry')
        self.store.change(a['id'], 'cancel')
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.change(a['id'], 'cancel')

    def test_all_adapter_roundtrips_and_session_resume(self):
        for agent in set(adapters.AGENTS) - set(adapters.BODIES):
            with self.subTest(agent=agent):
                self.enqueue(agent=agent)
                result = json.loads(self.cli('worker', '--once').stdout)
                self.assertEqual(result['answer'], 'first:hello')
                self.enqueue(text='followup', agent=agent)
                result = json.loads(self.cli('worker', '--once').stdout)
                self.assertEqual(result['answer'], 'resumed:followup')

    def test_session_isolation(self):
        a = self.enqueue()
        self.store.finish(a, 'done', answer='hi', session='s1')
        self.assertEqual(self.store.session(a), 's1')
        for b in (self.enqueue(agent='codex'), self.enqueue(room='another'), self.enqueue(channel='telegram')):
            self.assertIsNone(self.store.session(b))

    def test_failure_logs_and_manual_retry(self):
        a = self.enqueue(text='error')
        p = self.cli('worker', '--once', ok=False)
        self.assertEqual(p.returncode, 1)
        result = json.loads(p.stdout)
        self.assertEqual(result['state'], 'failed')
        log = self.store.home / 'logs' / a['id'] / '1' / 'stderr.log'
        self.assertIn('simulated failure', log.read_text())
        self.cli('worker', '--drain')
        self.assertEqual(self.store.get(a['id'])['attempts'], 1)
        self.cli('retry', a['id'])
        self.cli('worker', '--once', ok=False)
        self.assertEqual(self.store.get(a['id'])['attempts'], 2)
        self.assertTrue(log.exists())

    def test_timeout(self):
        a = self.enqueue(text='timeout')
        self.cli('worker', '--once', '--timeout', '0.1', ok=False)
        result = self.store.get(a['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertIn('timeout', result['error'])

    def test_worker_lock_and_graceful_termination(self):
        a = self.enqueue(text='timeout')
        p = subprocess.Popen([sys.executable, '-m', 'channel_cli', '--json', 'worker'], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT)
        try:
            for _ in range(100):
                if self.store.get(a['id'])['state'] == 'running':
                    break
                time.sleep(0.02)
            else:
                self.fail('worker did not claim job')
            other = self.cli('worker', '--once', ok=False)
            self.assertEqual(other.returncode, 2)
            self.assertIn('another worker', other.stdout)
            p.terminate()
            out, err = p.communicate(timeout=6)
            self.assertEqual(p.returncode, 130, out + err)
            self.assertEqual(self.store.get(a['id'])['state'], 'interrupted')
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_event_cursor_and_channel_filter(self):
        a = self.enqueue()
        self.enqueue(channel='telegram')
        events = self.store.events(0, 100, 'web')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['id'], a['id'])
        cursor = events[-1]['seq']
        self.store.finish(a, 'done', answer='reply', session='s')
        events = self.store.events(cursor, 100, 'web')
        self.assertEqual([e['type'] for e in events], ['done'])
        self.assertEqual(events[0]['payload']['answer'], 'reply')

    def test_json_cli_and_stdin_no_shell_expansion(self):
        text = '@claude say $(touch should-not-exist) `echo nope`'
        p = subprocess.run([sys.executable, '-m', 'channel_cli', '--json', 'send', '-', '--room', 'stdin'],
                           input=text, env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        sent = json.loads(p.stdout)
        result = json.loads(self.cli('worker', '--once').stdout)
        self.assertEqual(result['answer'], 'first:' + text.replace('@claude ', ''))
        self.assertEqual(json.loads(self.cli('result', sent['id']).stdout)['state'], 'done')
        self.assertFalse((Path(sent['cwd']) / 'should-not-exist').exists())

    def test_native_errors_and_missing_session_rejected(self):
        for agent in ('claude', 'agy'):
            with self.assertRaises(ValueError):
                adapters.parse(agent, json.dumps({'error': {'message': 'failed'}}))
        with self.assertRaises(ValueError):
            adapters.parse('codex', '{"type":"turn.failed","error":{"message":"failed"}}')
        with self.assertRaises(ValueError):
            adapters.parse('claude', '{"result":"answer"}')
        for agent in ('claude', 'agy', 'codex', 'opencode'):
            with self.assertRaises(ValueError):
                adapters.parse(agent, '[]')

    def test_missing_executable(self):
        with patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'executable not found'):
                adapters.run('claude', 'hi', str(self.base), None, 1, self.base / 'logs')

    def test_nonzero_json_provider_error_on_stdout(self):
        script = self.bin / 'claude'
        script.write_text('#!' + sys.executable + '\nimport json,sys\n'
                          'print(json.dumps({"is_error":True,"result":"organization disabled subscription"}))\n'
                          'sys.exit(1)\n')
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(ValueError, 'organization disabled subscription'):
                adapters.run('claude', 'hi', str(self.base), None, 2, self.base / 'logs')

    def test_human_default_and_explicit_json_modes(self):
        human = subprocess.run([sys.executable, '-m', 'channel_cli', 'agents'], env=self.env,
                               capture_output=True, text=True, timeout=5, cwd=ROOT)
        self.assertEqual(human.returncode, 0)
        self.assertIn('에이전트 4개 설치됨', human.stdout)
        self.assertNotIn('unchecked', human.stdout)
        self.assertNotIn(str(self.bin), human.stdout)
        verbose = subprocess.run([sys.executable, '-m', 'channel_cli', 'agents', '-v'], env=self.env,
                                 capture_output=True, text=True, timeout=5, cwd=ROOT)
        self.assertIn(str(self.bin), verbose.stdout)
        for args in (['--json', 'agents'], ['agents', '--json']):
            p = subprocess.run([sys.executable, '-m', 'channel_cli', *args], env=self.env,
                               capture_output=True, text=True, timeout=5, cwd=ROOT)
            self.assertEqual(len(json.loads(p.stdout)), len(adapters.AGENTS))
        job = self.enqueue()
        result = subprocess.run([sys.executable, '-m', 'channel_cli', 'worker', '--once'], env=self.env,
                                capture_output=True, text=True, timeout=5, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('first:hello', result.stdout)
        self.assertNotIn('"prompt"', result.stdout)
        self.assertIn(job['id'][:8], result.stdout)

    def test_short_job_ids_resolve_and_reject_ambiguity(self):
        a = self.enqueue()
        self.assertEqual(self.store.get(a['id'][:8])['id'], a['id'])
        self.store.change(a['id'][:8], 'cancel')
        self.assertEqual(self.store.get(a['id'])['state'], 'cancelled')
        b = self.enqueue(text='second')
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET id=? WHERE id=?', ('a' * 8 + '1' * 24, a['id']))
            self.store.db.execute('UPDATE jobs SET id=? WHERE id=?', ('a' * 8 + '2' * 24, b['id']))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.store.get('a' * 8)

    def test_literal_prompt_preserves_mentions_in_quoted_context(self):
        text = 'Quote: @codex and @someone\nNew message: explain this decorator'
        job = json.loads(self.cli('send', '--agent', 'agy', '--literal', text).stdout)
        self.assertEqual(job['agent'], 'agy')
        self.assertEqual(job['prompt'], text)
        self.assertEqual(self.cli('send', '--literal', text, ok=False).returncode, 2)


if __name__ == '__main__':
    unittest.main()
