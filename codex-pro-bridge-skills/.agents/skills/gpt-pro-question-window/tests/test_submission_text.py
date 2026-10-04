from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from bridge_runtime.submission_text import copied_prompt_codec


class SubmissionTextTests(unittest.TestCase):
    def test_literal_ui_serialization_preserves_original_characters(self):
        expected='## Heading\nRead `a_b` https://example.com\n1. Done\n'
        copied='\\## Heading\\\nRead \\`a\\_b\\` [https://example.com](https://example.com)\\\n1\\. Done\\'
        self.assertEqual(copied_prompt_codec(copied,expected,literal_user_text=True),'literal-user-markdown/v1')
        self.assertIsNone(copied_prompt_codec(copied,expected))

    def test_no_whitespace_formatting_or_link_target_fuzzing(self):
        for actual,expected in [('a  b','a b'),('**a**','a'),('[a](https://evil.test)','a'),
                                ('[https://good.test](https://evil.test)','https://good.test'),
                                ('line1\n\nline2','line1\nline2'),('wrong','right')]:
            self.assertIsNone(copied_prompt_codec(actual,expected,literal_user_text=True))

    def test_literal_backslash_is_not_deleted(self):
        self.assertEqual(copied_prompt_codec(r'path\\name',r'path\name',literal_user_text=True),'literal-user-markdown/v1')
        self.assertIsNone(copied_prompt_codec(r'path\\name','pathname',literal_user_text=True))


if __name__=='__main__': unittest.main()
