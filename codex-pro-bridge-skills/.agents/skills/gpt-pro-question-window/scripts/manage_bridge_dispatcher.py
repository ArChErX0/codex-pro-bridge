#!/usr/bin/env python3
"""Claim one host-global Codex task as the only Pro Bridge browser dispatcher."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1


class DispatcherError(ValueError):
    """Raised when dispatcher ownership is missing or ambiguous."""


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="milliseconds")


def state_root(explicit: str = "") -> Path:
    configured = explicit.strip() or os.environ.get(
        "CODEX_PRO_BRIDGE_STATE_DIR", ""
    ).strip()
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".codex" / "state" / "codex-pro-bridge"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def dispatcher_path(root: Path) -> Path:
    return root / "dispatcher.json"


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatcherError(f"Invalid dispatcher state: {path}") from exc
    if not isinstance(value, dict):
        raise DispatcherError(f"Dispatcher state is not an object: {path}")
    return value


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise DispatcherError("Dispatcher expiry timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise DispatcherError("Dispatcher expiry timestamp lacks a timezone")
    return parsed


def is_active(state: dict[str, Any]) -> bool:
    return bool(state.get("token")) and parse_time(str(state.get("expires_at", ""))) > now()


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key != "token"
    } | {"active": is_active(state) if state else False}


def assert_dispatcher_held(
    *,
    token: str,
    expected_thread_id: str = "",
    explicit_state_dir: str = "",
) -> dict[str, Any]:
    state = read_state(dispatcher_path(state_root(explicit_state_dir)))
    if not state or not is_active(state):
        raise DispatcherError("No active Pro Bridge dispatcher claim")
    if not token.strip() or state.get("token") != token.strip():
        raise DispatcherError("Pro Bridge dispatcher token mismatch")
    if expected_thread_id.strip() and state.get("thread_id") != expected_thread_id.strip():
        raise DispatcherError("Pro Bridge dispatcher task identity mismatch")
    return state


def command_claim(args: argparse.Namespace) -> dict[str, Any]:
    thread_id = args.thread_id.strip()
    host_id = args.host_id.strip()
    holder = args.holder.strip()
    if not thread_id or not host_id or not holder:
        raise DispatcherError("--thread-id, --host-id, and --holder are required")
    if args.ttl_hours <= 0 or args.ttl_hours > 24 * 30:
        raise DispatcherError("--ttl-hours must be between 0 and 720")
    root = state_root(args.state_dir)
    path = dispatcher_path(root)
    with file_lock(path.with_suffix(".lock")):
        current = read_state(path)
        same_owner = (
            current.get("thread_id") == thread_id
            and current.get("host_id") == host_id
            and current.get("holder") == holder
        )
        if current and is_active(current) and not same_owner:
            raise DispatcherError(
                "Dispatcher already held by another task: "
                f"{current.get('thread_id')} on {current.get('host_id')}"
            )
        if current and not is_active(current) and not same_owner and not args.recover_expired:
            raise DispatcherError(
                "Expired dispatcher belongs to another task; rerun with --recover-expired "
                "after checking that task is no longer using Chrome"
            )
        token = str(current.get("token", "")) if same_owner else ""
        acquired_at = str(current.get("acquired_at", "")) if same_owner else ""
        state = {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "host_id": host_id,
            "holder": holder,
            "token": token or uuid.uuid4().hex,
            "acquired_at": acquired_at or now_iso(),
            "renewed_at": now_iso(),
            "expires_at": (now() + dt.timedelta(hours=args.ttl_hours)).isoformat(
                timespec="milliseconds"
            ),
            "browser_profile": "ordinary-stable-chrome",
            "mcp_transport": "stdio",
            "routing_policy": "one-long-lived-dispatcher",
        }
        atomic_write(path, state)
        return {"claimed": True, "idempotent": same_owner, **state, "path": str(path)}


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    path = dispatcher_path(state_root(args.state_dir))
    return {**public_state(read_state(path)), "path": str(path)}


def command_release(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(args.state_dir)
    path = dispatcher_path(root)
    with file_lock(path.with_suffix(".lock")):
        current = read_state(path)
        if not current:
            return {"released": False, "path": str(path)}
        if current.get("token") != args.token.strip():
            raise DispatcherError("Dispatcher token mismatch")
        atomic_write(path, {})
        return {"released": True, "path": str(path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the one Pro Bridge browser dispatcher.")
    parser.add_argument("--state-dir", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim")
    claim.add_argument("--thread-id", required=True)
    claim.add_argument("--host-id", required=True)
    claim.add_argument("--holder", required=True)
    claim.add_argument("--ttl-hours", type=float, default=168)
    claim.add_argument("--recover-expired", action="store_true")
    sub.add_parser("status")
    release = sub.add_parser("release")
    release.add_argument("--token", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = {
            "claim": command_claim,
            "status": command_status,
            "release": command_release,
        }[args.command](args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (DispatcherError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
