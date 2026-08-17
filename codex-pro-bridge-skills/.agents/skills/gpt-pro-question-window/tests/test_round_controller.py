from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
ROUND_SCRIPT = SKILL_DIR / "scripts" / "manage_bridge_round.py"
PREFLIGHT_SCRIPT = SKILL_DIR / "scripts" / "check_browser_preflight.py"
CAPTURE_SCRIPT = SKILL_DIR / "scripts" / "save_bridge_turn.py"
VERIFY_SCRIPT = SKILL_DIR / "scripts" / "verify_bridge_thread.py"
REMOTE_REQUEST_SCRIPT = SKILL_DIR / "scripts" / "validate_remote_bridge_request.py"
BROWSER_CAPTURE_SCRIPT = SKILL_DIR / "scripts" / "capture_browser_markdown.py"
BROWSER_CAPTURE_SPEC_SCRIPT = SKILL_DIR / "scripts" / "build_browser_capture_spec.py"
DISPATCHER_SCRIPT = SKILL_DIR / "scripts" / "manage_bridge_dispatcher.py"
CALLBACK_SCRIPT = SKILL_DIR / "scripts" / "prepare_bridge_callback.py"
REAPER_SCRIPT = SKILL_DIR / "scripts" / "reap_idle_chrome_mcp.py"
PROTOCOL = SKILL_DIR / "references" / "bridge_protocol.md"
PREPARE_NOTES_SCRIPT = (
    SKILL_DIR.parent / "bundle-algorithm-context" / "scripts" / "prepare_codex_session_notes.py"
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rounds = load_module("manage_bridge_round", ROUND_SCRIPT)
preflight = load_module("check_browser_preflight", PREFLIGHT_SCRIPT)
capture = load_module("save_bridge_turn", CAPTURE_SCRIPT)
reaper = load_module("reap_idle_chrome_mcp", REAPER_SCRIPT)


class RoundControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.prompt_hash = hashlib.sha256(b"focused question").hexdigest()
        self.bundle_hash = hashlib.sha256(b"bundle").hexdigest()
        self.capture_hash = hashlib.sha256(b"answer").hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str, expect: int = 0) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(ROUND_SCRIPT),
                "--state-dir",
                str(self.state_dir),
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return json.loads(result.stdout) if result.stdout else {}

    def create(self, request_id: str, *, deadline: str = "2030-08-14T15:00:00+08:00") -> dict:
        return self.run_cli(
            "create",
            "--request-id",
            request_id,
            "--bridge-thread-id",
            f"bridge-{request_id}",
            "--source-kind",
            "local",
            "--bundle-path",
            "/tmp/focused.zip",
            "--bundle-sha256",
            self.bundle_hash,
            "--prompt-sha256",
            self.prompt_hash,
            "--requested-model-family",
            "GPT-5.6 Sol",
            "--requested-effort",
            "Pro",
            "--account-key",
            "chatgpt-account-a",
            "--deadline-at",
            deadline,
        )

    def test_fast_threshold_is_millisecond_exact(self) -> None:
        elapsed, status = rounds.classify_execution(
            "2026-08-14T12:00:00.000+08:00",
            "2026-08-14T12:00:59.999+08:00",
        )
        self.assertEqual((elapsed, status), (59_999, "degraded_fast"))
        elapsed, status = rounds.classify_execution(
            "2026-08-14T12:00:00.000+08:00",
            "2026-08-14T12:01:00.000+08:00",
        )
        self.assertEqual((elapsed, status), (60_000, "not_fast_degraded"))

    def test_create_is_idempotent_and_conflicts_fail_closed(self) -> None:
        first = self.create("req-a")
        second = self.create("req-a")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        failed = subprocess.run(
            [
                sys.executable,
                str(ROUND_SCRIPT),
                "--state-dir",
                str(self.state_dir),
                "create",
                "--request-id",
                "req-a",
                "--bridge-thread-id",
                "different-thread",
                "--source-kind",
                "local",
                "--prompt-sha256",
                self.prompt_hash,
                "--requested-model-family",
                "GPT-5.6 Sol",
                "--requested-effort",
                "Pro",
                "--account-key",
                "chatgpt-account-a",
                "--deadline-at",
                "2030-08-14T15:00:00+08:00",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("conflicting immutable fields", failed.stderr)

    def test_generation_slot_serializes_and_never_steals_expired_holder(self) -> None:
        self.create("req-a", deadline="2020-08-14T10:00:00+08:00")
        self.create("req-b", deadline="2030-08-14T16:00:00+08:00")
        first = self.run_cli(
            "slot-acquire",
            "--request-id",
            "req-a",
            "--account-key",
            "chatgpt-account-a",
            "--deadline-at",
            "2020-08-14T10:00:00+08:00",
        )
        blocked = self.run_cli(
            "slot-acquire",
            "--request-id",
            "req-b",
            "--account-key",
            "chatgpt-account-a",
            "--deadline-at",
            "2030-08-14T16:00:00+08:00",
        )
        self.assertTrue(first["acquired"])
        self.assertFalse(blocked["acquired"])
        self.assertEqual(blocked["reason"], "expired-recovery-required")
        queued = self.run_cli("status", "--request-id", "req-b")
        self.assertEqual(queued["status"], "queued")
        self.run_cli(
            "slot-release",
            "--request-id",
            "req-a",
        )
        acquired = self.run_cli(
            "slot-acquire",
            "--request-id",
            "req-b",
            "--account-key",
            "chatgpt-account-a",
            "--deadline-at",
            "2030-08-14T16:00:00+08:00",
        )
        self.assertTrue(acquired["acquired"])

    def test_round_transitions_classify_and_generate_no_resubmit_watcher(self) -> None:
        self.create("req-a")
        staged = Path(self.temp.name) / "focused.zip"
        staged.write_bytes(b"bundle")
        staged_state = self.run_cli(
            "staging",
            "--request-id",
            "req-a",
            "--staging-status",
            "verified",
            "--staged-file",
            str(staged),
            "--staged-sha256",
            self.bundle_hash,
        )
        self.assertEqual(staged_state["staging_status"], "verified")
        self.run_cli(
            "transition",
            "--request-id",
            "req-a",
            "--to",
            "submitting",
            "--conversation-id",
            "conversation-a",
            "--pre-submit-boundary",
            "turn-before",
            "--selected-model-family",
            "GPT-5.6 Sol",
            "--selected-effort",
            "Pro",
            "--model-selection-status",
            "verified",
        )
        self.run_cli(
            "transition",
            "--request-id",
            "req-a",
            "--to",
            "submitted",
            "--submitted-at",
            "2026-08-14T12:00:00.000+08:00",
        )
        complete = self.run_cli(
            "transition",
            "--request-id",
            "req-a",
            "--to",
            "capture_needed",
            "--remote-turn-id",
            "remote-turn-a",
            "--response-completed-at",
            "2026-08-14T12:00:59.999+08:00",
        )
        self.assertEqual(complete["execution_status"], "degraded_fast")
        self.assertEqual(complete["response_elapsed_ms"], 59_999)
        spec = self.run_cli(
            "watcher-spec",
            "--request-id",
            "req-a",
            "--target-thread-id",
            "dispatcher-thread",
        )
        self.assertIn("Never send or resubmit", spec["prompt"])
        self.assertIn("conversation-a", spec["prompt"])
        self.assertIn(self.prompt_hash, spec["prompt"])
        self.assertEqual(spec["rrule"], "RRULE:FREQ=MINUTELY;INTERVAL=2")
        captured = self.run_cli(
            "transition",
            "--request-id",
            "req-a",
            "--to",
            "captured",
            "--capture-route",
            "native-read-thread",
            "--capture-path",
            "/tmp/answer.md",
            "--capture-sha256",
            self.capture_hash,
        )
        self.assertEqual(captured["delivery_status"], "not_required")

    def test_watcher_cleanup_cannot_disappear_without_identity(self) -> None:
        self.create("req-watcher")
        missing = subprocess.run(
            [
                sys.executable,
                str(ROUND_SCRIPT),
                "--state-dir",
                str(self.state_dir),
                "watcher",
                "--request-id",
                "req-watcher",
                "--watcher-status",
                "deleted",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        active = self.run_cli(
            "watcher",
            "--request-id",
            "req-watcher",
            "--automation-id",
            "automation-a",
            "--watcher-status",
            "active",
        )
        self.assertEqual(active["watcher_status"], "active")
        failed = self.run_cli(
            "watcher",
            "--request-id",
            "req-watcher",
            "--watcher-status",
            "cleanup_failed",
            "--detail",
            "automation delete rejected; pause required",
        )
        self.assertEqual(failed["watcher_status"], "cleanup_failed")
        listed = self.run_cli("list", "--active-only")
        self.assertIn("req-watcher", {item["request_id"] for item in listed["rounds"]})

    def test_submit_ambiguity_recovery_archives_and_clears_failure(self) -> None:
        self.create("req-recovered")
        self.run_cli(
            "transition",
            "--request-id",
            "req-recovered",
            "--to",
            "submitting",
            "--conversation-id",
            "conversation-a",
            "--pre-submit-boundary",
            "turn-before",
            "--selected-model-family",
            "GPT-5.6 Sol",
            "--selected-effort",
            "Pro",
            "--model-selection-status",
            "verified",
        )
        ambiguous = self.run_cli(
            "transition",
            "--request-id",
            "req-recovered",
            "--to",
            "submit_ambiguous",
            "--failure-code",
            "send-click-timeout",
            "--failure-detail",
            "Send click timed out after acceptance may have occurred.",
        )
        self.assertEqual(ambiguous["failure_code"], "send-click-timeout")
        recovered = self.run_cli(
            "transition",
            "--request-id",
            "req-recovered",
            "--to",
            "submitted",
            "--submitted-at",
            "2026-08-14T12:00:00.000+08:00",
        )
        self.assertEqual(recovered["failure_code"], "")
        self.assertEqual(recovered["failure_detail"], "")
        self.assertEqual(
            recovered["recovered_diagnostics"][-1]["code"],
            "send-click-timeout",
        )

    def test_ssh_round_requires_complete_callback_identity(self) -> None:
        base = [
            "create",
            "--request-id",
            "req-ssh",
            "--bridge-thread-id",
            "bridge-req-ssh",
            "--bridge-repo",
            self.temp.name,
            "--source-kind",
            "ssh",
            "--source-thread-id",
            "remote-task-a",
            "--bundle-path",
            "/tmp/codex-pro-bridge-client/req-ssh/review.zip",
            "--bundle-sha256",
            self.bundle_hash,
            "--prompt-sha256",
            self.prompt_hash,
            "--requested-model-family",
            "GPT-5.6 Sol",
            "--requested-effort",
            "Pro",
            "--account-key",
            "chatgpt-account-a",
            "--deadline-at",
            "2030-08-14T15:00:00+08:00",
        ]
        failed = subprocess.run(
            [sys.executable, str(ROUND_SCRIPT), "--state-dir", str(self.state_dir), *base],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("--source-host-id", failed.stderr)

        created = self.run_cli(
            *base,
            "--source-host-id",
            "remote-host-a",
            "--ssh-alias",
            "devbox-review",
            "--source-repo",
            "/srv/workspaces/example-project",
        )
        self.assertTrue(created["created"])
        self.assertEqual(created["round"]["delivery_status"], "not_ready")

    def test_reconcile_repairs_only_deterministic_legacy_state(self) -> None:
        self.create("req-legacy")
        path = self.state_dir / "rounds" / "req-legacy.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(
            {
                "status": "verdict_recorded",
                "selected_model_family": "",
                "selected_effort": "Pro",
                "model_selection_status": "verified",
                "delivery_status": "pending",
                "failure_code": "send-click-timeout",
                "failure_detail": "historical timeout",
                "transitions": [
                    {"from": "submitting", "to": "submit_ambiguous", "at": "2026-08-14T12:00:00+08:00"},
                    {"from": "submit_ambiguous", "to": "submitted", "at": "2026-08-14T12:00:01+08:00"},
                ],
            }
        )
        path.write_text(json.dumps(state), encoding="utf-8")
        reconciled = self.run_cli("reconcile", "--request-id", "req-legacy")
        self.assertEqual(reconciled["round"]["failure_code"], "")
        self.assertEqual(reconciled["round"]["model_selection_status"], "unverified")
        self.assertEqual(reconciled["round"]["delivery_status"], "not_required")
        self.assertEqual(
            set(reconciled["repairs"]),
            {
                "archived_recovered_failure",
                "corrected_model_selection_status",
                "closed_local_delivery",
            },
        )


class ModelAndCaptureTests(unittest.TestCase):
    def modern_args(self, **overrides: str) -> argparse.Namespace:
        values = {
            "requested_model_family": "GPT-5.6 Sol",
            "selected_model_family": "GPT-5.6 Sol",
            "requested_effort": "Pro",
            "selected_effort": "Pro",
            "requested_model": "",
            "selected_ui_label": "",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_preflight_splits_model_family_and_effort(self) -> None:
        verified = preflight.verify_model_selection(self.modern_args())
        self.assertEqual(verified["model_verification"], "verified")
        with self.assertRaises(Exception):
            preflight.verify_model_selection(
                self.modern_args(requested_model_family="")
            )
        with self.assertRaises(Exception):
            preflight.verify_model_selection(
                self.modern_args(requested_model_family="", selected_model_family="")
            )
        with self.assertRaises(Exception):
            preflight.verify_model_selection(
                self.modern_args(selected_effort="Thinking")
            )

    def test_capture_marks_fast_completion_degraded(self) -> None:
        elapsed, status = capture.classify_execution(
            "2026-08-14T12:00:00.000+08:00",
            "2026-08-14T12:00:18.202+08:00",
            "",
        )
        self.assertEqual(elapsed, 18_202)
        self.assertEqual(status, "degraded_fast")

    def test_capture_never_verifies_blank_model_family(self) -> None:
        selection = capture.resolve_model_selection(
            self.modern_args(requested_model_family="", selected_model_family="")
        )
        self.assertEqual(selection["model_verification"], "unverified")

    def test_preflight_verifies_aliased_staged_file_under_one_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "canonical.zip"
            staged = root / "local-devtools-smoke.zip"
            bundle.write_bytes(b"same bytes")
            staged.write_bytes(b"same bytes")
            claim = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCHER_SCRIPT),
                    "--state-dir",
                    str(root / "state"),
                    "claim",
                    "--thread-id",
                    "dispatcher-task-a",
                    "--host-id",
                    "local-mac",
                    "--holder",
                    "pro-bridge-dispatcher",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(claim.returncode, 0, claim.stderr)
            token = json.loads(claim.stdout)["token"]
            env = dict(os.environ)
            env["CODEX_PRO_BRIDGE_STATE_DIR"] = str(root / "state")
            result = subprocess.run(
                [
                    sys.executable,
                    str(PREFLIGHT_SCRIPT),
                    "--repo",
                    str(root),
                    "--bridge-thread-id",
                    "review-thread-a",
                    "--dispatcher-thread-id",
                    "dispatcher-task-a",
                    "--dispatcher-token",
                    token,
                    "--requested-model-family",
                    "GPT-5.6 Sol",
                    "--selected-model-family",
                    "GPT-5.6 Sol",
                    "--requested-effort",
                    "Pro",
                    "--selected-effort",
                    "Pro",
                    "--bundle",
                    str(bundle),
                    "--staged-file",
                    str(staged),
                    "--attachment-name",
                    staged.name,
                    "--upload-control",
                    "devtools-mcp-upload-file",
                    "--expected-conversation-id",
                    "conversation-a",
                    "--observed-conversation-id",
                    "conversation-a",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["attachment_verification"], "verified")
            self.assertEqual(payload["dispatcher_thread_id"], "dispatcher-task-a")

    def test_protocol_places_request_id_on_capture_not_snapshot(self) -> None:
        text = PROTOCOL.read_text(encoding="utf-8")
        snapshot = text.split("prepare_codex_session_notes.py", 1)[1].split("```", 1)[0]
        capture_block = text.split("scripts/save_bridge_turn.py", 1)[1].split("```", 1)[0]
        self.assertNotIn("--request-id", snapshot)
        self.assertIn("--request-id <request-id>", capture_block)


class CapturePersistenceIntegrationTests(unittest.TestCase):
    def test_same_request_replays_once_and_conflicting_content_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(PREPARE_NOTES_SCRIPT),
                    "--repo",
                    str(repo),
                    "--bridge-thread-id",
                    "capture-integration",
                    "--goal",
                    "Test idempotent capture.",
                    "--gpt-pro-question",
                    "Review the synthetic state.",
                    "--summary",
                    "Synthetic no-send test.",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            base = [
                sys.executable,
                str(CAPTURE_SCRIPT),
                "--repo",
                str(repo),
                "--bridge-thread-id",
                "capture-integration",
                "--request-id",
                "request-integration",
                "--standalone",
                "--single-round",
                "--expected-conversation-id",
                "conversation-integration",
                "--web-url",
                "https://chatgpt.com/c/conversation-integration",
                "--submitted-at",
                "2026-08-14T12:00:00.000+08:00",
                "--response-completed-at",
                "2026-08-14T12:00:18.202+08:00",
                "--requested-model-family",
                "GPT-5.6 Sol",
                "--selected-model-family",
                "GPT-5.6 Sol",
                "--requested-effort",
                "Pro",
                "--selected-effort",
                "Pro",
                "--capture-route",
                "native-read-thread",
                "--remote-turn-id",
                "remote-turn-integration",
                "--prompt",
                "Synthetic prompt, never sent.",
            ]
            for _ in range(2):
                saved = subprocess.run(
                    [*base, "--answer", "Synthetic complete answer."],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(saved.returncode, 0, saved.stderr)
            ledger = repo / ".codex/codex-pro-bridge/threads/capture-integration.jsonl"
            events = [json.loads(line) for line in ledger.read_text().splitlines()]
            exchanges = [event for event in events if event["event_type"] == "gpt-exchange"]
            self.assertEqual(len(exchanges), 1)
            self.assertEqual(exchanges[0]["data"]["response_elapsed_ms"], 18_202)
            self.assertEqual(exchanges[0]["data"]["execution_status"], "degraded_fast")
            verified = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "--repo",
                    str(repo),
                    "--bridge-thread-id",
                    "capture-integration",
                    "--require-verified-provenance",
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["provenance_status"], "verified")
            conflict = subprocess.run(
                [*base, "--answer", "Conflicting answer."],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("already captured with different content", conflict.stderr)

    def test_attachment_alias_is_verified_by_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            bundle = repo / "canonical-review.zip"
            bundle.write_bytes(b"bundle payload")
            bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(PREPARE_NOTES_SCRIPT),
                    "--repo",
                    str(repo),
                    "--bridge-thread-id",
                    "alias-integration",
                    "--goal",
                    "Test attachment provenance.",
                    "--gpt-pro-question",
                    "Review the synthetic state.",
                    "--summary",
                    "Synthetic no-send test.",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            saved = subprocess.run(
                [
                    sys.executable,
                    str(CAPTURE_SCRIPT),
                    "--repo",
                    str(repo),
                    "--bridge-thread-id",
                    "alias-integration",
                    "--request-id",
                    "alias-request",
                    "--standalone",
                    "--single-round",
                    "--expected-conversation-id",
                    "conversation-alias",
                    "--web-url",
                    "https://chatgpt.com/c/conversation-alias",
                    "--bundle",
                    str(bundle),
                    "--attachment-name",
                    "local-devtools-smoke.zip",
                    "--attachment-sha256",
                    bundle_hash,
                    "--upload-control",
                    "devtools-mcp-upload-file",
                    "--submitted-at",
                    "2026-08-14T12:00:00.000+08:00",
                    "--response-completed-at",
                    "2026-08-14T12:02:00.000+08:00",
                    "--requested-model-family",
                    "GPT-5.6 Sol",
                    "--selected-model-family",
                    "GPT-5.6 Sol",
                    "--requested-effort",
                    "Pro",
                    "--selected-effort",
                    "Pro",
                    "--capture-route",
                    "native-read-thread",
                    "--remote-turn-id",
                    "remote-turn-alias",
                    "--prompt",
                    "Synthetic prompt, never sent.",
                    "--answer",
                    "Synthetic complete answer.",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(saved.returncode, 0, saved.stderr)
            events = [
                json.loads(line)
                for line in (
                    repo / ".codex/codex-pro-bridge/threads/alias-integration.jsonl"
                ).read_text().splitlines()
            ]
            exchange = next(e for e in events if e["event_type"] == "gpt-exchange")
            self.assertEqual(exchange["data"]["attachment_verification"], "verified")
            self.assertEqual(exchange["data"]["attachment_sha256"], bundle_hash)
            strict = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "--repo",
                    str(repo),
                    "--bridge-thread-id",
                    "alias-integration",
                    "--require-verified-provenance",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(strict.returncode, 0, strict.stderr)


class RemoteRequestAndBrowserCaptureTests(unittest.TestCase):
    def test_schema_v2_request_validates_exact_remote_identity(self) -> None:
        request = """<pro_bridge_request schema_version="2">
  <request_id>ssh-review-001</request_id>
  <source_thread_id>remote-task-a</source_thread_id>
  <source_host_id>remote-host-a</source_host_id>
  <ssh_alias>devbox-review</ssh_alias>
  <remote_repository>/srv/workspaces/example-project</remote_repository>
  <transient_bundle_path>/tmp/codex-pro-bridge-client/ssh-review-001/review.zip</transient_bundle_path>
  <bundle_sha256>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</bundle_sha256>
  <focused_question>Review only the supplied algorithm evidence.</focused_question>
  <evidence_paths><path>dl_model/config.py</path><path>dl_model/model.py</path></evidence_paths>
</pro_bridge_request>"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "request.xml"
            path.write_text(request, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(REMOTE_REQUEST_SCRIPT), "--request-file", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["source_host_id"], "remote-host-a")
            self.assertEqual(payload["evidence_paths"], ["dl_model/config.py", "dl_model/model.py"])

    def test_browser_capture_preserves_semantic_markdown(self) -> None:
        payload = {
            "schema_version": 1,
            "conversation_id": "conversation-a",
            "remote_turn_id": "remote-turn-a",
            "prompt_sha256": hashlib.sha256(b"prompt").hexdigest(),
            "html": (
                "<h2>Algorithm Review</h2><p>Use <strong>two</strong> stages.</p>"
                "<table><thead><tr><th>Stage</th><th>Metric</th></tr></thead>"
                "<tbody><tr><td>A</td><td>Recall</td></tr></tbody></table>"
                "<pre><code class=\"language-python\">x = 1\n</code></pre>"
                "<p><a href=\"https://example.com/source\">Source</a></p>"
            ),
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "capture.json"
            output = Path(temp) / "answer.md"
            source.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(BROWSER_CAPTURE_SCRIPT),
                    "--payload-file",
                    str(source),
                    "--output",
                    str(output),
                    "--expected-conversation-id",
                    "conversation-a",
                    "--expected-remote-turn-id",
                    "remote-turn-a",
                    "--expected-prompt-sha256",
                    payload["prompt_sha256"],
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            markdown = output.read_text(encoding="utf-8")
            self.assertIn("## Algorithm Review", markdown)
            self.assertIn("**two**", markdown)
            self.assertIn("| Stage | Metric |", markdown)
            self.assertIn("```python", markdown)
            self.assertIn("[Source](https://example.com/source)", markdown)

    def test_browser_capture_spec_uses_file_output_and_exact_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "capture.json"
            prompt_sha = hashlib.sha256(b"prompt").hexdigest()
            result = subprocess.run(
                [
                    sys.executable,
                    str(BROWSER_CAPTURE_SPEC_SCRIPT),
                    "--assistant-uid",
                    "uid-42",
                    "--conversation-id",
                    "conversation-a",
                    "--remote-turn-id",
                    "remote-turn-a",
                    "--prompt-sha256",
                    prompt_sha,
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            spec = json.loads(result.stdout)
            self.assertEqual(spec["args"]["args"], ["uid-42"])
            self.assertEqual(Path(spec["args"]["filePath"]), output.resolve())
            self.assertIn("html: el.innerHTML", spec["args"]["function"])


class DispatcherTests(unittest.TestCase):
    def test_one_dispatcher_claim_serializes_browser_owners(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            def run(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        str(DISPATCHER_SCRIPT),
                        "--state-dir",
                        temp,
                        *args,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            first = run(
                "claim",
                "--thread-id",
                "dispatcher-task-a",
                "--host-id",
                "local-mac",
                "--holder",
                "pro-bridge-dispatcher",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_payload = json.loads(first.stdout)
            token = first_payload["token"]

            second = run(
                "claim",
                "--thread-id",
                "dispatcher-task-b",
                "--host-id",
                "local-mac",
                "--holder",
                "pro-bridge-dispatcher",
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already held", second.stderr)

            status = run("status")
            self.assertEqual(status.returncode, 0, status.stderr)
            public = json.loads(status.stdout)
            self.assertTrue(public["active"])
            self.assertNotIn("token", public)

            released = run("release", "--token", token)
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertTrue(json.loads(released.stdout)["released"])

    def test_reaper_selects_only_exact_versioned_children(self) -> None:
        table = reaper.parse_process_table(
            """
  10 1 /Applications/Codex.app/Contents/MacOS/codex app-server
  20 10 npm exec chrome-devtools-mcp@1.7.0 --autoConnect
  21 20 chrome-devtools-mcp
  30 10 npm exec another-mcp@1.0.0
  31 30 another-mcp
"""
        )
        wrappers, servers = reaper.select_tree(table, 10)
        self.assertEqual([process.pid for process in wrappers], [20])
        self.assertEqual([process.pid for process in servers], [21])


class CallbackTests(unittest.TestCase):
    def test_callback_binds_exact_source_task_host_and_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rounds_dir = root / "rounds"
            rounds_dir.mkdir()
            digest = "a" * 64
            state = {
                "request_id": "ssh-review-001",
                "source_kind": "ssh",
                "source_thread_id": "remote-task-a",
                "source_host_id": "remote-host-a",
                "ssh_alias": "devbox-review",
                "status": "verdict_recorded",
                "model_selection_status": "verified",
                "execution_status": "not_fast_degraded",
                "bundle_sha256": "d" * 64,
                "capture_sha256": digest,
                "verdict_sha256": "b" * 64,
            }
            (rounds_dir / "ssh-review-001.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(CALLBACK_SCRIPT),
                    "--state-dir",
                    str(root),
                    "--request-id",
                    "ssh-review-001",
                    "--terminal-status",
                    "completed",
                    "--summary",
                    "Review completed and locally verified.",
                    "--result-path",
                    "/tmp/codex-pro-bridge-client/ssh-review-001/results/full-answer.md",
                    "--result-sha256",
                    "c" * 64,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["target_thread_id"], "remote-task-a")
            self.assertEqual(payload["target_host_id"], "remote-host-a")
            self.assertIn("<request_id>ssh-review-001</request_id>", payload["message"])
            self.assertIn("<source_host_id>remote-host-a</source_host_id>", payload["message"])
            self.assertIn(f"<bundle_sha256>{'d' * 64}</bundle_sha256>", payload["message"])


if __name__ == "__main__":
    unittest.main()
