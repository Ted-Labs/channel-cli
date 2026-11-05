import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from channel_cli import agy_body


class GeminiStreamingEdgeTests(unittest.TestCase):
    def test_missing_background_completion_event_returns_final_answer_without_timeout(self):
        """When <SYSTEM_MESSAGE> task completion is omitted from transcript,
        run() must return the final PLANNER_RESPONSE without timing out,
        while streaming the intermediate progress via on_part.
        """
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"GEMINI_HOME": home}):
            transcript = Path(home) / ".gemini/antigravity-cli/brain/own/.system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            buffer = {}
            sleep_calls = {"count": 0}

            def write_running_transcript():
                rows = [
                    {"type": "USER_INPUT", "content": buffer["prompt"]},
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": "백그라운드 명령을 실행합니다.",
                        "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 1"}}],
                    },
                    {
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "RUNNING",
                        "step_index": 2,
                        "content": "Tool is running as a background task with task id task-55",
                    },
                ]
                transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            def write_completed_transcript():
                rows = [
                    {"type": "USER_INPUT", "content": buffer["prompt"]},
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": "백그라운드 명령을 실행합니다.",
                        "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 1"}}],
                    },
                    {
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "RUNNING",
                        "step_index": 2,
                        "content": "Tool is running as a background task with task id task-55",
                    },
                    # Note: SYSTEM_MESSAGE with task-55 completion is omitted/dropped!
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": "백그라운드 작업이 완료되었고 최종 결과를 정리했습니다.",
                    },
                ]
                transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            def tmux(args, **kwargs):
                if "load-buffer" in args:
                    buffer["prompt"] = kwargs["input"].decode()
                if "send-keys" in args and args[-1] == "Enter":
                    write_running_transcript()
                return Mock(returncode=0)

            def sleep(_seconds):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 22:
                    write_completed_transcript()

            parts = []
            with patch.object(agy_body, "is_online", return_value=True), \
                 patch.object(agy_body, "is_busy", return_value=False), \
                 patch("subprocess.run", side_effect=tmux), \
                 patch.object(agy_body.time, "sleep", side_effect=sleep):
                result = agy_body.run("request", timeout=5, session="gemini", on_part=parts.append)

            self.assertEqual(result.text, "백그라운드 작업이 완료되었고 최종 결과를 정리했습니다.")
            self.assertEqual(result.session, "gemini")
            self.assertEqual(parts, ["백그라운드 명령을 실행합니다."])

    def test_missing_completion_event_resolved_by_disk_task_log(self):
        """When transcript lacks completion event but task log on disk shows
        exit code, transcript_has_unfinished_work detects completion.
        """
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"GEMINI_HOME": home}):
            brain_dir = Path(home) / ".gemini/antigravity-cli/brain/own"
            transcript = brain_dir / ".system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            tasks_dir = brain_dir / ".system_generated/tasks"
            tasks_dir.mkdir(parents=True)

            # Task log shows command completed
            (tasks_dir / "task-42.log").write_text("All tests passed.\nCommand exited with code 0.\n", encoding="utf-8")

            rows = [
                {"type": "USER_INPUT", "content": "[Atrium request 1]\nrun task"},
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "running command",
                    "tool_calls": [{"name": "run_command", "args": {}}],
                },
                {
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "step_index": 2,
                    "content": "Tool is running as a background task with task id task-42",
                },
                # No SYSTEM_MESSAGE
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "작업 결과 보고서",
                },
            ]
            transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            # Without check_settled, check_disk_task_completion should clear task-42
            unfinished = agy_body.transcript_has_unfinished_work(transcript, min_line=0, check_settled=False)
            self.assertFalse(unfinished)

    def test_missing_completion_event_resolved_by_step_output(self):
        """When transcript lacks completion event but step output exists on disk,
        transcript_has_unfinished_work detects completion.
        """
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"GEMINI_HOME": home}):
            brain_dir = Path(home) / ".gemini/antigravity-cli/brain/own"
            transcript = brain_dir / ".system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            step_dir = brain_dir / ".system_generated/steps/7"
            step_dir.mkdir(parents=True)

            # Step output file exists
            (step_dir / "output.txt").write_text("Created At: ...\nCommand exited with code 0.\n", encoding="utf-8")

            rows = [
                {"type": "USER_INPUT", "content": "[Atrium request 2]\nrun task"},
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "running tool",
                    "tool_calls": [{"name": "run_command", "args": {}}],
                },
                {
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "step_index": 7,
                    "content": "Tool is running as a background task with task id task-7",
                },
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "완료되었습니다.",
                },
            ]
            transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            unfinished = agy_body.transcript_has_unfinished_work(transcript, min_line=0, check_settled=False)
            self.assertFalse(unfinished)

    def test_missing_completion_event_resolved_by_message_file(self):
        """When transcript lacks completion event but message json file exists on disk,
        transcript_has_unfinished_work detects completion.
        """
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"GEMINI_HOME": home}):
            brain_dir = Path(home) / ".gemini/antigravity-cli/brain/own"
            transcript = brain_dir / ".system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            messages_dir = brain_dir / ".system_generated/messages"
            messages_dir.mkdir(parents=True)

            msg_payload = {
                "sender": "own/task-88",
                "content": 'Task id "own/task-88" finished with result:\nCommand exited with code 0.',
            }
            (messages_dir / "msg-1.json").write_text(json.dumps(msg_payload), encoding="utf-8")

            rows = [
                {"type": "USER_INPUT", "content": "[Atrium request 3]\nrun task"},
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "starting task",
                    "tool_calls": [{"name": "run_command", "args": {}}],
                },
                {
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "step_index": 3,
                    "content": "Tool is running as a background task with task id task-88",
                },
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "작업 마감 결과",
                },
            ]
            transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            unfinished = agy_body.transcript_has_unfinished_work(transcript, min_line=0, check_settled=False)
            self.assertFalse(unfinished)

    def test_actively_running_background_task_still_waits_and_streams_intermediate_progress(self):
        """When task log exists on disk without exit code, the task is actively running.
        transcript_has_unfinished_work must still return True (do not exit prematurely).
        """
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"GEMINI_HOME": home}):
            brain_dir = Path(home) / ".gemini/antigravity-cli/brain/own"
            transcript = brain_dir / ".system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            tasks_dir = brain_dir / ".system_generated/tasks"
            tasks_dir.mkdir(parents=True)

            # Task log is actively running (output stream with no exit code)
            (tasks_dir / "task-10.log").write_text("Running tests in progress...\nRunning test 5/20...\n", encoding="utf-8")

            rows = [
                {"type": "USER_INPUT", "content": "[Atrium request 4]\nrun task"},
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "진행 중...",
                    "tool_calls": [{"name": "run_command", "args": {}}],
                },
                {
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "step_index": 4,
                    "content": "Tool is running as a background task with task id task-10",
                },
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "중간 응답: 백그라운드에서 여전히 실행 중입니다.",
                },
            ]
            transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            # With check_settled=True, actively running task on disk must NOT be cleared
            unfinished = agy_body.transcript_has_unfinished_work(transcript, min_line=0, check_settled=True)
            self.assertTrue(unfinished)

class GeminiUnconfirmedDeliveryTests(unittest.TestCase):
    def test_interrupt_without_marker_idle_does_not_send_escape(self):
        interrupt = Mock()
        calls = iter([False, True])
        with patch.object(agy_body, "is_online", return_value=True), \
             patch.object(agy_body, "is_busy", return_value=False), \
             patch.object(agy_body, "request_transcript", return_value=None), \
             patch.object(agy_body, "interrupt", interrupt), \
             patch.object(agy_body.time, "sleep"), \
             patch("subprocess.run", return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(agy_body.Interrupted, "unconfirmed"):
                agy_body.run("request", session="cora", should_interrupt=lambda: next(calls))
        interrupt.assert_not_called()

    def test_idle_request_without_transcript_fails_before_turn_timeout(self):
        clock = [0]
        def sleep(seconds): clock[0] += seconds
        with patch.object(agy_body, "is_online", return_value=True), \
             patch.object(agy_body, "is_busy", return_value=False), \
             patch.object(agy_body, "request_transcript", return_value=None), \
             patch.object(agy_body.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(agy_body.time, "sleep", side_effect=sleep), \
             patch("subprocess.run", return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(ValueError, "delivery was not confirmed"):
                agy_body.run("request", timeout=1800, session="cora")
        self.assertLess(clock[0], 60)

    def test_interrupt_without_marker_does_not_interrupt_other_busy_session(self):
        clock = [0]
        def sleep(seconds): clock[0] += seconds
        interrupt = Mock()
        busy = iter([False] + [True] * 1000)
        calls = iter([False] + [True] * 1000)
        with patch.object(agy_body, "is_online", return_value=True), \
             patch.object(agy_body, "is_busy", side_effect=lambda *_: next(busy)), \
             patch.object(agy_body, "request_transcript", return_value=None), \
             patch.object(agy_body, "interrupt", interrupt), \
             patch.object(agy_body.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(agy_body.time, "sleep", side_effect=sleep), \
             patch("subprocess.run", return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(ValueError, "cannot confirm request ownership"):
                agy_body.run("request", timeout=1800, session="cora", should_interrupt=lambda: next(calls))
        self.assertLess(clock[0], 25)
        interrupt.assert_not_called()
