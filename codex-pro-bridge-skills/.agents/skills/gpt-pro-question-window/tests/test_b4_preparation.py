"""No browser: early admission and stage-only recovery of exact packaging."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILLS = Path(__file__).resolve().parents[2]
SCRIPTS = SKILLS / "gpt-pro-question-window/scripts"
sys.path[:0] = [str(SCRIPTS), str(SKILLS / ".shared")]
from bridge_store import BridgeError, file_sha256
from prepare_review import read_request, verify_packaging, verify_preparation


class PreparationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "notes.md").write_text("frozen notes\n")
        (self.repo / "evidence.md").write_text("exact evidence\n")
        self.request = self.repo / "request.json"
        self.data = {"repo":str(self.repo),"bridge_thread_id":"prepare-test","goal":"fixture",
            "question":"Read `evidence.md`", "notes":"notes.md", "files":["evidence.md"],
            "file_digests":{v:file_sha256(self.repo/v) for v in ("notes.md","evidence.md")}}
        self.request.write_text(json.dumps(self.data))
        self.bundle = self.repo / "bundle.zip"
        self.receipt = self.repo / "packaging.json"
        self.env = {**os.environ,"CODEX_PRO_BRIDGE_HOST_CONFIG":"",
            "CODEX_BRIDGE_STAGING_WSL_ROOT":str(self.root/"staging"),
            "CODEX_BRIDGE_STAGING_WINDOWS_ROOT":r"C:\B4Fixture", "CODEX_BRIDGE_STAGING_LOCK":str(self.root/"stage.lock")}

    def call(self, *flags, fail_stage=False, env=None):
        argv = ["--request",str(self.request),"--out",str(self.bundle),"--packaging-receipt",str(self.receipt),*flags]
        if fail_stage:
            code = (f"import sys,runpy;sys.path.insert(0,{str(SKILLS/'.shared')!r});"
                    "from unittest.mock import patch;from bridge_store import BridgeError;"
                    "p=patch('browser_host.stage_browser_file',side_effect=BridgeError('injected stage failure'));p.start();"
                    f"runpy.run_path({str(SCRIPTS/'prepare_review.py')!r},run_name='__main__')")
            command = [sys.executable,"-c",code,*argv]
        else:
            command = [sys.executable,str(SCRIPTS/"prepare_review.py"),*argv]
        return subprocess.run(command,env=env or self.env,capture_output=True,text=True,timeout=30)

    def test_missing_host_mapping_fails_before_any_packaging(self):
        env = dict(self.env)
        env.update(CODEX_BRIDGE_STAGING_WSL_ROOT="",CODEX_BRIDGE_STAGING_WINDOWS_ROOT="")
        result = self.call("--stage",env=env)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(self.receipt.exists())

    def test_codex_and_secret_paths_are_not_broadly_exempt(self):
        for relative in (".codex/private.md", ".codex/codex-pro-bridge/codex-sessions/x/credentials.md"):
            path = self.repo/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text("fixture")
            self.data["notes"] = relative
            self.data.pop("file_digests",None)
            self.request.write_text(json.dumps(self.data))
            result = self.call()
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertFalse(self.bundle.exists())

    def test_stage_failure_resumes_same_hashes_without_repackaging(self):
        failed = self.call("--stage",fail_stage=True)
        self.assertNotEqual(failed.returncode,0)
        self.assertIn("injected stage failure",failed.stdout)
        frozen = {p:(file_sha256(p),p.stat().st_mtime_ns) for p in
                  (self.bundle,self.bundle.with_suffix(".prompt.md"),self.bundle.with_suffix(".materials.json"),self.receipt)}
        resumed = self.call("--stage","--reuse-packaging")
        self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["staging"]["ready"])
        self.assertEqual(frozen,{p:(file_sha256(p),p.stat().st_mtime_ns) for p in frozen})
        staged_receipt = self.receipt.with_suffix(".staging.json")
        stage_before = staged_receipt.read_bytes()
        repeated = self.call("--stage","--reuse-packaging")
        self.assertEqual(repeated.returncode,0,repeated.stdout)
        self.assertEqual(stage_before,staged_receipt.read_bytes())

    def test_packaging_digest_input_and_owner_drift_are_specific(self):
        result = self.call()
        self.assertEqual(result.returncode,0,result.stdout)
        frozen = json.loads(self.receipt.read_text())
        request = read_request(self.request)
        for field in ("request_sha256","input_sha256","code_sha256","bundle_sha256","materials_sha256"):
            changed = {**frozen,field:"changed"}
            with self.subTest(field=field),self.assertRaisesRegex(BridgeError,"drift"):
                verify_packaging(changed,self.request,request)
        (self.repo/"evidence.md").write_text("drift")
        with patch("prepare_review.subprocess.run") as builder:
            with self.assertRaisesRegex(BridgeError,"input_sha256"):
                verify_packaging(frozen,self.request,request)
            builder.assert_not_called()

    def test_staging_receipt_consumed_fields_cannot_drift(self):
        result = self.call("--stage")
        self.assertEqual(result.returncode,0,result.stdout)
        path = self.receipt.with_suffix(".staging.json")
        original = json.loads(path.read_text())
        for key in ("attachment_name","staged_sha256","source_path","staged_browser_path","source_sha256"):
            path.write_text(json.dumps({**original,key:"changed"}))
            resumed = self.call("--stage","--reuse-packaging")
            self.assertNotEqual(resumed.returncode,0,(key,resumed.stdout))
        path.write_text(json.dumps(original))

    def test_new_and_legacy_preparation_verify_all_needed_artifacts(self):
        result = self.call("--stage")
        self.assertEqual(result.returncode,0,result.stdout)
        prep = json.loads(result.stdout)
        legacy = {key:value for key,value in prep.items() if key not in
                  {"schema_version","packaging_verified","request_sha256","input_sha256","code_sha256","materials_sha256"}}
        request = read_request(self.request)
        with patch.dict(os.environ,self.env):
            verify_preparation(legacy,self.request,request)
            for field in ("bundle","materials_file"):
                path = Path(prep[field]); original = path.read_bytes();path.write_bytes(b"drift")
                for candidate in (prep,legacy):
                    with self.subTest(field=field,new=candidate is prep),self.assertRaises((BridgeError,ValueError)):
                        verify_preparation(candidate,self.request,request)
                path.write_bytes(original)
            path = Path(prep["staging"]["staged_execution_path"])
            path.write_bytes(b"drift")
            for candidate in (prep,legacy):
                with self.assertRaisesRegex(BridgeError,"SHA-256"):
                    verify_preparation(candidate,self.request,request)

    def test_new_receipt_cannot_downgrade_and_legacy_prompt_still_binds_request(self):
        result = self.call("--stage")
        self.assertEqual(result.returncode,0,result.stdout)
        prep = json.loads(result.stdout)
        request = read_request(self.request)
        with patch.dict(os.environ,self.env):
            for field in ("schema_version","packaging_verified","code_sha256"):
                changed = {k:v for k,v in prep.items() if k != field}
                with self.subTest(field=field),self.assertRaisesRegex(BridgeError,"receipt/envelope drift"):
                    verify_preparation(changed,self.request,request,packaging_receipt_path=self.receipt)
            legacy = {k:v for k,v in prep.items() if k not in {"schema_version","packaging_verified"}}
            Path(prep["prompt_file"]).write_text("Question B")
            legacy["prompt_sha256"] = file_sha256(Path(prep["prompt_file"]))
            with self.assertRaisesRegex(BridgeError,"prompt/request bytes drift"):
                verify_preparation(legacy,self.request,request)

    def test_only_qualified_legacy_text_writer_accepts_exact_windows_newlines(self):
        result = self.call("--stage")
        self.assertEqual(result.returncode,0,result.stdout)
        prep = json.loads(result.stdout)
        request = read_request(self.request)
        prompt = Path(prep["prompt_file"])
        original = prompt.read_bytes()
        legacy = {k:v for k,v in prep.items() if k not in
                  {"schema_version","packaging_verified","request_sha256","input_sha256","code_sha256","materials_sha256"}}
        # The frozen baseline writer opened a text file without newline= and
        # hashed the LF string before Windows translated each LF on write.
        output = io.BytesIO()
        writer = io.TextIOWrapper(output,encoding="utf-8",newline="\r\n")
        writer.write(original.decode("utf-8"));writer.flush()
        windows_bytes = output.getvalue()
        writer.detach()
        self.assertNotEqual(windows_bytes,original)
        self.assertEqual(legacy["prompt_sha256"],hashlib.sha256(original).hexdigest())
        prompt.write_bytes(windows_bytes)
        with patch.dict(os.environ,self.env):
            checked = verify_preparation(legacy,self.request,request)
            self.assertEqual(checked,{"prompt":original.decode("utf-8"),"codec":"legacy-windows-text/v1"})
            with self.assertRaisesRegex(BridgeError,"prompt/request bytes drift"):
                verify_preparation(prep,self.request,request,packaging_receipt_path=self.receipt)
            with self.assertRaisesRegex(BridgeError,"prompt/request bytes drift"):
                verify_preparation(legacy,self.request,request,packaging_receipt_path=self.receipt)
            for changed in (windows_bytes.replace(b"Read",b"Wrong"),
                            windows_bytes.replace(b"\r\n",b"\n",1),
                            windows_bytes.replace(b"\r\n",b"\r\r\n",1),windows_bytes+b" "):
                prompt.write_bytes(changed)
                candidate = {**legacy,"prompt_sha256":hashlib.sha256(changed).hexdigest()}
                with self.subTest(changed=changed[:20]),self.assertRaisesRegex(BridgeError,"prompt/request bytes drift"):
                    verify_preparation(candidate,self.request,request)
            prompt.write_bytes(windows_bytes)
            with self.assertRaisesRegex(BridgeError,"prompt/request bytes drift"):
                verify_preparation({**legacy,"prompt_sha256":hashlib.sha256(windows_bytes).hexdigest()},self.request,request)
            prompt.write_bytes(original)
            self.assertEqual(verify_preparation(prep,self.request,request,packaging_receipt_path=self.receipt)["codec"],"utf8-exact")


if __name__ == "__main__":
    unittest.main()
