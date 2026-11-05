import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
