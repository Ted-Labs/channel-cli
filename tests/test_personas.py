import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from channel_cli import cli, personas, followup, agy_body, assignment
from channel_cli.store import Store

class PersonasTest(unittest.TestCase):
    def test_fourth_persona_followup_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'agents.json'
            registry.write_text(json.dumps([{'id':'cora','name':'코라'},{'id':'nova','name':'노바'}]))
            with patch.dict(os.environ, {'ATRIUM_AGENTS_PATH':str(registry)}):
                args=cli.parser().parse_args(['followup-claim','--job-id','a'*32,'--persona','nova','--client-pid','10','--request-id','request'])
                self.assertEqual(args.persona,'nova')
                store = Store(tmp)
                try:
                    job=store.enqueue('atrium-web:nova','dev','claude-body','hello','hello',cwd=tmp,persona='nova',message_id='msg')
                    job=store.claim('claude-body','nova')
                    store.bind_executor(job,{'persona':'nova','engine':'claude-body','pane':{'pid':10,'start':'20'}})
                    store.finish(job,'done',answer='ok')
                    with patch('channel_cli.followup.authorized',return_value=True):
                        self.assertEqual(store.reserve_followup(job['id'],args.persona,10,'request')['index'],1)
                finally:
                    store.close()
                self.assertEqual(agy_body.session_for_channel('atrium-web:nova'),'nova')
                self.assertEqual(assignment._used_percent('nova',None,{'nova':[{'used':42}]}),42)

    def test_unreadable_registry_uses_legacy_defaults(self):
        with patch.dict(os.environ, {'ATRIUM_AGENTS_PATH':'/nonexistent/agents.json'}):
            self.assertEqual(personas.ids(),('cora','pixie','gemini'))

if __name__ == '__main__': unittest.main()

class CompactHeaderTest(unittest.TestCase):
    def test_followup_is_merged_into_header_preserving_security_fields(self):
        original = '[Atrium metadata] {"chat_id":"dev"}\n[Atrium] {"persona":"픽시","lang":"ko","ssh":"/safe/ssh_rule.md","rules":"~/Corpus/agents/ATRIUM_RULES.md"}\n본문'
        result = followup.prompt({'persona':'pixie','id':'a'*32}, original)
        lines = result.splitlines()
        self.assertTrue(lines[0].startswith('[Atrium metadata] '))
        header = json.loads(lines[1][len('[Atrium] '):])
        self.assertEqual(header['ssh'], '/safe/ssh_rule.md')
        self.assertEqual(header['followup'], 'a'*32)
        self.assertEqual(lines[2], '본문')
        self.assertNotIn('[Atrium 후속 알림]', result)

    def test_invalid_existing_header_is_rejected(self):
        with self.assertRaises(ValueError):
            followup.prompt({'persona':'pixie','id':'a'*32}, '[Atrium] broken\n본문')
