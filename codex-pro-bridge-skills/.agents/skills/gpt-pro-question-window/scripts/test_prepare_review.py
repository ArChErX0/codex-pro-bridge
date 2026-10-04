"""真实子进程调用既有builder/staging；独立目录，不调用浏览器或模型。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("prepare_review.py")
BUILDER = SCRIPT.resolve().parents[2] / "bundle-algorithm-context/scripts/build_algorithm_bundle.py"
sys.path.insert(0, str(BUILDER.parent))
from build_algorithm_bundle import is_candidate  # noqa: E402


class PrepareReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "facts.md").write_text("当前原始说明；不能省略否定条件。\n")
        (self.root / "evidence.md").write_text("完整证据\n")
        self.request = {"repo": str(self.root), "bridge_thread_id": "fixture-review",
                        "goal": "不要省略条件；`echo x` $(echo y)", "question": "哪些结论不成立？",
                        "notes": "facts.md", "files": ["evidence.md"]}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, env=None):
        request_file = self.root / "request.json"
        request_file.write_text(json.dumps(self.request, ensure_ascii=False))
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--request", str(request_file), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_full_originals_and_no_git_metadata(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "unrelated-visible-only-in-git.txt").write_text("unrelated")
        content = ("正文不能截断，末尾例外仍属于用户要求。\n" * 2000).encode()
        (self.root / "evidence.md").write_bytes(content)
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        self.assertTrue(data["ready"])
        self.assertFalse(data["sent"])
        with zipfile.ZipFile(data["bundle"]) as archive:
            self.assertEqual(archive.read("source/evidence.md"), content)
            self.assertEqual(archive.read("context/codex-session-notes.md"), (self.root / "facts.md").read_bytes())
            self.assertNotIn("context/git-status.txt", archive.namelist())
            overview = archive.read("README_FOR_GPT_PRO.md").decode()
            self.assertIn(self.request["goal"], overview)
            self.assertNotIn("unrelated-visible-only-in-git.txt", overview)

    def test_builder_error_is_not_success(self):
        (self.root / ".env").write_text("fixture only")
        self.request["files"] = [".env"]
        result, data = self.call("--stage")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["phase"], "request")
        self.assertFalse(data["ready"])
        self.assertNotIn("staging", data)

    def test_explicit_frozen_input_raises_threshold_to_actual_largest_file(self):
        content = b"x" * 250_001
        (self.root / "evidence.md").write_bytes(content)
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        with zipfile.ZipFile(data["bundle"]) as archive:
            self.assertEqual(archive.read("source/evidence.md"), content)

    def test_explicit_fv_schemes_and_tsv_members_are_packed_with_digests(self):
        (self.root / "case" / "system").mkdir(parents=True)
        (self.root / "receipts").mkdir()
        fv_schemes = b"FoamFile\n{ format ascii; object fvSchemes; }\n"
        tsv = b"rank\tglobalCell\tfluxNet\n3\t356\t0\n"
        (self.root / "case" / "system" / "fvSchemes").write_bytes(fv_schemes)
        (self.root / "receipts" / "phase.tsv").write_bytes(tsv)
        self.request["files"] = ["case/system/fvSchemes", "receipts/phase.tsv"]
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        mapping = json.loads(Path(data["materials_file"]).read_text())
        with zipfile.ZipFile(data["bundle"]) as archive:
            self.assertEqual(sum(name.startswith("source/") for name in archive.namelist()), 2)
            for item in mapping["materials"]:
                source = self.root / item["source_path"]
                packed = archive.read(item["archive_path"])
                self.assertEqual(packed, source.read_bytes())
                self.assertEqual(hashlib.sha256(packed).hexdigest(), item["sha256"])
        self.assertEqual(mapping["bundle_sha256"], data["bundle_sha256"])

    def test_auto_context_keeps_extended_text_types_out_of_global_candidates(self):
        (self.root / "case" / "system").mkdir(parents=True)
        (self.root / "case" / "system" / "fvSchemes").write_text("object fvSchemes;\n")
        (self.root / "phase.tsv").write_text("rank\tvalue\n0\t1\n")
        self.request["context_policy"] = "auto"
        self.request["max_files"] = 4
        self.request["files"] = ["case/system/fvSchemes", "phase.tsv"]
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["phase"], "request")
        self.assertIn("Explicit evidence path is excluded by the safety policy", data["error"])

    def test_extended_candidate_requires_explicit_in_repo_path(self):
        (self.root / "phase.tsv").write_text("rank\tvalue\n0\t1\n")
        path = self.root / "phase.tsv"
        self.assertFalse(is_candidate(path, self.root, True))
        self.assertTrue(is_candidate(path, self.root, True, explicit_paths=[path]))
        with tempfile.TemporaryDirectory() as external_dir:
            external = Path(external_dir) / "external-phase.tsv"
            external.write_text("rank\tvalue\n0\t1\n")
            self.assertFalse(is_candidate(external, self.root, True, explicit_paths=[external]))

    def test_explicit_extended_admission_rejects_nontext_nul_secret_and_arbitrary_names(self):
        cases = [
            ("plain", b"extensionless but not an OpenFOAM dictionary\n", "excluded"),
            ("bad.tsv", b"\xff\xfe", "excluded"),
            ("nul.tsv", b"header\x00value\n", "excluded"),
            ("credentials.tsv", b"rank\tvalue\n0\t1\n", "excluded"),
            ("secret.tsv", b"-----BEGIN RSA PRIVATE KEY-----\n", "High-confidence"),
        ]
        for name, content, expected in cases:
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(content)
                self.request["files"] = [name]
                result, data = self.call()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, data["error"])
        target = self.root / "phase.tsv"
        target.write_text("rank\tvalue\n0\t1\n")
        link = self.root / "link.tsv"
        link.symlink_to(target)
        self.request["files"] = ["link.tsv"]
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular non-symlink", data["error"])

    def test_frozen_sixty_member_explicit_zip_preserves_every_source_digest(self):
        files = []
        for index in range(58):
            relative = f"evidence/{index:02d}.md"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"evidence {index}\n")
            files.append(relative)
        (self.root / "case" / "system").mkdir(parents=True)
        (self.root / "case" / "system" / "fvSchemes").write_text("object fvSchemes;\n")
        (self.root / "receipts.tsv").write_text("rank\tvalue\n0\t1\n")
        files.extend(["case/system/fvSchemes", "receipts.tsv"])
        self.assertEqual(len(files), 60)
        self.request["files"] = files
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        mapping = json.loads(Path(data["materials_file"]).read_text())
        self.assertEqual(len(mapping["materials"]), 61)  # notes plus 60 evidence files
        with zipfile.ZipFile(data["bundle"]) as archive:
            source_members = [name for name in archive.namelist() if name.startswith("source/")]
            self.assertEqual(len(source_members), 60)
            for item in mapping["materials"]:
                source = self.root / item["source_path"]
                packed = archive.read(item["archive_path"])
                self.assertEqual(packed, source.read_bytes())
                self.assertEqual(hashlib.sha256(packed).hexdigest(), item["sha256"])

    def test_declared_paths_and_input_hash_drift_are_rejected(self):
        self.request["file_digests"] = {
            "facts.md": hashlib.sha256((self.root / "facts.md").read_bytes()).hexdigest(),
            "evidence.md": hashlib.sha256((self.root / "evidence.md").read_bytes()).hexdigest(),
        }
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        (self.root / "evidence.md").write_text("changed after freeze\n")
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file drift detected", data["error"])
        self.request["file_digests"] = {"facts.md": self.request["file_digests"]["facts.md"]}
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file_digests must cover exactly notes and files", data["error"])

    def test_auto_context_keeps_builder_default_size_threshold(self):
        (self.root / "evidence.md").write_bytes(b"x" * 250_001)
        self.request["context_policy"] = "auto"
        self.request["max_files"] = 3
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["phase"], "build")
        self.assertIn("over size threshold", data["error"])

    def test_prompt_uses_verified_archive_paths_for_notes_and_evidence(self):
        self.request["question"] = "请先阅读facts.md，再读 `evidence.md`。不要更改科学判断。"
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        prompt = Path(data["prompt_file"]).read_text()
        self.assertIn("请先阅读context/codex-session-notes.md，再读 `source/evidence.md`。不要更改科学判断。", prompt)
        self.assertIn('"facts.md" → "context/codex-session-notes.md"', prompt)
        self.assertEqual(data["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        mapping = json.loads(Path(data["materials_file"]).read_text())
        self.assertEqual(mapping["bundle_sha256"], data["bundle_sha256"])
        with zipfile.ZipFile(data["bundle"]) as archive:
            for item in mapping["materials"]:
                self.assertEqual(hashlib.sha256(archive.read(item["archive_path"])).hexdigest(), item["sha256"])
            self.assertIn("请先阅读context/codex-session-notes.md", archive.read("README_FOR_GPT_PRO.md").decode())

    def test_duplicate_basenames_are_not_silently_resolved(self):
        for folder in ("first", "second"):
            (self.root / folder).mkdir()
            (self.root / folder / "same.md").write_text(folder)
        self.request["files"] = ["first/same.md", "second/same.md"]
        self.request["question"] = "请比较same.md。"
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous material reference", data["error"])
        self.request["question"] = "请比较first/same.md与second/same.md。"
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        self.assertIn("请比较source/first/same.md与source/second/same.md。", Path(data["prompt_file"]).read_text())

    def test_already_canonical_references_and_notes_in_files_remain_consistent(self):
        self.request["files"].append("facts.md")
        self.request["question"] = "阅读context/codex-session-notes.md及source/evidence.md；facts.md是背景。"
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        prompt = Path(data["prompt_file"]).read_text()
        self.assertIn("阅读context/codex-session-notes.md及source/evidence.md；context/codex-session-notes.md是背景。", prompt)
        self.assertNotIn("source/source/", prompt)

    def test_existing_prompt_is_not_overwritten(self):
        path = self.root / "bundle.prompt.md"
        path.write_text("preserve")
        result, _data = self.call("--out", str(self.root / "bundle.zip"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), "preserve")
        self.assertFalse((self.root / "bundle.zip").exists())

    def test_existing_output_not_overwritten(self):
        file = self.root / "existing.zip"
        file.write_bytes(b"original evidence")
        result, _data = self.call("--out", str(file))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(file.read_bytes(), b"original evidence")

    def test_invalid_stage_host_is_rejected_before_bundle(self):
        env = {**os.environ, "CODEX_BRIDGE_STAGING_WSL_ROOT": str(self.root / "missing")}
        result, data = self.call("--stage", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["phase"], "request")
        self.assertFalse(data["sent"])
        self.assertNotIn("bundle", data)
        self.assertFalse((self.root / ".codex" / "codex-pro-bridge" / "bundles").exists())

    def test_successful_stage_uses_matching_bundle(self):
        stage = self.root / "stage"
        stage.mkdir()
        env = {**os.environ, "CODEX_BRIDGE_STAGING_WSL_ROOT": str(stage),
               "CODEX_BRIDGE_STAGING_WINDOWS_ROOT": "C:\\BridgeStaging",
               "CODEX_BRIDGE_STAGING_LOCK": str(self.root / "staging.lock")}
        result, data = self.call("--stage", env=env)
        self.assertEqual(result.returncode, 0, data)
        self.assertEqual(Path(data["bundle"]).read_bytes(), Path(data["staging"]["staged_wsl_path"]).read_bytes())
        self.assertEqual(data["bundle_sha256"], data["staging"]["source_sha256"])

    def test_none_context_builds_zero_source_bundle_without_attachment_guide(self):
        self.request["context_policy"] = "none"
        self.request["max_files"] = 0
        self.request["files"] = []
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        self.assertTrue(data["ready"])
        self.assertEqual(data["context_policy"], "none")
        self.assertEqual(data["max_files"], 0)
        self.assertEqual(data["attachment_policy"], "none")
        self.assertNotIn("bundle", data)
        prompt = Path(data["prompt_file"]).read_text()
        self.assertIn("本轮不附加仓库文件", prompt)
        self.assertNotIn("附件阅读路径", prompt)
        self.assertIn("仅使用问题和对话上下文", prompt)
        self.assertFalse((self.root / ".codex" / "codex-pro-bridge" / "bundles").exists())

    def test_none_context_cannot_stage_a_browser_attachment(self):
        self.request["context_policy"] = "none"
        self.request["max_files"] = 0
        self.request["files"] = []
        stage = self.root / "stage"
        stage.mkdir()
        env = {**os.environ, "CODEX_BRIDGE_STAGING_WSL_ROOT": str(stage),
               "CODEX_BRIDGE_STAGING_WINDOWS_ROOT": "C:\\BridgeStaging",
               "CODEX_BRIDGE_STAGING_LOCK": str(self.root / "staging.lock")}
        result, data = self.call("--stage", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["phase"], "request")
        self.assertIn("no browser attachment", data["error"])

    def test_none_context_rejects_bundle_output_path(self):
        self.request["context_policy"] = "none"
        self.request["max_files"] = 0
        self.request["files"] = []
        result, data = self.call("--out", str(self.root / "should-not-exist.zip"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot create a bundle", data["error"])
        self.assertFalse((self.root / "should-not-exist.zip").exists())

    def test_auto_context_still_builds_and_uses_bundle(self):
        self.request["context_policy"] = "auto"
        self.request["max_files"] = 3
        self.request["files"] = []
        result, data = self.call()
        self.assertEqual(result.returncode, 0, data)
        self.assertTrue(data["ready"])
        self.assertEqual(data["context_policy"], "auto")
        self.assertEqual(data["max_files"], 3)
        self.assertEqual(data["attachment_policy"], "bundle")
        self.assertIn("bundle", data)

    def test_context_policy_rejects_contradictory_file_lists(self):
        self.request["context_policy"] = "none"
        self.request["max_files"] = 0
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", data["error"])
        self.request["context_policy"] = "explicit"
        self.request["files"] = []
        self.request["max_files"] = 0
        result, data = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires at least one", data["error"])

    def test_legacy_builder_default_keeps_git_contract(self):
        result = subprocess.run(
            [
                sys.executable,
                str(BUILDER),
                "--repo",
                str(self.root),
                "--bridge-thread-id",
                "fixture-legacy",
                "--goal",
                "fixture",
                "--codex-session-notes",
                "facts.md",
                "--repo-context",
                "explicit",
                "--include",
                "evidence.md",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(result.stdout.strip()) as archive:
            self.assertIn("context/git-status.txt", archive.namelist())


if __name__ == "__main__":
    unittest.main()
