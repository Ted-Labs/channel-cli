import os
import tempfile
import time
import unittest
from unittest.mock import patch
from channel_cli.store import Store
from channel_cli import followup

class QueueFollowupTests(unittest.TestCase):
    def test_three_engines_common_limits_identity_expiry_and_restart(self):
        with tempfile.TemporaryDirectory() as home:
            s = Store(home)
            try:
                for persona, engine in zip(followup.PERSONAS, followup.ENGINES):
                    job = s.enqueue(f'atrium-web:{persona}', 'dev', engine, 'one', 'one', cwd=home, persona=persona, message_id=persona)
                    job = s.claim(engine, persona)
                    s.bind_executor(job, {'persona': persona, 'engine': engine, 'pane': {'pid': 10, 'start': '20'}})
                    with patch('channel_cli.followup.authorized', return_value=True):
                        with self.assertRaisesRegex(ValueError, '완료된'): s.reserve_followup(job['id'], persona, 10, 'running')
                        s.finish(job, 'done', answer='2')
                        with self.assertRaisesRegex(ValueError, '이 본체'): s.reserve_followup(job['id'], 'other', 10, 'other')
                        for i in range(3):
                            result = s.reserve_followup(job['id'], persona, 10, f'{persona}-{i}')
                            self.assertEqual(result, {'jobId': job['id'], 'topic': 'dev', 'replyTo': persona, 'index': i + 1})
                        self.assertEqual(s.reserve_followup(job['id'], persona, 10, f'{persona}-0')['index'], 1)
                        with self.assertRaisesRegex(ValueError, '최대 3개'): s.reserve_followup(job['id'], persona, 10, f'{persona}-4')
                    with patch('channel_cli.followup.authorized', return_value=False):
                        with self.assertRaisesRegex(ValueError, '현재 본체'): s.reserve_followup(job['id'], persona, 99, 'stale')
                    s.close(); s = Store(home)
                    with patch('channel_cli.followup.authorized', return_value=True):
                        with self.assertRaisesRegex(ValueError, '최대 3개'): s.reserve_followup(job['id'], persona, 10, f'{persona}-restart')
                        with patch('channel_cli.store.time.time', return_value=time.time() + 86401):
                            with self.assertRaisesRegex(ValueError, '24시간'): s.reserve_followup(job['id'], persona, 10, 'expired')
            finally: s.close()

    def test_concurrent_reservations_never_exceed_three(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as home:
            s = Store(home)
            j = s.enqueue('atrium-web:cora', 'dev', 'claude-body', 'one', 'one', cwd=home, persona='cora', message_id='original')
            j = s.claim('claude-body', 'cora');s.bind_executor(j, {'persona':'cora','engine':'claude-body','pane':{'pid':10,'start':'20'}});s.finish(j,'done',answer='2');s.close()
            def reserve(i):
                local = Store(home)
                try:
                    try:return local.reserve_followup(j['id'],'cora',10,str(i))['index']
                    except ValueError:return None
                finally:local.close()
            with patch('channel_cli.followup.authorized',return_value=True), ThreadPoolExecutor(max_workers=6) as pool:
                indices = list(pool.map(reserve,range(6)))
            self.assertEqual(sorted(i for i in indices if i),[1,2,3])

    def test_native_steer_ack_can_precede_completed_target_recording(self):
        with tempfile.TemporaryDirectory() as home:
            s = Store(home)
            try:
                first = s.enqueue('test', 'room', 'codex-body', 'first', 'first', cwd=home)
                first = s.claim('codex-body')
                extra = s.enqueue('test', 'room', 'codex-body', 'extra', 'extra', cwd=home)
                s.finish(first, 'done', answer='native accepted extra')
                with self.assertRaises(ValueError): s.steered(extra['id'], first['id'], defer_prompt='extra')
                with self.assertRaises(ValueError): s.steered(extra['id'], first['id'])
                self.assertEqual(s.steered(extra['id'], first['id'], native_accepted=True)['state'], 'steered')
            finally: s.close()

    def test_process_and_native_thread_checks_reject_clones_replacements_and_other_threads(self):
        ident = {'persona': 'pixie', 'engine': 'claude-body', 'pane': {'pid': 10, 'start': '20'}}
        def proc(pid):
            return {'pid': pid, 'parent': {30:20,20:10,40:10}.get(pid,1), 'start':'20', 'args': ['claude'] if pid == 40 else ['python3', '-m', 'channel_cli', 'followup', '--agent', 'gemini']}
        with patch.object(followup,'pane',return_value={'pid':10,'start':'20','args':['claude']}), patch.object(followup,'process',side_effect=proc):
            self.assertTrue(followup.authorized(ident,30))
            self.assertFalse(followup.authorized(ident,40))
        with patch.object(followup,'pane',return_value={'pid':11,'start':'21','args':['claude']}):
            self.assertFalse(followup.authorized(ident,30))
        agy = {**ident, 'engine': 'agy-body', 'conversation': 'main-conversation'}
        with patch.object(followup,'pane',return_value={'pid':10,'start':'20','args':['agy']}), patch.object(followup,'process',side_effect=proc), patch('pathlib.Path.read_bytes',return_value=b'ANTIGRAVITY_CONVERSATION_ID=main-conversation\0'):
            self.assertTrue(followup.authorized(agy,30))
            with patch('pathlib.Path.read_bytes',return_value=b'ANTIGRAVITY_CONVERSATION_ID=child-conversation\0'):
                self.assertFalse(followup.authorized(agy,30))
        native = {**ident,'engine':'codex-body','session':'main-thread','daemon':{'pid':10,'start':'20'}}
        with patch.object(followup,'pane',return_value={'pid':10,'start':'20','args':['codex']}), patch.object(followup,'daemon',return_value={'pid':10,'start':'20'}), patch.object(followup,'process',side_effect=proc), patch('pathlib.Path.read_bytes',return_value=b'CODEX_THREAD_ID=main-thread\0'):
            self.assertTrue(followup.authorized(native,30))
            with patch('pathlib.Path.read_bytes',return_value=b'CODEX_THREAD_ID=clone-thread\0'):
                self.assertFalse(followup.authorized(native,30))
