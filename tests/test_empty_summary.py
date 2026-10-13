import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from src.ai.summarizer import Summarizer

class EmptySummaryTests(unittest.TestCase):
    def summarizer(self, bodies):
        s = Summarizer.__new__(Summarizer)
        s.providers = [{'name': 'existing', 'models': ['first', 'fallback']}]
        client = Mock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))], usage=None)
            for body in bodies]
        s._clients = {'existing': client}
        return s, client

    def test_empty_or_whitespace_response_uses_next_configured_model(self):
        for body in ('', '  \n', None):
            with self.subTest(body=body):
                s, client = self.summarizer([body, 'Useful course notes'])
                self.assertEqual(s.summarize('Course', 'Actual transcript'),
                                 ('Useful course notes', 'existing/fallback'))
                self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_all_empty_responses_report_failure(self):
        s, _ = self.summarizer(['', ' \n'])
        with self.assertRaisesRegex(RuntimeError, 'All LLM models failed'):
            s.summarize('Course', 'Actual transcript')

if __name__ == '__main__':
    unittest.main()
