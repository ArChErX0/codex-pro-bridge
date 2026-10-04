import json
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bridge_runtime.rpc import Client, RpcError, evaluated


def mcp_text(text):
    return {"content": [{"type": "text", "text": text}]}

class EvaluatedTest(unittest.TestCase):
    """One outer JSON envelope or raw JSON; never several fence-matched blocks."""

    def test_raw_json_payload_is_accepted(self):
        self.assertEqual(evaluated(mcp_text('{"a": 1}')), {"a": 1})

    def test_one_fenced_envelope_may_contain_json_strings_with_fences(self):
        payload = {"messages": [{"id": "m1", "text": "```python\nprint(1)\n```"}]}
        text = ("Script ran on page and returned:\n```json\n"
                + json.dumps(payload, ensure_ascii=False) + "\n```")
        self.assertEqual(evaluated(mcp_text(text)), payload)

    def test_envelope_prefix_may_span_separate_text_blocks(self):
        self.assertEqual(evaluated({"content": [
            {"type": "text", "text": "Script ran on page and returned:"},
            {"type": "text", "text": '```json\n{"a": 1}\n```'}]}), {"a": 1})

    def test_fence_without_info_string_is_accepted(self):
        self.assertEqual(evaluated(mcp_text('```\n{"a": 1}\n```')), {"a": 1})

    def test_two_independent_results_are_rejected(self):
        with self.assertRaisesRegex(RpcError, "content after its JSON envelope"):
            evaluated(mcp_text('```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```'))

    def test_trailing_garbage_is_rejected(self):
        with self.assertRaisesRegex(RpcError, "content after its JSON envelope"):
            evaluated(mcp_text('```json\n{"a": 1}\n```\nsee the note above'))
        with self.assertRaisesRegex(RpcError, "neither raw JSON"):
            evaluated(mcp_text('{"a": 1}\nsee the note above'))

    def test_error_text_is_rejected(self):
        with self.assertRaisesRegex(RpcError, "neither raw JSON"):
            evaluated(mcp_text("Script threw an error: boom\nat line 3"))
        with self.assertRaisesRegex(RpcError, "prose before its JSON envelope"):
            evaluated(mcp_text('Script threw an error:\nboom\n```json\n{"a": 1}\n```'))

    def test_incomplete_and_unknown_envelopes_are_rejected(self):
        with self.assertRaisesRegex(RpcError, "one complete JSON value"):
            evaluated(mcp_text('```json\n{"a": 1'))
        with self.assertRaisesRegex(RpcError, "unsupported format"):
            evaluated(mcp_text('```yaml\na: 1\n```'))
        with self.assertRaisesRegex(RpcError, "Empty evaluate_script result"):
            evaluated(mcp_text("   "))

class NavigationEnvelopeTest(unittest.TestCase):
    def response(self, value, tail=""):
        return {"content": [{"type": "text", "text":
            "Script ran on page and returned:\n```json\n" + json.dumps(value)
            + "\n```" + tail}]}

    def test_observed_native_navigation_envelope(self):
        # Chrome MCP emits this on same-document navigation too (wait=false).
        value = {"url": "about:blank#bridge-envelope-probe", "probe": True}
        self.assertEqual(evaluated(self.response(value,
            "\nPage navigated to about:blank#bridge-envelope-probe.")), value)

    def test_payload_code_fences_are_not_envelope_boundaries(self):
        value = {"url": "https://chatgpt.com/c/test", "text": "```json\n{}\n```"}
        self.assertEqual(evaluated(self.response(value,
            "\nPage navigated to https://chatgpt.com/c/test.")), value)

    def test_unknown_or_conflicting_metadata_still_fails(self):
        value = {"url": "https://chatgpt.com/c/test"}
        for tail in ("\nPage navigated to https://chatgpt.com/c/other.",
                     "\nPage navigated to https://chatgpt.com/c/test.\nextra",
                     "\nNote: browser reconnected", "\n# Open dialog", "\n{}"):
            with self.subTest(tail=tail), self.assertRaises(RpcError):
                evaluated(self.response(value, tail))

    def test_no_independent_url_is_not_navigation_evidence(self):
        for value in (None, True, "https://chatgpt.com/c/test", {}, {"url": ""}):
            with self.subTest(value=value), self.assertRaises(RpcError):
                evaluated(self.response(value, "\nPage navigated to https://chatgpt.com/c/test."))


class ShutdownTest(unittest.TestCase):
    def test_only_initial_read_uses_connection_budget(self):
        client = Client.__new__(Client)
        client.schemas = {"list_pages": {}, "click": {"properties": {"pageId": {}}}}
        client.timeout, client.connect_timeout = 90, 300
        client.browser_connected = False
        client.request = Mock(return_value={"content": []})
        client.call("list_pages")
        client.call("list_pages")
        client.call("click", pageId=1, uid="x")
        self.assertEqual([c.kwargs["timeout"] for c in client.request.call_args_list], [300, 90, 90])

    def test_failed_initial_read_does_not_mark_connected_or_retry(self):
        client = Client.__new__(Client)
        client.schemas = {"list_pages": {}}
        client.timeout, client.connect_timeout = 90, 300
        client.browser_connected = False
        client.request = Mock(return_value={"isError": True, "content": []})
        with self.assertRaises(RuntimeError):
            client.call("list_pages")
        self.assertFalse(client.browser_connected)
        client.request.assert_called_once()

    def test_stdio_eof_runs_server_cleanup(self):
        client = Client.__new__(Client)
        client.process = subprocess.Popen([sys.executable, "-c",
            "import sys; sys.stdin.read(); print('clean-disconnect', flush=True)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        try:
            client.close()
            self.assertEqual(client.process.returncode, 0)
            self.assertEqual(client.process.stdout.read().strip(), "clean-disconnect")
            client.close()  # idempotent, including already-closed stdin
        finally:
            if client.process.poll() is None:
                client.process.kill()
                client.process.wait()
            client.process.stdout.close()
            client.process.stderr.close()

    def test_unresponsive_server_still_has_bounded_shutdown(self):
        client = Client.__new__(Client)
        client.process = Mock()
        client.process.poll.return_value = None
        client.process.wait.side_effect = [subprocess.TimeoutExpired("mcp", 5), None]
        client.close()
        client.process.stdin.close.assert_called_once()
        client.process.terminate.assert_called_once()
        client.process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
