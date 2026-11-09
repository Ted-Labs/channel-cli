import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from channel_cli import accounts

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'HOME': str(self.home)}, clear=False)
        self.env.start()
    def tearDown(self):
        self.env.stop(); self.temp.cleanup()
    def test_default_personal_is_read_only_and_uses_legacy_metadata(self):
        self.assertEqual(accounts.describe()['metadata_file'], str(self.home / '.claude.json'))
        self.assertFalse(accounts.selection_file().exists())
        env = accounts.launch_env({'CLAUDE_CONFIG_DIR': 'stale-team'})
        self.assertNotIn('CLAUDE_CONFIG_DIR', env)
        self.assertEqual(env['ATRIUM_CLAUDE_ACCOUNT'], 'personal')
    def test_team_shares_only_policy_and_preserves_auth(self):
        personal = self.home / '.claude'; personal.mkdir()
        (personal / '.credentials.json').write_text('personal-sentinel')
        (self.home / '.claude.json').write_text('personal-metadata')
        info = accounts.choose('team')
        team = self.home / '.claude-team'
        self.assertEqual(accounts.selected(), 'team')
        for name in accounts.SHARED:
            self.assertTrue((team / name).is_symlink())
            self.assertEqual((team / name).readlink(), personal / name)
        self.assertFalse((team / '.credentials.json').exists())
        self.assertFalse((team / '.claude.json').exists())
        self.assertFalse((team / 'projects').exists())
        self.assertEqual((personal / '.credentials.json').read_text(), 'personal-sentinel')
        self.assertEqual(info['metadata_file'], str(team / '.claude.json'))
        self.assertEqual(accounts.selection_file().stat().st_mode & 0o777, 0o600)
        self.assertEqual(accounts.launch_env({})['CLAUDE_CONFIG_DIR'], str(team))
        accounts.choose('team') # idempotent
        accounts.choose('personal')
        self.assertEqual(accounts.selected(), 'personal')
    def test_existing_team_policy_is_not_overwritten_or_selected(self):
        team = self.home / '.claude-team'; team.mkdir()
        (team / 'settings.json').write_text('keep')
        with self.assertRaises(ValueError): accounts.choose('team')
        self.assertEqual((team / 'settings.json').read_text(), 'keep')
        self.assertFalse(accounts.selection_file().exists())
        self.assertFalse((team / 'hooks').is_symlink())
    def test_credentials_cannot_be_shared_through_links(self):
        team = self.home / '.claude-team'; team.mkdir()
        (team / '.credentials.json').symlink_to(self.home / '.claude/.credentials.json')
        with self.assertRaises(ValueError): accounts.choose('team')
    def test_invalid_selection_fails_closed(self):
        file = accounts.selection_file(); file.parent.mkdir(parents=True)
        file.write_text('{"account":"other"}')
        with self.assertRaises(ValueError): accounts.describe()
    def test_cli_changes_one_file_without_creating_queue_or_running_login(self):
        result = subprocess.run([sys.executable, '-m', 'channel_cli', 'account', 'team', '--json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['account'], 'team')
        self.assertFalse((self.home / '.local/state/channel').exists())
        self.assertFalse((self.home / '.claude-team/.credentials.json').exists())
        shown = subprocess.run([sys.executable, '-m', 'channel_cli', '--json', 'account'], capture_output=True, text=True)
        self.assertEqual(json.loads(shown.stdout)['account'], 'team')
    def test_running_environment_is_not_rewritten_when_selection_changes(self):
        old_env = accounts.launch_env({})
        accounts.choose('team')
        self.assertNotIn('CLAUDE_CONFIG_DIR', old_env)
        self.assertEqual(accounts.launch_env({})['ATRIUM_CLAUDE_ACCOUNT'], 'team')
        self.assertEqual(accounts.launch_env(old_env)['ATRIUM_CLAUDE_ACCOUNT'], 'personal')

if __name__ == '__main__': unittest.main()
