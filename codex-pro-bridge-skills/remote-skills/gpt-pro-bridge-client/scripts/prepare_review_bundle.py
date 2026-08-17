#!/usr/bin/env python3
"""Build or clean an explicit, transient bundle for the Mac GPT Pro dispatcher."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import zipfile
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from typing import Any, Iterable


CREATED_BY = "gpt-pro-bridge-client"
DEFAULT_ROOT = Path(tempfile.gettempdir()) / "codex-pro-bridge-client"
DEFAULT_RESULT_ROOT = DEFAULT_ROOT
DEFAULT_CONFIG_PATH = (
    Path.home() / ".codex" / "state" / "codex-pro-bridge-client" / "config.json"
)
DEFAULT_MAX_FILES = 80
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_RESULT_BYTES = 10 * 1024 * 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
BLOCKED_DIRS = {".git", ".codex", ".svn", "node_modules", ".venv", "__pycache__"}
BLOCKED_NAMES = {
    ".env", "credentials", "credentials.json", "cookies", "cookies.sqlite",
    "login data", "known_hosts", "authorized_keys", "id_rsa", "id_ed25519",
}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
]


class BundleError(RuntimeError):
    """Raised when a review bundle would be unsafe or ambiguous."""


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise BundleError(f"Refusing to replace a symlinked config: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def config_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve() if raw else DEFAULT_CONFIG_PATH.resolve()


def configure(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "schema_version": 1,
        "dispatcher_thread_id": args.dispatcher_thread_id.strip(),
        "dispatcher_host_id": args.dispatcher_host_id.strip(),
        "ssh_alias": args.ssh_alias.strip(),
    }
    if not all(values[key] for key in ("dispatcher_thread_id", "dispatcher_host_id", "ssh_alias")):
        raise BundleError("Dispatcher task, host, and SSH alias must all be non-empty")
    if any(character.isspace() for character in values["dispatcher_thread_id"] + values["dispatcher_host_id"]):
        raise BundleError("Dispatcher task and host IDs cannot contain whitespace")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", values["ssh_alias"]):
        raise BundleError("SSH alias contains unsupported characters")
    path = config_path(args.config)
    if path.exists():
        current = load_config(args.config)
        same = all(
            current.get(key) == values[key]
            for key in ("dispatcher_thread_id", "dispatcher_host_id", "ssh_alias")
        )
        if same:
            return {"configured": True, "idempotent": True, "config": str(path), **values}
        if not args.replace:
            raise BundleError(
                "Bridge client is already bound to different deployment identities; "
                "inspect them and pass --replace explicitly"
            )
    atomic_write_json(path, values)
    return {"configured": True, "idempotent": False, "config": str(path), **values}


def load_config(raw: str, *, required: bool = True) -> dict[str, Any]:
    path = config_path(raw)
    if not path.exists():
        if required:
            raise BundleError(
                f"Bridge client is not configured: {path}; run the configure command first"
            )
        return {"configured": False, "config": str(path)}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"Invalid Bridge client config: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise BundleError(f"Unsupported Bridge client config: {path}")
    for field in ("dispatcher_thread_id", "dispatcher_host_id", "ssh_alias"):
        if not str(value.get(field, "")).strip():
            raise BundleError(f"Bridge client config is missing {field}")
    return {"configured": True, "config": str(path), **value}


def validate_request_id(value: str) -> str:
    value = (value or "").strip()
    if not REQUEST_ID_RE.fullmatch(value):
        raise BundleError(
            "request id must be 1-128 letters, digits, dots, underscores, or hyphens"
        )
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def reject_sensitive_path(relative: Path) -> None:
    lowered_parts = {part.lower() for part in relative.parts}
    if lowered_parts & BLOCKED_DIRS:
        raise BundleError(f"Blocked directory in evidence path: {relative}")
    if relative.name.lower() in BLOCKED_NAMES or relative.suffix.lower() in BLOCKED_SUFFIXES:
        raise BundleError(f"Sensitive filename is not allowed: {relative}")


def iter_selected(repo: Path, raw_paths: Iterable[str]) -> list[Path]:
    selected: dict[str, Path] = {}
    for raw in raw_paths:
        candidate = Path(raw).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
        if not inside(candidate, repo):
            raise BundleError(f"Evidence path is outside repository: {raw}")
        if not candidate.exists():
            raise BundleError(f"Evidence path does not exist: {raw}")
        if candidate.is_symlink():
            raise BundleError(f"Symlinks are not allowed: {raw}")
        paths = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(repo)
            if any(part.lower() in BLOCKED_DIRS for part in relative.parts):
                continue
            reject_sensitive_path(relative)
            selected[relative.as_posix()] = path
    if not selected:
        raise BundleError("No regular evidence files were selected")
    return [selected[key] for key in sorted(selected)]


def scan_file(path: Path) -> None:
    with path.open("rb") as handle:
        sample = handle.read(2 * 1024 * 1024)
    for pattern in SECRET_PATTERNS:
        if pattern.search(sample):
            raise BundleError(f"High-confidence secret pattern detected in {path.name}")


def safe_root(raw: str) -> Path:
    system_temp = Path(tempfile.gettempdir()).resolve()
    root = Path(raw).expanduser().resolve() if raw else DEFAULT_ROOT.resolve()
    if not inside(root, system_temp):
        raise BundleError(f"Bundle root must remain inside OS temp: {system_temp}")
    if "codex-pro-bridge-client" not in root.parts:
        raise BundleError("Bundle root must include a codex-pro-bridge-client directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def safe_result_root(raw: str) -> Path:
    system_temp = Path(tempfile.gettempdir()).resolve()
    root = Path(raw).expanduser().resolve() if raw else DEFAULT_RESULT_ROOT.resolve()
    if not inside(root, system_temp):
        raise BundleError(f"Result root must remain inside OS temp: {system_temp}")
    if "codex-pro-bridge-client" not in root.parts:
        raise BundleError("Result root must include a codex-pro-bridge-client directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def prepare(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise BundleError(f"Repository root is not a directory: {repo}")
    question = args.question.strip()
    if not question:
        raise BundleError("--question must be non-empty")
    source_host_id = args.source_host_id.strip()
    if not source_host_id:
        raise BundleError("--source-host-id must be the exact current Codex host id")
    source_thread_id = args.source_thread_id.strip()
    if not source_thread_id:
        raise BundleError("--source-thread-id must be the exact current Codex task id")
    client = load_config(args.config)
    files = iter_selected(repo, args.include)
    if len(files) > args.max_files:
        raise BundleError(f"Selected {len(files)} files, above --max-files={args.max_files}")
    total = sum(path.stat().st_size for path in files)
    if total > args.max_bytes:
        raise BundleError(f"Selected {total} bytes, above --max-bytes={args.max_bytes}")
    for path in files:
        scan_file(path)

    now = dt.datetime.now().astimezone()
    thread_slug = re.sub(r"[^a-zA-Z0-9-]+", "-", source_thread_id).strip("-")[:20]
    request_id = f"{thread_slug or 'remote'}-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    request_dir = root / request_id
    request_dir.mkdir(mode=0o700)
    bundle = request_dir / f"{request_id}-review.zip"
    entries = []
    for path in files:
        relative = path.relative_to(repo).as_posix()
        entries.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    manifest = {
        "schema_version": 2,
        "created_by": CREATED_BY,
        "request_id": request_id,
        "source_thread_id": source_thread_id,
        "source_host_id": source_host_id,
        "dispatcher_thread_id": client["dispatcher_thread_id"],
        "dispatcher_host_id": client["dispatcher_host_id"],
        "ssh_alias": client["ssh_alias"],
        "repo_label": repo.name,
        "question": question,
        "entries": entries,
        "total_bytes": total,
    }
    try:
        with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("REVIEW_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for path in files:
                archive.write(path, f"evidence/{path.relative_to(repo).as_posix()}")
        bundle.chmod(0o600)
        ownership = request_dir / "ownership.json"
        ownership.write_text(
            json.dumps({"created_by": CREATED_BY, "bundle": str(bundle)}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        ownership.chmod(0o600)
        with zipfile.ZipFile(bundle, "r") as archive:
            bad = archive.testzip()
        if bad:
            raise BundleError(f"ZIP integrity failed at {bad}")
        evidence_paths = [entry["path"] for entry in entries]
        request_fields = {
            "request_id": request_id,
            "source_thread_id": source_thread_id,
            "source_host_id": source_host_id,
            "ssh_alias": str(client["ssh_alias"]),
            "remote_repository": str(repo),
            "transient_bundle_path": str(bundle),
            "bundle_sha256": sha256_file(bundle),
            "focused_question": question,
        }
        request_xml = "\n".join(
            [
                '<pro_bridge_request schema_version="2">',
                *[
                    f"  <{key}>{xml_escape(value)}</{key}>"
                    for key, value in request_fields.items()
                ],
                "  <evidence_paths>",
                *[
                    f"    <path>{xml_escape(path)}</path>"
                    for path in evidence_paths
                ],
                "  </evidence_paths>",
                "</pro_bridge_request>",
            ]
        )
        return {
            "ready": True,
            **request_fields,
            "dispatcher_thread_id": client["dispatcher_thread_id"],
            "dispatcher_host_id": client["dispatcher_host_id"],
            "bytes": bundle.stat().st_size,
            "file_count": len(files),
            "evidence_paths": evidence_paths,
            "request_xml": request_xml,
        }
    except Exception:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise


def cleanup(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    bundle = Path(args.bundle).expanduser().resolve()
    request_dir = bundle.parent
    if request_dir.parent != root:
        raise BundleError("Refusing cleanup outside a direct request directory")
    ownership = request_dir / "ownership.json"
    try:
        record = json.loads(ownership.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise BundleError("Refusing cleanup without a valid ownership record") from exc
    if record.get("created_by") != CREATED_BY or Path(record.get("bundle", "")).resolve() != bundle:
        raise BundleError("Refusing cleanup: ownership mismatch")
    shutil.rmtree(request_dir)
    return {"removed": True, "request_dir": str(request_dir)}


def result_import(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    request_id = validate_request_id(args.request_id)
    expected_sha256 = args.expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BundleError("--expected-sha256 must be a lowercase SHA-256 digest")
    request_root = root / request_id
    request_root.mkdir(mode=0o700, exist_ok=True)
    request_dir = request_root / "results"
    request_dir.mkdir(mode=0o700, exist_ok=True)
    result_path = request_dir / "full-answer.md"
    if result_path.exists():
        observed = sha256_file(result_path)
        if observed != expected_sha256:
            raise BundleError("Existing result has a conflicting digest")
        return {
            "stored": True,
            "idempotent": True,
            "request_id": request_id,
            "result": str(result_path),
            "sha256": observed,
            "bytes": result_path.stat().st_size,
        }
    partial = request_dir / ".gpt-pro-result.md.part"
    digest = hashlib.sha256()
    written = 0
    try:
        with partial.open("xb") as handle:
            partial.chmod(0o600)
            for chunk in iter(lambda: sys.stdin.buffer.read(1024 * 1024), b""):
                written += len(chunk)
                if written > args.max_bytes:
                    raise BundleError(
                        f"Result exceeds --max-bytes={args.max_bytes}"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        observed = digest.hexdigest()
        if observed != expected_sha256:
            raise BundleError(
                f"Result digest mismatch: expected {expected_sha256}, got {observed}"
            )
        os.replace(partial, result_path)
        result_path.chmod(0o600)
        ownership = request_dir / "ownership.json"
        ownership.write_text(
            json.dumps(
                {
                    "created_by": CREATED_BY,
                    "request_id": request_id,
                    "result": str(result_path),
                    "sha256": observed,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        ownership.chmod(0o600)
        return {
            "stored": True,
            "idempotent": False,
            "request_id": request_id,
            "result": str(result_path),
            "sha256": observed,
            "bytes": written,
        }
    except Exception:
        partial.unlink(missing_ok=True)
        if not result_path.exists():
            shutil.rmtree(request_dir, ignore_errors=True)
        raise


def result_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    request_id = validate_request_id(args.request_id)
    request_dir = root / request_id / "results"
    ownership = request_dir / "ownership.json"
    try:
        record = json.loads(ownership.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"ready": False, "request_id": request_id}
    result_path = Path(record.get("result", "")).resolve()
    if (
        record.get("created_by") != CREATED_BY
        or record.get("request_id") != request_id
        or result_path.parent != request_dir.resolve()
        or not result_path.is_file()
    ):
        raise BundleError("Invalid result ownership record")
    observed = sha256_file(result_path)
    if observed != record.get("sha256"):
        raise BundleError("Stored result digest mismatch")
    return {
        "ready": True,
        "request_id": request_id,
        "result": str(result_path),
        "sha256": observed,
        "bytes": result_path.stat().st_size,
    }


def result_cleanup(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    status = result_status(args, root)
    if not status.get("ready"):
        return {"removed": False, "request_id": status["request_id"]}
    request_dir = Path(status["result"]).parent
    shutil.rmtree(request_dir)
    request_root = request_dir.parent
    if request_root.is_dir() and not any(request_root.iterdir()):
        request_root.rmdir()
    return {"removed": True, "request_id": status["request_id"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a transient remote GPT Pro review bundle.")
    parser.add_argument("--root", default="")
    parser.add_argument("--config", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_client = subparsers.add_parser("configure")
    configure_client.add_argument("--dispatcher-thread-id", required=True)
    configure_client.add_argument("--dispatcher-host-id", required=True)
    configure_client.add_argument("--ssh-alias", required=True)
    configure_client.add_argument("--replace", action="store_true")
    subparsers.add_parser("config-status")
    make = subparsers.add_parser("prepare")
    make.add_argument("--repo", default=".")
    make.add_argument("--source-thread-id", required=True)
    make.add_argument("--source-host-id", required=True)
    make.add_argument("--question", required=True)
    make.add_argument("--include", action="append", required=True)
    make.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    make.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    clean = subparsers.add_parser("cleanup")
    clean.add_argument("--bundle", required=True)
    import_result = subparsers.add_parser("result-import")
    import_result.add_argument("--request-id", required=True)
    import_result.add_argument("--expected-sha256", required=True)
    import_result.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_RESULT_BYTES)
    status_result = subparsers.add_parser("result-status")
    status_result.add_argument("--request-id", required=True)
    clean_result = subparsers.add_parser("result-cleanup")
    clean_result.add_argument("--request-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "configure":
            result = configure(args)
        elif args.command == "config-status":
            result = load_config(args.config, required=False)
        elif args.command in {"prepare", "cleanup"}:
            root = safe_root(args.root)
            result = prepare(args, root) if args.command == "prepare" else cleanup(args, root)
        else:
            root = safe_result_root(args.root)
            handlers = {
                "result-import": result_import,
                "result-status": result_status,
                "result-cleanup": result_cleanup,
            }
            result = handlers[args.command](args, root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (BundleError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
