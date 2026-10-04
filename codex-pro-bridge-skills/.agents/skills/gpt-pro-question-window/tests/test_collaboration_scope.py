from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import unittest

import test_prepare_bridge_execution as preparation_tests

REMOTE_A, REMOTE_B = preparation_tests.REMOTE_A, preparation_tests.REMOTE_B

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "gpt-pro-question-window/scripts"))
from bridge_store import BridgeError, file_sha256
from bridge_runtime.jobs import validate_inputs
from prepare_review import read_request


class CollaborationScopeTests(unittest.TestCase):
    setUp = preparation_tests.PrepareBridgeExecutionTests.setUp
    tearDown = preparation_tests.PrepareBridgeExecutionTests.tearDown
    call = preparation_tests.PrepareBridgeExecutionTests.call

    def scoped(self, root="root-a", owner="/root", remote=REMOTE_A, *extra):
        result = self.call("--codex-root-thread-id", root, "--owner-agent-id", owner,
                           "--target-project-url", f"https://chatgpt.com/g/{remote}/project",
                           *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_two_roots_do_not_rebind_legacy_or_each_other(self):
        a = self.scoped()
        b = self.scoped("root-b", remote=REMOTE_B)
        self.assertNotEqual(a["bridge_project_id"], b["bridge_project_id"])
        self.assertNotEqual(a["bridge_thread_id"], b["bridge_thread_id"])
        self.assertEqual(self.store.load_binding("research")["remote_project_id"], REMOTE_A)
        self.assertEqual(self.store.load_binding(a["bridge_project_id"])["remote_project_id"], REMOTE_A)

    def test_owners_are_distinct_and_followup_keeps_thread(self):
        first = self.scoped()
        child = self.scoped(owner="/root/5_6sm_numerics")
        self.assertNotEqual(first["bridge_thread_id"], child["bridge_thread_id"])
        self.assertEqual(first["bridge_project_id"], child["bridge_project_id"])
        self.assertEqual(first, self.scoped())
        self.question.write_text("A follow-up with different evidence questions.\n", encoding="utf-8")
        followup = self.scoped()
        self.assertEqual(first["bridge_thread_id"], followup["bridge_thread_id"])
        self.assertNotEqual(first["handoff_file"], followup["handoff_file"])
        self.assertTrue(Path(first["handoff_file"]).is_file())

    def test_first_consultation_requires_project_instead_of_standalone(self):
        result = self.call("--codex-root-thread-id", "root-a", "--owner-agent-id", "/root")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("create-and-bind-project", result.stderr)
        self.assertEqual(len(self.store.list_project_ids()), 2)
        self.assertFalse((self.repo / ".codex/codex-pro-bridge/executor-preparations").exists())

    def test_root_and_owner_must_be_paired(self):
        result = self.call("--codex-root-thread-id", "root-a")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.store.list_project_ids(), ["research"])

    def test_explicit_other_owners_thread_rejected(self):
        first = self.scoped()
        result = self.call("--codex-root-thread-id", "root-a", "--owner-agent-id", "/root/child",
                           "--target-project-url", f"https://chatgpt.com/g/{REMOTE_A}/project",
                           "--bridge-thread-id", first["bridge_thread_id"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

    def test_same_owner_concurrent_preparation_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: self.scoped(), range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(len(self.store.task_states(receipts[0]["bridge_project_id"])), 1)

    def test_rebind_does_not_reuse_old_project_conversation(self):
        first = self.scoped()
        second = self.scoped(remote=REMOTE_B)
        self.assertEqual(first["bridge_project_id"], second["bridge_project_id"])
        self.assertNotEqual(first["bridge_thread_id"], second["bridge_thread_id"])

    def test_request_and_handoff_scope_drift_rejected(self):
        receipt = self.scoped()
        request_path = Path(receipt["request_file"])
        request = json.loads(request_path.read_text())
        read_request(request_path)
        request["owner_agent_id"] = "/root/other"
        forged = self.repo / "forged.json"
        forged.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "owner"):
            read_request(forged)
        request.pop("owner_agent_id")
        request.pop("codex_root_thread_id")
        forged.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "root"):
            read_request(forged)
        handoff = json.loads(Path(receipt["handoff_file"]).read_text())
        handoff["owner_agent_id"] = "/root/other"
        forged.write_text(json.dumps(handoff), encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "disagree on owner_agent_id"):
            validate_inputs(forged, file_sha256(forged), {"allowed_repos": [str(self.repo)]})


if __name__ == "__main__":
    unittest.main()
