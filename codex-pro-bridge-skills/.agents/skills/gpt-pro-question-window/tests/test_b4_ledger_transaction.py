"""Real append lock qualification, including competing process candidates."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / ".shared"))
from bridge_store import BridgeError, append_event, bridge_root, file_sha256, load_events, verify_thread_integrity
from b4_fixtures import exchange_proof, input_proof


class LedgerTransactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        for name in ("snapshot", "turn", "verdict"):
            (self.repo / (name + ".md")).write_text(name)
        self.ledger = bridge_root(self.repo) / "threads/thread.jsonl"

    def event(self, kind, *, codex="thread-codex", data=None, key=""):
        stem, actor, artifact = {"codex-snapshot":("snapshot", "codex", "codex-notes"),
            "gpt-exchange":("turn", "gpt-pro", "gpt-pro-turn"),
            "codex-verdict":("verdict", "codex", "codex-verdict")}[kind]
        payload = dict(data or {})
        if kind == "codex-snapshot":
            payload.update(input_proof(self.repo,"thread",codex,self.repo/"snapshot.md"))
        elif kind == "gpt-exchange":
            snapshots = [e for e in load_events(bridge_root(self.repo),"thread") if e["event_type"] == "codex-snapshot"]
            if snapshots:
                payload.setdefault("snapshot_inputs_sha256",snapshots[-1]["data"]["inputs_sha256"])
                payload.update(exchange_proof(self.repo,snapshots[-1]))
        return dict(thread_id="thread", event_type=kind, actor=actor, codex_session_id=codex,
            gpt_pro_session_id="thread-gpt-pro" if kind != "codex-snapshot" else "",
            artifact={"kind":artifact,"path":stem + ".md", "sha256":file_sha256(self.repo / (stem + ".md"))},
            data=payload, dedupe_key=key)

    def snapshot(self, key=""):
        return append_event(self.repo, **self.event("codex-snapshot", key="codex-snapshot-round:" + key if key else "",
            data={"round_key":key,"inputs_sha256":"a"*64} if key else {}))

    def test_missing_snapshot_never_creates_ledger(self):
        with self.assertRaisesRegex(BridgeError, "no available Codex snapshot"):
            append_event(self.repo, **self.event("gpt-exchange"))
        self.assertFalse(self.ledger.exists())

    def test_pending_verdict_and_wrong_session_or_job_cannot_append(self):
        self.snapshot("job-a")
        before = self.ledger.read_bytes()
        for kwargs in (self.event("gpt-exchange", codex="other-codex", data={"round_key":"job-a"}),
                       self.event("gpt-exchange", data={"round_key":"job-b"}), self.event("gpt-exchange")):
            with self.subTest(kwargs=kwargs), self.assertRaises(BridgeError):
                append_event(self.repo, **kwargs)
            self.assertEqual(before, self.ledger.read_bytes())
        append_event(self.repo, **self.event("gpt-exchange", data={"round_key":"job-a"}))
        before = self.ledger.read_bytes()
        for kind in ("codex-snapshot", "gpt-exchange"):
            with self.assertRaises(BridgeError):
                append_event(self.repo, **self.event(kind))
            self.assertEqual(before, self.ledger.read_bytes())

    def test_artifact_drift_and_dedupe_payload_drift_fail_before_write(self):
        event = self.event("codex-snapshot", key="snapshot-key")
        append_event(self.repo, **event)
        before = self.ledger.read_bytes()
        changed = {**event,"data":{"summary":"changed"}}
        with self.assertRaisesRegex(BridgeError, "changed identity or payload"):
            append_event(self.repo, **changed)
        self.assertEqual(before, self.ledger.read_bytes())
        (self.repo / "snapshot.md").write_text("changed")
        with self.assertRaisesRegex(BridgeError, "hash mismatch"):
            append_event(self.repo, **event)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_missing_artifact_and_wrong_verdict_target_fail(self):
        kwargs = self.event("codex-snapshot")
        kwargs["artifact"] = {}
        with self.assertRaisesRegex(BridgeError, "artifact is required"):
            append_event(self.repo, **kwargs)
        self.snapshot()
        append_event(self.repo, **self.event("gpt-exchange"))
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(BridgeError, "pending GPT exchange"):
            append_event(self.repo, **self.event("codex-verdict", data={"turn":"wrong.md"}))
        self.assertEqual(before, self.ledger.read_bytes())

    def test_two_real_processes_cannot_consume_one_snapshot(self):
        self.snapshot("job-a")
        code = "import json,sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;from bridge_store import append_event;append_event(Path(sys.argv[2]),**json.loads(sys.argv[3]))"
        argv = [sys.executable,"-c",code,str(SKILLS / ".shared"),str(self.repo),
                json.dumps(self.event("gpt-exchange",data={"round_key":"job-a"}))]
        children = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [(*p.communicate(timeout=30),p.returncode) for p in children]
        self.assertEqual(sorted(r[2] for r in results), [0,1], results)
        self.assertIn("no available Codex snapshot", next(r[1] for r in results if r[2]))
        report = verify_thread_integrity(self.repo,"thread")
        self.assertEqual(report["event_count"], 2)

    def test_same_key_wrong_input_and_wrong_zip_notes_do_not_append(self):
        self.snapshot("job-a")
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(BridgeError,"input/request digest mismatch"):
            append_event(self.repo,**self.event("gpt-exchange",data={"round_key":"job-a","snapshot_inputs_sha256":"b"*64}))
        bundle = self.repo/"wrong.zip"
        with zipfile.ZipFile(bundle,"w") as archive:
            archive.writestr("context/codex-session-notes.md","different input")
        with self.assertRaisesRegex(BridgeError,"differ from frozen snapshot source"):
            append_event(self.repo,**self.event("gpt-exchange",data={"round_key":"job-a","bundle":bundle.name,"bundle_sha256":file_sha256(bundle)}))
        self.assertEqual(before,self.ledger.read_bytes())

    def test_new_candidates_cannot_omit_proof_to_downgrade(self):
        candidate = self.event("codex-snapshot")
        candidate["data"] = {}
        with self.assertRaisesRegex(BridgeError,"immutable input/source proof"):
            append_event(self.repo,**candidate)
        self.assertFalse(self.ledger.exists())

    def test_prospective_receipt_cannot_replace_new_snapshot_proof(self):
        import copy
        snapshot = self.snapshot("job-a")
        receipt = {"schema_version":"snapshot-inputs/v1","snapshot_event_id":snapshot["event_id"],
            "snapshot_artifact":snapshot["artifact"],"round_key":"job-a",
            "inputs_sha256":snapshot["data"]["inputs_sha256"],"input_proof":snapshot["data"]["input_proof"]}
        path = self.repo/"receipt.json"
        before = self.ledger.read_bytes()
        for field in ("inputs","request","delivery_prompt","source_notes"):
            changed = copy.deepcopy(receipt)
            changed["input_proof"][field] = {"path":"replacement.md","sha256":"b"*64}
            path.write_text(json.dumps(changed))
            with self.subTest(field=field),self.assertRaisesRegex(BridgeError,"cannot replace immutable"):
                append_event(self.repo,**self.event("gpt-exchange",data={"round_key":"job-a",
                    "snapshot_input_receipt":{"path":path.name,"sha256":file_sha256(path)}}))
            self.assertEqual(before,self.ledger.read_bytes())
        changed = copy.deepcopy(receipt)
        changed["input_proof"].pop("request")
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(BridgeError,"cannot replace immutable"):
            append_event(self.repo,**self.event("gpt-exchange",data={"round_key":"job-a",
                "snapshot_input_receipt":{"path":path.name,"sha256":file_sha256(path)}}))
        self.assertEqual(before,self.ledger.read_bytes())
        path.write_text(json.dumps(receipt))
        append_event(self.repo,**self.event("gpt-exchange",data={"round_key":"job-a",
            "snapshot_input_receipt":{"path":path.name,"sha256":file_sha256(path)}}))
        self.assertEqual(verify_thread_integrity(self.repo,"thread")["event_count"],2)

    def test_real_helper_proof_cannot_be_overridden_by_another_valid_request_or_policy(self):
        import copy
        from material_prompt import delivery_prompt
        for name in ("notes.md","evidence.md","other.md"):
            (self.repo/name).write_text("fixture source\n")
        request = {"repo":str(self.repo),"bridge_thread_id":"thread","goal":"fixture","question":"Question",
                   "notes":"notes.md","files":["evidence.md"],"context_policy":"explicit","max_files":1}
        original = self.repo/"request.json"
        original.write_text(json.dumps(request))
        result = subprocess.run([sys.executable,str(SKILLS/"bundle-algorithm-context/scripts/prepare_codex_session_notes.py"),
            "--repo",str(self.repo),"--bridge-thread-id","thread","--round-key","job-a","--goal","fixture",
            "--summary-file",str(self.repo/"notes.md"),"--gpt-pro-question","Question","--round-request-file",str(original)],
            capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        snapshot = load_events(bridge_root(self.repo),"thread")[0]
        receipt = {"schema_version":"snapshot-inputs/v1","snapshot_event_id":snapshot["event_id"],
            "snapshot_artifact":snapshot["artifact"],"round_key":"job-a",
            "inputs_sha256":snapshot["data"]["inputs_sha256"],"input_proof":snapshot["data"]["input_proof"]}
        receipt_path = self.repo/"receipt.json"
        altered_path,altered_prompt = self.repo/"altered-request.json",self.repo/"altered-prompt.md"
        before = self.ledger.read_bytes()
        for change in ({"files":["other.md"]},{"files":[],"context_policy":"none","max_files":0}):
            altered = {**request,**change}
            altered_path.write_text(json.dumps(altered))
            altered_prompt.write_text(delivery_prompt(altered))
            changed = copy.deepcopy(receipt)
            for field,path in (("request",altered_path),("delivery_prompt",altered_prompt)):
                changed["input_proof"][field] = {"path":path.name,"sha256":file_sha256(path)}
            receipt_path.write_text(json.dumps(changed))
            candidate = self.event("gpt-exchange",data={"round_key":"job-a",
                "snapshot_request_sha256":file_sha256(altered_path),
                "snapshot_input_receipt":{"path":receipt_path.name,"sha256":file_sha256(receipt_path)}})
            candidate["data"]["raw_prompt"] = changed["input_proof"]["delivery_prompt"]
            candidate["data"]["prompt_sha256"] = file_sha256(altered_prompt)
            with self.subTest(change=change),self.assertRaisesRegex(BridgeError,"cannot replace immutable"):
                append_event(self.repo,**candidate)
            self.assertEqual(before,self.ledger.read_bytes())
        receipt_path.write_text(json.dumps(receipt))
        candidate = self.event("gpt-exchange",data={"round_key":"job-a",
            "snapshot_request_sha256":file_sha256(original),
            "snapshot_input_receipt":{"path":receipt_path.name,"sha256":file_sha256(receipt_path)}})
        append_event(self.repo,**candidate)
        self.assertEqual(verify_thread_integrity(self.repo,"thread")["event_count"],2)


if __name__ == "__main__":
    unittest.main()
