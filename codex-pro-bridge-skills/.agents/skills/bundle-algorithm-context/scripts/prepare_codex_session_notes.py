#!/usr/bin/env python3
"""Write current Codex notes and record an immutable task snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[2] / ".shared"
sys.path.insert(0, str(SHARED_DIR))

from bridge_store import (  # noqa: E402
    BridgeError,
    append_event,
    atomic_write_text,
    bridge_root,
    default_codex_session_id,
    file_sha256,
    file_lock,
    load_events,
    legacy_project_binding_source,
    now_iso,
    parse_metadata,
    repo_relative,
    unique_artifact_path,
    validate_id,
    verify_thread_integrity,
    write_bound_metadata,
    write_session_index,
)
from project_store import BridgeProjectStore  # noqa: E402
from material_prompt import delivery_prompt  # noqa: E402


def freeze_inputs(repo, stem, inputs, source_bytes, request_bytes):
    prompt = delivery_prompt(json.loads(request_bytes)) if request_bytes is not None else inputs["question"]
    sources = {"inputs":(stem.with_suffix(".inputs.json"),json.dumps(inputs,ensure_ascii=False,sort_keys=True).encode()),
               "source_notes":(stem.with_suffix(".source-notes.md"),source_bytes),
               "delivery_prompt":(stem.with_suffix(".delivery-prompt.md"),prompt.encode())}
    if request_bytes is not None:
        sources["request"] = (stem.with_suffix(".request.json"),request_bytes)
    proof = {"request":None}
    for kind,(path,contents) in sources.items():
        if path.exists() and path.read_bytes() != contents:
            raise BridgeError("Immutable snapshot input proof drift")
        if not path.exists():
            atomic_write_text(path,contents.decode("utf-8"))
        proof[kind] = {"path":repo_relative(path,repo),"sha256":file_sha256(path)}
    return proof


def publish_input_receipt(repo, out, event, proof, handoff_file="", project_inference=None):
    if not out:
        return
    path = Path(out).resolve()
    if not path.is_relative_to(repo) or Path(out).is_symlink():
        raise BridgeError("Snapshot receipt must stay inside the repository")
    receipt = {"schema_version":"snapshot-inputs/v1","snapshot_event_id":event["event_id"],
               "snapshot_artifact":event["artifact"],"round_key":event["data"].get("round_key",""),
               "inputs_sha256":event["data"]["inputs_sha256"],"input_proof":proof}
    if project_inference:
        receipt["project_inference"] = project_inference
    if handoff_file:
        source = Path(handoff_file).resolve(strict=True)
        if not source.is_relative_to(repo):
            raise BridgeError("Round handoff must stay inside the repository")
        sha = file_sha256(source)
        handoff = json.loads(source.read_text(encoding="utf-8"))
        expected_job = hashlib.sha256((str(repo)+"\n"+sha).encode()).hexdigest()
        if (receipt["round_key"] != expected_job or handoff.get("request_sha256") != (proof["request"] or {}).get("sha256")
                or handoff.get("repo") != str(repo) or handoff.get("bridge_thread_id") != event["thread_id"]):
            raise BridgeError("Round handoff/job/request anchor mismatch")
        frozen = path.with_suffix(".handoff.json")
        contents = source.read_bytes()
        if frozen.exists() and frozen.read_bytes() != contents:
            raise BridgeError("Immutable prospective handoff drift")
        if not frozen.exists():
            atomic_write_text(frozen,contents.decode("utf-8"))
        receipt["job_handoff"] = {"path":repo_relative(frozen,repo),"sha256":sha}
    contents = json.dumps(receipt,ensure_ascii=False,sort_keys=True)+"\n"
    if path.exists() and path.read_text(encoding="utf-8") != contents:
        raise BridgeError("Immutable snapshot input receipt drift")
    if not path.exists():
        atomic_write_text(path,contents)


def freeze_project_inference(repo, stem, event):
    kind,source = legacy_project_binding_source(repo,event)
    digest = file_sha256(source)
    frozen = stem.with_suffix(".project-binding.md" if kind == "session-metadata/v1" else ".project-binding.jsonl")
    contents = source.read_bytes()
    if frozen.exists() and frozen.read_bytes() != contents:
        raise BridgeError("Immutable legacy Project binding drift")
    if not frozen.exists():
        atomic_write_text(frozen,contents.decode("utf-8"))
    return {"schema_version":"legacy-project-inference/v1","project":event["bridge_project_id"],
            "binding_kind":kind,"binding_source":{"path":repo_relative(source,repo),"sha256":digest},
            "binding":{"path":repo_relative(frozen,repo),"sha256":digest}}


def read_value(text: str, file_path: str) -> str:
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    return (text or "").strip()


def section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip() if body.strip() else '_Not recorded._'}\n"


def one_line(value: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[: limit - 3].rstrip() + "..." if len(text) > limit else text or "-"


def build_notes(
    *,
    thread_id: str,
    bridge_project_id: str,
    codex_session_id: str,
    title: str,
    goal: str,
    question: str,
    summary: str,
    raw_history: str,
    history_source: str,
    created_at: str,
    updated_at: str,
) -> str:
    raw = raw_history or (
        "_Raw Codex turns were not included. The detailed summary is the complete "
        "Codex-side context for this snapshot._"
    )
    return "\n".join(
        [
            "# Codex Session Notes",
            "",
            "## Metadata",
            f"- Bridge Thread ID: `{thread_id}`",
            f"- Bridge Project ID: `{bridge_project_id or '-'}`",
            f"- Codex Session ID: `{codex_session_id}`",
            f"- Title: {title}",
            f"- Created at: {created_at}",
            f"- Updated at: {updated_at}",
            f"- History source: {history_source}",
            "",
            section("Goal", goal),
            section("GPT Pro Question", question),
            section("Detailed Session Summary", summary),
            section("Recent Raw Conversation", raw),
            section(
                "Evidence Rules",
                "\n".join(
                    [
                        "- Treat the detailed summary as primary context.",
                        "- Use raw turns only for nuance; they may be incomplete.",
                        "- Do not infer repository facts that are absent from the attached evidence.",
                    ]
                ),
            ),
        ]
    ).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write current Codex notes and append an immutable codex-snapshot event."
    )
    parser.add_argument("--repo", default=".", help="Repository root.")
    parser.add_argument("--bridge-thread-id", required=True, help="Canonical task id.")
    parser.add_argument(
        "--bridge-project-id",
        default="",
        help="Optional parent Bridge Project. Existing standalone usage may omit it.",
    )
    parser.add_argument("--codex-session-id", default="", help="Defaults to <bridge-thread-id>-codex.")
    parser.add_argument("--title", default="", help="Task title.")
    parser.add_argument("--goal", default="", help="User goal.")
    parser.add_argument("--goal-file", default="", help="File containing the user goal.")
    parser.add_argument("--gpt-pro-question", default="", help="Question intended for GPT Pro.")
    parser.add_argument("--gpt-pro-question-file", default="", help="File containing the question.")
    parser.add_argument("--summary", default="", help="Detailed Codex summary.")
    parser.add_argument("--summary-file", default="", help="File containing the summary.")
    parser.add_argument("--raw-history", default="", help="Optional recent raw turns.")
    parser.add_argument("--raw-history-file", default="", help="File containing optional raw turns.")
    parser.add_argument("--history-source", default="", help="Examples: visible-codex-context, exported-transcript.")
    parser.add_argument("--round-key", default="", help="Stable job identity; retries reuse only identical snapshot inputs.")
    parser.add_argument("--round-request-file", default="", help="同round已冻结request的原始来源；复制为不可变证据")
    parser.add_argument("--input-receipt-out", default="", help="向当前job发布不可变snapshot输入收据，不修改旧事件")
    parser.add_argument("--round-handoff-file", default="", help="同job原hand-off摘要锚，防旧snapshot来源换绑")
    args = parser.parse_args()

    try:
        repo = Path(args.repo).resolve()
        thread = validate_id(args.bridge_thread_id, "bridge thread id")
        if not repo.is_dir():
            raise BridgeError("Repository root is not a directory")
        with file_lock(bridge_root(repo) / "threads" / f".{thread}.snapshot.lock"):
            return write_notes(args, parser)
    except (BridgeError, OSError) as exc:
        parser.error(str(exc))


def write_notes(args, parser) -> int:

    try:
        repo = Path(args.repo).resolve()
        if not repo.is_dir():
            raise BridgeError(f"Repository root is not a directory: {repo}")
        thread_id = validate_id(args.bridge_thread_id, "bridge thread id")
        codex_session_id = validate_id(
            args.codex_session_id or default_codex_session_id(thread_id), "Codex session id"
        )
        bridge_dir = bridge_root(repo)
        sessions_dir = bridge_dir / "codex-sessions"
        session_dir = sessions_dir / codex_session_id
        session_path = session_dir / "session.md"
        notes_path = session_dir / "notes.md"
        previous = parse_metadata(session_path)
        if previous.get("bridge_thread_id") not in (None,"",thread_id):
            raise BridgeError("Codex session is already bound to another thread")
        project_store = BridgeProjectStore(repo)
        bound_project = project_store.project_for_thread(thread_id)
        if previous.get("bridge_project_id") and bound_project and previous["bridge_project_id"] != bound_project:
            raise BridgeError("Codex session and unique Project store binding disagree")
        bridge_project_id = args.bridge_project_id or previous.get("bridge_project_id","") or bound_project
        if bridge_project_id:
            bridge_project_id = project_store.resolve_project_id(bridge_project_id)
        if (previous.get("bridge_project_id") not in (None,"",bridge_project_id)
                or bound_project not in ("",bridge_project_id)):
            raise BridgeError("Codex session and unique Project store binding disagree")
        goal = read_value(args.goal, args.goal_file)
        question = read_value(args.gpt_pro_question, args.gpt_pro_question_file)
        summary = read_value(args.summary, args.summary_file)
        raw_history = read_value(args.raw_history, args.raw_history_file)
        if not summary:
            raise BridgeError("Provide --summary or --summary-file")
        if args.history_source and args.history_source != "unavailable" and not raw_history:
            raise BridgeError(
                "A non-unavailable --history-source requires --raw-history or --raw-history-file"
            )

        round_key = validate_id(args.round_key, "round key") if args.round_key else ""
        inputs = {
            "thread": thread_id, "session": codex_session_id, "project": bridge_project_id,
            "goal": goal, "question": question, "summary": summary, "raw_history": raw_history,
            "history_source": args.history_source, "title": args.title,
        }
        inputs_sha256 = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        request_bytes = None
        if args.round_request_file:
            request_path = Path(args.round_request_file).resolve(strict=True)
            if not request_path.is_relative_to(repo) or not request_path.is_file():
                raise BridgeError("Round request must be a regular repository file")
            request_bytes = request_path.read_bytes()
            request = json.loads(request_bytes)
            if (request.get("repo") != str(repo) or request.get("bridge_thread_id") != thread_id
                    or request.get("goal", "").strip() != goal or request.get("question", "").strip() != question):
                raise BridgeError("Round request and snapshot inputs disagree")
        events = load_events(bridge_root(repo), thread_id)
        if round_key:
            # Session existence is not evidence of a snapshot for this round.
            # Check before writes, including when a worker crashed after append.
            matches = [e for e in events if e.get("dedupe_key") == f"codex-snapshot-round:{round_key}"]
            if matches:
                event = matches[0]
                if event.get("bridge_project_id","") != bridge_project_id:
                    raise BridgeError("Round snapshot Project changed or lacks an existing binding")
                project_inference = None
                needs_project_inference = False
                legacy_inputs = {**inputs,"project":""}
                legacy_sha = hashlib.sha256(json.dumps(legacy_inputs,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                if (not event.get("data",{}).get("input_proof") and bridge_project_id
                        and event.get("bridge_project_id") == bridge_project_id
                        and event.get("data",{}).get("inputs_sha256") == legacy_sha):
                    # Preserve the old nine-field hash. Only the original snapshot
                    # plus a qualified existing binding can prove its empty arg.
                    inputs,inputs_sha256 = legacy_inputs,legacy_sha
                    needs_project_inference = True
                if (len(matches) != 1 or event.get("event_type") != "codex-snapshot"
                        or event.get("data", {}).get("inputs_sha256") != inputs_sha256):
                    raise BridgeError("Round snapshot inputs changed")
                expected_request = hashlib.sha256(request_bytes).hexdigest() if request_bytes is not None else ""
                prior_proof = event.get("data", {}).get("input_proof")
                if prior_proof and (prior_proof.get("request") or {}).get("sha256", "") != expected_request:
                    raise BridgeError("Round snapshot request changed or lacks immutable input proof")
                verify_thread_integrity(repo, thread_id)
                position = events.index(event)
                if any(e.get("event_type") == "gpt-exchange" for e in events[position + 1:]):
                    raise BridgeError("Round snapshot was already consumed; use a new round key")
                if not prior_proof:
                    if not args.input_receipt_out or request_bytes is None or not args.round_handoff_file:
                        raise BridgeError("Legacy snapshot needs frozen current-job/request proof for prospective recovery")
                    stem = (repo/event["artifact"]["path"]).with_name("prospective-"+round_key)
                    if needs_project_inference:
                        project_inference = freeze_project_inference(repo,stem,event)
                    source_bytes = Path(args.summary_file).read_bytes() if args.summary_file else summary.encode()
                    prior_proof = freeze_inputs(repo,stem,inputs,source_bytes,request_bytes)
                publish_input_receipt(repo,args.input_receipt_out,event,prior_proof,args.round_handoff_file,project_inference)
                print(repo / event["artifact"]["path"])
                return 0
            if events:
                verify_thread_integrity(repo, thread_id)
                normal = [e for e in events if e.get("event_type") in
                          {"codex-snapshot", "gpt-exchange", "codex-verdict"}
                          and not e.get("data", {}).get("recovery_for_exchange")]
                if normal and normal[-1]["event_type"] == "gpt-exchange":
                    raise BridgeError("Record the pending Codex verdict before starting a new round")

        session_dir.mkdir(parents=True, exist_ok=True)
        if bridge_project_id:
            project_store.attach_thread(
                bridge_project_id,
                thread_id,
                title=args.title or goal or thread_id,
                goal=goal,
            )

        now = now_iso()
        created_at = previous.get("created_at", "") or now
        title = one_line(args.title or previous.get("title", "") or goal or thread_id, 120)
        history_source = args.history_source or (
            "visible-codex-context" if raw_history else "unavailable"
        )
        notes = build_notes(
            thread_id=thread_id,
            bridge_project_id=bridge_project_id,
            codex_session_id=codex_session_id,
            title=title,
            goal=goal,
            question=question,
            summary=summary,
            raw_history=raw_history,
            history_source=history_source,
            created_at=created_at,
            updated_at=now,
        )
        snapshot_path = unique_artifact_path(session_dir / "snapshots", "notes", ".md")
        atomic_write_text(snapshot_path, notes)
        source_bytes = Path(args.summary_file).read_bytes() if args.summary_file else summary.encode("utf-8")
        input_proof = freeze_inputs(repo,snapshot_path,inputs,source_bytes,request_bytes)
        atomic_write_text(notes_path, notes)

        write_bound_metadata(
            session_path,
            {
                "codex_session_id": codex_session_id,
                "bridge_thread_id": thread_id,
                "bridge_project_id": bridge_project_id,
                "title": title,
                "history_source": history_source,
                "created_at": created_at,
                "last_used_at": now,
                "notes_path": repo_relative(notes_path, repo),
                "latest_snapshot": repo_relative(snapshot_path, repo),
            },
            ordered_keys=(
                "codex_session_id",
                "bridge_thread_id",
                "bridge_project_id",
                "title",
                "history_source",
                "created_at",
                "last_used_at",
                "notes_path",
                "latest_snapshot",
            ),
            immutable_keys=(
                "codex_session_id",
                "bridge_thread_id",
                "bridge_project_id",
                "created_at",
            ),
        )
        write_session_index(sessions_dir, kind="codex")
        snapshot_rel = repo_relative(snapshot_path, repo)
        event = append_event(
            repo,
            thread_id=thread_id,
            event_type="codex-snapshot",
            actor="codex",
            thread_title=title,
            bridge_project_id=bridge_project_id,
            codex_session_id=codex_session_id,
            artifact={
                "kind": "codex-notes",
                "path": snapshot_rel,
                "sha256": file_sha256(snapshot_path),
            },
            data={
                "goal": one_line(goal),
                "summary": one_line(summary),
                "question": one_line(question),
                "history_source": history_source,
                "current_notes": repo_relative(notes_path, repo),
                "inputs_sha256": inputs_sha256, "input_proof": input_proof,
                **({"round_key": round_key, "inputs_sha256": inputs_sha256} if round_key else {}),
            },
            dedupe_key=f"codex-snapshot-round:{round_key}" if round_key else f"codex-snapshot:{snapshot_rel}",
            occurred_at=now,
        )
        publish_input_receipt(repo,args.input_receipt_out,event,input_proof,args.round_handoff_file)
        print(notes_path)
        print(f"Immutable snapshot: {snapshot_path}", file=sys.stderr)
        return 0
    except (BridgeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
