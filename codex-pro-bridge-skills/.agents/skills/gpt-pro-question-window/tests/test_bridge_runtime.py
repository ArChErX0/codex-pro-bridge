"""Offline acceptance: canonical gates/ledger with a deterministic browser double."""
from __future__ import annotations

import concurrent.futures
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / ".shared"))
from bridge_store import BridgeError, file_sha256, now_iso
from bridge_attempts import active_attempt, digest
from bridge_runtime import pipeline
from bridge_runtime.browser import Browser, unique_uid
from bridge_runtime.jobs import Jobs, Job, write_json, worker_lock
from bridge_mcp import serve


from bridge_store import (atomic_write_text, bridge_root, default_gpt_session_id,
                          parse_metadata)

from bridge_attempts import (mark_send_started, prepare_attempt,
                             record_submission)

from bridge_runtime.rpc import RpcError

DOM_METRICS_HARNESS = r'''
'use strict';
// Minimal element model for the production DOM-metrics script: it implements only the
// selectors and node properties that script uses, plus the observed page structure.
class El {
  constructor(tag, attrs = {}, children = []) {
    this.nodeName = tag;
    this.attrs = attrs;
    this.children = [];
    this.parentElement = null;
    this.disabled = false;
    this.clicks = 0;
    for (const child of children) this.append(child);
  }
  append(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  }
  getClientRects() { return [{}]; }
  click() { this.clicks += 1; }
  querySelectorAll(selector) { return descendants(this).filter(node => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) {
      if (matches(node, selector)) return node;
    }
    return null;
  }
}
const descendants = node => node.children.flatMap(child => [child, ...descendants(child)]);
const classes = node => (node.attrs.class || '').split(/\s+/).filter(Boolean);
const matches = (node, selector) => selector.split(',').some(part => {
  const tokens = part.trim().match(/^[a-zA-Z][\w-]*|\.[\w-]+|\[[^\]]+\]/g);
  if (!tokens) throw new Error('unsupported selector: ' + part);
  return tokens.every(token => {
    if (token.startsWith('.')) return classes(node).includes(token.slice(1));
    if (token.startsWith('[')) {
      const spec = /^\[([\w-]+)(?:([~^]?=)"([^"]*)")?\]$/.exec(token);
      if (!spec) throw new Error('unsupported attribute selector: ' + token);
      const value = node.getAttribute(spec[1]);
      if (value === null) return false;
      if (!spec[2]) return true;
      if (spec[2] === '~=') return value.split(/\s+/).includes(spec[3]);
      return spec[2] === '^=' ? value.startsWith(spec[3]) : value === spec[3];
    }
    return node.nodeName.toLowerCase() === token.toLowerCase();
  });
});
const fileIcon = () => new El('span', {'data-testid': 'library-file-icon'});
const body = new El('div', {class: 'markdown'});
const answer = new El('div', {'data-message-id': 'runtime-answer', 'data-message-author-role': 'assistant'}, [body]);
const turn = new El('div', {'data-testid': 'conversation-turn-3'}, [
  answer,
  new El('button', {'data-testid': 'copy-turn-action-button'}),
]);
const root = new El('div', {}, [turn]);
// Two headings, one inline math, three display math (role=math around .katex-display,
// .katex-display around role=math, and a nested role=math around .katex-display around
// role=math that must still count as one), one table, one code block whose outer pre wraps
// a CodeMirror pre, four citation anchors, one sandbox download button and one ordinary
// action button that is not a link. The first citation anchor wraps an icon button to pin
// that such a button stays part of the anchor instead of becoming a second link.
body.append(new El('h2'));
body.append(new El('h3'));
body.append(new El('p', {}, [new El('span', {role: 'math'}, [new El('span', {class: 'katex'})])]));
body.append(new El('p', {}, [new El('span', {role: 'math'}, [new El('span', {class: 'katex-display'}, [new El('span', {class: 'katex'})])])]));
body.append(new El('p', {}, [new El('span', {class: 'katex-display'}, [new El('span', {role: 'math'})])]));
body.append(new El('p', {}, [new El('span', {role: 'math'}, [new El('span', {class: 'katex-display'}, [new El('span', {role: 'math'}, [new El('span', {class: 'katex'})])])])]));
body.append(new El('table', {}, [new El('tbody', {}, [new El('tr', {}, [new El('td')])])]));
body.append(new El('pre', {}, [new El('pre', {}, [new El('code')])]));
body.append(new El('p', {}, [
  new El('a', {href: 'https://example.test/one'}, [new El('button', {}, [fileIcon()])]),
  new El('a', {href: 'https://example.test/one'}),
  new El('a', {href: 'https://example.test/two'}),
  new El('a', {href: 'https://example.test/three'}),
]));
body.append(new El('p', {}, [new El('button', {'data-testid': 'library-file-download'}, [fileIcon()])]));
body.append(new El('button', {'data-testid': 'share-turn-action-button'}));
const document = {querySelectorAll: selector => descendants(root).filter(node => matches(node, selector))};
const metrics = eval(process.argv[1])();
process.stdout.write(JSON.stringify(metrics));
'''

DOM_STRUCTURE_MARKDOWN = r'''## Result

### Evidence

One inline expression \(a_e\) and three displayed ones.

$$
E_1
$$

$$
E_2
$$

$$
E_3
$$

| Name | Value |
| --- | --- |
| a | 1 |

```cpp
// [shell](https://example.test/fake) is prose, not a link
void f() {}
```

Citing [OpenFOAM][1] twice ([OpenFOAM][1]), plus [matrix][2] and [scheme][3], an
undefined [ghost][9], and the [sandbox file](sandbox:/mnt/data/evidence.txt).

[1]: https://example.test/one
[2]: https://example.test/two
[3]: https://example.test/three
'''

def dom_metrics_from_node(script: str) -> dict:
    """Run the production metrics script against the stub document with the project's node."""
    proc = subprocess.run(["node", "-e", DOM_METRICS_HARNESS, script],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"stub DOM run failed: {proc.stderr}")
    return json.loads(proc.stdout)

class DomMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {"state_dir": str(self.root / "state")}

    def run_capture(self, markdown, observed):
        attempt = {"state": "submitted", "attempt_id": "dom-attempt",
                   "conversation_url": "https://chatgpt.com/c/runtime-chat",
                   "conversation_id": "runtime-chat", "project_id": "",
                   "tab_owner_token": "dom-owner", "remote_turn_id": "runtime-user",
                   "deadline": None}
        output = self.root / "answer.md"
        def evaluate(function, page_id=None):
            if "const record = bridgePinnedMessage" in function:
                observed.update(dom_metrics_from_node(function))
                return observed
            if "waitForReply" in function:
                return {"status": "ready-for-capture", "assistant_turn_id": "runtime-answer",
                        "observed_at": now_iso()}
            if "Answer identity changed" in function:
                return True
            raise AssertionError(function)
        class Browser:
            url = "https://chatgpt.com/c/runtime-chat"
            owner = "dom-owner"
            page_id = "7"
            def evaluate(self, function, page_id=None):
                return evaluate(function, page_id)
            def assert_identity(self):
                pass
            def snapshot(self):
                return "uid=7_1"
            def copied_link_source(self, *args):
                return {"status": "unsupported"}
            def copy_with_proof(self, *args):
                return {"text": markdown, "provenance": "visible-copy-write/v1"}
        browser = Browser()
        receipt = pipeline.capture(browser, attempt, output, self.config)
        return output, receipt

    def test_dom_metrics_track_display_math_nested_pre_and_sandbox_links(self):
        observed = {}
        output, receipt = self.run_capture(DOM_STRUCTURE_MARKDOWN, observed)
        # The nested role=math>.katex-display>role=math paragraph must stay one display
        # expression; a double count would show up as display_math 4 / inline_math 0 here
        # and as a capture failure against the three $$ blocks below.
        self.assertEqual(observed, {"headings": 2, "display_math": 3, "inline_math": 1,
                                    "tables": 1, "code_blocks": 1, "links": 5})
        self.assertEqual(output.read_text(encoding="utf-8"), DOM_STRUCTURE_MARKDOWN)
        self.assertEqual(receipt["assistant_turn_id"], "runtime-answer")
        from capture_copied_reply import markdown_metrics
        self.assertEqual({key: markdown_metrics(DOM_STRUCTURE_MARKDOWN)[key] for key in observed},
                         observed)

    def test_capture_rejects_markdown_that_drops_the_sandbox_link(self):
        mismatched = DOM_STRUCTURE_MARKDOWN.replace(
            "[sandbox file](sandbox:/mnt/data/evidence.txt)", "the sandbox file")
        with self.assertRaisesRegex(BridgeError, "Copied Markdown link mismatch requires complete source proof"):
            self.run_capture(mismatched, {})

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temp)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = patch.dict(os.environ, {
            "CODEX_PRO_BRIDGE_BROWSER_STATE_DIR": str(self.root / "browser-state"),
            "CODEX_BRIDGE_STAGING_WSL_ROOT": str(self.root / "staging"),
            "CODEX_BRIDGE_STAGING_WINDOWS_ROOT": r"C:\BridgeTest",
            "CODEX_PRO_BRIDGE_HOST_CONFIG": "",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        self.env.start()
        self.addCleanup(self.env.stop)
        notes = self.repo / "notes.md"
        notes.write_text("frozen evidence", encoding="utf-8")
        request = {"repo": str(self.repo), "bridge_thread_id": "runtime-test", "goal": "Test",
            "question": "请原样回答 OK", "notes": "notes.md", "files": [],
            "context_policy": "none", "max_files": 0, "file_digests": {"notes.md": file_sha256(notes)}}
        request_path = self.repo / "request.json"
        write_json(request_path, request)
        self.h = {"schema_version": "executor_handoff/v2", "mode": "prepare-and-run", "repo": str(self.repo),
            "bridge_thread_id": "runtime-test", "request_file": str(request_path),
            "request_sha256": file_sha256(request_path), "allow_send": True,
            "allowed_external_actions": ["send-once", "capture-reply"],
            "expected_output_dir": str(self.repo / "artifacts"), "requested_model": "最新",
            "model_selection_kind": "latest-alias", "requested_thinking_intensity": "极高",
            "context_policy": "none", "max_files": 0, "attachment_policy": "none",
            "target_project_url": "", "binding_action": "none", "business_deadline": None}
        self.h_path = self.repo / "handoff.json"
        write_json(self.h_path, self.h)
        self.config = {"state_dir": str(self.root / "jobs"), "allowed_repos": [str(self.repo)],
            "browser_command": [sys.executable, "-c", "pass"], "ui": {
                "model": "#model", "thinking": "#thinking", "composer": "#composer",
                "attachment_chip": "#composer .chip", "model_menu_labels": ["Model"],
                "thinking_menu_labels": ["Thinking"], "attachment_labels": ["Add files"],
                "upload_labels": ["Upload from computer"], "send_labels": ["Send"]}}
        self.config_path = self.root / "runtime.json"
        write_json(self.config_path, self.config)
        self.jobs = Jobs(self.config_path)

    def submit(self, tag="", attachment_policy=None):
        # These fixtures start independent follow-ups after a completed seed
        # round. Close that synthetic round through the real verdict helper.
        from bridge_store import load_events
        events = load_events(bridge_root(self.repo), self.h['bridge_thread_id'])
        if events and events[-1].get('event_type') == 'gpt-exchange':
            turn = self.repo / events[-1]['artifact']['path']
            pipeline.run_helper(SCRIPTS / 'record_codex_verdict.py',
                ['--repo', self.repo, '--bridge-thread-id', self.h['bridge_thread_id'],
                 '--turn', turn, '--summary', 'Synthetic seed round',
                 '--verification', 'Synthetic raw answer checked before fixture follow-up'],
                self.repo, json_output=False)
        handoff_path = self.h_path
        if tag or attachment_policy:
            request = {**json.loads(Path(self.h["request_file"]).read_text(encoding="utf-8"))}
            if tag:
                request["question"] = f"请原样回答 OK（{tag}）"
            request_path = self.repo / f"request-{tag or 'base'}.json"
            handoff = {**self.h, "request_file": str(request_path)}
            if attachment_policy:
                request["context_policy"] = handoff["context_policy"] = "explicit"
                request["max_files"] = handoff["max_files"] = 1
                request["files"] = ["notes.md"]
                handoff["attachment_policy"] = attachment_policy
                handoff["allowed_external_actions"] = [*self.h["allowed_external_actions"],
                                                        "upload-task-bundle"]
            write_json(request_path, request)
            handoff["request_sha256"] = file_sha256(request_path)
            handoff_path = self.repo / f"handoff-{tag or 'bundle'}.json"
            write_json(handoff_path, handoff)
        with patch.object(Jobs, "_spawn"):
            result = self.jobs.submit(str(handoff_path), file_sha256(handoff_path))
        return Job(self.jobs.directory(result["job_id"]))

    def cleanup_temp(self):
        # Job state can be persisted just before the detached process releases
        # its stdout log. Windows correctly refuses unlinking that open handle.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def test_concurrent_submit_is_idempotent(self):
        with patch.object(Jobs, "_spawn") as spawn, concurrent.futures.ThreadPoolExecutor(4) as executor:
            results = list(executor.map(lambda _: self.jobs.submit(str(self.h_path), file_sha256(self.h_path)), range(8)))
        self.assertEqual(len({r["job_id"] for r in results}), 1)
        self.assertEqual(spawn.call_count, 1)

    def test_frozen_inputs_and_repository_allowlist(self):
        (self.repo / "notes.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "drift"):
            self.submit()
        (self.repo / "notes.md").write_text("frozen evidence", encoding="utf-8")
        self.jobs.config["allowed_repos"] = [str(self.root / "other")]
        with self.assertRaisesRegex(BridgeError, "allowed_repos"):
            self.submit()

    def test_malformed_job_ids_cannot_escape_store(self):
        for identifier in ("../state", "a" * 63, "z" * 64):
            with self.subTest(identifier=identifier), self.assertRaises(BridgeError):
                self.jobs.status(identifier)

    def test_wait_timeout_preserves_job_and_does_not_spawn(self):
        job = self.submit()
        with patch.object(Jobs, "_spawn") as spawn:
            result = self.jobs.wait(job.data["job_id"], 0.01)
        self.assertEqual(result["state"], "queued")
        self.assertFalse(result["may_resend"])
        spawn.assert_not_called()

    def test_cleanup_recovers_missing_receipt_without_redeleting(self):
        job = self.submit()
        job.update(preparation={"staging": {"staged_execution_path": str(self.root / "already-removed.zip"),
                                            "source_sha256": "a" * 64}})
        with patch.object(pipeline, "cleanup_staged_file") as remove:
            pipeline.cleanup(job)
        remove.assert_not_called()
        self.assertEqual(job.data["staging_cleaned"]["status"], "already-absent")
        self.assertFalse(job.data["staging_cleaned"]["cleaned"])

    def test_single_worker_lock(self):
        job = self.submit()
        with worker_lock(job.directory) as first:
            self.assertTrue(first)
            with worker_lock(job.directory) as second:
                self.assertFalse(second)

    def test_pre_send_resume_never_repeats_upload(self):
        job = self.submit()
        job.update(stage="upload-started")
        with patch.object(pipeline, "Client") as client, self.assertRaisesRegex(BridgeError, "will not be replayed"):
            pipeline.execute(job, self.config)
        client.assert_not_called()

    def test_unverified_source_inventory_blocks_before_browser(self):
        from unittest.mock import Mock
        request_path = Path(self.h["request_file"])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["bridge_project_id"] = "project-test"
        write_json(request_path, request)
        self.h.update(request_sha256=file_sha256(request_path), bridge_project_id="project-test",
                      remote_project_id="g-p-demo", binding_action="reuse",
                      target_project_url="https://chatgpt.com/g/g-p-demo/project")
        write_json(self.h_path, self.h)
        self.config["ui"].update(workspace="#workspace", account="#account")
        write_json(self.config_path, self.config)
        self.jobs = Jobs(self.config_path)
        job = self.submit()
        store = Mock()
        store.load_binding.return_value = {"status": "active", "remote_project_id": "g-p-demo"}
        store.verify.return_value = {"project_status": "active", "inventory_verified": False, "unsynced_source_count": 0}
        store.project_for_thread.return_value = "project-test"
        with patch.object(pipeline, "BridgeProjectStore", return_value=store), patch.object(pipeline, "Client") as client:
            with self.assertRaisesRegex(BridgeError, "reconciliation"):
                pipeline.execute(job, self.config)
        client.assert_not_called()

    def test_mcp_dispatch_and_bad_arguments(self):
        source = io.StringIO("\n".join(json.dumps(v) for v in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "bridge_status", "arguments": {"job_id": "../outside"}}}]) + "\n")
        sink = io.StringIO()
        serve(self.jobs, source, sink)
        messages = {m["id"]: m for m in map(json.loads, sink.getvalue().splitlines())}
        self.assertEqual(len(messages[2]["result"]["tools"]), 5)
        self.assertTrue(messages[3]["result"]["isError"])

    def test_unique_snapshot_uid_rejects_duplicate_and_disabled(self):
        self.assertEqual(unique_uid('uid=2_1 button "Send"', ("button",), ["Send"]), "2_1")
        for text in ('uid=2_1 button "Send" [disabled]', 'uid=2_1 button "Send"\nuid=2_2 button "Send"'):
            with self.assertRaises(BridgeError):
                unique_uid(text, ("button",), ["Send"])

    def test_submission_requires_exact_prompt_after_boundary(self):
        attempt = {"tab_owner_token": "owner", "project_id": "", "conversation_id": "chat",
                   "pre_submit_boundary": "before", "prompt": "frozen prompt"}
        observation = {"url": "https://chatgpt.com/c/chat", "owner": "owner", "messages": [
            {"id": "before", "role": "assistant", "text": ""},
            {"id": "user", "role": "user", "text": "frozen prompt"}]}
        self.assertEqual(pipeline.observed_submission(observation, attempt), "user")
        observation["messages"][1]["text"] = "unrelated"
        with self.assertRaisesRegex(BridgeError, "no resend"):
            pipeline.observed_submission(observation, attempt)

    def test_upload_uses_one_menu_click_and_one_upload(self):
        from unittest.mock import Mock
        client = Mock()
        browser = Browser(client, {"attachment_labels": ["Add files"], "upload_labels": ["Upload from computer"],
                                   "attachment_chip": "#composer .chip"})
        browser.page_id, browser.owner, browser.url = "7", "owner", "https://chatgpt.com/"
        browser.assert_identity = Mock()
        browser.snapshot = Mock(side_effect=['uid=7_1 button "Add files"', 'uid=7_2 menuitem "Upload from computer"'])
        browser.evaluate = Mock(return_value=[{"name": "bundle.zip", "busy": False}])
        marker = Mock()
        receipt = browser.upload({"staged_browser_path": r"C:\BridgeTest\bundle.zip", "source_sha256": "a" * 64,
                                 "attachment_name": "bundle.zip"}, self.root / "plan.json", self.root / "receipt.json",
                                 write_json, marker)
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["click", "upload_file"])
        marker.assert_called_once()
        self.assertEqual(receipt["status"], "accepted")

    def test_upload_error_never_retries(self):
        from unittest.mock import Mock
        client = Mock()
        client.call.side_effect = [{}, RuntimeError("unknown upload outcome")]
        browser = Browser(client, {"attachment_labels": ["Add files"], "upload_labels": ["Upload from computer"]})
        browser.page_id = "7"
        browser.assert_identity = Mock()
        browser.snapshot = Mock(side_effect=['uid=7_1 button "Add files"', 'uid=7_2 menuitem "Upload from computer"'])
        with self.assertRaisesRegex(RuntimeError, "unknown upload"):
            browser.upload({"staged_browser_path": r"C:\BridgeTest\bundle.zip", "source_sha256": "a" * 64,
                            "attachment_name": "bundle.zip"}, self.root / "plan.json", self.root / "receipt.json",
                            write_json, Mock())
        self.assertEqual(client.call.call_count, 2)
        self.assertFalse((self.root / "receipt.json").exists())

    def test_detached_worker_outlives_mcp_connection(self):
        fake = self.root / "slow-browser.py"
        fake.write_text('''import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg: continue
    if msg["method"] == "initialize":
        time.sleep(2)
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}
    else: result = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
''', encoding="utf-8")
        self.config["browser_command"] = [sys.executable, str(fake)]
        write_json(self.config_path, self.config)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "bridge_submit", "arguments": {"handoff_path": str(self.h_path),
                                                     "handoff_sha256": file_sha256(self.h_path)}}}
        parent = subprocess.run([sys.executable, str(SCRIPTS / "bridge_mcp.py"), "--config", str(self.config_path)],
            input=json.dumps(request) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=40, check=True)
        job_id = json.loads(parent.stdout)["result"]["structuredContent"]["job_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = self.jobs.status(job_id)
            if result["state"] == "blocked" and not result["worker_recently_alive"]:
                break
            time.sleep(0.1)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("lacks list_pages", result["error"])
        self.assertTrue((self.jobs.directory(job_id) / "job.json").is_file())

    def browser_double(self, crash_after_send=False, crash_after_upload=False, **seed):
        state = {"url": "https://chatgpt.com/", "owner": "", "sent": 0, "prompt": "", "messages": [],
                 "attachments": [], "uploads": 0, "messages_crashed": False, "draft": True}
        state.update(seed)
        class Client:
            def __init__(self, *args, **kwargs):
                pass
            def close(self):
                pass
            def call(self, name, **kwargs):
                if name == "click":
                    if crash_after_send == "before":
                        raise RuntimeError("injected disconnect before click reached browser")
                    state["sent"] += 1
                    state["url"] = "https://chatgpt.com/c/runtime-chat"
                    user_id = "runtime-user" if state["sent"] == 1 else "runtime-user-" + str(state["sent"])
                    state["messages"].append({"id": user_id, "role": "user", "text": state["prompt"]})
                    state["draft"] = False
                    if crash_after_send and state["sent"] == 1:
                        raise RuntimeError("injected disconnect after accepted Send")
                return {}
        class Browser:
            def __init__(self, client, ui):
                self.client = client
                self.tool_metrics = {}
                self.ui = ui
                self.page_id, self.owner, self.url = None, "", ""
            def call(self, name, **kwargs):
                return self.client.call(name, **kwargs)
            def pages(self):
                return [{"page_id": "7", "url": state["url"]}]
            def owners(self, pages):
                return [{"page_id": "7", "owner_token": state["owner"]}]
            def evaluate(self, function, page_id=None):
                if "sessionStorage.setItem" in function:
                    state["owner"] = json.loads(re.search(r'sessionStorage.setItem\(key, ("[^"]+")\)', function)[1])
                    return True
                if "waitForReply" in function:
                    return {"status": "ready-for-capture", "assistant_turn_id": "runtime-answer", "observed_at": now_iso()}
                raise AssertionError(function)
            def assert_identity(self):
                if self.owner != state["owner"] or self.url != state["url"]:
                    raise BridgeError("fake owner mismatch")
            def snapshot(self):
                self.assert_identity()
                return 'uid=7_1 button "Send"'
            def composer_text(self):
                return state["prompt"].strip() if state["draft"] else ""
            def attachments(self):
                self.assert_identity()
                return list(state["attachments"])
            def attachment_count(self):
                return len(state["attachments"])
            def attachment_ready(self, name):
                cards = self.attachments()
                return len(cards) == 1 and cards[0] == {"name": name, "busy": False}
            def upload(self, staging, plan_path, result_path, save, before_action):
                from devtools_upload import build_upload_action_plan, verify_upload_result
                before_action()
                plan = build_upload_action_plan(page_id=str(self.page_id),
                    attachment_button_uid="11_229", menu_snapshot_uid="snapshot-26_2", menu_item_uid="26_2",
                    windows_path=staging["staged_browser_path"], staged_sha256=staging["source_sha256"])
                save(plan_path, plan)
                save(result_path.with_suffix(".transport.json"), {"status": "accepted",
                    "page_id": str(self.page_id), "owner": self.owner, "url": self.url, "plan": plan})
                state["attachments"] = [{"name": staging["attachment_name"], "busy": False}]
                state["uploads"] += 1
                receipt = verify_upload_result(plan, {"status": "accepted", "attachment_chip": True,
                                                      "attachment_name": staging["attachment_name"]})
                save(result_path, receipt)
                return receipt
            def adjust_controls(self, h):
                from model_controls import validate_model_control_trace
                return validate_model_control_trace({"schema_version": "model-controls/v1", "page_id": "7",
                    "tab_owner_token": self.owner, "observed_page_url": self.url,
                    "requested_model": "最新", "selected_model": "最新", "model_selection_kind": "latest-alias",
                    "requested_thinking_intensity": "极高", "selected_thinking_intensity": "极高",
                    "initial_model": "最新", "initial_thinking_intensity": "极高", "counts": {
                        "combined_initial_read": 1, "model_menu_open": 0, "model_selection": 0,
                        "thinking_control_open": 0, "thinking_adjustment": 0, "thinking_progress_read": 0,
                        "combined_final_confirmation": 1, "post_preflight_recheck": 0}})
            def fill_prompt(self, prompt):
                if crash_after_upload and state['uploads'] and not state['messages_crashed']:
                    state['messages_crashed'] = True
                    raise RpcError('Ambiguous evaluate_script result')
                state["prompt"] = prompt
                state["draft"] = True
            def messages(self, **kwargs):
                if crash_after_upload and state["uploads"] and not state["messages_crashed"]:
                    state["messages_crashed"] = True
                    raise RpcError("Ambiguous evaluate_script result")
                return {"url": state["url"], "owner": state["owner"], "messages": state["messages"]}
        def capture(browser, attempt, output, config):
            output.write_text("# 完整原始回答\n\nOK\n", encoding="utf-8")
            return {"answer_path": str(output), "answer_sha256": file_sha256(output),
                    "assistant_turn_id": "runtime-answer", "completed_at": now_iso()}
        return state, Client, Browser, capture

    def test_connection_only_interruption_reuses_frozen_preparation(self):
        job = self.submit()
        with patch.object(pipeline, "Client", side_effect=RuntimeError("connection pending")):
            with self.assertRaisesRegex(RuntimeError, "connection pending"):
                pipeline.execute(job, self.config)
        frozen = dict(job.data["preparation"])
        self.assertEqual(job.data["browser_phase"], "connecting-browser")
        state, client, browser, capture = self.browser_double()
        with patch.object(pipeline,"Client",client), patch.object(pipeline,"Browser",browser), patch.object(pipeline,"capture",capture):
            pipeline.execute(job,self.config)
        self.assertEqual(job.data["preparation"],frozen)
        self.assertEqual(job.data["state"],"complete")
        self.assertEqual(state["sent"],1)

    def test_connection_resume_never_replays_started_ui_work(self):
        job = self.submit()
        job.update(stage="prepared-materials",browser_phase="connected")
        with self.assertRaisesRegex(BridgeError,"will not be replayed"):
            pipeline.execute(job,self.config)

    def test_claim_contention_resume_reuses_preparation(self):
        job = self.submit()
        with patch.object(pipeline, "acquire_browser_lease", side_effect=BridgeError("claim conflict")), \
                patch.object(pipeline, "Client") as blocked_client:
            with self.assertRaisesRegex(BridgeError, "claim conflict"):
                pipeline.execute(job, self.config)
            blocked_client.assert_not_called()
        frozen = dict(job.data["preparation"])
        self.assertEqual(job.data["browser_phase"], "acquiring-claim")
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertEqual(job.data["preparation"], frozen)
        self.assertEqual(job.data["state"], "complete")
        self.assertEqual(state["sent"], 1)

    def test_copied_submission_requires_exact_raw_prompt(self):
        from unittest.mock import Mock
        prompt = "Read `payload.md`"
        attempt = {"prompt": prompt, "prompt_sha256": digest(prompt), "tab_owner_token": "owner",
                   "project_id": "", "conversation_id": "", "pre_submit_boundary": "new-conversation"}
        obs = {"owner":"owner", "url":"https://chatgpt.com/c/copied-chat",
               "messages":[{"role":"user", "id":"user-1", "text":"Read payload.md"}]}
        with self.assertRaisesRegex(BridgeError, "no resend"):
            pipeline.observed_submission(obs, attempt)
        browser = Mock(owner="owner")
        browser.assert_identity = Mock()
        browser.messages.return_value = obs
        browser.copy_with_proof.return_value = {"text":prompt,"provenance":"visible-copy-write/v1"}
        self.assertEqual(pipeline.verify_copied_submission(browser, obs, attempt, self.root, self.config), "user-1")
        self.assertEqual((self.root / "copied-user-prompt.md").read_text(), prompt)
        browser.copy_with_proof.assert_called_once_with("user-1", "user")
        browser.copy_with_proof.return_value = {"text":"wrong raw text","provenance":"visible-copy-write/v1"}
        with self.assertRaisesRegex(BridgeError, "differs from frozen"):
            pipeline.verify_copied_submission(browser, obs, attempt, self.root, self.config)

    def test_round_uses_real_preflight_and_canonical_capture(self):
        job = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertEqual(state["sent"], 1)
        result = self.jobs.result(job.data["job_id"])
        self.assertEqual(result["state"], "complete")
        self.assertTrue(Path(result["result"]["turn_path"]).is_file())
        self.assertIsNone(active_attempt(self.repo, "runtime-test"))
        Path(result["result"]["answer_path"]).write_text("drift", encoding="utf-8")
        result = self.jobs.result(job.data["job_id"])
        self.assertEqual(result["state"], "blocked")
        self.assertIn("digest drift", result["completion_validation_error"])

    def test_two_full_rounds_have_middle_verdict_and_real_verifier(self):
        from bridge_store import record_codex_verdict, verify_thread_integrity
        state, client, browser, capture = self.browser_double()
        for index in range(2):
            job = self.submit()
            with patch.multiple(pipeline,Client=client,Browser=browser,capture=capture):
                pipeline.execute(job,self.config)
            result = self.jobs.result(job.data["job_id"])
            self.assertEqual(result["result"]["local_verdict"],"pending")
            record_codex_verdict(self.repo,thread_id="runtime-test",codex_session_id="runtime-test-codex",
                gpt_pro_session_id="runtime-test-gpt-pro",turn_path=Path(result["result"]["turn_path"]),
                summary="fixture result",verification="independent local fixture check")
            request = json.loads(Path(self.h["request_file"]).read_text())
            request["question"] = "第二轮问题" + str(index)
            write_json(Path(self.h["request_file"]),request)
            self.h["request_sha256"] = file_sha256(Path(self.h["request_file"]))
            write_json(self.h_path,self.h)
        self.assertEqual(state["sent"],2)
        report = verify_thread_integrity(self.repo,"runtime-test",require_complete_rounds=True)
        self.assertEqual(report["complete_rounds"],2)
        self.assertEqual(report["event_count"],6)

    def test_page_route_joint_saver_schema_completion_and_readonly_missing_receipt(self):
        from test_page_serialization import raw_observation
        self.config["capture_route"] = "browser-page-serialized"
        write_json(self.config_path,self.config)
        self.jobs = Jobs(self.config_path)
        job = self.submit()
        state,client,original,_ = self.browser_double()
        class Browser(original):
            def page_serialization(self,attempt,assistant_id):
                observation = raw_observation(attempt,assistant_id,node_parent=True)
                parts = observation["messages"][1]["message"]["content"]["parts"]
                parts[0] = parts[0].replace("\n","\r\n")
                return observation
        with patch.multiple(pipeline,Client=client,Browser=Browser):
            pipeline.execute(job,self.config)
        self.assertEqual(state["sent"],1)
        from jsonschema import Draft7Validator
        from capture_provenance import SCHEMA_PATH
        captured = self.jobs.result(job.data["job_id"])["result"]
        Draft7Validator(json.loads(SCHEMA_PATH.read_text())).validate(json.loads(Path(captured["capture_proof_path"]).read_text()))
        corrupted = dict(job.data)
        corrupted["capture"] = {**corrupted["capture"],"completed_at":"synthetic-private-marker",
            "capture_proof_path":"synthetic-private-marker","capture_proof_sha256":"synthetic-private-marker",
            "assistant_turn_id":"synthetic-private-marker"}
        write_json(job.path,corrupted)
        projected = self.jobs.result(job.data["job_id"])
        self.assertEqual(projected["state"],"complete")
        self.assertEqual(projected["result"],captured)
        self.assertNotIn("synthetic-private-marker",json.dumps(projected))
        data = dict(job.data)
        data.pop("capture")
        data.pop("result")
        write_json(job.path,data)
        before = job.path.read_bytes()
        with patch.object(self.jobs,"_spawn") as spawn,patch.object(pipeline,"Client") as connect:
            result = self.jobs.resume(job.data["job_id"])
        spawn.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(result["state"],"complete")
        self.assertEqual(result["result"]["receipt_recovered_from"],"canonical-exchange-raw-evidence")
        self.assertEqual(result["result"]["local_verdict"],"pending")
        self.assertEqual(before,job.path.read_bytes())
        self.assertNotIn("owner_token",json.dumps(result))

    def test_prepared_connection_resume_checks_drift_before_browser(self):
        evidence = self.repo/"evidence.md"
        evidence.write_text("frozen material")
        request_path = Path(self.h["request_file"])
        request = json.loads(request_path.read_text())
        request.update(files=["evidence.md"],context_policy="explicit",max_files=1)
        request["file_digests"]["evidence.md"] = file_sha256(evidence)
        write_json(request_path,request)
        self.h.update(context_policy="explicit",max_files=1,attachment_policy="bundle",request_sha256=file_sha256(request_path))
        self.h["allowed_external_actions"].append("upload-task-bundle")
        write_json(self.h_path,self.h)
        job = self.submit()
        with patch.object(pipeline,"Client",side_effect=RuntimeError("fixture connection interruption")):
            with self.assertRaisesRegex(RuntimeError,"connection interruption"):
                pipeline.execute(job,self.config)
        self.assertEqual(job.data["browser_phase"],"connecting-browser")
        prep = job.data["preparation"]
        for path in (Path(prep["bundle"]),Path(prep["materials_file"]),Path(prep["staging"]["staged_execution_path"])):
            original = path.read_bytes();path.write_bytes(b"drift")
            with patch.object(pipeline,"Client") as client,self.assertRaises(BridgeError):
                pipeline.execute(Job(job.directory),self.config)
            client.assert_not_called()
            path.write_bytes(original)

    def test_actual_baseline_keyed_preparation_uses_prospective_proof_without_repack(self):
        self.check_baseline_keyed_preparation(windows_writer=False)

    def test_actual_baseline_windows_prompt_recovers_without_repack(self):
        self.check_baseline_keyed_preparation(windows_writer=True)

    def check_baseline_keyed_preparation(self, *, windows_writer):
        from bridge_store import bridge_root, load_events, verify_thread_integrity
        baseline = os.environ.get("BRIDGE_B4_BASELINE_SKILLS")
        if not baseline:
            self.skipTest("Exact immutable baseline owner is supplied by local acceptance")
        owner = Path(baseline)
        helper = owner/"bundle-algorithm-context/scripts/prepare_codex_session_notes.py"
        prepare = owner/"gpt-pro-question-window/scripts/prepare_review.py"
        job = self.submit()
        env = {**os.environ,"PYTHONPATH":os.pathsep.join((str(SCRIPTS.parents[1]/".shared"),str(SCRIPTS.parents[1]/"bundle-algorithm-context/scripts")))}
        result = subprocess.run([sys.executable,str(helper),"--repo",str(self.repo),"--bridge-thread-id","runtime-test",
            "--goal","Test","--summary-file",str(self.repo/"notes.md"),"--gpt-pro-question","请原样回答 OK",
            "--round-key",job.data["job_id"]],env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        events = load_events(bridge_root(self.repo),"runtime-test")
        self.assertNotIn("input_proof",events[0]["data"])
        ledger = bridge_root(self.repo)/"threads/runtime-test.jsonl"
        prior = ledger.read_bytes()
        packaged = subprocess.run([sys.executable,str(prepare),"--request",self.h["request_file"]],
                                 env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(packaged.returncode,0,packaged.stderr)
        prep = json.loads(packaged.stdout)
        if windows_writer:
            prompt = Path(prep["prompt_file"])
            prompt.write_bytes(prompt.read_bytes().replace(b"\n",b"\r\n"))
        frozen_prompt = Path(prep["prompt_file"]).read_bytes()
        job.update(state="blocked",stage="prepared-materials",browser_phase="connecting-browser",error="legacy connection interruption",preparation=prep)
        state,client,browser,capture = self.browser_double()
        actual_helper = pipeline.run_helper
        calls = []
        def checked_helper(script,args,*extra,**kw):
            calls.append(Path(script).name)
            return actual_helper(script,args,*extra,**kw)
        with patch.multiple(pipeline,Client=client,Browser=browser,capture=capture),patch.object(pipeline,"run_helper",side_effect=checked_helper):
            pipeline.execute(job,self.config)
        self.assertEqual(job.data["state"],"complete")
        self.assertNotIn("prepare_review.py",calls)
        self.assertEqual(Path(prep["prompt_file"]).read_bytes(),frozen_prompt)
        self.assertTrue(ledger.read_bytes().startswith(prior))
        self.assertEqual(state["sent"],1)
        self.assertEqual(verify_thread_integrity(self.repo,"runtime-test")["event_count"],2)
        self.assertEqual(job.data["preparation_prompt_codec"],"legacy-windows-text/v1" if windows_writer else "utf8-exact")

    def test_disconnect_after_send_recovers_without_resend(self):
        job = self.submit()
        state, client, browser, capture = self.browser_double(crash_after_send=True)
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            with self.assertRaisesRegex(RuntimeError, "injected disconnect"):
                pipeline.execute(job, self.config)
            self.assertEqual(active_attempt(self.repo, "runtime-test")["state"], "send-started")
            pipeline.execute(Job(job.directory), self.config)
        self.assertEqual(state["sent"], 1)
        self.assertEqual(self.jobs.result(job.data["job_id"])["state"], "complete")

    def test_bootstrap_waits_past_local_optimistic_url_without_intervention(self):
        job = self.submit()
        state, client, original, capture = self.browser_double()
        class Browser(original):
            def messages(self, **kwargs):
                observation = super().messages(**kwargs)
                if state['sent'] and not state.get('provisional_seen'):
                    state['provisional_seen'] = True
                    observation['url'] = 'https://chatgpt.com/c/local-chatgpt%3Atemporary'
                return observation
        with patch.multiple(pipeline, Client=client, Browser=Browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertTrue(state['provisional_seen'])
        self.assertEqual(state['sent'], 1)
        self.assertEqual(self.jobs.result(job.data['job_id'])['state'], 'complete')
        self.assertEqual(job.data['conversation_url'], 'https://chatgpt.com/c/runtime-chat')

    def test_unknown_send_that_never_arrived_is_still_not_replayed(self):
        job = self.submit()
        state, client, browser, capture = self.browser_double(crash_after_send="before")
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            with self.assertRaisesRegex(RuntimeError, "before click"):
                pipeline.execute(job, self.config)
            self.assertEqual(active_attempt(self.repo, "runtime-test")["state"], "send-started")
            clock = iter((0, 0, 100, 200))
            with patch.object(pipeline.time, "monotonic", side_effect=lambda: next(clock)), self.assertRaises(BridgeError):
                pipeline.execute(Job(job.directory), self.config)
        self.assertEqual(state["sent"], 0)

    def session_file(self):
        return (bridge_root(self.repo) / "gpt-pro-sessions"
                / default_gpt_session_id("runtime-test") / "session.md")

    def write_session_metadata(self, **values):
        """Hand-write session metadata for field combinations the writer cannot emit."""
        path = self.session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, "".join(f"{key}: {json.dumps(value)}\n" for key, value in values.items()))

    def claim_spy(self, *, delegate=True, stop_after=None):
        """Record every browser-claim request, optionally aborting after the first."""
        calls = []
        real = pipeline.acquire_browser_lease
        def spy(repo, **kwargs):
            calls.append(kwargs)
            claim = real(repo, **kwargs) if delegate else None
            if stop_after:
                raise BridgeError(stop_after)
            return claim
        return calls, spy

    def test_failed_preparing_without_side_effects_resumes_same_job(self):
        job = self.submit()
        job.update(state="blocked", stage="preparing", error="BridgeError: fixture preparation failed")
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertEqual(job.data["state"], "complete")
        self.assertEqual(job.data["error"], "")
        self.assertEqual(state["sent"], 1)

    def test_failed_preparing_with_output_artifact_is_not_replayed(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        (output / "partial-bundle.zip").write_bytes(b"partial")
        job.update(state="blocked", stage="preparing", error="BridgeError: fixture preparation failed")
        with patch.object(pipeline, "Client") as client, self.assertRaisesRegex(
                BridgeError, "will not be replayed"):
            pipeline.execute(job, self.config)
        client.assert_not_called()

    def test_exact_project_identity_preflight_failure_is_resumable(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        artifacts = {}
        for key, name, content in (
                ("bundle", "bundle.zip", b"bundle"),
                ("prompt_file", "bundle.prompt.md", b"prompt"),
                ("materials_file", "bundle.materials.json", b"{}")):
            path = output / name
            path.write_bytes(content)
            artifacts[key] = str(path)
        bundle_sha = file_sha256(Path(artifacts["bundle"]))
        job.update(state="blocked", stage="prepared-materials", browser_phase="connected",
                   error=pipeline.ACCOUNT_WORKSPACE_PREFLIGHT_ERROR,
                   preparation={"ready": True, **artifacts, "bundle_sha256": bundle_sha,
                                "prompt_sha256": file_sha256(Path(artifacts["prompt_file"])),
                                "staging": {"source_sha256": bundle_sha}})
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        self.assertTrue(pipeline.identity_preflight_resume_allowed(job, output, handoff))

    def test_connected_identity_resume_rejects_unknown_error_or_ui_artifact(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        artifacts = {}
        for key, name in (("bundle", "bundle.zip"), ("prompt_file", "bundle.prompt.md"),
                          ("materials_file", "bundle.materials.json")):
            path = output / name
            path.write_text(key)
            artifacts[key] = str(path)
        bundle_sha = file_sha256(Path(artifacts["bundle"]))
        prep = {"ready": True, **artifacts, "bundle_sha256": bundle_sha,
                "prompt_sha256": file_sha256(Path(artifacts["prompt_file"])),
                "staging": {"source_sha256": bundle_sha}}
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        job.update(state="blocked", stage="prepared-materials", browser_phase="connected",
                   error="BridgeError: unknown connected failure", preparation=prep)
        self.assertFalse(pipeline.identity_preflight_resume_allowed(job, output, handoff))
        job.update(error=pipeline.ACCOUNT_WORKSPACE_PREFLIGHT_ERROR)
        (output / "model-controls.json").write_text("{}")
        self.assertFalse(pipeline.identity_preflight_resume_allowed(job, output, handoff))

    def _ui_overlay_fixture(self, job, output, handoff):
        artifacts = {}
        for key, name, content in (
                ("bundle", "bundle.zip", b"bundle"),
                ("prompt_file", "bundle.prompt.md", b"prompt"),
                ("materials_file", "bundle.materials.json", b"{}")):
            path = output / name
            path.write_bytes(content)
            artifacts[key] = str(path)
        bundle_sha = file_sha256(Path(artifacts["bundle"]))
        prep = {"ready": True, "sent": False, **artifacts, "bundle_sha256": bundle_sha,
                "prompt_sha256": file_sha256(Path(artifacts["prompt_file"])),
                "staging": {"source_sha256": bundle_sha}}
        job.update(state="blocked", stage="prepared-materials", browser_phase="connected",
                   error=pipeline.UI_PROFILE_PREFLIGHT_ERROR, preparation=prep)
        observation = self.repo / "ui-observation.json"
        ui = {
            "account": '[role="menu"] [role="menuitem"]:first-child',
            "workspace": '[role="menu"] [role="menuitem"]:first-child',
            "account_menu_labels": ["Open profile menu"],
            "attachment_chip": 'form [class~="group/composer-attachment"]',
            "attachment_labels": ["Add files and more"],
            "composer": 'form [role="textbox"][contenteditable="true"]',
            "control_layout": "nested-slider",
            "model": '[role="menuitemradio"][aria-checked="true"][data-model-selected="true"]',
            "model_menu_labels": ["Select model"],
            "send_labels": ["Send"],
            "thinking": '[role="menuitem"][aria-label="Select model"]',
            "thinking_keyboard_control": '[role="menuitem"][data-reasoning-slider][aria-label="Power"]',
            "thinking_label_location": "outer-menu",
            "thinking_menu_labels": ["Select ChatGPT model"],
            "thinking_placeholder_labels": ["Thinking effort"],
            "thinking_positions": {"6 Pro": 4},
            "thinking_slider": '[data-reasoning-slider] [role="slider"]',
            "upload_labels": ["Add photos & files Upload from computer"],
        }
        observation.write_text(json.dumps({
            "schema": "live-ui-observation/v2-controlled-draft",
            "job": {"job_id": job.data["job_id"], "bridge_thread_id": handoff["bridge_thread_id"],
                    "original_error": pipeline.UI_PROFILE_PREFLIGHT_ERROR},
            "identity": {"remote_project_id": "g-p-demo",
                         "project_url": "https://chatgpt.com/g/g-p-demo/project",
                         "bootstrap": True, "expected_conversation_id": "", "conversation_policy": "create"},
            "frozen_prompt": {"path": artifacts["prompt_file"],
                              "sha256": prep["prompt_sha256"]},
            "ui": ui,
            "send_observation": {"accessible_name": "Send", "count": 1, "type": "submit",
                                  "disabled": False, "aria_disabled": None,
                                  "same_form_as_composer": True, "send_clicked": False},
            "model": {"latest_checked_prior_receipt": True, "outer_visible_label": "6 Pro",
                      "power_description": "Pro, 5 of 5. Use Left and Right arrow keys to adjust power",
                      "value": 4},
            "before_draft": {"composer_empty": True, "attachment_count": 0},
            "controlled_fill": {"semantic_draft_equal_frozen_prompt_stripped": True,
                                "send_not_clicked": True},
            "after_clear": {"composer_empty": True, "attachment_count": 0,
                             "send_count": 0, "no_new_conversation": True},
            "profile": {"account_workspace_label": "oile ELIO Pro"},
            "account_binding": {"workspace": "oile ELIO Pro", "account_label": "oile ELIO Pro"},
            "source_receipts": {"target6pro": "target6pro.json", "source_ui_labels": "source-ui.json"},
        }, ensure_ascii=False), encoding="utf-8")
        overlay = {
            "schema_version": pipeline.UI_PROFILE_OVERLAY_SCHEMA,
            "job_id": job.data["job_id"], "bridge_thread_id": handoff["bridge_thread_id"],
            "project_url": "https://chatgpt.com/g/g-p-demo/project",
            "bootstrap": True, "expected_conversation_id": "",
            "original_error": pipeline.UI_PROFILE_PREFLIGHT_ERROR,
            "handoff_sha256": job.data["handoff_sha256"],
            "request_sha256": handoff["request_sha256"],
            "base_config_sha256": job.data["config_sha256"],
            "package_sha256": bundle_sha, "staging_sha256": bundle_sha,
            "observation_artifact_path": str(observation),
            "observation_artifact_sha256": file_sha256(observation), "ui": ui,
            "account_binding": {"workspace": "oile ELIO Pro", "account_label": "oile ELIO Pro"},
        }
        write_json(output / pipeline.UI_PROFILE_OVERLAY_FILE, overlay)
        return prep, overlay

    def test_ui_profile_overlay_resume_accepts_known_checkpoint(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        self._ui_overlay_fixture(job, output, handoff)
        self.assertTrue(pipeline.ui_profile_resume_allowed(job, output, handoff))

    def test_ui_profile_overlay_only_changes_in_memory_ui_keys(self):
        config = {"browser_command": ["sentinel"], "browser_env": {"X": "1"},
                  "browser_profile": "chrome-stable-default", "ui": {"model": "old"}}
        overlay = {"ui": {"model": "new", "thinking_label_location": "outer-menu"}}
        merged = pipeline.apply_ui_profile_overlay(config, overlay)
        self.assertEqual(merged["ui"], {"model": "new", "thinking_label_location": "outer-menu"})
        self.assertEqual(merged["browser_command"], config["browser_command"])
        self.assertEqual(merged["browser_env"], config["browser_env"])

    def test_ui_profile_overlay_rejects_sha_drift_and_connected_side_effects(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        self._ui_overlay_fixture(job, output, handoff)
        overlay_path = output / pipeline.UI_PROFILE_OVERLAY_FILE
        overlay = json.loads(overlay_path.read_text())
        overlay["package_sha256"] = "0" * 64
        write_json(overlay_path, overlay)
        self.assertFalse(pipeline.ui_profile_resume_allowed(job, output, handoff))
        overlay["package_sha256"] = job.data["preparation"]["bundle_sha256"]
        write_json(overlay_path, overlay)
        (output / "model-controls.json").write_text("{}")
        self.assertFalse(pipeline.ui_profile_resume_allowed(job, output, handoff))
        (output / "model-controls.json").unlink()
        job.update(error="BridgeError: unknown connected failure")
        self.assertFalse(pipeline.ui_profile_resume_allowed(job, output, handoff))

    def test_bootstrap_messages_are_rejected_before_ui_side_effects(self):
        class Browser:
            def messages(self):
                self.message_calls = getattr(self, "message_calls", 0) + 1
                return {"messages": [{"id": "existing"}]}
        browser = Browser()
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        resolved = {"action": "reuse-owned-tab", "url": "https://chatgpt.com/g/g-p-demo/project",
                    "matching_page_count": 1, "owner_selected_count": 1}
        with self.assertRaisesRegex(BridgeError, "unexpectedly contains conversation messages"):
            pipeline.bootstrap_preflight_observation(browser, resolved, handoff, bootstrap=True)
        self.assertEqual(browser.message_calls, 1)

    def test_ui_profile_overlay_rejects_incomplete_live_send_observation(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        self._ui_overlay_fixture(job, output, handoff)
        observation = self.repo / "ui-observation.json"
        payload = json.loads(observation.read_text(encoding="utf-8"))
        payload["send_observation"]["accessible_name"] = ""
        observation.write_text(json.dumps(payload), encoding="utf-8")
        overlay_path = output / pipeline.UI_PROFILE_OVERLAY_FILE
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        overlay["observation_artifact_sha256"] = file_sha256(observation)
        write_json(overlay_path, overlay)
        self.assertFalse(pipeline.ui_profile_resume_allowed(job, output, handoff))

    def test_ui_profile_overlay_rejects_disabled_send_observation(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        self._ui_overlay_fixture(job, output, handoff)
        observation = self.repo / "ui-observation.json"
        payload = json.loads(observation.read_text(encoding="utf-8"))
        payload["send_observation"]["disabled"] = True
        observation.write_text(json.dumps(payload), encoding="utf-8")
        overlay_path = output / pipeline.UI_PROFILE_OVERLAY_FILE
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        overlay["observation_artifact_sha256"] = file_sha256(observation)
        write_json(overlay_path, overlay)
        self.assertFalse(pipeline.ui_profile_resume_allowed(job, output, handoff))

    def test_bootstrap_overlay_rejects_mutable_conversation_and_noncanonical_url(self):
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        with self.assertRaisesRegex(BridgeError, "exact canonical"):
            pipeline.assert_bootstrap_resolution(
                {"action": "reuse-owned-tab", "url": "https://chatgpt.com/g/g-p-demo/project?tab=chats",
                 "matching_page_count": 1, "owner_selected_count": 1}, handoff)
        with self.assertRaisesRegex(BridgeError, "mutable session metadata"):
            pipeline.frozen_bootstrap_conversation_id("old-conversation", {"bootstrap": True,
                                                                            "expected_conversation_id": ""})

    def test_adopted_overlay_revalidates_for_capture_and_sha_drift(self):
        job = self.submit()
        output = Path(self.h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
        output.mkdir(parents=True)
        handoff = {**self.h, "remote_project_id": "g-p-demo"}
        self._ui_overlay_fixture(job, output, handoff)
        overlay_path = output / pipeline.UI_PROFILE_OVERLAY_FILE
        overlay_data = json.loads(overlay_path.read_text(encoding="utf-8"))
        job.update(ui_profile_overlay_receipt={"overlay_path": str(overlay_path),
                                                "overlay_sha256": file_sha256(overlay_path),
                                                "observation_artifact_path": overlay_data["observation_artifact_path"],
                                                "observation_artifact_sha256": overlay_data["observation_artifact_sha256"]})
        adopted = pipeline.load_adopted_ui_profile_overlay(job, handoff)
        self.assertEqual(adopted["ui"]["send_labels"], ["Send"])
        job.update(ui_profile_overlay_receipt={"overlay_path": str(overlay_path),
                                                "overlay_sha256": "0" * 64,
                                                "observation_artifact_path": overlay_data["observation_artifact_path"],
                                                "observation_artifact_sha256": overlay_data["observation_artifact_sha256"]})
        with self.assertRaisesRegex(BridgeError, "digest drift"):
            pipeline.load_adopted_ui_profile_overlay(job, handoff)

    def test_new_session_claim_is_a_bootstrap(self):
        job = self.submit()
        calls, spy = self.claim_spy()
        state, client, browser, capture = self.browser_double()
        with patch.object(pipeline, "acquire_browser_lease", spy), \
                patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertEqual(self.jobs.result(job.data["job_id"])["state"], "complete")
        self.assertEqual(calls[0]["expected_conversation_id"], "")
        self.assertIs(calls[0]["bootstrap"], True)
        self.assertTrue(Path(self.session_file()).is_file())

    def test_followup_round_claims_recorded_conversation_without_bootstrap(self):
        job = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertEqual(self.jobs.result(job.data["job_id"])["state"], "complete")
        # The canonical writer is the only producer of this metadata; read it back verbatim.
        self.assertEqual(parse_metadata(self.session_file())["web_conversation_url"],
                         "https://chatgpt.com/c/runtime-chat")
        calls, spy = self.claim_spy(stop_after="claim observed")
        followup = self.submit("followup")
        with patch.object(pipeline, "acquire_browser_lease", spy), \
                patch.object(pipeline, "Client") as blocked_client:
            with self.assertRaisesRegex(BridgeError, "claim observed"):
                pipeline.execute(followup, self.config)
            blocked_client.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["expected_conversation_id"], "runtime-chat")
        self.assertIs(calls[0]["bootstrap"], False)
        self.assertEqual(followup.data["browser_phase"], "acquiring-claim")

    def test_legacy_session_aliases_still_pin_the_conversation(self):
        for index, values in enumerate(({"web_url": "https://chatgpt.com/c/legacy-chat-0"},
                                        {"expected_conversation_id": "legacy-chat-1"},
                                        {"web_conversation_url": "https://chatgpt.com/c/legacy-chat-2",
                                         "expected_conversation_id": "legacy-chat-2"})):
            with self.subTest(values=sorted(values)):
                self.setUp()
                self.write_session_metadata(**values)
                job = self.submit(f"legacy-{index}")
                calls, spy = self.claim_spy(delegate=False, stop_after="claim observed")
                with patch.object(pipeline, "acquire_browser_lease", spy), \
                        patch.object(pipeline, "Client") as blocked_client:
                    with self.assertRaisesRegex(BridgeError, "claim observed"):
                        pipeline.execute(job, self.config)
                    blocked_client.assert_not_called()
                self.assertEqual(calls[0]["expected_conversation_id"], f"legacy-chat-{index}")
                self.assertIs(calls[0]["bootstrap"], False)

    def test_conflicting_session_conversation_fields_fail_loud(self):
        for index, values in enumerate(({"web_conversation_url": "https://chatgpt.com/c/canonical-chat",
                                         "web_url": "https://chatgpt.com/c/legacy-chat"},
                                        {"web_conversation_url": "https://chatgpt.com/c/canonical-chat",
                                         "expected_conversation_id": "legacy-chat"})):
            with self.subTest(values=sorted(values)):
                self.setUp()
                self.write_session_metadata(**values)
                job = self.submit(f"conflict-{index}")
                calls, spy = self.claim_spy()
                with patch.object(pipeline, "acquire_browser_lease", spy), \
                        patch.object(pipeline, "Client") as blocked_client:
                    with self.assertRaisesRegex(BridgeError, "pins conflicting conversations"):
                        pipeline.execute(job, self.config)
                    blocked_client.assert_not_called()
                self.assertEqual(calls, [])
                self.assertNotIn("browser_phase", job.data)

    def test_recovery_keeps_attempt_conversation_over_session_metadata(self):
        job = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(job, self.config)
        self.assertEqual(self.jobs.result(job.data["job_id"])["state"], "complete")
        prompt = Path(job.data["preparation"]["prompt_file"]).read_text(encoding="utf-8")
        attempt = prepare_attempt(
            self.repo, "runtime-test", prompt,
            {"ready": True, "bridge_thread_id": "runtime-test",
             "browser_claim_verification": "verified", "turn_boundary_verification": "verified",
             "prompt_sha256": digest(prompt), "pre_submit_boundary": "new-conversation",
             "tab_owner_token": "owner-recovery", "expected_project_id": "",
             "expected_conversation_id": "", "observed_page_url": "https://chatgpt.com/"})
        mark_send_started(self.repo, "runtime-test", attempt["attempt_id"])
        record_submission(self.repo, "runtime-test", attempt["attempt_id"],
                          conversation_url="https://chatgpt.com/c/attempt-chat",
                          owner="owner-recovery", prompt_sha256=digest(prompt),
                          boundary="new-conversation", remote_turn_id="turn-1", after_boundary="yes")
        self.write_session_metadata(web_conversation_url="https://chatgpt.com/c/session-chat")
        recovered = Job(job.directory)
        # The finished round left no live claim behind; recovery must still take the
        # pinned conversation from its own attempt rather than from saved metadata.
        recovered.update(attempt_id="", claim_token="")
        calls, spy = self.claim_spy(delegate=False, stop_after="claim observed")
        with patch.object(pipeline, "acquire_browser_lease", spy), \
                patch.object(pipeline, "Client") as blocked_client:
            with self.assertRaisesRegex(BridgeError, "claim observed"):
                pipeline.execute(recovered, self.config)
            blocked_client.assert_not_called()
        self.assertEqual(calls[0]["expected_conversation_id"], "attempt-chat")
        self.assertIs(calls[0]["bootstrap"], False)

    def test_session_conversation_id_rejects_unusable_urls(self):
        with self.assertRaisesRegex(BridgeError, "web_conversation_url is unusable"):
            pipeline.session_conversation_id({"web_conversation_url": "https://chatgpt.com/g/g-demo"})
        self.assertEqual(pipeline.session_conversation_id({}), "")
        self.assertEqual(pipeline.session_conversation_id({"web_conversation_url": ""}), "")
        self.assertEqual(pipeline.session_conversation_id({"expected_conversation_id": "", "web_url": ""}), "")

    def uploaded_crash(self, job, **seed):
        """Reach the production state: verified upload, worker crash, no Send."""
        state, client, browser, capture = self.browser_double(crash_after_upload=True, **seed)
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            with self.assertRaisesRegex(RpcError, "Ambiguous evaluate_script result"):
                pipeline.execute(job, self.config)
        # run_worker records a crashed job exactly like this; stage stays at the upload.
        job.update(state="blocked", may_resend=False,
                   error="RpcError: Ambiguous evaluate_script result")
        self.assertEqual(job.data["stage"], "uploaded")
        self.assertEqual(job.data["browser_phase"], "connected")
        self.assertIsNone(active_attempt(self.repo, "runtime-test"))
        return state

    def upload_output(self, job):
        prepared = job.data["preparation"]
        output = Path(prepared["bundle"]).parent
        for name in ("upload-plan.json", "upload-result.transport.json", "upload-result.json",
                     "model-controls.json"):
            self.assertTrue((output / name).is_file(), name)
        return prepared, output

    def test_uploaded_stage_resumes_read_only_without_replaying_the_upload(self):
        first = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(first, self.config)
        self.assertEqual(self.jobs.result(first.data["job_id"])["state"], "complete")
        conversation_url, owner = state["url"], state["owner"]
        job = self.submit("upload", attachment_policy="bundle")
        crashed = self.uploaded_crash(job, url=conversation_url, owner=owner)
        self.assertEqual(crashed["uploads"], 1)
        self.assertNotIn("attempt_id", job.data)
        prepared, output = self.upload_output(job)
        frozen_preparation = dict(prepared)
        controls_sha = file_sha256(output / "model-controls.json")
        staged = prepared["staging"]
        resumed = Job(job.directory)
        state2, client2, browser2, capture2 = self.browser_double(
            url=conversation_url, owner=owner,
            attachments=[{"name": staged["attachment_name"], "busy": False}])
        helpers = []
        real_helper = pipeline.run_helper
        def spy_helper(script, args, repo, *, json_output=True):
            helpers.append(Path(script).name)
            return real_helper(script, args, repo, json_output=json_output)
        with patch.object(pipeline, "run_helper", spy_helper), \
                patch.multiple(pipeline, Client=client2, Browser=browser2, capture=capture2):
            pipeline.execute(resumed, self.config)
        self.assertNotIn("prepare_review.py", helpers)
        # Re-entering preparation validates/reuses the same round snapshot;
        # the consumed seed snapshot must never stand in for this round.
        self.assertIn("prepare_codex_session_notes.py", helpers)
        self.assertIn("check_browser_preflight.py", helpers)
        self.assertEqual(state2["uploads"], 0)
        self.assertEqual(state2["sent"], 1)
        self.assertEqual(self.jobs.result(resumed.data["job_id"])["state"], "complete")
        self.assertEqual(resumed.data["preparation"], frozen_preparation)
        self.assertEqual(file_sha256(output / "model-controls.json"), controls_sha)
        self.assertEqual(state2["prompt"],
                         Path(frozen_preparation["prompt_file"]).read_text(encoding="utf-8"))

    def test_uploaded_resume_adopts_only_exact_frozen_draft(self):
        first = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(first, self.config)
        job = self.submit("uploaded-draft", attachment_policy="bundle")
        self.uploaded_crash(job, url=state["url"], owner=state["owner"])
        prepared, _ = self.upload_output(job)
        prompt = Path(prepared["prompt_file"]).read_text(encoding="utf-8")
        resumed = Job(job.directory)
        state2, client2, browser2, capture2 = self.browser_double(
            url=state["url"], owner=state["owner"], prompt=prompt,
            attachments=[{"name": prepared["staging"]["attachment_name"], "busy": False}])
        with patch.object(browser2, "fill_prompt", side_effect=AssertionError("must not refill")), \
                patch.multiple(pipeline, Client=client2, Browser=browser2, capture=capture2):
            pipeline.execute(resumed, self.config)
        self.assertEqual(state2["uploads"], 0)
        self.assertEqual(state2["sent"], 1)
        self.assertEqual(state2["prompt"], prompt)
        self.assertEqual(resumed.data["state"], "complete")

    def test_uploaded_resume_rejects_different_draft_without_changes(self):
        first = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(first, self.config)
        job = self.submit("uploaded-other-draft", attachment_policy="bundle")
        self.uploaded_crash(job, url=state["url"], owner=state["owner"])
        prepared, _ = self.upload_output(job)
        state2, client2, browser2, capture2 = self.browser_double(
            url=state["url"], owner=state["owner"], prompt="unrelated draft",
            attachments=[{"name": prepared["staging"]["attachment_name"], "busy": False}])
        with patch.object(browser2, "fill_prompt", side_effect=AssertionError("must not overwrite")), \
                patch.multiple(pipeline, Client=client2, Browser=browser2, capture=capture2):
            with self.assertRaisesRegex(BridgeError, "Existing draft"):
                pipeline.execute(Job(job.directory), self.config)
        self.assertEqual(state2["uploads"], 0)
        self.assertEqual(state2["sent"], 0)
        self.assertEqual(state2["prompt"], "unrelated draft")

    def test_uploaded_resume_requires_the_single_ready_attachment(self):
        first = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(first, self.config)
        conversation_url, owner = state["url"], state["owner"]
        job = self.submit("upload-busy", attachment_policy="bundle")
        self.uploaded_crash(job, url=conversation_url, owner=owner)
        prepared, _ = self.upload_output(job)
        resumed = Job(job.directory)
        state2, client2, browser2, capture2 = self.browser_double(url=conversation_url, owner=owner,
            attachments=[{"name": prepared["staging"]["attachment_name"], "busy": True}])
        with patch.multiple(pipeline, Client=client2, Browser=browser2, capture=capture2):
            with self.assertRaisesRegex(BridgeError, "unique ready attachment"):
                pipeline.execute(resumed, self.config)
        self.assertEqual(state2["uploads"], 0)
        self.assertEqual(state2["sent"], 0)

    def test_uploaded_resume_rejects_a_tampered_upload_receipt(self):
        first = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(first, self.config)
        conversation_url, owner = state["url"], state["owner"]
        job = self.submit("upload-tampered", attachment_policy="bundle")
        self.uploaded_crash(job, url=conversation_url, owner=owner)
        prepared, output = self.upload_output(job)
        tampered = json.loads((output / "upload-result.json").read_text(encoding="utf-8"))
        tampered["attachment_name"] = "someone-elses-bundle.zip"
        write_json(output / "upload-result.json", tampered)
        resumed = Job(job.directory)
        state2, client2, browser2, capture2 = self.browser_double(url=conversation_url, owner=owner,
            attachments=[{"name": prepared["staging"]["attachment_name"], "busy": False}])
        with patch.multiple(pipeline, Client=client2, Browser=browser2, capture=capture2):
            with self.assertRaisesRegex(BridgeError, "not the verified receipt for its plan"):
                pipeline.execute(resumed, self.config)
        self.assertEqual(state2["uploads"], 0)
        self.assertEqual(state2["sent"], 0)

    def test_uploaded_stage_without_receipts_still_requires_inspection(self):
        first = self.submit()
        state, client, browser, capture = self.browser_double()
        with patch.multiple(pipeline, Client=client, Browser=browser, capture=capture):
            pipeline.execute(first, self.config)
        conversation_url, owner = state["url"], state["owner"]
        job = self.submit("upload-missing", attachment_policy="bundle")
        self.uploaded_crash(job, url=conversation_url, owner=owner)
        prepared, output = self.upload_output(job)
        (output / "upload-result.transport.json").unlink()
        resumed = Job(job.directory)
        with patch.object(pipeline, "Client") as blocked:
            with self.assertRaisesRegex(BridgeError, "requires inspection"):
                pipeline.execute(resumed, self.config)
            blocked.assert_not_called()
        self.assertEqual(resumed.data["state"], "blocked")
        self.assertEqual(resumed.data["stage"], "uploaded")

    def test_uploaded_resume_refuses_a_bootstrap_upload_without_a_conversation(self):
        job = self.submit(attachment_policy="bundle")
        state = self.uploaded_crash(job)
        self.assertEqual(state["uploads"], 1)
        prepared, output = self.upload_output(job)
        resumed = Job(job.directory)
        state2, client2, browser2, capture2 = self.browser_double(url=state["url"], owner=state["owner"],
            attachments=[{"name": prepared["staging"]["attachment_name"], "busy": False}])
        with patch.multiple(pipeline, Client=client2, Browser=browser2, capture=capture2):
            with self.assertRaisesRegex(BridgeError, "not pinned to one exact conversation"):
                pipeline.execute(resumed, self.config)
        self.assertEqual(state2["uploads"], 0)
        self.assertEqual(state2["sent"], 0)

    def test_clipboard_baseline_rejects_real_read_errors(self):
        from capture_copied_reply import CaptureError
        from unittest.mock import Mock
        read = Mock(side_effect=CaptureError("Windows clipboard read failed with exit 3"))
        with self.assertRaisesRegex(CaptureError, "exit 3"):
            pipeline.clipboard_baseline(read, CaptureError)
        read.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
