import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from channel_cli import cweb
from channel_cli.store import Store


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / '.local/state/channel')
        self.patch = patch('channel_cli.cweb.Path.home', return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, message):
        return self.store.enqueue('test', message, 'codex-body', message, message)

    @patch('channel_cli.cweb.subprocess.run')
    def test_pause_is_atomic_and_supervisor_restarts_after_running_finishes(self, run):
        run.return_value.returncode = 0
        run.return_value.stderr = ''
        first = self.enqueue('first')
        self.store.claim('codex-body')
        second = self.enqueue('second')
        request = cweb.command(self.store, 'restart')
        self.assertIsNone(self.store.claim('codex-body'))
        self.store.finish(self.store.get(first['id']), 'done', answer='ok')
        cweb.supervise(self.store.home, request['token'], timeout=2)
        self.assertEqual(cweb.status(self.store)['state'], 'done')
        self.assertEqual(self.store.claim('codex-body')['id'], second['id'])
        self.assertEqual(run.call_count, 4)

    @patch('channel_cli.cweb.subprocess.run')
    def test_cancel_unblocks_waiting_queue_without_restart(self, run):
        run.return_value.returncode = 0
        run.return_value.stderr = ''
        job = self.enqueue('wait')
        request = cweb.command(self.store, 'restart')
        self.assertEqual(cweb.command(self.store, 'cancel')['state'], 'cancelled')
        cweb.supervise(self.store.home, request['token'], timeout=1)
        self.assertEqual(self.store.claim('codex-body')['id'], job['id'])
        self.assertEqual(run.call_count, 1)

    @patch('channel_cli.cweb.subprocess.run', side_effect=OSError('no tmux'))
    def test_launch_failure_unblocks_queue(self, run):
        job = self.enqueue('wait')
        with self.assertRaises(ValueError):
            cweb.command(self.store, 'restart')
        self.assertEqual(cweb.status(self.store)['state'], 'failed')
        self.assertEqual(self.store.claim('codex-body')['id'], job['id'])

    def test_other_home_cannot_restart_host(self):
        other = Store(self.home / 'other')
        try:
            with self.assertRaises(ValueError):
                cweb.command(other, 'restart')
        finally:
            other.close()

    @patch('channel_cli.cweb.subprocess.run')
    def test_timeout_records_failure_and_resumes_claims(self, run):
        self.enqueue('running')
        self.store.claim('codex-body')
        pending = self.enqueue('pending')
        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=0)
        self.assertEqual(cweb.status(self.store)['state'], 'failed')
        self.assertIn('timed out', cweb.status(self.store)['error'])
        self.assertEqual(self.store.claim('codex-body')['id'], pending['id'])
        self.assertEqual(run.call_count, 1)

    @patch('channel_cli.cweb.subprocess.run')
    def test_kill_session_missing_session_is_tolerated_and_succeeds(self, run):
        import subprocess as sp
        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ['tmux', 'kill-session']:
                return sp.CompletedProcess(cmd, 1, stdout='', stderr="can't find session: cweb\n")
            return sp.CompletedProcess(cmd, 0, stdout='', stderr='')
        run.side_effect = fake_run
        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=1)
        self.assertEqual(cweb.status(self.store)['state'], 'done')

    @patch('channel_cli.cweb.subprocess.run')
    def test_kill_session_unexpected_failure_records_failed_state(self, run):
        import subprocess as sp
        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ['tmux', 'kill-session']:
                return sp.CompletedProcess(cmd, 1, stdout='', stderr="unexpected tmux server communication failure\n")
            return sp.CompletedProcess(cmd, 0, stdout='', stderr='')
        run.side_effect = fake_run
        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=1)
        self.assertEqual(cweb.status(self.store)['state'], 'failed')
        self.assertIn('failed to kill existing cweb', cweb.status(self.store)['error'])

    @patch('channel_cli.cweb.urllib.request.urlopen')
    @patch('channel_cli.cweb.subprocess.run')
    def test_supervise_sends_system_notification_on_completion(self, run, urlopen_mock):
        run.return_value.returncode = 0
        run.return_value.stderr = ''
        token_dir = self.home / '.claude/channels/corpus-web'
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / 'notify-token').write_text('test-token-123\n')

        fake_resp = MagicMock()
        fake_resp.status = 200
        urlopen_mock.return_value.__enter__.return_value = fake_resp

        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=1)

        self.assertEqual(cweb.status(self.store)['state'], 'done')
        self.assertTrue(urlopen_mock.called)
        req = urlopen_mock.call_args[0][0]
        self.assertEqual(req.get_full_url(), 'http://127.0.0.1:8790/notify')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-token-123')
        payload = json.loads(req.data.decode('utf-8'))
        self.assertEqual(payload['topic'], 'dev')
        self.assertEqual(payload['from'], 'system')
        self.assertEqual(payload['key'], f"cweb-restart:{request['token'][:12]}")
        self.assertTrue(payload['text'].startswith('✅ cweb 안전 재시작이 완료되었습니다. ('))
        self.assertTrue(payload['text'].endswith('초 소요)'))

    @patch.dict(os.environ, {'ATRIUM_TOKEN': 'env-token-xyz'})
    @patch('channel_cli.cweb.urllib.request.urlopen')
    @patch('channel_cli.cweb.subprocess.run')
    def test_supervise_uses_env_token(self, run, urlopen_mock):
        run.return_value.returncode = 0
        run.return_value.stderr = ''

        fake_resp = MagicMock()
        fake_resp.status = 200
        urlopen_mock.return_value.__enter__.return_value = fake_resp

        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=1)

        self.assertEqual(cweb.status(self.store)['state'], 'done')
        self.assertTrue(urlopen_mock.called)
        req = urlopen_mock.call_args[0][0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer env-token-xyz')

    @patch('channel_cli.cweb.urllib.request.urlopen')
    @patch('channel_cli.cweb.subprocess.run')
    def test_notification_failure_does_not_fail_restart(self, run, urlopen_mock):
        run.return_value.returncode = 0
        run.return_value.stderr = ''
        token_dir = self.home / '.claude/channels/corpus-web'
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / 'notify-token').write_text('test-token-123\n')

        urlopen_mock.side_effect = urllib.error.URLError('server unreachable')

        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=1, notify_timeout=0.1)

        status_info = cweb.status(self.store)
        self.assertEqual(status_info['state'], 'done')
        self.assertIsNone(status_info['error'])

    @patch('channel_cli.cweb.urllib.request.urlopen')
    @patch('channel_cli.cweb.subprocess.run')
    def test_notification_retries_and_succeeds(self, run, urlopen_mock):
        run.return_value.returncode = 0
        run.return_value.stderr = ''
        token_dir = self.home / '.claude/channels/corpus-web'
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / 'notify-token').write_text('test-token-123\n')

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__.return_value = fake_resp

        urlopen_mock.side_effect = [
            urllib.error.URLError('starting up...'),
            fake_resp,
        ]

        request = cweb.command(self.store, 'restart')
        cweb.supervise(self.store.home, request['token'], timeout=1, notify_timeout=2)

        self.assertEqual(cweb.status(self.store)['state'], 'done')
        self.assertEqual(urlopen_mock.call_count, 2)


if __name__ == '__main__':
    unittest.main()
