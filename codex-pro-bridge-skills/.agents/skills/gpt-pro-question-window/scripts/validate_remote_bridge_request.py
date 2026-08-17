#!/usr/bin/env python3
"""Validate one untrusted schema-v2 remote Codex Pro Bridge request.

This is syntax and identity validation only.  The dispatcher must still run the
documented live ``ssh -G`` and remote identity/mount checks before staging.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath


REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
SINGLE_FIELDS = {
    "request_id",
    "source_thread_id",
    "source_host_id",
    "ssh_alias",
    "remote_repository",
    "transient_bundle_path",
    "bundle_sha256",
    "focused_question",
}


class RequestError(ValueError):
    """Raised when a remote request is incomplete, conflicting, or unsafe."""


def _one(root: ET.Element, name: str) -> str:
    nodes = root.findall(name)
    if len(nodes) != 1:
        raise RequestError(f"Exactly one <{name}> is required")
    value = (nodes[0].text or "").strip()
    if not value:
        raise RequestError(f"<{name}> cannot be empty")
    return value


def _safe_absolute(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise RequestError(f"<{field}> must be a normalized absolute POSIX path")
    return path


def _validate_evidence(root: ET.Element) -> list[str]:
    containers = root.findall("evidence_paths")
    if len(containers) != 1:
        raise RequestError("Exactly one <evidence_paths> container is required")
    paths: list[str] = []
    for node in containers[0]:
        if node.tag != "path" or list(node):
            raise RequestError("<evidence_paths> may contain only plain <path> entries")
        value = (node.text or "").strip()
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
            or value.startswith("-")
        ):
            raise RequestError("Evidence paths must be normalized repository-relative paths")
        paths.append(path.as_posix())
    if not paths:
        raise RequestError("At least one evidence path is required")
    if len(paths) != len(set(paths)):
        raise RequestError("Evidence paths must be unique")
    return paths


def validate_request(xml_text: str) -> dict[str, object]:
    if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise RequestError("DTD and entity declarations are forbidden")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RequestError(f"Malformed request XML: {exc}") from exc
    if root.tag != "pro_bridge_request" or root.attrib != {"schema_version": "2"}:
        raise RequestError(
            'Root must be exactly <pro_bridge_request schema_version="2">'
        )
    allowed = SINGLE_FIELDS | {"evidence_paths"}
    unexpected = sorted({child.tag for child in root if child.tag not in allowed})
    if unexpected:
        raise RequestError("Unexpected request fields: " + ", ".join(unexpected))
    nested = [
        child.tag
        for child in root
        if child.tag in SINGLE_FIELDS and list(child)
    ]
    if nested:
        raise RequestError("Scalar request fields cannot contain nested XML")

    request_id = _one(root, "request_id")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise RequestError("request_id has an invalid format")
    source_thread_id = _one(root, "source_thread_id")
    source_host_id = _one(root, "source_host_id")
    if len(source_thread_id) > 256 or len(source_host_id) > 256:
        raise RequestError("source task and host IDs must be at most 256 characters")
    if any(character.isspace() for character in source_thread_id + source_host_id):
        raise RequestError("source task and host IDs cannot contain whitespace")
    ssh_alias = _one(root, "ssh_alias")
    if not SSH_ALIAS_RE.fullmatch(ssh_alias) or ssh_alias.startswith("-"):
        raise RequestError("ssh_alias contains unsupported characters")
    remote_repository = _safe_absolute(
        _one(root, "remote_repository"), "remote_repository"
    )
    transient_bundle = _safe_absolute(
        _one(root, "transient_bundle_path"), "transient_bundle_path"
    )
    allowed_temp_prefixes = {
        ("/", "tmp"),
        ("/", "var", "tmp"),
        ("/", "var", "folders"),
        ("/", "private", "tmp"),
        ("/", "private", "var", "folders"),
    }
    under_temp = any(
        transient_bundle.parts[: len(prefix)] == prefix
        for prefix in allowed_temp_prefixes
    )
    if not (
        under_temp
        and "codex-pro-bridge-client" in transient_bundle.parts
        and transient_bundle.name
    ):
        raise RequestError(
            "transient_bundle_path must be under a supported OS temp root and "
            "include a codex-pro-bridge-client directory"
        )
    bundle_sha256 = _one(root, "bundle_sha256").lower()
    if not SHA256_RE.fullmatch(bundle_sha256):
        raise RequestError("bundle_sha256 must be a lowercase SHA-256 digest")
    focused_question = _one(root, "focused_question")
    if len(focused_question) > 20_000:
        raise RequestError("focused_question exceeds 20,000 characters")
    evidence_paths = _validate_evidence(root)
    return {
        "valid": True,
        "schema_version": 2,
        "request_id": request_id,
        "source_thread_id": source_thread_id,
        "source_host_id": source_host_id,
        "ssh_alias": ssh_alias,
        "remote_repository": remote_repository.as_posix(),
        "transient_bundle_path": transient_bundle.as_posix(),
        "bundle_sha256": bundle_sha256,
        "focused_question": focused_question,
        "evidence_paths": evidence_paths,
    }


def _match(payload: dict[str, object], field: str, expected: str) -> None:
    if expected and payload.get(field) != expected:
        raise RequestError(f"{field} conflicts with the dispatcher envelope")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an untrusted <pro_bridge_request schema_version=\"2\">."
    )
    parser.add_argument("--request-file", default="", help="Defaults to stdin.")
    parser.add_argument("--expected-request-id", default="")
    parser.add_argument("--expected-source-thread-id", default="")
    parser.add_argument("--expected-source-host-id", default="")
    parser.add_argument("--expected-ssh-alias", default="")
    args = parser.parse_args()
    try:
        xml_text = (
            Path(args.request_file).expanduser().resolve(strict=True).read_text(
                encoding="utf-8"
            )
            if args.request_file
            else sys.stdin.read()
        )
        payload = validate_request(xml_text)
        _match(payload, "request_id", args.expected_request_id.strip())
        _match(payload, "source_thread_id", args.expected_source_thread_id.strip())
        _match(payload, "source_host_id", args.expected_source_host_id.strip())
        _match(payload, "ssh_alias", args.expected_ssh_alias.strip())
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (RequestError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
