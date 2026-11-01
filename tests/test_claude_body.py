import json
import os
import tempfile
import unittest
from unittest.mock import patch, Mock
from pathlib import Path

from channel_cli import adapters, claude_body, agy_body
from channel_cli.cli import parser


class Response:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, *args): return json.dumps(self.value).encode()


class ClaudeBodyTests(unittest.TestCase):
    def test_existing_body_is_registered_without_new_claude_process(self):
        self.assertIn('claude-body', adapters.BODIES)
        self.assertEqual(adapters.ALIASES['cora-body'], 'claude-body')
        with patch.object(claude_body, 'run', return_value=adapters.Reply('answer', 'cora')) as run:
            result = adapters.run('claude-body', 'request', '.', None, 3, Path('/unused'))
        self.assertEqual(result.text, 'answer')
        run.assert_called_once_with('request', 3)

    def test_ack_and_final_result_return_to_worker_with_metadata_and_cleanup(self):
        requests = []
        states = iter([{'state': 'pending'}, {'state': 'accepted'}, {'state': 'done', 'answer': 'final answer'}])
        def urlopen(req, **kwargs):
            requests.append(req)
            return Response(next(states) if req.method == 'GET' else {})
        with patch.dict(os.environ, {'ATRIUM_TOKEN': 'test', 'CORPUS_WEB_URL': 'http://127.0.0.1:9999'}), patch('urllib.request.urlopen', side_effect=urlopen), patch('time.sleep'):
            result = claude_body.run('[Atrium metadata] {"message_id":"original","chat_id":"dev"}\nrequest', 3)
        self.assertEqual(result.text, 'final answer')
        data = json.loads(requests[0].data)
        self.assertEqual(data['meta']['message_id'], 'original')
        self.assertEqual(data['prompt'], 'request')
        self.assertEqual(requests[-1].method, 'DELETE')
        self.assertTrue(all(req.get_header('Authorization') == 'Bearer test' for req in requests))

    def test_unacknowledged_delivery_fails_instead_of_succeeding(self):
        ticks = iter([0, 0, 0, 1, 2, 11])
        calls = []
        def urlopen(req, **kwargs):
            calls.append(req.method)
            return Response({'state': 'pending'})
        with patch.dict(os.environ, {'ATRIUM_TOKEN': 'test'}), patch('urllib.request.urlopen', side_effect=urlopen), patch('time.sleep'), patch('time.monotonic', side_effect=lambda: next(ticks, 11)):
            with self.assertRaisesRegex(ValueError, 'acknowledge'):
                claude_body.run('request', 100)
        self.assertEqual(calls[-1], 'DELETE')

    def test_failure_or_empty_answer_never_becomes_success(self):
        for state in [{'state': 'failed', 'error': 'offline'}, {'state': 'done', 'answer': ''}]:
            with self.subTest(state=state), patch.dict(os.environ, {'ATRIUM_TOKEN': 'test'}), patch('urllib.request.urlopen', return_value=Response(state)):
                with self.assertRaises(ValueError):
                    claude_body.run('request', 1)

    def test_reply_parser_accepts_result_correlation(self):
        args = parser().parse_args(['reply', '--agent', 'cora', '--run-id', 'run', '--text', 'answer'])
        self.assertEqual(args.run_id, 'run')


class GeminiCorrelationTests(unittest.TestCase):
    def test_request_marker_selects_own_transcript_not_latest_foreign_session(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'GEMINI_HOME': home}):
            folder = Path(home) / '.gemini/antigravity-cli/brain'
            own = folder / 'own/.system_generated/logs/transcript.jsonl'
            foreign = folder / 'foreign/.system_generated/logs/transcript.jsonl'
            for file in (own, foreign): file.parent.mkdir(parents=True)
            own.write_text(json.dumps({'type': 'USER_INPUT', 'content': '[Atrium request abc]\nrequest'})+'\n'+json.dumps({'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': 'own answer'})+'\n')
            foreign.write_text(json.dumps({'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': 'foreign answer'})+'\n')
            path, line = agy_body.request_transcript('[Atrium request abc]')
            self.assertEqual(path, own)
            self.assertEqual(agy_body.extract_latest_answer(path, line), 'own answer')
            with own.open('a') as output:
                output.write(json.dumps({'type': 'USER_INPUT', 'content': 'another request'})+'\n')
            with self.assertRaisesRegex(ValueError, 'another input'):
                agy_body.request_transcript('[Atrium request abc]')


class GeminiTranscriptReplayTests(unittest.TestCase):
    def test_sanitized_recent_transcript_replay_has_no_completed_turn_false_positive(self):
        fixture = Path(__file__).parent / 'fixtures' / 'agy-transcript-structure-replay.jsonl'
        turns = [json.loads(line) for line in fixture.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(turns), 65)

        completed = pending_with_answer = unfinished_without_answer = legacy_false_positive = 0
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'transcript.jsonl'
            for turn in turns:
                transcript.write_text(
                    ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in turn['rows'][1:]),
                    encoding='utf-8',
                )
                actual = agy_body.transcript_has_unfinished_work(transcript)
                self.assertEqual(actual, turn['expected_unfinished'], turn['turn'])
                if turn['terminal_answer']:
                    if turn['expected_unfinished']:
                        pending_with_answer += 1
                    else:
                        completed += 1
                    legacy_false_positive += bool(turn['legacy_unfinished'])
                elif turn['expected_unfinished']:
                    unfinished_without_answer += 1

        self.assertEqual(legacy_false_positive, 13)
        self.assertEqual(completed, 59)
        self.assertEqual(pending_with_answer, 1)
        self.assertEqual(unfinished_without_answer, 5)


class GeminiFastReplyTests(unittest.TestCase):
    def test_fast_reply_completes_even_when_busy_indicator_was_never_seen(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'GEMINI_HOME': home}):
            transcript = Path(home) / '.gemini/antigravity-cli/brain/own/.system_generated/logs/transcript.jsonl'
            transcript.parent.mkdir(parents=True)
            buffer = {}
            def tmux(args, **kwargs):
                if 'load-buffer' in args:
                    buffer['prompt'] = kwargs['input'].decode()
                if 'send-keys' in args and args[-1] == 'Enter':
                    transcript.write_text(json.dumps({'type': 'USER_INPUT', 'content': buffer['prompt']})+'\n'+json.dumps({'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': 'fast answer'})+'\n')
                return Mock(returncode=0)
            with patch.object(agy_body, 'is_online', return_value=True), patch.object(agy_body, 'is_busy', return_value=False), patch('subprocess.run', side_effect=tmux), patch('time.sleep'):
                result = agy_body.run('request', timeout=1, session='gemini')
            self.assertEqual(result.text, 'fast answer')
            self.assertIn('[Atrium request ', buffer['prompt'])

    def test_b47dcb87_intermediate_response_waits_for_background_command_then_returns_final(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'GEMINI_HOME': home}):
            transcript = Path(home) / '.gemini/antigravity-cli/brain/own/.system_generated/logs/transcript.jsonl'
            transcript.parent.mkdir(parents=True)
            buffer = {}
            sleep_calls = {'count': 0}

            def write_running_transcript():
                rows = [
                    {'type': 'USER_INPUT', 'content': buffer['prompt']},
                    {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': '테스트 실행 결과를 기다립니다.',
                     'tool_calls': [{'name': 'run_command', 'args': {}}]},
                    {'source': 'MODEL', 'type': 'GENERIC', 'status': 'RUNNING', 'step_index': 2,
                     'content': 'Tool is running as a background task with task id task-2'},
                ]
                transcript.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

            def write_completed_transcript():
                rows = [
                    {'type': 'USER_INPUT', 'content': buffer['prompt']},
                    {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': '테스트 실행 결과를 기다립니다.',
                     'tool_calls': [{'name': 'run_command', 'args': {}}]},
                    {'source': 'MODEL', 'type': 'GENERIC', 'status': 'RUNNING', 'step_index': 2,
                     'content': 'Tool is running as a background task with task id task-2'},
                    {'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE', 'step_index': 3,
                     'content': 'Task task-2 completed.'},
                    {'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
                     'content': '테스트가 통과했고 수정 사항을 정리했습니다.'},
                ]
                transcript.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

            def tmux(args, **kwargs):
                if 'load-buffer' in args:
                    buffer['prompt'] = kwargs['input'].decode()
                if 'send-keys' in args and args[-1] == 'Enter':
                    write_running_transcript()
                return Mock(returncode=0)

            def sleep(_seconds):
                sleep_calls['count'] += 1
                # The initial settle and 20 start checks happen before
                # monitoring. The first idle check must not return progress.
                if sleep_calls['count'] == 22:
                    write_completed_transcript()

            parts = []
            with patch.object(agy_body, 'is_online', return_value=True), \
                 patch.object(agy_body, 'is_busy', return_value=False), \
                 patch('subprocess.run', side_effect=tmux), \
                 patch.object(agy_body.time, 'sleep', side_effect=sleep):
                result = agy_body.run('request', timeout=5, session='gemini', on_part=parts.append)

            self.assertEqual(result.text, '테스트가 통과했고 수정 사항을 정리했습니다.')
            self.assertEqual(parts, ['테스트 실행 결과를 기다립니다.'])

    def test_unfinished_background_command_times_out_instead_of_returning_progress(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'GEMINI_HOME': home}):
            transcript = Path(home) / '.gemini/antigravity-cli/brain/own/.system_generated/logs/transcript.jsonl'
            transcript.parent.mkdir(parents=True)
            buffer = {}

            def tmux(args, **kwargs):
                if 'load-buffer' in args:
                    buffer['prompt'] = kwargs['input'].decode()
                if 'send-keys' in args and args[-1] == 'Enter':
                    rows = [
                        {'type': 'USER_INPUT', 'content': buffer['prompt']},
                        {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'content': 'intermediate progress',
                         'tool_calls': [{'name': 'run_command', 'args': {}}]},
                        {'source': 'MODEL', 'type': 'GENERIC', 'status': 'RUNNING', 'step_index': 2,
                         'content': 'Tool is running as a background task with task id task-7'},
                    ]
                    transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                return Mock(returncode=0)

            ticks = iter([0.0, 0.5, 1.0, 1.5, 2.5])
            with patch.object(agy_body, 'is_online', return_value=True), \
                 patch.object(agy_body, 'is_busy', return_value=False), \
                 patch('subprocess.run', side_effect=tmux), \
                 patch.object(agy_body.time, 'sleep'), \
                 patch.object(agy_body.time, 'monotonic', side_effect=lambda: next(ticks, 2.5)):
                with self.assertRaisesRegex(TimeoutError, 'did not finish'):
                    agy_body.run('request', timeout=2, session='gemini')
