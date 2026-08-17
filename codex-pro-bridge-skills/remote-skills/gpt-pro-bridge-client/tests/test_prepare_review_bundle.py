from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_review_bundle.py"
REQUEST_VALIDATOR = (
    Path(__file__).resolve().parents[3]
    / ".agents/skills/gpt-pro-question-window/scripts/validate_remote_bridge_request.py"
)


class RemoteBridgeClientTests(unittest.TestCase):
    def run_cli(
        self,
        *args: str,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=input_bytes,
            check=False,
            capture_output=True,
        )

    def test_config_prepare_and_cleanup_emit_schema_v2_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / "repo"
            repo.mkdir()
            (repo / "algorithm.py").write_text("VALUE = 1\n", encoding="utf-8")
            root = base / "codex-pro-bridge-client"
            config = base / "config.json"
            configured = self.run_cli(
                "--config",
                str(config),
                "configure",
                "--dispatcher-thread-id",
                "dispatcher-task-a",
                "--dispatcher-host-id",
                "local",
                "--ssh-alias",
                "devbox-review",
            )
            self.assertEqual(configured.returncode, 0, configured.stderr.decode())
            conflicting = self.run_cli(
                "--config",
                str(config),
                "configure",
                "--dispatcher-thread-id",
                "dispatcher-task-b",
                "--dispatcher-host-id",
                "local",
                "--ssh-alias",
                "devbox-review",
            )
            self.assertNotEqual(conflicting.returncode, 0)
            self.assertIn(b"already bound", conflicting.stderr)
            prepared = self.run_cli(
                "--root",
                str(root),
                "--config",
                str(config),
                "prepare",
                "--repo",
                str(repo),
                "--source-thread-id",
                "remote-task-a",
                "--source-host-id",
                "remote-host-a",
                "--question",
                "Review this algorithm.",
                "--include",
                "algorithm.py",
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
            payload = json.loads(prepared.stdout)
            self.assertEqual(payload["dispatcher_thread_id"], "dispatcher-task-a")
            self.assertEqual(payload["remote_repository"], str(repo.resolve()))
            self.assertEqual(payload["focused_question"], "Review this algorithm.")
            self.assertEqual(payload["evidence_paths"], ["algorithm.py"])
            bundle = Path(payload["transient_bundle_path"])
            self.assertEqual(
                hashlib.sha256(bundle.read_bytes()).hexdigest(), payload["bundle_sha256"]
            )
            with zipfile.ZipFile(bundle) as archive:
                manifest = json.loads(archive.read("REVIEW_MANIFEST.json"))
            self.assertEqual(manifest["dispatcher_thread_id"], "dispatcher-task-a")
            self.assertEqual(manifest["ssh_alias"], "devbox-review")
            request_file = base / "request.xml"
            request_file.write_text(payload["request_xml"], encoding="utf-8")
            validated = subprocess.run(
                [sys.executable, str(REQUEST_VALIDATOR), "--request-file", str(request_file)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(json.loads(validated.stdout)["bundle_sha256"], payload["bundle_sha256"])
            cleaned = self.run_cli(
                "--root",
                str(root),
                "cleanup",
                "--bundle",
                str(bundle),
            )
            self.assertEqual(cleaned.returncode, 0, cleaned.stderr.decode())
            self.assertFalse(bundle.exists())

    def test_result_path_matches_callback_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex-pro-bridge-client"
            request_id = "remote-review-a"
            answer = b"# Full answer\n"
            digest = hashlib.sha256(answer).hexdigest()
            imported = self.run_cli(
                "--root",
                str(root),
                "result-import",
                "--request-id",
                request_id,
                "--expected-sha256",
                digest,
                input_bytes=answer,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr.decode())
            payload = json.loads(imported.stdout)
            expected = root.resolve() / request_id / "results" / "full-answer.md"
            self.assertEqual(Path(payload["result"]), expected)
            status = self.run_cli(
                "--root", str(root), "result-status", "--request-id", request_id
            )
            self.assertEqual(status.returncode, 0, status.stderr.decode())
            self.assertEqual(json.loads(status.stdout)["sha256"], digest)
            cleaned = self.run_cli(
                "--root", str(root), "result-cleanup", "--request-id", request_id
            )
            self.assertEqual(cleaned.returncode, 0, cleaned.stderr.decode())
            self.assertFalse(expected.exists())


if __name__ == "__main__":
    unittest.main()
