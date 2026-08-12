#!/usr/bin/env python3
"""Open one high-frequency, standalone GPT Pro Review Probe.

A Review Probe is the fast lane for high-frequency, fine-grained, parallel
review of a single idea, research proposal, or atomic sub-task. It is a
standalone, single-round Bridge Thread that NEVER attaches to a Bridge Project,
so it never consults Project source-sync and is never blocked when unrelated
shared sources are stale. Many probes run in parallel because each is a distinct
thread with its own ledger; only browser-mutating critical sections are
serialized by the repository-local advisory browser lease. Separate worktrees
still require one declared dispatcher.

This orchestrator prepares the immutable Codex snapshot and builds the standalone
evidence bundle. It deliberately does NOT import BridgeProjectStore and never
runs source-sync. It stops before the browser step and prints the exact next
handoff (lease -> preflight -> Send -> release -> native read -> capture) so the
browser seam stays an explicit, verifiable human/agent action.
"""

from __future__ import annotations

import argparse
import datetime as dt
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

    lease_script = QW_SCRIPTS / "manage_browser_lease.py"
    preflight_script = QW_SCRIPTS / "check_browser_preflight.py"
    save_script = QW_SCRIPTS / "save_bridge_turn.py"

    print(f"probe_thread_id: {thread_id}")
    print(f"codex_notes: {notes_path}")
    print(f"bundle: {bundle_path}")
    print()
    print("Next (release the browser while ChatGPT generates):", file=sys.stderr)
    print(f"  1. python3 {lease_script} --repo {repo} acquire \\", file=sys.stderr)
    print(f"       --holder <worker-id> --bridge-thread-id {thread_id} \\", file=sys.stderr)
    print("       --expected-conversation-id <reserved-chat-id>", file=sys.stderr)
    print("     Select DevTools MCP or the Codex Chrome connector per", file=sys.stderr)
    print("     gpt-pro-question-window/references/browser_adapters.md.", file=sys.stderr)
    print(f"  2. python3 {preflight_script} --repo {repo} \\", file=sys.stderr)
    print(f"       --bridge-thread-id {thread_id} --browser-lease-token <token> \\", file=sys.stderr)
    print("       --requested-model Pro \\", file=sys.stderr)
    print("       --selected-ui-label Pro --bundle <abs-bundle-path> \\", file=sys.stderr)
    print("       --attachment-name <visible-name> --upload-control <observed-route> \\", file=sys.stderr)
    print("       --expected-conversation-id <id> --observed-conversation-id <id>", file=sys.stderr)
    print("  3. Before Send, record existing turn IDs/cursor and prompt digest.", file=sys.stderr)
    print("     Click Send once; after acceptance, record submitted_at and the", file=sys.stderr)
    print("     target turn ID when available. Do not wait in the browser.", file=sys.stderr)
    print(f"  4. python3 {lease_script} --repo {repo} release --token <token>", file=sys.stderr)
    print("  5. Poll the exact ChatGPT conversation with Codex read_thread; pin", file=sys.stderr)
    print("     the matched completed, untruncated remote turn id.", file=sys.stderr)
    print(f"  6. python3 {save_script} --repo {repo} --bridge-thread-id {thread_id} \\", file=sys.stderr)
    print(f"       --standalone --single-round --bundle {bundle_path} \\", file=sys.stderr)
    print("       --web-url <conversation-url> --expected-conversation-id <id> \\", file=sys.stderr)
    print("       --capture-route native-read-thread --remote-turn-id <turn-id> \\", file=sys.stderr)
    print("       --requested-model Pro --selected-ui-label Pro \\", file=sys.stderr)
    print("       --prompt-file <prompt> --answer-file <full-answer>", file=sys.stderr)
    print("  7. If native output is unavailable, ambiguous, or truncated, acquire", file=sys.stderr)
    print("     a new lease; locate the pinned turn, not the latest response; and", file=sys.stderr)
    print("     capture with --capture-route browser-fallback,", file=sys.stderr)
    print("     --remote-turn-id <same-turn-id>, and the new", file=sys.stderr)
    print("     --browser-lease-token. Release that lease afterward.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
