import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from channel_cli.store import Store, SCHEMA

class CanvasRevisionTests(unittest.TestCase):
    def test_old_database_migrates_and_replay_keeps_original_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / 'queue.sqlite3')
            db.executescript(SCHEMA.replace(', canvas_revision INTEGER', ''))
            db.close()
            store = Store(tmp)
            try:
                first = store.enqueue('atrium-web:pixie', 'dev', 'codex-body', 'old', 'old', cwd=tmp, message_id='one', persona='pixie', canvas_revision=2)
                second = store.enqueue('atrium-web:gemini', 'dev', 'agy-body', 'new', 'new', cwd=tmp, message_id='two', persona='gemini', canvas_revision=4)
                duplicate = store.enqueue('atrium-web:pixie', 'dev', 'codex-body', 'old', 'old', cwd=tmp, message_id='one', persona='pixie', canvas_revision=4)
                self.assertEqual((duplicate['id'], duplicate['canvas_revision']), (first['id'], 2))
                claimed = store.claim('codex-body', 'pixie')
                self.assertEqual((claimed['id'], claimed['agent'], claimed['canvas_revision']), (first['id'], 'codex-body', 2))
                self.assertEqual((store.get(second['id'])['state'], store.get(second['id'])['canvas_revision']), ('queued', 4))
                legacy = store.enqueue('legacy', 'dev', 'codex', 'legacy', 'legacy', cwd=tmp)
                self.assertIsNone(legacy['canvas_revision'])
                for value in (0, -1, True, 1.5, 2**53):
                    with self.assertRaises(ValueError):
                        store.enqueue('legacy', 'dev', 'codex', 'bad', 'bad', cwd=tmp, canvas_revision=value)
            finally:
                store.close()
            reopened = Store(tmp)
            try:
                self.assertEqual(reopened.get(first['id'])['canvas_revision'], 2)
            finally:
                reopened.close()
