#!/usr/bin/env python3
"""Stage a local or SSH-hosted review attachment inside the OS temp root.

Chrome DevTools MCP 1.7 restricts file access to negotiated workspace roots or
the OS temp directory.  Codex Desktop does not currently negotiate roots for
this server, so both local and remote inputs use one verified temp staging
path.  Remote files are streamed over SSH; no remote temporary file is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


CREATED_BY = "gpt-pro-question-window"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


class StagingError(RuntimeError):
    """Raised when an attachment cannot be staged and verified safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    name = Path(value).name.strip()
    name = SAFE_ID_RE.sub("-", name).strip(".-")
    if not name or name in {".", ".."}:
        raise StagingError("Attachment filename is empty or unsafe")
    return name[:180]


def _staging_root(raw: str) -> Path:
    system_temp = Path(tempfile.gettempdir()).resolve()
    root = Path(raw).expanduser().resolve() if raw else system_temp / "codex-pro-bridge-staging"
    try:
        root.relative_to(system_temp)
    except ValueError as exc:
        raise StagingError(f"Staging root must stay inside OS temp: {system_temp}") from exc
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _new_stage(root: Path, thread_id: str) -> Path:
    prefix = SAFE_ID_RE.sub("-", thread_id).strip(".-") or "bridge"
    path = Path(tempfile.mkdtemp(prefix=f"{prefix[:48]}-", dir=str(root)))
    path.chmod(0o700)
    return path


def _write_manifest(stage_dir: Path, payload: dict[str, Any]) -> Path:
    manifest = stage_dir / "staging-manifest.json"
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return manifest


def _copy_local(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    source = Path(args.source).expanduser().resolve(strict=True)
    if not source.is_file():
        raise StagingError(f"Local source is not a regular file: {source}")
    size = source.stat().st_size
    if size > args.max_bytes:
        raise StagingError(f"Local source is {size} bytes, above --max-bytes={args.max_bytes}")
    stage_dir = _new_stage(root, args.bridge_thread_id)
    staged = stage_dir / _safe_name(args.name or source.name)
    try:
        shutil.copyfile(source, staged)
        staged.chmod(0o600)
        source_hash = _sha256(source)
        staged_hash = _sha256(staged)
        if source_hash != staged_hash or staged.stat().st_size != size:
            raise StagingError("Local staging hash or byte count mismatch")
        payload = {
            "created_by": CREATED_BY,
            "source_kind": "local",
            "source": str(source),
            "source_name": source.name,
            "source_sha256": source_hash,
            "staged_file": str(staged),
            "staged_name": staged.name,
            "staged_sha256": staged_hash,
            "bytes": size,
            "bridge_thread_id": args.bridge_thread_id,
        }
        payload["manifest"] = str(_write_manifest(stage_dir, payload))
        return payload
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _ssh_base(host: str) -> list[str]:
    if not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
        raise StagingError("--ssh-host contains unsupported characters")
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ClearAllForwardings=yes",
        host,
    ]


def _ssh_resolution(host: str) -> dict[str, str]:
    result = subprocess.run(
        ["ssh", "-G", host], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise StagingError(f"ssh -G failed for {host}: {result.stderr.strip()}")
    wanted = {"hostname", "user", "port"}
    resolved: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in wanted and key not in resolved:
            resolved[key] = value.strip()
    if set(resolved) != wanted:
        raise StagingError(f"ssh -G did not resolve hostname/user/port for {host}")
    return resolved


def _remote_metadata(host: str, remote_path: str) -> tuple[int, str]:
    script = r'''set -eu
p=$1
test -f "$p"
bytes=$(wc -c < "$p" | tr -d '[:space:]')
if command -v sha256sum >/dev/null 2>&1; then
  digest=$(sha256sum -- "$p" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  digest=$(shasum -a 256 -- "$p" | awk '{print $1}')
else
  exit 68
fi
printf '%s\t%s\n' "$bytes" "$digest"
'''
    remote_command = (
        f"sh -c {shlex.quote(script)} bridge-stage {shlex.quote(remote_path)}"
    )
    result = subprocess.run(
        _ssh_base(host) + [remote_command], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"remote exit {result.returncode}"
        raise StagingError(f"Unable to inspect remote file: {detail}")
    fields = result.stdout.strip().split("\t")
    if len(fields) != 2 or not fields[0].isdigit() or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[1]):
        raise StagingError("Remote file metadata was malformed")
    return int(fields[0]), fields[1].lower()


def _copy_remote(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    remote_path = str(PurePosixPath(args.remote_file))
    if not remote_path.startswith("/"):
        raise StagingError("--remote-file must be an absolute POSIX path")
    resolution = _ssh_resolution(args.ssh_host)
    remote_size, remote_hash = _remote_metadata(args.ssh_host, remote_path)
    if remote_size > args.max_bytes:
        raise StagingError(
            f"Remote source is {remote_size} bytes, above --max-bytes={args.max_bytes}"
        )
    stage_dir = _new_stage(root, args.bridge_thread_id)
    remote_name = PurePosixPath(remote_path).name
    staged = stage_dir / _safe_name(args.name or remote_name)
    partial = staged.with_suffix(staged.suffix + ".part")
    remote_command = f"cat -- {shlex.quote(remote_path)}"
    try:
        with partial.open("wb") as handle:
            result = subprocess.run(
                _ssh_base(args.ssh_host) + [remote_command],
                check=False,
                stdout=handle,
                stderr=subprocess.PIPE,
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StagingError(f"Remote transfer failed: {detail or result.returncode}")
        os.replace(partial, staged)
        staged.chmod(0o600)
        staged_hash = _sha256(staged)
        if staged.stat().st_size != remote_size or staged_hash != remote_hash:
            raise StagingError("Remote-to-local staging hash or byte count mismatch")
        payload = {
            "created_by": CREATED_BY,
            "source_kind": "ssh",
            "ssh_host": args.ssh_host,
            "ssh_resolution": resolution,
            "remote_file": remote_path,
            "source_name": remote_name,
            "remote_sha256": remote_hash,
            "staged_file": str(staged),
            "staged_name": staged.name,
            "staged_sha256": staged_hash,
            "bytes": remote_size,
            "bridge_thread_id": args.bridge_thread_id,
            "remote_temp_created": False,
        }
        payload["manifest"] = str(_write_manifest(stage_dir, payload))
        return payload
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _cleanup(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    staged = Path(args.staged_file).expanduser().resolve()
    stage_dir = staged.parent
    try:
        stage_dir.relative_to(root)
    except ValueError as exc:
        raise StagingError("Refusing cleanup outside the configured staging root") from exc
    if stage_dir.parent != root:
        raise StagingError("Refusing cleanup of a non-session staging directory")
    manifest = stage_dir / "staging-manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise StagingError("Refusing cleanup without a valid staging manifest") from exc
    if data.get("created_by") != CREATED_BY or Path(data.get("staged_file", "")).resolve() != staged:
        raise StagingError("Refusing cleanup: staging manifest ownership mismatch")
    shutil.rmtree(stage_dir)
    return {"removed": True, "staging_dir": str(stage_dir)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage a verified Pro Bridge attachment.")
    parser.add_argument("--staging-root", default="")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    subparsers = parser.add_subparsers(dest="command", required=True)

    local = subparsers.add_parser("local")
    local.add_argument("--source", required=True)
    local.add_argument("--bridge-thread-id", required=True)
    local.add_argument("--name", default="")

    remote = subparsers.add_parser("remote")
    remote.add_argument("--ssh-host", required=True)
    remote.add_argument("--remote-file", required=True)
    remote.add_argument("--bridge-thread-id", required=True)
    remote.add_argument("--name", default="")

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--staged-file", required=True)

    args = parser.parse_args()
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be positive")
    try:
        root = _staging_root(args.staging_root)
        if args.command == "local":
            result = _copy_local(args, root)
        elif args.command == "remote":
            result = _copy_remote(args, root)
        else:
            result = _cleanup(args, root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (StagingError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
