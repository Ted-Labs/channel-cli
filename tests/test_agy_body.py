import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channel_cli import agy_body


class AgyBodyTests(unittest.TestCase):
    def test_fresh_clears_the_existing_body_before_recording_session(self):
        calls = []

        def run(args, check=False):
            calls.append((args, check))
            return type('Result', (), {'returncode': 0})()

        sessions = []
        with patch('channel_cli.agy_body.subprocess.run', side_effect=run), \
             patch('channel_cli.agy_body.time.sleep') as sleep:
            agy_body.clear_session('gemini', sessions.append)

        self.assertEqual(calls, [([agy_body.tmux_bin(), 'send-keys', '-t', 'gemini', '/clear', 'Enter'], False)])
        sleep.assert_called_once_with(0.4)
        self.assertEqual(sessions, ['gemini'])

    def test_fresh_clear_failure_does_not_record_session(self):
        sessions = []
        result = type('Result', (), {'returncode': 1})()
        with patch('channel_cli.agy_body.subprocess.run', return_value=result), \
             patch('channel_cli.agy_body.time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'fresh Gemini conversation'):
                agy_body.clear_session('gemini', sessions.append)
        sleep.assert_not_called()
        self.assertEqual(sessions, [])

    def test_native_session_is_persisted_before_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            conversation = '11111111-2222-3333-4444-555555555555'
            transcript = Path(tmp) / 'brain' / conversation / '.system_generated/logs/transcript.jsonl'
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': 'Gemini answer'}) + '\n')
            steps = []

            def tmux(args, check=False, input=None):
                steps.append(('tmux', args))
                return SimpleNamespace(returncode=0)

            with patch.object(agy_body, 'is_online', return_value=True), \
                 patch.object(agy_body, 'is_busy', return_value=False), \
                 patch.object(agy_body, 'request_transcript', return_value=(transcript, 0)), \
                 patch.object(agy_body.subprocess, 'run', side_effect=tmux), \
                 patch.object(agy_body.time, 'sleep'):
                reply = agy_body.run('short task', session='gemini', on_session=lambda session: steps.append(('session', session)), on_turn=lambda thread, turn: steps.append(('turn', thread)))

        self.assertEqual(reply.text, 'Gemini answer')
        self.assertEqual(reply.session, 'gemini')
        self.assertEqual(steps[0], ('session', 'gemini'))
        self.assertEqual(steps[1][1][1], 'load-buffer')
        self.assertEqual([step for step in steps if step[0] == 'turn'], [('turn', 'gemini'), ('turn', conversation)])

    def test_persona_channel_routes_to_its_own_agy_tmux_session(self):
        with patch.object(agy_body, 'is_agy_session', side_effect=lambda s: s in {'cora', 'gemini'}):
            self.assertEqual(agy_body.resolve_session('gemini', 'atrium-web:cora'), 'cora')
            self.assertEqual(agy_body.resolve_session(None, 'atrium-web:cora'), 'cora')
        with patch.object(agy_body, 'is_agy_session', side_effect=lambda s: s == 'gemini'):
            with self.assertRaises(ValueError):
                agy_body.resolve_session('gemini', 'atrium-web:cora')
            with self.assertRaises(ValueError):
                agy_body.resolve_session(None, 'atrium-web:cora')

    def test_extract_latest_answer_recovers_truncated_content_from_full_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / 'transcript.jsonl'
            full_transcript = Path(tmp) / 'transcript_full.jsonl'
            transcript.write_text(json.dumps({
                'source': 'MODEL',
                'type': 'PLANNER_RESPONSE',
                'content': 'prefix <truncated 100 bytes> suffix',
                'truncated_fields': ['content'],
            }) + '\n')
            full_transcript.write_text(json.dumps({
                'source': 'MODEL',
                'type': 'PLANNER_RESPONSE',
                'content': 'prefix complete full body without truncation suffix',
            }) + '\n')
            answer = agy_body.extract_latest_answer(transcript, 0)
            self.assertEqual(answer, 'prefix complete full body without truncation suffix')
