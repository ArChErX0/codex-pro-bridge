#!/usr/bin/env python3
"""Prepare a bounded, exact-target callback for one terminal SSH Bridge round."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from html import escape
from pathlib import Path, PurePosixPath


REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CallbackError(ValueError):
    """Raised when callback identity or terminal evidence is incomplete."""


def state_root(explicit: str) -> Path:
    configured = explicit.strip() or os.environ.get(
        "CODEX_PRO_BRIDGE_STATE_DIR", ""
    ).strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".codex" / "state" / "codex-pro-bridge"
    )


def read_value(value: str, filename: str) -> str:
    if value and filename:
        raise CallbackError("Use either inline summary/detail or a file, not both")
    result = (
        Path(filename).expanduser().resolve(strict=True).read_text(encoding="utf-8")
        if filename
        else value
    ).strip()
    if len(result) > 4000:
        raise CallbackError("Callback summary/detail must be at most 4,000 characters")
    return result


def validate_result_path(request_id: str, value: str) -> str:
    if not value:
        return ""
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise CallbackError("Result path must be a normalized absolute POSIX path")
    prefixes = {
        ("/", "tmp"),
        ("/", "var", "tmp"),
        ("/", "var", "folders"),
        ("/", "private", "tmp"),
        ("/", "private", "var", "folders"),
    }
    under_temp = any(path.parts[: len(prefix)] == prefix for prefix in prefixes)
    try:
        client_index = path.parts.index("codex-pro-bridge-client")
    except ValueError as exc:
        raise CallbackError(
            "Result path must include a codex-pro-bridge-client directory"
        ) from exc
    if (
        not under_temp
        or path.parts[client_index + 1 : client_index + 2] != (request_id,)
        or path.name == ""
    ):
        raise CallbackError(
            "Result path must stay below an OS-temp "
            "codex-pro-bridge-client/<request-id>/ directory"
        )
    return path.as_posix()


def load_round(root: Path, request_id: str) -> dict[str, object]:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise CallbackError("Invalid request ID")
    path = root / "rounds" / f"{request_id}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CallbackError(f"Unable to read round state: {path}") from exc
    if not isinstance(state, dict) or state.get("request_id") != request_id:
        raise CallbackError("Round state identity mismatch")
    if state.get("source_kind") != "ssh":
        raise CallbackError("Callbacks are generated only for SSH source rounds")
    for field in ("source_thread_id", "source_host_id", "ssh_alias"):
        if not str(state.get(field, "")).strip():
            raise CallbackError(f"Round is missing {field}")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare one exact-target schema-v2 callback from terminal round state."
    )
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--request-id", required=True)
    parser.add_argument(
        "--terminal-status", choices=("completed", "failed", "timed_out"), required=True
    )
    parser.add_argument("--summary", default="")
    parser.add_argument("--summary-file", default="")
    parser.add_argument("--result-path", default="")
    parser.add_argument("--result-sha256", default="")
    args = parser.parse_args()
    try:
        request_id = args.request_id.strip()
        state = load_round(state_root(args.state_dir), request_id)
        summary = read_value(args.summary, args.summary_file)
        status = str(state.get("status", ""))
        if args.terminal_status == "completed":
            if status != "verdict_recorded":
                raise CallbackError("Completed callback requires verdict_recorded round state")
            for field in ("bundle_sha256", "capture_sha256", "verdict_sha256"):
                if not SHA256_RE.fullmatch(str(state.get(field, ""))):
                    raise CallbackError(f"Completed callback requires a valid {field}")
            if not summary:
                raise CallbackError("Completed callback requires a bounded summary")
        elif status not in {"failed", "timed_out"}:
            raise CallbackError("Failure callback requires failed or timed_out round state")
        result_path = validate_result_path(request_id, args.result_path.strip())
        result_sha = args.result_sha256.strip().lower()
        if bool(result_path) != bool(result_sha):
            raise CallbackError("--result-path and --result-sha256 must be supplied together")
        if result_sha and not SHA256_RE.fullmatch(result_sha):
            raise CallbackError("--result-sha256 must be a lowercase SHA-256")

        fields = {
            "request_id": request_id,
            "source_thread_id": str(state["source_thread_id"]),
            "source_host_id": str(state["source_host_id"]),
            "terminal_status": args.terminal_status,
            "model_selection_status": str(state.get("model_selection_status", "unverified")),
            "execution_status": str(state.get("execution_status", "unknown")),
            "bundle_sha256": str(state.get("bundle_sha256", "")),
            "capture_sha256": str(state.get("capture_sha256", "")),
            "verdict_sha256": str(state.get("verdict_sha256", "")),
            "result_path": result_path,
            "result_sha256": result_sha,
            "summary": summary or str(state.get("failure_detail", ""))[:4000],
        }
        body = [
            '<pro_bridge_result schema_version="2">',
            *[
                f"  <{key}>{escape(value)}</{key}>"
                for key, value in fields.items()
            ],
            "</pro_bridge_result>",
        ]
        message = "\n".join(body)
        print(
            json.dumps(
                {
                    "valid": True,
                    "request_id": request_id,
                    "target_thread_id": state["source_thread_id"],
                    "target_host_id": state["source_host_id"],
                    "message": message,
                    "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                    "delivery_command_after_send": (
                        "manage_bridge_round.py delivery --request-id "
                        f"{request_id} --delivery-status delivered"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (CallbackError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
