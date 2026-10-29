import os
import unittest
from unittest.mock import patch, MagicMock

from channel_cli import agy_body, adapters


class AgyBodyTests(unittest.TestCase):
    def test_adapter_discovery_includes_agy_body(self):
        with patch.object(agy_body, "is_online", return_value=True):
            discovered = adapters.discover()
            item = next((a for a in discovered if a["id"] == "agy-body"), None)
            self.assertIsNotNone(item)
            self.assertTrue(item["installed"])
            self.assertIn("gemini-body", item["aliases"])

    def test_run_offline_raises(self):
        with patch.object(agy_body, "is_online", return_value=False):
            with self.assertRaisesRegex(ValueError, "not online"):
                agy_body.run("hello", session="test-session")

    def test_interrupt_sends_escape(self):
        with patch("subprocess.run") as mock_run:
            agy_body.interrupt("test-session")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[-3:], ["-t", "test-session", "Escape"])
