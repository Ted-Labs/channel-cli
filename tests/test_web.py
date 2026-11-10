import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.followup_headers = []
        cls.fail_cora_usage = False
        cls.canvas_result = None

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                cls.requests.append((self.command, self.path, self.headers.get('Authorization'),
                                     json.loads(data) if data else None))
                if self.path == '/sys/usage' and cls.fail_cora_usage:
                    self.send_response(502)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'usage unavailable')
                    return
                if self.path == '/canvas/workflow/route' and cls.canvas_result == 'unavailable':
                    self.send_response(404)
                    self.end_headers()
                    return
                response = {'ok': True}
                if self.path == '/notify/followup':
                    cls.followup_headers.append(dict(self.headers))
                if self.path == '/canvas/workflow/route':
                    response = cls.canvas_result or {'target': 'pixie', 'revision': None}
                if self.path == '/agents':
                    response = {'runtimes': [
                        {'id': 'cora', 'state': 'busy', 'queued': 0},
                        {'id': 'pixie', 'state': 'idle', 'queued': 0},
                        {'id': 'gemini', 'state': 'idle', 'queued': 0},
                    ]}
                if self.path == '/sys/usage':
                    response = {'session': {'utilization': 10}, 'weekly': {'utilization': 20}}
                if self.path == '/sys/usage/agents':
                    response = {'pixie': [{'name': '5시간', 'used': 5}], 'gemini': [{'name': '5시간', 'used': 90}]}
                if self.path == '/notify':
                    response = {'id': 'attachment', 'file': '/files/result.png'}
                if self.path.endswith('/log?since=2'):
                    response = []
                if self.path.endswith('/draft'):
                    response = {'goal': 'goal', 'versions': []}
                payload = b'image-bytes' if self.path == '/files/result.png' else json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png' if self.path.startswith('/files/') else 'application/json')
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = handle_request

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.requests.clear()
        type(self).fail_cora_usage = False
        type(self).canvas_result = None
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.env = {**os.environ, 'HOME': str(self.home), 'CHANNEL_HOME': str(self.home / 'queue'),
                    'PYTHONPATH': str(ROOT), 'CORPUS_WEB_URL': self.url,
                    'ATRIUM_PUBLIC_URL': self.url, 'ATRIUM_TOKEN': 'test-new'}
        for name in ('ARTRIUM_TOKEN', 'ARTRIUM_PUBLIC_URL', 'CORPUS_PERSONA'):
            self.env.pop(name, None)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-m', 'channel_cli', *args], env=self.env,
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_help_contains_queue_and_web_without_auth_or_database(self):
        self.env.pop('ATRIUM_TOKEN')
        result = self.cli('--help')
        for command in ('send', 'worker', 'reply', 'share', 'usage', 'assign'):
            self.assertIn(command, result.stdout)
        self.assertFalse((self.home / 'queue').exists())
        self.assertEqual(self.requests, [])

    def test_followup_uses_common_job_id_and_server_destination_without_bridge_owner(self):
        job_id = 'a' * 32
        self.cli('followup', '--agent', 'pixie', '--job-id', job_id, '--text', '후속 알림')
        method, path, auth, payload = self.requests[-1]
        self.assertEqual((method, path, auth), ('POST', '/notify/followup', 'Bearer test-new'))
        self.assertEqual(payload, {'agent': 'pixie', 'jobId': job_id, 'text': '후속 알림'})
        headers = {k.lower(): v for k, v in self.followup_headers[-1].items()}
        self.assertNotIn('x-atrium-owner', headers)
        self.assertTrue(headers['x-atrium-client-pid'].isdigit())
        self.assertFalse((self.home / 'queue').exists())
        self.cli('followup', '--agent', 'pixie', '--run-id', job_id, '--text', '이전 옵션 별칭')
        self.assertEqual(self.requests[-1][3]['jobId'], job_id)
        self.cli('followup', '--agent', 'pixie', '--job-id', job_id, '--text', 'bad', '--topic', 'other', expected=2)

    def test_followup_without_common_job_or_persona_is_rejected_locally(self):
        self.cli('followup', '--agent', 'cora', '--run-id', '123', '--text', '알림', expected=2)
        self.cli('followup', '--agent', 'cora', '--text', '알림', expected=2)
        self.assertEqual(self.requests, [])

    def test_reply_preserves_literal_text_and_does_not_create_queue(self):
        text = 'hello @All; $(touch should-not-exist)\nsecond line'
        self.cli('--json', 'reply', '--agent', 'pixie', '--topic', 'dev',
                 '--reply-to', 'original', '--text', text)
        method, path, auth, payload = self.requests[-1]
        self.assertEqual((method, path, auth), ('POST', '/agent-action', 'Bearer test-new'))
        self.assertEqual(payload, {'action': 'reply', 'agent': 'pixie', 'topic': 'dev',
                                   'replyTo': 'original', 'text': text})
        self.assertFalse((self.home / 'queue').exists())

    def test_legacy_token_and_new_token_precedence(self):
        self.env['ARTRIUM_TOKEN'] = 'test-old'
        self.cli('usage', '--json')
        self.assertTrue(all(row[2] == 'Bearer test-new' for row in self.requests))
        self.requests.clear()
        self.env.pop('ATRIUM_TOKEN')
        self.cli('usage', '--json')
        self.assertEqual([row[2] for row in self.requests], ['Bearer test-old'] * 2)

    def test_deliver_preserves_board_thread_and_fresh_session(self):
        self.cli('deliver', 'gemini', 'work', '--agent', 'cora', '--thread', 't12',
                 '--board', 'b1', '--title', 'title', '--fresh')
        self.assertEqual(self.requests[-1][3], {'action': 'deliver', 'agent': 'cora',
                         'target': 'gemini', 'text': 'work', 'thread': 't12',
                         'board': 'b1', 'title': 'title', 'fresh': True})

    def test_deliver_can_record_assignment_reason(self):
        self.cli('deliver', 'pixie', 'work', '--agent', 'cora', '--reason', '1지망 픽시가 사용 가능')
        self.assertEqual(self.requests[-1][3]['assignmentReason'], '1지망 픽시가 사용 가능')

    def test_deliver_auto_assignment_propagates_priority(self):
        self.cli("deliver", "work", "--agent", "cora", "--role", "구현", "--difficulty", "보통")
        body = self.requests[-1][3]
        self.assertEqual((body["target"], body["priority"], body["tier"]), ("pixie", 80, "body"))
        self.assertFalse(body["assignmentQueued"])
        self.assertIn("픽시", body["assignmentReason"])

    def test_assign_reads_active_revision_and_keeps_legacy_without_active(self):
        legacy = json.loads(self.cli('--json', 'assign', '조사', '보통').stdout)
        self.assertEqual((legacy['target'], legacy['canvas_revision']), ('pixie', None))
        type(self).canvas_result = {'target': 'gemini', 'revision': 12, 'priority': 72}
        changed = json.loads(self.cli('--json', 'assign', '조사', '보통').stdout)
        self.assertEqual((changed['target'], changed['canvas_revision'], changed['priority']), ('gemini', 12, 72))
        self.assertTrue(changed['queued'])
        self.assertEqual(self.requests[-1][1], '/canvas/workflow/route')

    def test_assign_falls_back_when_old_server_has_no_canvas_route(self):
        type(self).canvas_result = 'unavailable'
        result = json.loads(self.cli('--json', 'assign', '조사', '보통').stdout)
        self.assertEqual((result['target'], result['canvas_revision']), ('pixie', None))

    def test_assign_skips_busy_and_low_quota_candidates(self):
        result = json.loads(self.cli('--json', 'assign', '조사', '보통').stdout)
        self.assertEqual(result['target'], 'pixie')
        self.assertEqual(result['preferenceRank'], 3)
        self.assertEqual(result['priority'], 80)
        self.assertEqual(result['tier'], 'body')
        self.assertTrue(result['available'])
        self.assertEqual(result['considered'][0]['agent'], 'gemini')
        self.assertIn('Gemini', result['reason'])
        self.assertEqual([row[1] for row in self.requests], ['/agents', '/sys/usage', '/sys/usage/agents', '/canvas/workflow/route'])

    def test_assign_and_usage_tolerate_cora_usage_502(self):
        type(self).fail_cora_usage = True
        result = json.loads(self.cli('--json', 'assign', '조사', '보통').stdout)
        self.assertEqual(result['target'], 'pixie')
        self.assertEqual(result['preferenceRank'], 3)
        usage = json.loads(self.cli('--json', 'usage').stdout)
        self.assertIsNone(usage['cora'])
        self.assertEqual(usage['pixie'][0]['used'], 5)

    def test_share_checks_download_and_preserves_direct_room(self):
        image = self.home / 'result.png'
        image.write_bytes(b'image-bytes')
        result = self.cli('share', str(image), 'description', '--agent', 'gemini')
        data = json.loads(result.stdout)
        self.assertEqual(data['bytes'], len(b'image-bytes'))
        self.assertEqual(data['url'], self.url + '/files/result.png')
        self.assertEqual(self.requests[0][3]['topic'], 'direct-gemini')
        self.assertEqual(self.requests[0][3]['file_path'], str(image))

    def test_log_escapes_thread_and_draft_put_preserves_content(self):
        self.cli('log', 'thread/space', '--since', '2', '--json')
        self.assertEqual(self.requests[-1][1], '/threads/thread%2Fspace/log?since=2')
        draft = self.home / 'draft.md'
        draft.write_text('saved content')
        self.cli('draft', 't1', '--put', str(draft), '--goal', 'goal', '--agent', 'pixie', '--json')
        self.assertEqual(self.requests[-1][0:2], ('PUT', '/threads/t1/draft'))
        self.assertEqual(self.requests[-1][3], {'text': 'saved content', 'by': 'pixie', 'goal': 'goal'})

    def test_run_id_callback_is_forwarded_without_queue_side_effect(self):
        self.cli('reply', '--agent', 'cora', '--run-id', 'run-123', '--text', 'final')
        self.assertEqual(self.requests[-1][3]['runId'], 'run-123')
        self.assertFalse((self.home / 'queue').exists())
        self.requests.clear()
        self.cli('usage', '--run-id', 'invalid', expected=2)
        self.assertEqual(self.requests, [])

    def test_missing_auth_fails_without_creating_queue(self):
        self.env.pop('ATRIUM_TOKEN')
        self.cli('reply', '--agent', 'pixie', '--text', 'hello', expected=2)
        self.assertEqual(self.requests, [])
        self.assertFalse((self.home / 'queue').exists())

    def test_queue_and_web_work_from_same_cli(self):
        job = json.loads(self.cli('send', '--agent', 'codex', '--room', 'test', 'hello', '--json').stdout)
        self.assertEqual(job['state'], 'queued')
        self.cli('react', '--agent', 'pixie', '--topic', 'dev', '--message-id', 'm1', '--emoji', '❤')
        jobs = json.loads(self.cli('jobs', '--json').stdout)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['id'], job['id'])


if __name__ == '__main__':
    unittest.main()
