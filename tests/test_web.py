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

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                cls.requests.append((self.command, self.path, self.headers.get('Authorization'),
                                     json.loads(data) if data else None))
                response = {'ok': True}
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
        for command in ('send', 'worker', 'reply', 'share', 'usage'):
            self.assertIn(command, result.stdout)
        self.assertFalse((self.home / 'queue').exists())
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
