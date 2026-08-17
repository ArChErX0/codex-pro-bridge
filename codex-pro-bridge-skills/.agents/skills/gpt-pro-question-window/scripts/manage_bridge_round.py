#!/usr/bin/env python3
"""Manage recoverable Codex Pro Bridge rounds and account-level generation slots.

The mutable state written by this command is operational state.  It is not a
fourth canonical Bridge event: ``gpt-exchange`` is still appended only after a
full answer has been captured by ``save_bridge_turn.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 2
FAST_DEGRADED_THRESHOLD_MS = 60_000
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATUSES = {"captured", "verdict_recorded", "failed", "timed_out"}
ROUND_STATUSES = {
    "prepared",
    "queued",
    "submitting",
    "submitted",
    "running",
    "submit_ambiguous",
    "capture_needed",
    "captured",
    "verdict_recorded",
    "failed",
    "timed_out",
}
ALLOWED_TRANSITIONS = {
    "prepared": {"queued", "submitting", "failed", "timed_out"},
    "queued": {"submitting", "failed", "timed_out"},
    "submitting": {"submitted", "submit_ambiguous", "failed", "timed_out"},
    "submit_ambiguous": {"submitted", "failed", "timed_out"},
    "submitted": {"running", "capture_needed", "failed", "timed_out"},
    "running": {"capture_needed", "failed", "timed_out"},
    "capture_needed": {"captured", "failed"},
    "captured": {"verdict_recorded"},
    "verdict_recorded": set(),
    "failed": set(),
    "timed_out": set(),
}
SET_ONCE_FIELDS = {
    "conversation_id",
    "pre_submit_boundary",
    "remote_turn_id",
    "submitted_at",
    "generation_observed_at",
    "response_completed_at",
    "selected_model_family",
    "selected_effort",
    "capture_route",
    "capture_path",
    "capture_sha256",
    "verdict_path",
    "verdict_sha256",
}


class RoundError(ValueError):
    """Raised when a round mutation would be ambiguous or unsafe."""


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def parse_timestamp(value: str, flag: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise RoundError(f"{flag} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RoundError(f"{flag} must include a timezone offset")
    return parsed


def validate_request_id(value: str) -> str:
    value = (value or "").strip()
    if not REQUEST_ID_RE.fullmatch(value):
        raise RoundError(
            "request id must be 1-128 letters, digits, dots, underscores, or hyphens"
        )
    return value


def validate_sha256(value: str, flag: str, *, required: bool = False) -> str:
    value = (value or "").strip().lower()
    if required and not value:
        raise RoundError(f"{flag} is required")
    if value and not SHA256_RE.fullmatch(value):
        raise RoundError(f"{flag} must be a lowercase SHA-256 digest")
    return value


def classify_execution(
    submitted_at: str,
    response_completed_at: str,
    explicit_degradation_reason: str = "",
) -> tuple[int | None, str]:
    """Return measured elapsed milliseconds and the evidence-bounded status."""
    reason = explicit_degradation_reason.strip()
    if not submitted_at or not response_completed_at:
        return None, "degraded_explicit" if reason else "unknown"
    submitted = parse_timestamp(submitted_at, "submitted_at")
    completed = parse_timestamp(response_completed_at, "response_completed_at")
    elapsed_ms = round((completed - submitted).total_seconds() * 1000)
    if elapsed_ms < 0:
        raise RoundError("response_completed_at cannot precede submitted_at")
    if reason:
        return elapsed_ms, "degraded_explicit"
    if elapsed_ms < FAST_DEGRADED_THRESHOLD_MS:
        return elapsed_ms, "degraded_fast"
    return elapsed_ms, "not_fast_degraded"


def state_root(explicit: str = "") -> Path:
    configured = explicit.strip() or os.environ.get("CODEX_PRO_BRIDGE_STATE_DIR", "").strip()
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".codex" / "state" / "codex-pro-bridge"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def round_path(root: Path, request_id: str) -> Path:
    return root / "rounds" / f"{validate_request_id(request_id)}.json"


def round_lock_path(root: Path, request_id: str) -> Path:
    return root / "locks" / f"round-{validate_request_id(request_id)}.lock"


def account_hash(account_key: str) -> str:
    value = (account_key or "").strip()
    if not value:
        raise RoundError("account key is required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def slot_path(root: Path, account_key: str) -> Path:
    return root / "locks" / f"pro-generation-{account_hash(account_key)[:24]}.lease.json"


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        path.chmod(0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RoundError(f"invalid state file: {path}") from exc
    if not isinstance(data, dict):
        raise RoundError(f"state file is not an object: {path}")
    return data


def read_round(root: Path, request_id: str) -> dict[str, Any]:
    path = round_path(root, request_id)
    state = read_json(path)
    if not state:
        raise RoundError(f"unknown request id: {request_id}")
    return state


def _set_once(state: dict[str, Any], field: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    previous = str(state.get(field, ""))
    if previous and previous != value:
        raise RoundError(f"{field} is already fixed to a different value")
    state[field] = value


def _record_transition(state: dict[str, Any], previous: str, target: str, at: str) -> None:
    if previous == target:
        return
    history = list(state.get("transitions", []))
    history.append({"from": previous, "to": target, "at": at})
    state["transitions"] = history[-100:]


def command_create(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    deadline_at = args.deadline_at.strip()
    parse_timestamp(deadline_at, "--deadline-at")
    bundle_sha256 = validate_sha256(args.bundle_sha256, "--bundle-sha256")
    prompt_sha256 = validate_sha256(args.prompt_sha256, "--prompt-sha256", required=True)
    bridge_repo = Path(args.bridge_repo).expanduser().resolve()
    if not bridge_repo.is_dir():
        raise RoundError(f"Bridge repository is not a directory: {bridge_repo}")
    immutable = {
        "request_id": request_id,
        "bridge_thread_id": args.bridge_thread_id.strip(),
        "source_kind": args.source_kind,
        "source_thread_id": args.source_thread_id.strip(),
        "source_host_id": args.source_host_id.strip(),
        "ssh_alias": args.ssh_alias.strip(),
        "source_repo": args.source_repo.strip(),
        "bridge_repo": str(bridge_repo),
        "bundle_path": args.bundle_path.strip(),
        "bundle_sha256": bundle_sha256,
        "prompt_sha256": prompt_sha256,
        "requested_model_family": args.requested_model_family.strip(),
        "requested_effort": args.requested_effort.strip(),
        "account_key_sha256": account_hash(args.account_key),
        "deadline_at": deadline_at,
    }
    if not immutable["bridge_thread_id"]:
        raise RoundError("--bridge-thread-id is required")
    if not immutable["requested_model_family"]:
        raise RoundError("--requested-model-family is required")
    if not immutable["requested_effort"]:
        raise RoundError("--requested-effort is required")
    if args.source_kind == "ssh":
        required_remote = {
            "--source-thread-id": immutable["source_thread_id"],
            "--source-host-id": immutable["source_host_id"],
            "--ssh-alias": immutable["ssh_alias"],
            "--source-repo": immutable["source_repo"],
            "--bundle-path": immutable["bundle_path"],
            "--bundle-sha256": immutable["bundle_sha256"],
        }
        missing_remote = [flag for flag, value in required_remote.items() if not value]
        if missing_remote:
            raise RoundError("SSH rounds require " + ", ".join(missing_remote))
    path = round_path(root, request_id)
    with file_lock(round_lock_path(root, request_id)):
        existing = read_json(path)
        if existing:
            conflicts = [
                key for key, value in immutable.items() if existing.get(key, "") != value
            ]
            if conflicts:
                raise RoundError(
                    "request id already exists with conflicting immutable fields: "
                    + ", ".join(conflicts)
                )
            return {"created": False, "round": existing, "path": str(path)}
        created_at = now_iso()
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            **immutable,
            "status": "prepared",
            "conversation_id": args.conversation_id.strip(),
            "pre_submit_boundary": args.pre_submit_boundary.strip(),
            "selected_model_family": "",
            "selected_effort": "",
            "model_selection_status": "unverified",
            "execution_status": "unknown",
            "response_elapsed_ms": None,
            "explicit_degradation_reason": "",
            "remote_turn_id": "",
            "submitted_at": "",
            "generation_observed_at": "",
            "response_completed_at": "",
            "capture_route": "",
            "capture_path": "",
            "capture_sha256": "",
            "verdict_path": "",
            "verdict_sha256": "",
            "delivery_status": "not_ready",
            "delivery_attempts": 0,
            "staging_status": "pending" if bundle_sha256 else "not_required",
            "staged_file": "",
            "staged_name": "",
            "staged_sha256": "",
            "watcher_id": "",
            "watcher_status": "not_created",
            "generation_slot_status": "not_acquired",
            "generation_slot_token": "",
            "failure_code": "",
            "failure_detail": "",
            "recovered_diagnostics": [],
            "created_at": created_at,
            "updated_at": created_at,
            "transitions": [],
        }
        atomic_write_json(path, state)
        return {"created": True, "round": state, "path": str(path)}


def command_transition(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    path = round_path(root, request_id)
    with file_lock(round_lock_path(root, request_id)):
        state = read_round(root, request_id)
        previous = str(state.get("status", ""))
        target = args.to
        if previous != target and target not in ALLOWED_TRANSITIONS.get(previous, set()):
            raise RoundError(f"unsafe round transition: {previous!r} -> {target!r}")
        values = {
            "conversation_id": args.conversation_id,
            "pre_submit_boundary": args.pre_submit_boundary,
            "remote_turn_id": args.remote_turn_id,
            "submitted_at": args.submitted_at,
            "generation_observed_at": args.generation_observed_at,
            "response_completed_at": args.response_completed_at,
            "selected_model_family": args.selected_model_family,
            "selected_effort": args.selected_effort,
            "capture_route": args.capture_route,
            "capture_path": args.capture_path,
            "capture_sha256": validate_sha256(args.capture_sha256, "--capture-sha256"),
            "verdict_path": args.verdict_path,
            "verdict_sha256": validate_sha256(args.verdict_sha256, "--verdict-sha256"),
        }
        for field, value in values.items():
            if field in SET_ONCE_FIELDS:
                _set_once(state, field, value)
        for field in ("submitted_at", "generation_observed_at", "response_completed_at"):
            if state.get(field):
                parse_timestamp(str(state[field]), f"--{field.replace('_', '-')}")
        if args.model_selection_status:
            state["model_selection_status"] = args.model_selection_status
        if args.explicit_degradation_reason:
            state["explicit_degradation_reason"] = args.explicit_degradation_reason.strip()
        if args.failure_code:
            state["failure_code"] = args.failure_code.strip()
        if args.failure_detail:
            state["failure_detail"] = args.failure_detail.strip()[:1000]

        if previous == "submit_ambiguous" and target == "submitted":
            recovered_code = str(state.get("failure_code", "")).strip()
            recovered_detail = str(state.get("failure_detail", "")).strip()
            if recovered_code:
                recovered = list(state.get("recovered_diagnostics", []))
                recovered.append(
                    {
                        "code": recovered_code,
                        "detail": recovered_detail,
                        "recovered_at": now_iso(),
                        "recovered_by_transition": "submit_ambiguous->submitted",
                    }
                )
                state["recovered_diagnostics"] = recovered[-50:]
                state["failure_code"] = ""
                state["failure_detail"] = ""

        if target == "submitting":
            if state.get("model_selection_status") != "verified":
                raise RoundError("submitting requires verified model selection")
            for required in ("selected_model_family", "selected_effort"):
                if not state.get(required):
                    raise RoundError(f"submitting requires {required}")
        if target in {"submitted", "running", "capture_needed"}:
            for required in ("conversation_id", "submitted_at", "prompt_sha256"):
                if not state.get(required):
                    raise RoundError(f"{target} requires {required}")
        if target == "capture_needed":
            if not state.get("response_completed_at") or not state.get("remote_turn_id"):
                raise RoundError("capture_needed requires response_completed_at and remote_turn_id")
            elapsed_ms, execution_status = classify_execution(
                str(state.get("submitted_at", "")),
                str(state.get("response_completed_at", "")),
                str(state.get("explicit_degradation_reason", "")),
            )
            state["response_elapsed_ms"] = elapsed_ms
            state["execution_status"] = execution_status
        if target == "captured":
            for required in ("capture_route", "capture_path", "capture_sha256"):
                if not state.get(required):
                    raise RoundError(f"captured requires {required}")
            state["delivery_status"] = (
                "pending" if state.get("source_kind") == "ssh" else "not_required"
            )
        if target == "verdict_recorded":
            for required in ("verdict_path", "verdict_sha256"):
                if not state.get(required):
                    raise RoundError(f"verdict_recorded requires {required}")
        if target in {"failed", "timed_out", "submit_ambiguous"} and not state.get(
            "failure_code"
        ):
            raise RoundError(f"{target} requires --failure-code")

        changed_at = now_iso()
        _record_transition(state, previous, target, changed_at)
        state["status"] = target
        state["updated_at"] = changed_at
        atomic_write_json(path, state)
        return state


def command_delivery(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    path = round_path(root, request_id)
    with file_lock(round_lock_path(root, request_id)):
        state = read_round(root, request_id)
        if state.get("status") not in {"captured", "verdict_recorded"}:
            raise RoundError("delivery is allowed only after capture")
        state["delivery_status"] = args.delivery_status
        state["delivery_attempts"] = int(state.get("delivery_attempts", 0)) + 1
        if args.result_path:
            state["delivery_result_path"] = args.result_path.strip()
        if args.result_sha256:
            state["delivery_result_sha256"] = validate_sha256(
                args.result_sha256, "--result-sha256"
            )
        if args.detail:
            state["delivery_detail"] = args.detail.strip()[:1000]
        if args.delivery_status == "delivered":
            state["delivered_at"] = now_iso()
        elif args.delivery_status == "not_required":
            if state.get("source_kind") != "local":
                raise RoundError("not_required delivery is valid only for local rounds")
            state["delivery_not_required_at"] = now_iso()
        state["updated_at"] = now_iso()
        atomic_write_json(path, state)
        return state


def command_staging(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    path = round_path(root, request_id)
    with file_lock(round_lock_path(root, request_id)):
        state = read_round(root, request_id)
        if args.staging_status == "verified":
            staged_file = Path(args.staged_file).expanduser().resolve()
            if not staged_file.is_file():
                raise RoundError(f"staged file does not exist: {staged_file}")
            staged_sha256 = validate_sha256(
                args.staged_sha256, "--staged-sha256", required=True
            )
            bundle_sha256 = str(state.get("bundle_sha256", ""))
            if bundle_sha256 and staged_sha256 != bundle_sha256:
                raise RoundError("staged SHA-256 does not match the round bundle")
            state["staged_file"] = str(staged_file)
            state["staged_name"] = staged_file.name
            state["staged_sha256"] = staged_sha256
            state["staging_verified_at"] = now_iso()
        elif args.staging_status == "failed":
            if not args.detail.strip():
                raise RoundError("failed staging requires --detail")
            state["staging_detail"] = args.detail.strip()[:1000]
        elif args.staging_status == "cleaned":
            if state.get("staging_status") != "verified":
                raise RoundError("only verified staging can be marked cleaned")
            state["staging_cleaned_at"] = now_iso()
        state["staging_status"] = args.staging_status
        state["updated_at"] = now_iso()
        atomic_write_json(path, state)
        return state


def command_watcher(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    path = round_path(root, request_id)
    with file_lock(round_lock_path(root, request_id)):
        state = read_round(root, request_id)
        if args.watcher_status == "active":
            if not args.automation_id.strip():
                raise RoundError("active watcher requires --automation-id")
            _set_once(state, "watcher_id", args.automation_id)
        elif args.automation_id.strip():
            _set_once(state, "watcher_id", args.automation_id)
        if args.watcher_status != "active" and not state.get("watcher_id"):
            raise RoundError(
                f"{args.watcher_status} watcher state requires a recorded automation id"
            )
        if args.watcher_status == "cleanup_failed" and not args.detail.strip():
            raise RoundError("cleanup_failed watcher state requires --detail")
        if args.detail.strip():
            state["watcher_detail"] = args.detail.strip()[:1000]
        if args.watcher_status == "deleted":
            state["watcher_deleted_at"] = now_iso()
        elif args.watcher_status == "paused":
            state["watcher_paused_at"] = now_iso()
        elif args.watcher_status == "cleanup_failed":
            state["watcher_cleanup_failed_at"] = now_iso()
        state["watcher_status"] = args.watcher_status
        state["updated_at"] = now_iso()
        atomic_write_json(path, state)
        return state


def command_slot_acquire(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    deadline = parse_timestamp(args.deadline_at, "--deadline-at")
    path = slot_path(root, args.account_key)
    lock = path.with_name(f".{path.name}.lock")
    with file_lock(lock):
        current = read_json(path)
        if current and current.get("request_id") == request_id:
            result = {"acquired": True, "idempotent": True, **current}
        elif current and current.get("request_id"):
            current_deadline = parse_timestamp(str(current.get("deadline_at", "")), "slot deadline")
            reason = (
                "expired-recovery-required"
                if current_deadline <= dt.datetime.now().astimezone()
                else "held"
            )
            result = {
                "acquired": False,
                "reason": reason,
                "holder_request_id": current.get("request_id"),
                "deadline_at": current.get("deadline_at"),
            }
        else:
            token = uuid.uuid4().hex
            slot = {
                "schema_version": SCHEMA_VERSION,
                "scope": "chatgpt-account",
                "account_key_sha256": account_hash(args.account_key),
                "request_id": request_id,
                "token": token,
                "acquired_at": now_iso(),
                "deadline_at": deadline.isoformat(timespec="milliseconds"),
            }
            atomic_write_json(path, slot)
            result = {"acquired": True, "idempotent": False, **slot}
    if result.get("acquired"):
        round_file = round_path(root, request_id)
        if round_file.exists():
            with file_lock(round_lock_path(root, request_id)):
                state = read_round(root, request_id)
                state["generation_slot_status"] = "acquired"
                state["generation_slot_token"] = str(result.get("token", ""))
                state["generation_slot_path"] = str(path)
                state["generation_slot_holder_request_id"] = ""
                state["generation_slot_holder_deadline_at"] = ""
                state["updated_at"] = now_iso()
                atomic_write_json(round_file, state)
    else:
        round_file = round_path(root, request_id)
        if round_file.exists():
            with file_lock(round_lock_path(root, request_id)):
                state = read_round(root, request_id)
                changed_at = now_iso()
                if state.get("status") == "prepared":
                    _record_transition(state, "prepared", "queued", changed_at)
                    state["status"] = "queued"
                state["generation_slot_status"] = str(result.get("reason", "held"))
                state["generation_slot_holder_request_id"] = str(
                    result.get("holder_request_id", "")
                )
                state["generation_slot_holder_deadline_at"] = str(
                    result.get("deadline_at", "")
                )
                state["updated_at"] = changed_at
                atomic_write_json(round_file, state)
    return result


def command_slot_release(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    round_file = round_path(root, request_id)
    round_state = read_round(root, request_id) if round_file.exists() else {}
    if args.account_key.strip():
        path = slot_path(root, args.account_key)
    else:
        raw_path = str(round_state.get("generation_slot_path", ""))
        path = Path(raw_path).resolve() if raw_path else Path()
        expected_root = (root / "locks").resolve()
        if (
            not raw_path
            or path.parent != expected_root
            or not path.name.startswith("pro-generation-")
        ):
            raise RoundError("--account-key is required when the round has no saved slot path")
    token = args.token.strip() or str(round_state.get("generation_slot_token", ""))
    if not token:
        raise RoundError("--token is required when the round has no saved slot token")
    lock = path.with_name(f".{path.name}.lock")
    with file_lock(lock):
        current = read_json(path)
        if not current:
            released = False
        else:
            if current.get("request_id") != request_id or current.get("token") != token:
                raise RoundError("generation slot holder or token mismatch")
            atomic_write_json(path, {})
            released = True
    if round_file.exists():
        with file_lock(round_lock_path(root, request_id)):
            state = read_round(root, request_id)
            state["generation_slot_status"] = "released"
            state["generation_slot_token"] = ""
            state["generation_slot_released_at"] = now_iso()
            state["updated_at"] = now_iso()
            atomic_write_json(round_file, state)
    return {"released": released, "request_id": request_id}


def command_slot_status(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    current = read_json(slot_path(root, args.account_key))
    if current:
        current = {key: value for key, value in current.items() if key != "token"}
    return current


def watcher_prompt(state: Mapping[str, Any], script: Path) -> str:
    required = ("conversation_id", "prompt_sha256", "deadline_at")
    missing = [field for field in required if not state.get(field)]
    if missing:
        raise RoundError("watcher requires: " + ", ".join(missing))
    boundary = str(state.get("pre_submit_boundary", "")) or "<empty-before-first-turn>"
    return f"""Use $gpt-pro-question-window to resume exactly one submitted Bridge round.

Request ID: {state['request_id']}
Bridge thread ID: {state['bridge_thread_id']}
ChatGPT conversation ID: {state['conversation_id']}
Pre-submit boundary: {boundary}
Prompt SHA-256: {state['prompt_sha256']}
Deadline: {state['deadline_at']}
Round state command: python3 {script} status --request-id {state['request_id']}
Bridge repository: {state['bridge_repo']}
Source task ID: {state.get('source_thread_id') or '<current-local-task>'}
Source host ID: {state.get('source_host_id') or '<local>'}

Read only that exact ChatGPT conversation. Match the new user turn after the saved boundary by prompt digest and pin its remote turn ID. Never select by title or merely take the latest turn. Never send or resubmit a prompt.

If the target is still absent or generating before the deadline, stay silent and leave canonical Bridge state unchanged. If it is complete and untruncated, persist the exact timestamps and full answer once, classify elapsed time below 60000 ms as degraded_fast, capture through save_bridge_turn.py in the saved Bridge repository, verify locally, deliver by the saved source task/request ID, release the generation slot with `python3 {script} slot-release --request-id {state['request_id']}`, then delete this automation before one terminal notification. A duration of at least 60000 ms is only not_fast_degraded, not proof of full Pro execution.

If the target is complete but truncated, pin the same remote turn ID and use one browser fallback under a fresh browser lease. Export that exact assistant element's innerHTML and convert it with capture_browser_markdown.py; never persist innerText. If the lease is busy before the deadline, stay silent and retry on the next heartbeat. On ambiguity, explicit remote failure, or deadline, persist diagnostics, release the generation slot only after the target is known terminal, delete or pause this automation, and notify once. Retry delivery only; never retry submission automatically."""


def command_watcher_spec(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    state = read_round(root, args.request_id)
    return {
        "request_id": state["request_id"],
        "target_thread_id": args.target_thread_id.strip(),
        "kind": "heartbeat",
        "name": f"Pro Bridge {state['request_id'][:24]}",
        "rrule": args.rrule,
        "prompt": watcher_prompt(state, Path(__file__).resolve()),
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    return read_round(state_root(args.state_dir), args.request_id)


def command_list(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    rounds = []
    for path in sorted((root / "rounds").glob("*.json")) if (root / "rounds").exists() else []:
        state = read_json(path)
        if args.active_only and (
            state.get("status") in TERMINAL_STATUSES
            and state.get("delivery_status") in {"delivered", "not_ready", "not_required"}
            and state.get("watcher_status") in {"deleted", "paused", "not_created"}
        ):
            continue
        rounds.append(state)
    return {"count": len(rounds), "rounds": rounds}


def command_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    """Repair only deterministic mutable-state inconsistencies from older rounds."""
    root = state_root(args.state_dir)
    request_id = validate_request_id(args.request_id)
    path = round_path(root, request_id)
    with file_lock(round_lock_path(root, request_id)):
        state = read_round(root, request_id)
        repairs: list[str] = []
        issues: list[str] = []

        transitions = list(state.get("transitions", []))
        recovered_submission = any(
            item.get("from") == "submit_ambiguous" and item.get("to") == "submitted"
            for item in transitions
            if isinstance(item, Mapping)
        )
        if recovered_submission and str(state.get("failure_code", "")).strip():
            recovered = list(state.get("recovered_diagnostics", []))
            recovered.append(
                {
                    "code": str(state.get("failure_code", "")),
                    "detail": str(state.get("failure_detail", "")),
                    "recovered_at": now_iso(),
                    "recovered_by_transition": "historical-reconciliation",
                }
            )
            state["recovered_diagnostics"] = recovered[-50:]
            state["failure_code"] = ""
            state["failure_detail"] = ""
            repairs.append("archived_recovered_failure")

        model_fields = (
            str(state.get("requested_model_family", "")).strip(),
            str(state.get("selected_model_family", "")).strip(),
            str(state.get("requested_effort", "")).strip(),
            str(state.get("selected_effort", "")).strip(),
        )
        complete_model = all(model_fields)
        exact_model = model_fields[0] == model_fields[1] and model_fields[2] == model_fields[3]
        truthful_status = (
            "verified" if complete_model and exact_model else "mismatch" if complete_model else "unverified"
        )
        if state.get("model_selection_status") != truthful_status:
            corrections = list(state.get("provenance_corrections", []))
            corrections.append(
                {
                    "field": "model_selection_status",
                    "from": state.get("model_selection_status", ""),
                    "to": truthful_status,
                    "at": now_iso(),
                    "reason": "exact-family-and-effort-reconciliation",
                }
            )
            state["provenance_corrections"] = corrections[-50:]
            state["model_selection_status"] = truthful_status
            repairs.append("corrected_model_selection_status")

        if state.get("source_kind") == "local" and state.get("delivery_status") == "pending":
            state["delivery_status"] = "not_required"
            state["delivery_not_required_at"] = now_iso()
            repairs.append("closed_local_delivery")
        if state.get("source_kind") == "ssh":
            missing = [
                field
                for field in ("source_thread_id", "source_host_id", "ssh_alias", "source_repo")
                if not str(state.get(field, "")).strip()
            ]
            if missing:
                issues.append("missing_ssh_callback_identity:" + ",".join(missing))

        state["last_reconciled_at"] = now_iso()
        state["updated_at"] = now_iso()
        atomic_write_json(path, state)
        return {"request_id": request_id, "repairs": repairs, "issues": issues, "round": state}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage recoverable Pro Bridge operational round state."
    )
    parser.add_argument("--state-dir", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--request-id", required=True)
    create.add_argument("--bridge-thread-id", required=True)
    create.add_argument("--bridge-repo", default=".")
    create.add_argument("--source-kind", choices=("local", "ssh"), required=True)
    create.add_argument("--source-thread-id", default="")
    create.add_argument("--source-host-id", default="")
    create.add_argument("--ssh-alias", default="")
    create.add_argument("--source-repo", default="")
    create.add_argument("--bundle-path", default="")
    create.add_argument("--bundle-sha256", default="")
    create.add_argument("--prompt-sha256", required=True)
    create.add_argument("--requested-model-family", default="")
    create.add_argument("--requested-effort", required=True)
    create.add_argument("--account-key", required=True)
    create.add_argument("--conversation-id", default="")
    create.add_argument("--pre-submit-boundary", default="")
    create.add_argument("--deadline-at", required=True)

    transition = sub.add_parser("transition")
    transition.add_argument("--request-id", required=True)
    transition.add_argument("--to", choices=sorted(ROUND_STATUSES), required=True)
    transition.add_argument("--conversation-id", default="")
    transition.add_argument("--pre-submit-boundary", default="")
    transition.add_argument("--remote-turn-id", default="")
    transition.add_argument("--submitted-at", default="")
    transition.add_argument("--generation-observed-at", default="")
    transition.add_argument("--response-completed-at", default="")
    transition.add_argument("--selected-model-family", default="")
    transition.add_argument("--selected-effort", default="")
    transition.add_argument(
        "--model-selection-status",
        choices=("verified", "mismatch", "unverified"),
        default="",
    )
    transition.add_argument("--explicit-degradation-reason", default="")
    transition.add_argument(
        "--capture-route",
        choices=("", "native-read-thread", "browser-fallback"),
        default="",
    )
    transition.add_argument("--capture-path", default="")
    transition.add_argument("--capture-sha256", default="")
    transition.add_argument("--verdict-path", default="")
    transition.add_argument("--verdict-sha256", default="")
    transition.add_argument("--failure-code", default="")
    transition.add_argument("--failure-detail", default="")

    delivery = sub.add_parser("delivery")
    delivery.add_argument("--request-id", required=True)
    delivery.add_argument(
        "--delivery-status",
        choices=("pending", "delivered", "failed", "not_required"),
        required=True,
    )
    delivery.add_argument("--result-path", default="")
    delivery.add_argument("--result-sha256", default="")
    delivery.add_argument("--detail", default="")

    staging = sub.add_parser("staging")
    staging.add_argument("--request-id", required=True)
    staging.add_argument(
        "--staging-status", choices=("verified", "failed", "cleaned"), required=True
    )
    staging.add_argument("--staged-file", default="")
    staging.add_argument("--staged-sha256", default="")
    staging.add_argument("--detail", default="")

    watcher = sub.add_parser("watcher")
    watcher.add_argument("--request-id", required=True)
    watcher.add_argument("--automation-id", default="")
    watcher.add_argument(
        "--watcher-status",
        choices=("active", "deleted", "paused", "cleanup_failed"),
        required=True,
    )
    watcher.add_argument("--detail", default="")

    acquire = sub.add_parser("slot-acquire")
    acquire.add_argument("--request-id", required=True)
    acquire.add_argument("--account-key", required=True)
    acquire.add_argument("--deadline-at", required=True)

    release = sub.add_parser("slot-release")
    release.add_argument("--request-id", required=True)
    release.add_argument("--account-key", default="")
    release.add_argument("--token", default="")

    slot_status = sub.add_parser("slot-status")
    slot_status.add_argument("--account-key", required=True)

    status = sub.add_parser("status")
    status.add_argument("--request-id", required=True)

    list_rounds = sub.add_parser("list")
    list_rounds.add_argument("--active-only", action="store_true")

    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--request-id", required=True)

    spec = sub.add_parser("watcher-spec")
    spec.add_argument("--request-id", required=True)
    spec.add_argument("--target-thread-id", required=True)
    spec.add_argument("--rrule", default="RRULE:FREQ=MINUTELY;INTERVAL=2")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        handlers = {
            "create": command_create,
            "transition": command_transition,
            "delivery": command_delivery,
            "staging": command_staging,
            "watcher": command_watcher,
            "slot-acquire": command_slot_acquire,
            "slot-release": command_slot_release,
            "slot-status": command_slot_status,
            "status": command_status,
            "list": command_list,
            "reconcile": command_reconcile,
            "watcher-spec": command_watcher_spec,
        }
        result = handlers[args.command](args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RoundError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
