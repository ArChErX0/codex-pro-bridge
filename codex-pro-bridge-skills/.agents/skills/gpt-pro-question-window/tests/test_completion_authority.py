import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS.parents[1] / ".shared")]
from bridge_store import BridgeError, append_event, bridge_root, file_sha256, load_events
from bridge_runtime.completion import validated_completion
from bridge_runtime.jobs import Jobs, write_json
from b4_fixtures import exchange_proof, input_proof


class CompletionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "answer.md").write_text("exact answer")
        (self.repo / "turn.md").write_text("immutable turn")
        prompt = "exact question"
        self.attempt = {"state":"captured", "attempt_id":"attempt", "prompt":prompt,
                        "prompt_sha256":hashlib.sha256(prompt.encode()).hexdigest(),
                        "conversation_id":"conversation", "conversation_url":"https://chatgpt.com/c/conversation",
                        "remote_turn_id":"user-turn", "capture":{"path":"turn.md", "sha256":file_sha256(self.repo / "turn.md")}}
        self.attempt["capture_fingerprint"] = hashlib.sha256(json.dumps({
            "attempt_id":"attempt", "thread_id":"thread", "remote_turn_id":"user-turn",
            "prompt":prompt, "answer":"exact answer", "answer_format":"copied-markdown"
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.job = {"repo":str(self.repo), "bridge_thread_id":"thread", "attempt_id":"attempt",
                    "job_id":"a"*64,"state":"blocked","stage":"capturing","updated_at":"now", "may_resend":False,
                    "preparation":{"prompt_sha256":self.attempt["prompt_sha256"]},
                    "capture":{"answer_path":str(self.repo/"answer.md"),"answer_sha256":file_sha256(self.repo/"answer.md")}}
        (self.repo / "notes.md").write_text("snapshot")
        snapshot = append_event(self.repo, thread_id="thread", event_type="codex-snapshot", actor="codex",
            codex_session_id="thread-codex", artifact={"kind":"codex-notes", "path":"notes.md",
            "sha256":file_sha256(self.repo / "notes.md")},data=input_proof(self.repo,"thread","thread-codex",self.repo/"notes.md",question=prompt))
        append_event(self.repo, thread_id="thread", event_type="gpt-exchange", actor="gpt-pro",
            codex_session_id="thread-codex", gpt_pro_session_id="thread-gpt-pro",
            artifact={"kind":"gpt-pro-turn", **self.attempt["capture"]}, data={
            "attempt_id":"attempt", "answer_sha256":self.job["capture"]["answer_sha256"],
            "snapshot_inputs_sha256":snapshot["data"]["inputs_sha256"],**exchange_proof(self.repo,snapshot),
            "prompt_sha256":self.attempt["prompt_sha256"], "remote_turn_id":"user-turn",
            "observed_conversation_id":"conversation", "capture_route":"browser-fallback", "answer_format":"copied-markdown",
            "response_completed_at":"2026-09-30T10:00:00+00:00"})

    def test_stale_blocked_envelope_reconciles_without_write_or_spawn(self):
        jobs = Jobs.__new__(Jobs)
        jobs.root = self.repo / "jobs"
        directory = jobs.directory(self.job["job_id"])
        directory.mkdir(parents=True)
        write_json(directory / "job.json", self.job)
        before = (directory / "job.json").read_bytes()
        with patch("bridge_attempts.read_attempt", return_value=self.attempt), \
                patch("bridge_runtime.completion.read_attempt", return_value=self.attempt), \
                patch.object(jobs, "_spawn") as spawn:
            result = jobs.resume(self.job["job_id"])
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["reconciled_from"], "canonical-captured-attempt")
            spawn.assert_not_called()
        self.assertEqual(before, (directory / "job.json").read_bytes())

    def test_changed_answer_and_wrong_job_prompt_are_rejected(self):
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            self.job["preparation"]["prompt_sha256"] = "wrong"
            with self.assertRaisesRegex(BridgeError, "another prepared"):
                validated_completion(self.job)
            self.job["preparation"]["prompt_sha256"] = self.attempt["prompt_sha256"]
            (self.repo / "answer.md").write_text("corrupted")
            with self.assertRaisesRegex(BridgeError, "digest drift"):
                validated_completion(self.job)

    def test_cached_result_cannot_override_canonical_identity(self):
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            self.job["result"] = {"remote_turn_id":"wrong-turn"}
            with self.assertRaisesRegex(BridgeError, "Cached job result"):
                validated_completion(self.job)

    def test_fingerprint_ties_answer_to_actual_capture(self):
        self.attempt["capture_fingerprint"] = "wrong"
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            with self.assertRaisesRegex(BridgeError, "fingerprint"):
                validated_completion(self.job)

    def test_uncaptured_attempt_cannot_be_complete(self):
        self.attempt["state"] = "waiting"
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            with self.assertRaisesRegex(BridgeError, "not captured"):
                validated_completion(self.job)

    def test_missing_envelope_receipt_without_canonical_raw_is_specific(self):
        self.job.pop("capture")
        with patch("bridge_runtime.completion.read_attempt",return_value=self.attempt):
            with self.assertRaisesRegex(BridgeError,"no raw answer receipt or canonical raw evidence"):
                validated_completion(self.job)

    def test_full_ledger_invalid_history_cannot_be_hidden_by_valid_exchange(self):
        events = load_events(bridge_root(self.repo),"thread")
        ledger = bridge_root(self.repo)/"threads/thread.jsonl"
        ledger.write_text(json.dumps({**events[1],"parent_event_id":""})+"\n")
        with patch("bridge_runtime.completion.read_attempt",return_value=self.attempt):
            with self.assertRaisesRegex(BridgeError,"no available Codex snapshot"):
                validated_completion(self.job)

    def test_nested_private_metadata_cannot_reach_public_status(self):
        jobs = Jobs.__new__(Jobs)
        jobs.root = self.repo/"jobs"
        directory = jobs.directory(self.job["job_id"])
        directory.mkdir(parents=True)
        self.job["capture"]["copy_provenance"] = {"owner_token":"synthetic-private-marker"}
        write_json(directory/"job.json",self.job)
        with patch("bridge_attempts.read_attempt",return_value=self.attempt),patch("bridge_runtime.completion.read_attempt",return_value=self.attempt):
            result = jobs.status(self.job["job_id"])
        self.assertEqual(result["state"],"blocked")
        self.assertIn("metadata must be a string",result["completion_validation_error"])
        self.assertNotIn("synthetic-private-marker",json.dumps(result))
        self.job["capture"]["copy_provenance"] = "invented-copy-route"
        with patch("bridge_runtime.completion.read_attempt",return_value=self.attempt),self.assertRaisesRegex(BridgeError,"invalid Copy provenance"):
            validated_completion(self.job)

    def test_string_metadata_uses_canonical_time_and_cannot_invent_optional_proof_or_identity(self):
        marker = "synthetic-private-marker"
        self.job["capture"].update(completed_at=marker,capture_proof_path=marker,
            capture_proof_sha256=marker,assistant_turn_id=marker,copy_provenance="visible-copy-write/v1")
        jobs = Jobs.__new__(Jobs)
        jobs.root = self.repo/"jobs"
        directory = jobs.directory(self.job["job_id"])
        directory.mkdir(parents=True)
        write_json(directory/"job.json",self.job)
        with patch("bridge_attempts.read_attempt",return_value=self.attempt),patch("bridge_runtime.completion.read_attempt",return_value=self.attempt):
            result = jobs.status(self.job["job_id"])
        self.assertEqual(result["state"],"complete")
        self.assertEqual(result["result"]["completed_at"],"2026-09-30T10:00:00+00:00")
        self.assertEqual(result["result"]["copy_provenance"],"visible-copy-write/v1")
        for field in ("capture_proof_path","capture_proof_sha256","assistant_turn_id"):
            self.assertNotIn(field,result["result"])
        self.assertNotIn(marker,json.dumps(result))
        self.job["result"] = {"completed_at":marker}
        with patch("bridge_runtime.completion.read_attempt",return_value=self.attempt),self.assertRaisesRegex(BridgeError,"Cached job result"):
            validated_completion(self.job)

    def test_round_closure_survives_a_later_pending_round(self):
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            pending = validated_completion(self.job)
        self.assertEqual(pending["local_verdict"], "pending")
        self.assertFalse(pending["ledger_round_complete"])
        self.assertFalse(pending["ledger_thread_complete"])

        verdict = self.repo / "verdict.md"
        verdict.write_text("Reviewed this exact captured turn")
        append_event(self.repo, thread_id="thread", event_type="codex-verdict", actor="codex",
            codex_session_id="thread-codex", gpt_pro_session_id="thread-gpt-pro",
            artifact={"kind":"codex-verdict", "path":"verdict.md", "sha256":file_sha256(verdict)},
            data={"turn":"turn.md"})
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            complete = validated_completion(self.job)
        self.assertTrue(complete["ledger_round_complete"])
        self.assertTrue(complete["ledger_thread_complete"])

        later_notes = self.repo / "later-notes.md"
        later_notes.write_text("A different, not-yet-captured round")
        append_event(self.repo, thread_id="thread", event_type="codex-snapshot", actor="codex",
            codex_session_id="thread-codex",
            artifact={"kind":"codex-notes", "path":"later-notes.md", "sha256":file_sha256(later_notes)},
            data=input_proof(self.repo,"thread","thread-codex",later_notes,question="next question"))
        # An older cached aggregate false remains a mutable report field.
        self.job["result"] = {"ledger_round_complete":False, "ledger_thread_complete":True}
        before = (bridge_root(self.repo) / "threads/thread.jsonl").read_bytes()
        with patch("bridge_runtime.completion.read_attempt", return_value=self.attempt):
            earlier = validated_completion(self.job)
        self.assertEqual(earlier["local_verdict"], "recorded")
        self.assertTrue(earlier["ledger_round_complete"])
        self.assertFalse(earlier["ledger_thread_complete"])
        self.assertEqual(before, (bridge_root(self.repo) / "threads/thread.jsonl").read_bytes())


if __name__ == "__main__":
    unittest.main()
