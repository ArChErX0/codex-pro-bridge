import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_copied_reply.py"
SPEC = importlib.util.spec_from_file_location("capture_copied_reply", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


from unittest.mock import patch

from subprocess import CompletedProcess

class ClipboardReadTests(unittest.TestCase):
    def test_null_clipboard_is_empty_not_an_access_error(self):
        result = CompletedProcess([], 3, stdout=b"", stderr=b"")
        with patch.object(MODULE.shutil, "which", return_value="powershell.exe"), \
                patch.object(MODULE.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(MODULE.CaptureError, "^Windows clipboard is empty$"):
                MODULE.read_windows_clipboard()
        self.assertIn("$ErrorActionPreference='Stop'", run.call_args.args[0][-1])

    def test_access_error_is_never_classified_as_empty(self):
        for code, diagnostic in ((1, b"access denied"), (3, b"access denied"), (1, b"")):
            with self.subTest(code=code, diagnostic=diagnostic), \
                    patch.object(MODULE.shutil, "which", return_value="powershell.exe"), \
                    patch.object(MODULE.subprocess, "run", return_value=CompletedProcess(
                        [], code, stdout=b"", stderr=diagnostic)):
                with self.assertRaisesRegex(MODULE.CaptureError, "clipboard read failed"):
                    MODULE.read_windows_clipboard()

class CopiedReplyMetricsTests(unittest.TestCase):
    def test_new_display_math_and_nested_fences_do_not_count_code_as_prose(self):
        reply = '# Title\n\\[x=1\\]\n\\(y=2\\)\n````python\n# not heading\n```\n\\(not math\\)\n[not link](x)\n````\n'
        metrics = MODULE.markdown_metrics(reply)
        self.assertEqual({k:metrics[k] for k in ('headings','display_math','inline_math','code_blocks','links')},
                         {'headings':1,'display_math':1,'inline_math':1,'code_blocks':1,'links':0})

    def test_preserves_structured_markdown_contract(self):
        reply = """# Result

Inline \\(a_e\\) and [source](sandbox:/mnt/data/source.txt).

$$
A_e=\\frac{x}{y}
$$

| Name | Value |
| --- | ---: |
| a | 1 |

```cpp
phase.phi();
```
"""
        metrics = MODULE.markdown_metrics(reply)
        self.assertEqual(metrics["headings"], 1)
        self.assertEqual(metrics["inline_math"], 1)
        self.assertEqual(metrics["display_math"], 1)
        self.assertEqual(metrics["tables"], 1)
        self.assertEqual(metrics["code_blocks"], 1)
        self.assertEqual(metrics["links"], 1)

    def test_counts_each_defined_reference_usage_and_ignores_definitions(self):
        reply = """# Result

Inline [source](sandbox:/mnt/data/source.txt) plus [first][1], [first][1]
again, and [collapsed][].

[1]: https://example.test/one "one"
[collapsed]: https://example.test/two
"""
        metrics = MODULE.markdown_metrics(reply)
        self.assertEqual(metrics["links"], 4)

    def test_ignores_undefined_references_and_fenced_pseudo_links(self):
        reply = """# Result

An undefined [text][missing] reference, an image ![alt][missing],
and a real [doc](https://example.test/doc).

```python
[fake](https://example.test/fake) and [fake][1]
```

[1]: https://example.test/one
"""
        metrics = MODULE.markdown_metrics(reply)
        self.assertEqual(metrics["links"], 1)


if __name__ == "__main__":
    unittest.main()
