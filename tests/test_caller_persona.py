import unittest
from unittest import mock

from channel_cli import followup


def tree(rows):
    return lambda pid: {'pid': pid, 'parent': rows[pid][0], 'start': '1', 'args': rows[pid][1]}


class CallerPersonaTest(unittest.TestCase):
    rows = {
        10: (1, ['claude']),              # cora pane
        11: (10, ['bash']), 12: (11, ['python3', 'clone.py']),
        20: (1, ['tmux']), 21: (20, ['claude', '--fork-session=x']), 22: (21, ['bash']),
        30: (1, ['bash']), 31: (30, ['python3']),
    }

    def run_with(self, pid, panes):
        def pane(persona):
            if persona not in panes:
                raise ValueError('no session')
            return tree(self.rows)(panes[persona])
        with mock.patch.object(followup, 'pane', pane), mock.patch.object(followup, 'process', tree(self.rows)), \
                mock.patch.object(followup, 'daemon', side_effect=ValueError('no daemon')):
            return followup.caller_persona(pid)

    def test_descendant_of_body_pane(self):
        self.assertEqual(self.run_with(12, {'cora': 10}), 'cora')

    def test_forked_session_is_rejected(self):
        self.assertIsNone(self.run_with(22, {'cora': 10}))

    def test_unrelated_process_is_rejected(self):
        self.assertIsNone(self.run_with(31, {'cora': 10, 'gemini': 20}))


if __name__ == '__main__':
    unittest.main()
