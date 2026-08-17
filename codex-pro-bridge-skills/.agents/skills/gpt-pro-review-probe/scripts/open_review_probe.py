#!/usr/bin/env python3
"""Open one high-frequency, standalone GPT Pro Review Probe.

A Review Probe is the fast lane for high-frequency, fine-grained, parallel
review of a single idea, research proposal, or atomic sub-task. It is a
standalone, single-round Bridge Thread that NEVER attaches to a Bridge Project,
so it never consults Project source-sync and is never blocked when unrelated
shared sources are stale. Evidence preparation may run in parallel because each
probe is a distinct thread. Formal Pro generations are serialized per account,
and browser mutations use a separate host-global lease. Separate worktrees
still require one declared dispatcher.

This orchestrator prepares the immutable Codex snapshot and builds the standalone
evidence bundle. It deliberately does NOT import BridgeProjectStore and never
runs source-sync. It stops before the browser step and prints the exact next
handoff (round -> generation slot -> lease -> Send -> watcher -> capture) so the
browser seam stays an explicit, verifiable human/agent action.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import subprocess
import sys
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_SCRIPT = SKILLS_ROOT / "bundle-algorithm-context" / "scripts" / "build_algorithm_bundle.py"
NOTES_SCRIPT = SKILLS_ROOT / "bundle-algorithm-context" / "scripts" / "prepare_codex_session_notes.py"
QW_SCRIPTS = SKILLS_ROOT / "gpt-pro-question-window" / "scripts"

VALID_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


def slug(value: str, fallback: str = "probe") -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return out or fallback


def build_probe_thread_id(track: str, candidate: str, probe_slug: str, date: str) -> str:
    parts = [slug(p) for p in (track, candidate, probe_slug, date) if slug(p) not in ("", "probe")]
    if not parts:
        parts = [slug(probe_slug)]
    thread_id = "-".join(parts)
    thread_id = re.sub(r"-+", "-", thread_id).strip("-")[:80].strip("-")
    if not VALID_ID.fullmatch(thread_id):
        raise SystemExit(f"Derived thread id is invalid: {thread_id!r}")
    return thread_id


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    if result.stderr.strip():
        sys.stderr.write(result.stderr)
    return result.stdout.strip()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a standalone GPT Pro Review Probe.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--track", default="", help="Research track/line label, e.g. m-ai.")
    parser.add_argument("--candidate", default="", help="Candidate/idea ref, e.g. c14. Opaque to Bridge.")
    parser.add_argument("--probe-slug", required=True, help="Short atomic-task slug, e.g. reward-leakage.")
    parser.add_argument("--date", default="", help="YYYYMMDD; defaults to today (local).")
    parser.add_argument("--bridge-thread-id", default="", help="Override the derived probe thread id.")
    parser.add_argument("--goal", required=True, help="What this probe asks GPT Pro to review.")
    parser.add_argument("--question", default="", help="Focused question for GPT Pro.")
    parser.add_argument("--summary", default="", help="Codex-side summary for the snapshot notes.")
    parser.add_argument("--summary-file", default="")
    parser.add_argument("--mode", default="algorithm_review",
                        help="Bundle mode: algorithm_review, paper_brainstorm, experiment_analysis, general_question, implementation_check.")
    parser.add_argument("--repo-context", default="auto", choices=["auto", "explicit", "none"])
    parser.add_argument("--include", nargs="*", default=[], help="Explicit files/dirs/globs for the bundle.")
    parser.add_argument("--max-files", type=int, default=24)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"Repository root is not a directory: {repo}")

    date = args.date.strip() or dt.datetime.now().astimezone().strftime("%Y%m%d")
    thread_id = args.bridge_thread_id.strip() or build_probe_thread_id(
        args.track, args.candidate, args.probe_slug, date
    )
    summary = args.summary
    if args.summary_file:
        summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
    if not summary:
        summary = args.goal

    # 1. Immutable Codex snapshot (standalone: no --bridge-project-id passed).
    notes_cmd = [
        sys.executable, str(NOTES_SCRIPT),
        "--repo", str(repo),
        "--bridge-thread-id", thread_id,
        "--goal", args.goal,
        "--summary", summary,
    ]
    if args.question:
        notes_cmd += ["--gpt-pro-question", args.question]
    notes_path = run(notes_cmd)

    # 2. Standalone scoped bundle: --standalone skips ALL Project source-sync
    #    gating, so a stale shared source never blocks this probe.
    bundle_cmd = [
        sys.executable, str(BUNDLE_SCRIPT),
        "--repo", str(repo),
        "--bridge-thread-id", thread_id,
        "--standalone",
        "--goal", args.goal,
        "--mode", args.mode,
        "--repo-context", args.repo_context,
        "--max-files", str(args.max_files),
    ]
    if args.question:
        bundle_cmd += ["--question", args.question]
    if args.include:
        bundle_cmd += ["--include", *args.include]
    bundle_path = run(bundle_cmd).splitlines()[-1].strip()
    bundle_sha256 = file_sha256(Path(bundle_path))
    request_id = f"{thread_id}-r1"

    dispatcher_script = QW_SCRIPTS / "manage_bridge_dispatcher.py"
    lease_script = QW_SCRIPTS / "manage_browser_lease.py"
    round_script = QW_SCRIPTS / "manage_bridge_round.py"
    stage_script = QW_SCRIPTS / "stage_bridge_attachment.py"
    preflight_script = QW_SCRIPTS / "check_browser_preflight.py"
    save_script = QW_SCRIPTS / "save_bridge_turn.py"

    print(f"probe_thread_id: {thread_id}")
    print(f"request_id: {request_id}")
    print(f"codex_notes: {notes_path}")
    print(f"bundle: {bundle_path}")
    print(f"bundle_sha256: {bundle_sha256}")
    print()
    print("Next (run in the one claimed Mac dispatcher task):", file=sys.stderr)
    print(f"  1. python3 {dispatcher_script} status", file=sys.stderr)
    print("     If this task is not the active owner, hand off the request;", file=sys.stderr)
    print("     do not call Chrome from here.", file=sys.stderr)
    print(f"  2. python3 {round_script} create \\", file=sys.stderr)
    print(f"       --request-id {request_id} --bridge-thread-id {thread_id} \\", file=sys.stderr)
    print(f"       --bridge-repo {repo} --source-kind local \\", file=sys.stderr)
    print(f"       --bundle-path {bundle_path} --bundle-sha256 {bundle_sha256} \\", file=sys.stderr)
    print("       --prompt-sha256 <exact-prompt-sha256> \\", file=sys.stderr)
    print("       --requested-model-family <required-model-family> \\", file=sys.stderr)
    print("       --requested-effort Pro \\", file=sys.stderr)
    print("       --account-key <workspace/account-key> --deadline-at <ISO-8601>", file=sys.stderr)
    print(f"  3. python3 {round_script} slot-acquire \\", file=sys.stderr)
    print(f"       --request-id {request_id} --account-key <workspace/account-key> \\", file=sys.stderr)
    print("       --deadline-at <same-deadline>; queue if acquired=false.", file=sys.stderr)
    print(f"  4. python3 {stage_script} local --source {bundle_path} \\", file=sys.stderr)
    print(f"       --bridge-thread-id {thread_id}", file=sys.stderr)
    print("     Retain staged_file and staged_sha256, then run:", file=sys.stderr)
    print(f"       python3 {round_script} staging --request-id {request_id} \\", file=sys.stderr)
    print("       --staging-status verified --staged-file <staged-file> \\", file=sys.stderr)
    print("       --staged-sha256 <staged-sha256>", file=sys.stderr)
    print(f"  5. python3 {lease_script} --repo {repo} acquire \\", file=sys.stderr)
    print(f"       --holder <worker-id> --bridge-thread-id {thread_id} \\", file=sys.stderr)
    print("       --expected-conversation-id <reserved-chat-id>", file=sys.stderr)
    print("     Use DevTools MCP as the primary route; use the Chrome connector", file=sys.stderr)
    print("     only for the documented pre-submit fallback.", file=sys.stderr)
    print(f"  6. python3 {preflight_script} --repo {repo} \\", file=sys.stderr)
    print(f"       --bridge-thread-id {thread_id} \\", file=sys.stderr)
    print("       --dispatcher-thread-id <dispatcher-task-id> \\", file=sys.stderr)
    print("       --dispatcher-token <dispatcher-token> --browser-lease-token <token> \\", file=sys.stderr)
    print("       --requested-model-family <required-model-family> \\", file=sys.stderr)
    print("       --selected-model-family <exact-visible-family> \\", file=sys.stderr)
    print("       --requested-effort Pro --selected-effort Pro \\", file=sys.stderr)
    print(f"       --bundle {bundle_path} --staged-file <staged-file> \\", file=sys.stderr)
    print("       --attachment-name <visible-name> \\", file=sys.stderr)
    print("       --upload-control <observed-route> --expected-conversation-id <id> \\", file=sys.stderr)
    print("       --observed-conversation-id <id>", file=sys.stderr)
    print("  7. Record the boundary, then before Send run:", file=sys.stderr)
    print(f"       python3 {round_script} transition --request-id {request_id} \\", file=sys.stderr)
    print("       --to submitting --conversation-id <id> \\", file=sys.stderr)
    print("       --pre-submit-boundary <boundary> --selected-model-family <family> \\", file=sys.stderr)
    print("       --selected-effort Pro --model-selection-status verified", file=sys.stderr)
    print("     Send once. After visible acceptance run:", file=sys.stderr)
    print(f"       python3 {round_script} transition --request-id {request_id} \\", file=sys.stderr)
    print("       --to submitted --submitted-at <millisecond-ISO-8601>", file=sys.stderr)
    print(f"  8. python3 {lease_script} --repo {repo} release --token <token>", file=sys.stderr)
    print("  9. Poll the exact ChatGPT conversation with read_thread and pin", file=sys.stderr)
    print("     the matched completed, untruncated remote turn id.", file=sys.stderr)
    print(f" 10. python3 {save_script} --repo {repo} --bridge-thread-id {thread_id} \\", file=sys.stderr)
    print(f"       --request-id {request_id} --standalone --single-round \\", file=sys.stderr)
    print(f"       --bundle {bundle_path} --web-url <conversation-url> \\", file=sys.stderr)
    print("       --expected-conversation-id <id> --capture-route native-read-thread \\", file=sys.stderr)
    print("       --remote-turn-id <turn-id> --requested-model-family <required-family> \\", file=sys.stderr)
    print("       --selected-model-family <family> --attachment-name <visible-name> \\", file=sys.stderr)
    print("       --attachment-sha256 <staged-sha256> --upload-control <observed-route> \\", file=sys.stderr)
    print("       --requested-effort Pro --selected-effort Pro \\", file=sys.stderr)
    print("       --prompt-file <prompt> --answer-file <full-answer>", file=sys.stderr)
    print(" 11. For a long wait, create one current-task heartbeat from:", file=sys.stderr)
    print(f"       python3 {round_script} watcher-spec --request-id {request_id} \\", file=sys.stderr)
    print("       --target-thread-id <dispatcher-task-id>; save its automation ID.", file=sys.stderr)
    print(" 12. If native output is unavailable, ambiguous, or truncated, acquire", file=sys.stderr)
    print("     a new browser lease and capture only the pinned turn. Never resend.", file=sys.stderr)
    print(" 13. After the target is terminal, capture/verdict/deliver, then run:", file=sys.stderr)
    print(f"       python3 {round_script} slot-release --request-id {request_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
