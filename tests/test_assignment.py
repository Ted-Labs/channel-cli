import unittest

from channel_cli.assignment import preference, recommend


class AssignmentTests(unittest.TestCase):
    def test_role_and_difficulty_preference_tables(self):
        self.assertEqual(preference('구현', '쉬움'), ['gemini', 'pixie', 'cora'])
        self.assertEqual(preference('구현', '어려움'), ['pixie', 'cora'])
        self.assertEqual(preference('조사', '보통'), ['gemini', 'cora', 'pixie'])
        with self.assertRaises(ValueError):
            preference('없는 역할', '쉬움')

    def test_skips_busy_and_low_quota_agents(self):
        result = recommend('조사', '보통', [
            {'id': 'gemini', 'state': 'idle', 'queued': 0},
            {'id': 'cora', 'state': 'busy', 'queued': 0},
            {'id': 'pixie', 'state': 'idle', 'queued': 0},
        ], {'session': {'utilization': 10}, 'weekly': {'utilization': 20}},
            {'gemini': [{'used': 86}], 'pixie': [{'used': 84}]})
        self.assertEqual(result['target'], 'pixie')
        self.assertEqual(result['preferenceRank'], 3)
        self.assertTrue(result['available'])

    def test_hard_work_uses_thirty_percent_remaining_cutoff(self):
        runtimes = [{'id': agent, 'state': 'idle', 'queued': 0} for agent in ('pixie', 'cora')]
        result = recommend('구현', '어려움', runtimes,
            {'session': {'utilization': 70}, 'weekly': {'utilization': 10}},
            {'pixie': [{'used': 71}]})
        self.assertEqual(result['target'], 'cora')
        self.assertEqual(result['preferenceRank'], 2)

    def test_all_busy_uses_first_preference_as_queue_fallback(self):
        result = recommend('이미지/UI 시안', '보통', [
            {'id': 'gemini', 'state': 'busy', 'queued': 0},
            {'id': 'pixie', 'state': 'idle', 'queued': 1},
        ], {'session': {'utilization': 10}},
            {'pixie': [{'used': 10}], 'gemini': [{'used': 10}]})
        self.assertEqual(result['target'], 'gemini')
        self.assertFalse(result['available'])
        self.assertTrue(result['queued'])


if __name__ == '__main__':
    unittest.main()
