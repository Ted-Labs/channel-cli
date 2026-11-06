import concurrent.futures
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from channel_cli import adapters
from channel_cli.cli import RoutingDecision, route, worker
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

    def enqueue(self, text='hello', agent='claude', room='r', channel='web', message_id=None, priority=90):
        return self.store.enqueue(channel, room, agent, text, text, message_id=message_id, priority=priority)

    def test_route(self):
        self.assertEqual(route('@Claude hello'), RoutingDecision('claude', 'hello', 90, 'worker'))
        self.assertEqual(route('hello a@codex.com', default='claude'), RoutingDecision('claude', 'hello a@codex.com', 90, 'worker'))
        self.assertEqual(route('@codex hi @codex'), RoutingDecision('codex', 'hi', 90, 'worker'))
        self.assertEqual(route('@gemini hello'), RoutingDecision('agy', 'hello', 90, 'worker'))
        self.assertEqual(route('@agy hi @gemini'), RoutingDecision('agy', 'hi', 90, 'worker'))
        self.assertEqual(route('@agy hello', priority=42), RoutingDecision('agy', 'hello', 42, 'worker'))
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

    def test_message_id_priority_is_part_of_idempotency(self):
        first = self.enqueue(message_id='priority-key', priority=80)
        self.assertEqual(first['id'], self.enqueue(message_id='priority-key', priority=80)['id'])
        with self.assertRaisesRegex(ValueError, 'priority'):
            self.enqueue(message_id='priority-key', priority=90)

    def test_retry_preserves_priority_in_job_and_event_payload(self):
        queued = self.enqueue(priority=33)
        running = self.store.claim()
        failed = self.store.finish(running, 'failed', error='retry me')
        self.assertEqual(failed['priority'], 33)
        retried = self.store.change(queued['id'], 'retry')
        self.assertEqual(retried['priority'], 33)
        retry_event = self.store.events(0, 100)[-1]
        self.assertEqual(retry_event['type'], 'retry')
        self.assertEqual(retry_event['payload']['priority'], 33)
        self.assertEqual(self.store.claim()['id'], queued['id'])

    def test_priority_claim_order_and_fifo_tie_break(self):
        low = self.enqueue(text='low', priority=0)
        high_first = self.enqueue(text='high first', priority=100)
        default = self.enqueue(text='default')
        high_second = self.enqueue(text='high second', priority=100)
        self.assertEqual(default['priority'], 90)
        self.assertEqual([self.store.claim()['id'] for _ in range(4)],
                         [high_first['id'], high_second['id'], default['id'], low['id']])

    def test_snapshot_groups_priority_order_and_bounds_pages(self):
        channel = 'atrium-web:pixie'
        low = self.store.enqueue(channel, 'dev', 'codex-body', 'low request', 'private prompt', message_id='m1', priority=80)
        tied = self.store.enqueue(channel, 'dev', 'codex-body', 'next tied request', 'private prompt', message_id='m2', priority=80)
        high = self.store.enqueue(channel, 'dev', 'codex-body', 'highest request', 'private prompt', message_id='m3', priority=95)
        self.store.enqueue('atrium-web:cora', 'dev', 'claude-body', 'other lane', 'private prompt', priority=99)
        first_page = self.store.snapshot('atrium-web:', per_agent_limit=2, recent_limit=0)
        lane = first_page['agents']['pixie']
        self.assertEqual([job['id'] for job in lane['queued']], [high['id'], low['id']])
        self.assertEqual(lane['queued_total'], 3)
        self.assertTrue(lane['queued_truncated'])
        self.assertNotIn('prompt', lane['queued'][0])
        second_page = self.store.snapshot('atrium-web:', per_agent_limit=2, recent_limit=0,
                                          only_lane='pixie', offset=2)
        self.assertEqual([job['id'] for job in second_page['agents']['pixie']['queued']], [tied['id']])
        self.assertFalse(second_page['agents']['pixie']['queued_truncated'])

    def test_priority_validation_and_cli_delivery(self):
        for priority in (-1, 101, True, 50.5, '50'):
            with self.subTest(priority=priority), self.assertRaises(ValueError):
                self.enqueue(priority=priority)
        job = json.loads(self.cli('send', '--agent', 'claude', '--priority', '7', 'urgent later').stdout)
        self.assertEqual(job['priority'], 7)
        invalid = self.cli('send', '--agent', 'claude', '--priority', '101', 'out of range', ok=False)
        self.assertEqual(invalid.returncode, 2)

    def test_legacy_database_migrates_priority_and_busy_timeout(self):
        home = self.base / 'legacy-state'
        home.mkdir()
        path = home / 'queue.sqlite3'
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL, room TEXT NOT NULL, agent TEXT NOT NULL,
                message_id TEXT, text TEXT NOT NULL, prompt TEXT NOT NULL, cwd TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', answer TEXT, error TEXT,
                created REAL NOT NULL, started REAL, finished REAL, attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(channel, room, message_id)
            );
            INSERT INTO jobs(id,channel,room,agent,text,prompt,cwd,created)
            VALUES('old-first','web','legacy','claude','first','first','/tmp',1.0);
            INSERT INTO jobs(id,channel,room,agent,text,prompt,cwd,created)
            VALUES('old-second','web','legacy','claude','second','second','/tmp',2.0);
        """)
        db.close()

        migrated = Store(home)
        self.assertEqual(migrated.db.execute('PRAGMA busy_timeout').fetchone()[0], 15000)
        self.assertIsNotNone(migrated.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='jobs_queue_priority'"
        ).fetchone())
        self.assertEqual(migrated.get('old-first')['priority'], 90)
        self.assertEqual(migrated.get('old-second')['priority'], 90)
        self.assertEqual(migrated.claim()['id'], 'old-first')
        migrated.close()

        reopened = Store(home)
        self.assertEqual(reopened.get('old-second')['priority'], 90)
        self.assertEqual(reopened.db.execute('PRAGMA busy_timeout').fetchone()[0], 15000)
        reopened.close()

    def test_legacy_migration_is_safe_under_concurrent_open(self):
        home = self.base / 'concurrent-legacy-state'
        home.mkdir()
        db = sqlite3.connect(home / 'queue.sqlite3')
        db.executescript("""
            CREATE TABLE jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL, room TEXT NOT NULL, agent TEXT NOT NULL,
                message_id TEXT, text TEXT NOT NULL, prompt TEXT NOT NULL, cwd TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', answer TEXT, error TEXT,
                created REAL NOT NULL, started REAL, finished REAL, attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(channel, room, message_id)
            );
            INSERT INTO jobs(id,channel,room,agent,text,prompt,cwd,created)
            VALUES('legacy-concurrent','web','legacy','claude','text','prompt','/tmp',1.0);
        """)
        db.close()
        barrier = threading.Barrier(6)

        def open_legacy(_):
            barrier.wait()
            store = Store(home)
            try:
                return store.get('legacy-concurrent')['priority']
            finally:
                store.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            priorities = list(pool.map(open_legacy, range(6)))
        self.assertEqual(priorities, [90] * 6)

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

    def test_fresh_body_session_is_recorded_once_and_reused_on_retry(self):
        job = self.store.enqueue('web', 'fresh-room', 'codex-body', 'new', 'new', fresh=True)
        self.assertEqual(job['fresh'], 1)
        claimed = self.store.claim()
        self.store.set_session(claimed, 'thread-fresh', consume_fresh=True)
        self.assertEqual(self.store.get(job['id'])['fresh'], 1)
        self.assertEqual(self.store.get(job['id'])['fresh_started'], 1)
        self.assertEqual(self.store.session(claimed), 'thread-fresh')
        self.store.finish(claimed, 'interrupted', error='controlled retry')
        self.store.change(job['id'], 'retry')
        retried = self.store.claim()
        self.assertEqual((retried['fresh'], retried['fresh_started']), (1, 1))
        self.assertEqual(self.store.session(retried), 'thread-fresh')

    def test_fresh_is_limited_to_supported_live_bodies(self):
        for agent in ('claude-body', 'claude'):
            with self.subTest(agent=agent), self.assertRaisesRegex(ValueError, 'fresh sessions are supported'):
                self.store.enqueue('web', 'fresh-room', agent, 'new', 'new', fresh=True)

    def test_cli_send_persists_fresh_for_live_body(self):
        result = self.cli('send', '--agent', 'codex-body', '--literal', '--fresh', 'new conversation')
        self.assertEqual(json.loads(result.stdout)['fresh'], 1)
        bad = self.cli('send', '--agent', 'claude-body', '--literal', '--fresh', 'not supported', ok=False)
        self.assertIn('--fresh requires codex-body or agy-body', bad.stdout + bad.stderr)

    def test_fifo_and_recovery(self):
        a, b = self.enqueue(agent='agy-body'), self.enqueue(agent='agy-body', text='second')
        self.assertEqual(self.store.claim()['id'], a['id'])
        self.store.recover()
        self.assertEqual(self.store.get(a['id'])['state'], 'queued')
        self.assertEqual(self.store.claim()['id'], a['id'])

    def test_recovery_requeues_only_undelivered_body_jobs(self):
        undelivered = self.enqueue(agent='agy-body')
        self.store.claim('agy-body')
        self.store.recover('agy-body')
        self.assertEqual(self.store.get(undelivered['id'])['state'], 'queued')
        delivered = self.store.claim('agy-body')
        self.store.set_turn(delivered['id'], 'gemini', 'turn-1')
        self.store.recover('agy-body')
        self.assertEqual(self.store.get(delivered['id'])['state'], 'interrupted')
        cora = self.enqueue(agent='claude-body', text='cora request')
        self.store.claim('claude-body')
        self.store.recover('claude-body')
        self.assertEqual(self.store.get(cora['id'])['state'], 'interrupted')

    def test_agent_worker_claims_its_fifo_without_waiting_for_other_agents(self):
        cora = self.enqueue(agent='claude-body')
        pixie = self.enqueue(agent='codex-body', text='pixie request')
        gemini = self.enqueue(agent='agy-body', text='gemini request')
        self.assertEqual(self.store.claim('codex-body')['id'], pixie['id'])
        self.assertEqual(self.store.get(cora['id'])['state'], 'queued')
        self.assertEqual(self.store.get(gemini['id'])['state'], 'queued')
        self.assertEqual(self.store.claim('claude-body')['id'], cora['id'])
        self.assertEqual(self.store.claim('agy-body')['id'], gemini['id'])

    def test_agent_worker_recovery_leaves_other_agent_runs_untouched(self):
        cora = self.enqueue(agent='claude-body')
        pixie = self.enqueue(agent='codex-body', text='pixie request')
        self.store.claim('claude-body')
        self.store.claim('codex-body')
        self.store.recover('claude-body')
        self.assertEqual(self.store.get(cora['id'])['state'], 'interrupted')
        self.assertEqual(self.store.get(pixie['id'])['state'], 'running')

    def test_agent_worker_completes_pixie_without_claiming_stalled_cora(self):
        cora = self.enqueue(agent='claude-body')
        pixie = self.enqueue(agent='codex-body', text='pixie request')
        self.store.claim('claude-body')
        args = SimpleNamespace(command='worker', json=True, verbose=False, once=True,
                               drain=False, timeout=5, poll=0.01, agent='codex-body')
        output = io.StringIO()
        with patch('channel_cli.cli.adapters.run', return_value=adapters.Reply('pixie answer', 'thread-1')):
            with redirect_stdout(output):
                worker(self.store, args)
        self.assertEqual(json.loads(output.getvalue())['id'], pixie['id'])
        self.assertEqual(self.store.get(pixie['id'])['state'], 'done')
        self.assertEqual(self.store.get(cora['id'])['state'], 'running')

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
        with patch.dict(os.environ, self.env):
            installed_count = sum(1 for a in adapters.discover() if a['installed'])
        self.assertIn(f'에이전트 {installed_count}개 설치됨', human.stdout)
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
