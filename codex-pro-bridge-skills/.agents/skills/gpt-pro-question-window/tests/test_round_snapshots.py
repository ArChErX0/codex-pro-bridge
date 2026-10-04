"""Round-scoped notes and narrowly evidenced append-only legacy repair."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

SKILLS = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SKILLS / ".shared"), str(SKILLS / "gpt-pro-question-window/scripts")]
from bridge_store import (BridgeError, SCHEMA_VERSION, append_event, atomic_write_text, bridge_root,
                          file_sha256, load_events, now_iso, verify_thread_integrity)
from recover_codex_snapshot import recover
from b4_fixtures import exchange_proof
from project_store import BridgeProjectStore


class RoundSnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.thread = "test-rounds"
        self.ledger = bridge_root(self.repo) / "threads/test-rounds.jsonl"

    def snapshot(self, key, summary="frozen notes", question="Question"):
        return subprocess.run([sys.executable,
            str(SKILLS / "bundle-algorithm-context/scripts/prepare_codex_session_notes.py"),
            "--repo", str(self.repo), "--bridge-thread-id", self.thread,
            "--round-key", key, "--summary", summary, "--gpt-pro-question", question],
            capture_output=True, text=True)

    def exchange(self, suffix="one", *, inject_legacy=False):
        snapshots = [e for e in load_events(bridge_root(self.repo), self.thread)
                     if e.get("event_type") == "codex-snapshot" and not e.get("data", {}).get("recovery_for_exchange")]
        source = snapshots[-1].get("data", {}).get("input_proof", {}).get("source_notes") if snapshots else None
        bundle = self.repo / f"{suffix}.zip"
        with zipfile.ZipFile(bundle, "w") as z:
            z.writestr("context/codex-session-notes.md", (self.repo / source["path"]).read_bytes() if source and not inject_legacy else b"original notes\n")
        artifact = self.repo / f"{suffix}.md"
        artifact.write_text("raw answer\n")
        kwargs = dict(thread_id=self.thread, event_type="gpt-exchange", actor="gpt-pro",
            gpt_pro_session_id="test-rounds-gpt-pro",
            codex_session_id="test-rounds-codex", artifact={"kind":"gpt-pro-turn", "path":artifact.name,
            "sha256":file_sha256(artifact)}, data={"bundle":bundle.name, "bundle_sha256":file_sha256(bundle)})
        if not inject_legacy:
            snapshots = [e for e in load_events(bridge_root(self.repo), self.thread)
                         if e.get("event_type") == "codex-snapshot" and not e.get("data", {}).get("recovery_for_exchange")]
            if snapshots and snapshots[-1].get("data", {}).get("round_key"):
                kwargs["data"]["round_key"] = snapshots[-1]["data"]["round_key"]
            if snapshots:
                kwargs["data"].update(exchange_proof(self.repo,snapshots[-1]))
                kwargs["data"]["snapshot_inputs_sha256"] = snapshots[-1]["data"].get("inputs_sha256", "")
                kwargs["data"]["snapshot_request_sha256"] = (snapshots[-1]["data"].get("input_proof", {}).get("request") or {}).get("sha256", "")
        return self.inject_legacy(**kwargs) if inject_legacy else append_event(self.repo, **kwargs)

    def inject_legacy(self, **kwargs):
        """Only fixture injection may construct historical invalid ledgers."""
        events = load_events(bridge_root(self.repo), self.thread)
        event = dict(kwargs, schema_version=SCHEMA_VERSION, event_id=f"legacy-{len(events)}",
                     parent_event_id=events[-1]["event_id"] if events else "", occurred_at=now_iso())
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        return event

    def verdict(self, *, inject_legacy=False):
        exchange = next(e for e in reversed(load_events(bridge_root(self.repo), self.thread))
                        if e["event_type"] == "gpt-exchange")
        path = self.repo / (exchange["artifact"]["path"] + ".verdict.md")
        path.write_text("verified locally\n")
        kwargs = dict(thread_id=self.thread, event_type="codex-verdict", actor="codex",
                      codex_session_id="test-rounds-codex", gpt_pro_session_id="test-rounds-gpt-pro",
                      artifact={"kind":"codex-verdict", "path":path.name, "sha256":file_sha256(path)},
                      data={"turn":exchange["artifact"]["path"]})
        return self.inject_legacy(**kwargs) if inject_legacy else append_event(self.repo, **kwargs)

    def test_second_round_has_new_snapshot_and_retry_is_idempotent(self):
        self.assertEqual(self.snapshot("job-one").returncode, 0)
        self.exchange()
        self.verdict()
        self.assertEqual(self.snapshot("job-two", question="Second question").returncode, 0)
        before = self.ledger.read_bytes()
        self.assertEqual(self.snapshot("job-two", question="Second question").returncode, 0)
        self.assertEqual(before, self.ledger.read_bytes())
        changed = self.snapshot("job-two", summary="different", question="Second question")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("inputs changed", changed.stderr)
        self.assertEqual(before, self.ledger.read_bytes())
        self.exchange("two")
        self.verdict()
        report = verify_thread_integrity(self.repo, self.thread, require_complete_rounds=True)
        self.assertEqual(report["complete_rounds"], 2)
        self.assertEqual(report["recovered_snapshot_count"], 0)

    def test_new_round_rejects_pending_verdict(self):
        self.assertEqual(self.snapshot("job-one").returncode, 0)
        self.exchange()
        before = self.ledger.read_bytes()
        result = self.snapshot("job-two")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending Codex verdict", result.stderr)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_consumed_round_key_cannot_supply_a_later_snapshot(self):
        self.assertEqual(self.snapshot("job-one").returncode, 0)
        self.exchange()
        self.verdict()
        before = self.ledger.read_bytes()
        result = self.snapshot("job-one")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already consumed", result.stderr)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_new_inferred_project_freezes_effective_identity_and_rejects_empty_proof(self):
        store = BridgeProjectStore(self.repo)
        store.create_project("project")
        store.attach_thread("project",self.thread)
        result = self.snapshot("job-one")
        self.assertEqual(result.returncode,0,result.stderr)
        snapshot = load_events(bridge_root(self.repo),self.thread)[0]
        self.assertEqual(snapshot["bridge_project_id"],"project")
        proof = copy.deepcopy(snapshot["data"]["input_proof"])
        inputs = json.loads((self.repo/proof["inputs"]["path"]).read_text())
        self.assertEqual(inputs["project"],"project")
        before = self.ledger.read_bytes()
        self.assertEqual(self.snapshot("job-one").returncode,0)
        self.assertEqual(before,self.ledger.read_bytes())
        inputs["project"] = ""
        contents = json.dumps(inputs,ensure_ascii=False,sort_keys=True)
        altered = self.repo/"altered-inputs.json"
        altered.write_text(contents)
        proof["inputs"] = {"path":altered.name,"sha256":file_sha256(altered)}
        with self.assertRaisesRegex(BridgeError,"input/source identity"):
            append_event(self.repo,thread_id=self.thread,event_type="codex-snapshot",actor="codex",
                bridge_project_id="project",codex_session_id="test-rounds-codex",artifact=snapshot["artifact"],
                data={"inputs_sha256":hashlib.sha256(contents.encode()).hexdigest(),"input_proof":proof})
        self.assertEqual(before,self.ledger.read_bytes())

    def inferred_legacy_checkpoint(self):
        baseline = os.environ.get("BRIDGE_B4_BASELINE_SKILLS")
        if not baseline:
            self.skipTest("Exact immutable baseline owner is supplied by local acceptance")
        store = BridgeProjectStore(self.repo)
        store.create_project("project")
        store.attach_thread("project",self.thread)
        notes = self.repo/"source-notes.md"
        notes.write_text("frozen notes\n")
        request = self.repo/"request.json"
        request.write_text(json.dumps({"repo":str(self.repo),"bridge_thread_id":self.thread,"goal":"fixture",
            "question":"Question","notes":notes.name,"files":[],"context_policy":"none","max_files":0}))
        handoff = self.repo/"handoff.json"
        handoff.write_text(json.dumps({"repo":str(self.repo),"bridge_thread_id":self.thread,
            "bridge_project_id":"project","request_sha256":file_sha256(request)}))
        key = hashlib.sha256((str(self.repo)+"\n"+file_sha256(handoff)).encode()).hexdigest()
        common = ["--repo",str(self.repo),"--bridge-thread-id",self.thread,"--round-key",key,"--goal","fixture",
                  "--summary-file",str(notes),"--gpt-pro-question","Question"]
        baseline_result = subprocess.run([sys.executable,str(Path(baseline)/"bundle-algorithm-context/scripts/prepare_codex_session_notes.py"),*common],
            env={**os.environ,"PYTHONPATH":str(SKILLS/".shared")},capture_output=True,text=True,timeout=20)
        self.assertEqual(baseline_result.returncode,0,baseline_result.stderr)
        snapshot = load_events(bridge_root(self.repo),self.thread)[0]
        self.assertNotIn("input_proof",snapshot["data"])
        receipt = self.repo/"prospective-receipt.json"
        command = [sys.executable,str(SKILLS/"bundle-algorithm-context/scripts/prepare_codex_session_notes.py"),*common,
                   "--round-request-file",str(request),"--round-handoff-file",str(handoff),"--input-receipt-out",str(receipt)]
        return snapshot,receipt,command

    def legacy_candidate(self,snapshot,receipt_path):
        receipt = json.loads(receipt_path.read_text())
        turn = self.repo/"turn.md"
        turn.write_text("raw fixture answer\n")
        proof = receipt["input_proof"]
        return dict(thread_id=self.thread,event_type="gpt-exchange",actor="gpt-pro",bridge_project_id="project",
            codex_session_id="test-rounds-codex",gpt_pro_session_id="test-rounds-gpt-pro",
            artifact={"kind":"gpt-pro-turn","path":turn.name,"sha256":file_sha256(turn)},data={
                "round_key":snapshot["data"]["round_key"],"snapshot_inputs_sha256":snapshot["data"]["inputs_sha256"],
                "snapshot_request_sha256":proof["request"]["sha256"],"raw_prompt":proof["delivery_prompt"],
                "prompt_sha256":proof["delivery_prompt"]["sha256"],
                "notes_reference":{k:v for k,v in snapshot["artifact"].items() if k != "kind"},
                "snapshot_input_receipt":{"path":receipt_path.name,"sha256":file_sha256(receipt_path)}})

    def test_legacy_inferred_project_session_proof_is_frozen_and_history_survives_binding_change(self):
        snapshot,path,command = self.inferred_legacy_checkpoint()
        before = self.ledger.read_bytes()
        result = subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(before,self.ledger.read_bytes())
        receipt = json.loads(path.read_text())
        inputs = json.loads((self.repo/receipt["input_proof"]["inputs"]["path"]).read_text())
        self.assertEqual(inputs["project"],"")
        self.assertEqual(receipt["inputs_sha256"],snapshot["data"]["inputs_sha256"])
        self.assertEqual(receipt["project_inference"]["binding_kind"],"session-metadata/v1")
        for missing in ("project_inference",):
            changed = copy.deepcopy(receipt);changed.pop(missing)
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(BridgeError,"input/source identity"):
                append_event(self.repo,**self.legacy_candidate(snapshot,path))
            self.assertEqual(before,self.ledger.read_bytes())
        changed = copy.deepcopy(receipt);changed["project_inference"].pop("binding")
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(BridgeError,"inference receipt is invalid"):
            append_event(self.repo,**self.legacy_candidate(snapshot,path))
        path.write_text(json.dumps(receipt))
        append_event(self.repo,**self.legacy_candidate(snapshot,path))
        session = bridge_root(self.repo)/"codex-sessions/test-rounds-codex/session.md"
        session.write_text(session.read_text().replace('"project"','"another-project"'))
        self.assertEqual(verify_thread_integrity(self.repo,self.thread)["event_count"],2)
        self.assertTrue(self.ledger.read_bytes().startswith(before))

    def test_legacy_inferred_project_store_only_proof_is_consumed(self):
        snapshot,path,command = self.inferred_legacy_checkpoint()
        session = bridge_root(self.repo)/"codex-sessions/test-rounds-codex/session.md"
        session.rename(session.with_suffix(".retained.md"))
        result = subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(path.read_text())["project_inference"]["binding_kind"],"store-activity/v1")
        append_event(self.repo,**self.legacy_candidate(snapshot,path))
        store = BridgeProjectStore(self.repo)
        store.update_task("project",self.thread,title="changed mutable title")
        self.assertEqual(verify_thread_integrity(self.repo,self.thread)["event_count"],2)

    def test_legacy_inferred_project_missing_or_conflicting_current_evidence_fails(self):
        _,path,command = self.inferred_legacy_checkpoint()
        before = self.ledger.read_bytes()
        session = bridge_root(self.repo)/"codex-sessions/test-rounds-codex/session.md"
        original = session.read_bytes()
        session.write_bytes(original.replace(b'"project"',b'"another-project"'))
        result = subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("binding disagree",result.stderr)
        self.assertFalse(path.exists())
        session.write_bytes(original)
        session.rename(session.with_suffix(".retained.md"))
        projects = bridge_root(self.repo)/"projects"
        projects.rename(bridge_root(self.repo)/"retained-projects")
        result = subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("lacks an existing binding",result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(before,self.ledger.read_bytes())

    def test_pipeline_checks_round_snapshot_even_with_existing_session_or_prepared_materials(self):
        from bridge_runtime import pipeline
        self.assertEqual(self.snapshot("previous-job").returncode, 0)
        self.exchange()
        self.verdict()
        handoff = {"repo": str(self.repo), "bridge_thread_id": self.thread,
                   "expected_output_dir": str(self.repo / "output"), "mode": "prepare-and-run", "request_file":"fixture-request.json"}
        (self.repo / "notes.md").write_text("fixture notes")
        request = {"repo":str(self.repo), "files":[], "goal": "test", "notes": str(self.repo / "notes.md"), "question": "next"}
        for stage in ("queued", "prepared-materials"):
            job = Mock()
            job.data = {"stage": stage, "job_id": "next-job", "handoff_path": "unused", "handoff_sha256": "unused"}
            with patch.object(pipeline, "validate_inputs", return_value=(handoff, request)), \
                    patch.object(pipeline, "active_attempt", return_value=None), \
                    patch.object(pipeline, "run_helper", side_effect=RuntimeError("snapshot boundary")) as helper:
                with self.assertRaisesRegex(RuntimeError, "snapshot boundary"):
                    pipeline.execute(job, {})
            call = helper.call_args.args
            self.assertEqual(call[0].name, "prepare_codex_session_notes.py")
            self.assertEqual(call[1][-2:], ["--round-key", "next-job"])

    def broken_round(self):
        target = self.exchange(inject_legacy=True)
        self.verdict(inject_legacy=True)
        return target

    def repair(self, target):
        return recover(self.repo, self.thread, target["event_id"], apply=True,
                       expected_sha=file_sha256(self.ledger))

    def test_repair_preserves_original_bytes_and_is_idempotent(self):
        target = self.broken_round()
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(BridgeError, "no available Codex snapshot"):
            verify_thread_integrity(self.repo, self.thread, require_complete_rounds=True)
        plan = recover(self.repo, self.thread, target["event_id"])
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(before, self.ledger.read_bytes())
        result = self.repair(target)
        self.assertEqual(result["verification"]["recovered_snapshot_count"], 1)
        self.assertTrue(self.ledger.read_bytes().startswith(before))
        repaired = self.ledger.read_bytes()
        self.assertEqual(self.repair(target)["status"], "already-recovered")
        self.assertEqual(repaired, self.ledger.read_bytes())
        self.assertEqual(self.snapshot("next-job").returncode, 0)
        self.exchange("next")
        self.verdict()
        self.assertEqual(verify_thread_integrity(self.repo, self.thread, require_complete_rounds=True)["complete_rounds"], 2)

    def test_repair_rejects_wrong_reviewed_ledger(self):
        target = self.broken_round()
        with self.assertRaisesRegex(BridgeError, "current ledger SHA"):
            recover(self.repo, self.thread, target["event_id"], apply=True, expected_sha="0"*64)

    def test_repair_does_not_bypass_other_invalid_history(self):
        target = self.broken_round()
        self.verdict(inject_legacy=True)  # Explicitly injected bad historical verdict.
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(BridgeError, "no pending GPT exchange"):
            self.repair(target)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_repair_rejects_redundant_snapshot(self):
        self.assertEqual(self.snapshot("first").returncode, 0)
        target = self.broken_round()
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(BridgeError, "redundant"):
            self.repair(target)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_recovered_notes_must_match_sent_bundle_even_if_rehashed(self):
        target = self.broken_round()
        self.repair(target)
        events = load_events(bridge_root(self.repo), self.thread)
        artifact = events[-1]["artifact"]
        (self.repo / artifact["path"]).write_text("invented replacement")
        artifact["sha256"] = file_sha256(self.repo / artifact["path"])
        atomic_write_text(self.ledger, "".join(json.dumps(e)+"\n" for e in events))
        with self.assertRaisesRegex(BridgeError, "differs from sent bundle"):
            verify_thread_integrity(self.repo, self.thread)

    def test_append_rejects_changed_parent(self):
        self.broken_round()
        with self.assertRaisesRegex(BridgeError, "Ledger changed"):
            append_event(self.repo, thread_id=self.thread, event_type="codex-snapshot", actor="codex",
                         expected_parent_event_id="old-parent")

    def test_corrupt_sent_bundle_is_not_recovered(self):
        target = self.broken_round()
        (self.repo / "one.zip").write_bytes(b"changed")
        with self.assertRaisesRegex(BridgeError, "digest"):
            self.repair(target)

    def test_original_summary_change_does_not_change_prior_round_proof(self):
        source = self.repo/"source-notes.md"
        source.write_text("first source\n")
        command = [sys.executable,str(SKILLS/"bundle-algorithm-context/scripts/prepare_codex_session_notes.py"),
                   "--repo",str(self.repo),"--bridge-thread-id",self.thread,"--round-key","first-job","--summary-file",str(source)]
        result = subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.exchange()
        self.verdict()
        source.write_text("later changed source\n")
        report = verify_thread_integrity(self.repo,self.thread,require_complete_rounds=True)
        self.assertEqual(report["complete_rounds"],1)

    def test_saver_actual_prompt_and_notes_cannot_rebind_same_key(self):
        self.assertEqual(self.snapshot("job-a",summary="input A",question="Question A").returncode,0)
        before = self.ledger.read_bytes()
        wrong = self.repo/"unrelated.md"
        wrong.write_text("input B")
        base = [sys.executable,str(SKILLS/"gpt-pro-question-window/scripts/save_bridge_turn.py"),
                "--repo",str(self.repo),"--bridge-thread-id",self.thread,"--round-key","job-a",
                "--web-url","https://chatgpt.com/c/conv","--expected-conversation-id","conv",
                "--remote-turn-id","user","--capture-route","native-read-thread","--answer","raw answer"]
        for flags,error in ((["--prompt","Question B"],"actual prompt differs"),
                            (["--prompt","Question A","--codex-notes",str(wrong)],"actual notes reference differs")):
            result = subprocess.run([*base,*flags],capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertIn(error,result.stderr)
            self.assertEqual(before,self.ledger.read_bytes())


if __name__ == "__main__":
    unittest.main()
