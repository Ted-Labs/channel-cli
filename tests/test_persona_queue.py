import tempfile
import threading
import unittest
import argparse
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from channel_cli.store import Store
from channel_cli import agy_body, adapters, cli

class PersonaQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def enqueue(self, persona, agent='agy-body', message='hello'):
        return self.store.enqueue('atrium-web:' + persona, 'dev', agent, message, message, persona=persona)
    def test_three_personas_claim_same_body_concurrently(self):
        for persona in ('cora', 'pixie', 'gemini'): self.enqueue(persona)
        barrier = threading.Barrier(3)
        result = []
        def claim(persona):
            store = Store(self.tmp.name)
            try:
                barrier.wait()
                result.append(store.claim('agy-body', persona))
            finally: store.close()
        threads = [threading.Thread(target=claim, args=(p,)) for p in ('cora', 'pixie', 'gemini')]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual({job['persona'] for job in result}, {'cora', 'pixie', 'gemini'})
        self.assertTrue(all(job['state'] == 'running' for job in result))
    def test_three_workers_run_until_all_released(self):
        for persona in ('cora', 'pixie', 'gemini'): self.enqueue(persona)
        started = threading.Barrier(4, timeout=5)
        release = threading.Event()
        errors = []
        def fake_run(*args, **kwargs):
            started.wait()
            if not release.wait(5): raise ValueError('test timed out')
            return adapters.Reply('ok', 'session')
        def work(persona):
            store = Store(self.tmp.name)
            try:
                args = argparse.Namespace(command='worker', agent=None, persona=persona, once=True, drain=False,
                                          json=True, poll=0.01, timeout=5)
                cli.worker(store, args)
            except Exception as exc: errors.append(exc)
            finally: store.close()
        with patch.object(adapters, 'run', side_effect=fake_run), redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=work, args=(p,)) for p in ('cora', 'pixie', 'gemini')]
            for thread in threads: thread.start()
            try:
                started.wait()
                self.assertEqual(len(self.store.jobs(20, 'running')), 3)
            finally:
                release.set()
                for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.jobs(20, 'done')), 3)

    def test_persona_serialized_across_bodies_and_steer_is_scoped(self):
        first = self.enqueue('pixie')
        self.enqueue('pixie', 'codex-body', 'next')
        self.enqueue('cora')
        running = self.store.claim(persona='pixie')
        self.assertEqual(first['id'], running['id'])
        self.assertIsNone(self.store.claim(persona='pixie'))
        self.assertIsNone(self.store.running('agy-body', 'cora'))
        self.assertIsNotNone(self.store.claim(persona='cora'))
        self.store.finish(running, 'done', 'ok')
        self.assertEqual(self.store.claim(persona='pixie')['agent'], 'codex-body')
    def test_legacy_body_jobs_are_serialized_and_recovery_is_scoped(self):
        self.store.enqueue('cli', 'one', 'agy-body', 'a', 'a')
        self.store.enqueue('cli', 'two', 'agy-body', 'b', 'b')
        first = self.store.claim('agy-body')
        self.enqueue('gemini')
        other = self.store.claim(persona='gemini')
        self.store.recover('agy-body')
        self.assertEqual(self.store.get(first['id'])['state'], 'queued')
        self.assertEqual(self.store.get(other['id'])['state'], 'running')
        self.assertIsNotNone(self.store.claim('agy-body'))
    def test_invalid_channel_and_unsafe_fallback_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.enqueue('atrium-web:cora', 'dev', 'agy-body', 'a', 'a', persona='pixie')
        with patch.object(agy_body, 'is_agy_session', return_value=False):
            with self.assertRaises(ValueError): agy_body.resolve_session(None, 'atrium-web:pixie')
