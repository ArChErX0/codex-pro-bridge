#!/usr/bin/env python3
"""Validate a non-canonical Auto Research Chat mapping snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROLE_TYPES = {
    "primary",
    "scoped_portfolio",
    "research_orchestrator",
    "mentor",
    "student",
    "execution",
}
BINDING_STATUSES = {"active", "superseded", "archived", "unknown"}
LINEAGE_STATUSES = {"active", "superseded", "archived", "unknown"}
CONVERSATION_KINDS = {"primary", "probe", "continuation", "fork-exploration"}
CONVERSATION_STATUSES = {
    "active",
    "waiting",
    "completed",
    "superseded",
    "archived",
    "failed",
    "unknown",
}
LIVE_CONVERSATION_STATUSES = {"active", "waiting"}
PRIMARY_PATH_KINDS = {"primary", "continuation"}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_mapping(data: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        return {"valid": False, "errors": ["root must be a JSON object"], "warnings": []}
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not _nonempty(data.get("sop_revision")):
        errors.append("sop_revision is required")

    bindings = data.get("bindings", [])
    lineages = data.get("pro_lineages", [])
    if not isinstance(bindings, list):
        errors.append("bindings must be a list")
        bindings = []
    if not isinstance(lineages, list):
        errors.append("pro_lineages must be a list")
        lineages = []

    binding_by_id: dict[str, dict[str, Any]] = {}
    active_students: dict[str, str] = {}
    active_research_lines: dict[str, str] = {}
    writer_owners: dict[str, str] = {}
    track_wip: Counter[str] = Counter()

    for index, raw in enumerate(bindings):
        label = f"bindings[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue
        binding_id = raw.get("binding_id")
        if not _nonempty(binding_id):
            errors.append(f"{label}.binding_id is required")
            continue
        binding_id = binding_id.strip()
        if binding_id in binding_by_id:
            errors.append(f"duplicate binding_id: {binding_id}")
            continue
        binding_by_id[binding_id] = raw

        role_type = raw.get("role_type")
        status = raw.get("status")
        if role_type not in ROLE_TYPES:
            errors.append(f"{label}.role_type is invalid: {role_type!r}")
        if status not in BINDING_STATUSES:
            errors.append(f"{label}.status is invalid: {status!r}")
        if status != "active":
            continue
        if not _nonempty(raw.get("local_thread_id")):
            errors.append(f"{label}.local_thread_id is required for an active binding")
        if not _nonempty(raw.get("research_scope_id")):
            errors.append(f"{label}.research_scope_id is required for an active binding")

        writer_scopes = raw.get("writer_scopes", [])
        if not isinstance(writer_scopes, list):
            errors.append(f"{label}.writer_scopes must be a list")
            writer_scopes = []
        for scope in writer_scopes:
            if not _nonempty(scope):
                errors.append(f"{label}.writer_scopes contains an empty value")
                continue
            scope = scope.strip()
            previous = writer_owners.get(scope)
            if previous and previous != binding_id:
                errors.append(
                    f"writer scope {scope!r} is owned by both {previous!r} and {binding_id!r}"
                )
            else:
                writer_owners[scope] = binding_id

        if role_type == "student":
            student_id = raw.get("student_id")
            track_id = raw.get("track_id")
            research_line = raw.get("research_scope_id")
            if not _nonempty(student_id):
                errors.append(f"{label}.student_id is required for an active Student")
            else:
                student_id = student_id.strip()
                previous = active_students.get(student_id)
                if previous and previous != binding_id:
                    errors.append(
                        f"Student {student_id!r} has multiple active local bindings: "
                        f"{previous!r}, {binding_id!r}"
                    )
                active_students[student_id] = binding_id
            if not _nonempty(track_id):
                errors.append(f"{label}.track_id is required for an active Student")
            else:
                track_wip[track_id.strip()] += 1
            if _nonempty(research_line):
                research_line = research_line.strip()
                previous = active_research_lines.get(research_line)
                if previous and previous != binding_id:
                    errors.append(
                        f"Research Line {research_line!r} has multiple active Students: "
                        f"{previous!r}, {binding_id!r}"
                    )
                active_research_lines[research_line] = binding_id

    conversation_owners: dict[str, str] = {}
    bridge_thread_owners: dict[str, str] = {}
    active_submission_owners: dict[str, str] = {}
    active_lineage_owners: dict[tuple[str, str], str] = {}
    lineage_ids: set[str] = set()

    for index, raw in enumerate(lineages):
        label = f"pro_lineages[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue
        lineage_id = raw.get("lineage_id")
        if not _nonempty(lineage_id):
            errors.append(f"{label}.lineage_id is required")
            continue
        lineage_id = lineage_id.strip()
        if lineage_id in lineage_ids:
            errors.append(f"duplicate lineage_id: {lineage_id}")
        lineage_ids.add(lineage_id)

        status = raw.get("status")
        if status not in LINEAGE_STATUSES:
            errors.append(f"{label}.status is invalid: {status!r}")
        owner_id = raw.get("owner_binding_id")
        owner = binding_by_id.get(owner_id) if _nonempty(owner_id) else None
        if owner is None:
            errors.append(f"{label}.owner_binding_id does not resolve: {owner_id!r}")
        elif status == "active" and owner.get("status") != "active":
            errors.append(f"{label} is active but its owner binding is not active")
        research_scope_id = raw.get("research_scope_id")
        if not _nonempty(research_scope_id):
            errors.append(f"{label}.research_scope_id is required")
        elif status == "active" and owner is not None:
            owner_scope = (owner_id.strip(), research_scope_id.strip())
            owner_research_scope = owner.get("research_scope_id")
            if (
                _nonempty(owner_research_scope)
                and owner_research_scope.strip() != owner_scope[1]
            ):
                errors.append(
                    f"{label}.research_scope_id {owner_scope[1]!r} does not match "
                    f"owner binding scope {owner_research_scope.strip()!r}"
                )
            previous = active_lineage_owners.get(owner_scope)
            if previous and previous != lineage_id:
                errors.append(
                    f"owner {owner_scope[0]!r} and scope {owner_scope[1]!r} have "
                    f"multiple active Pro lineages: {previous!r}, {lineage_id!r}"
                )
            else:
                active_lineage_owners[owner_scope] = lineage_id

        conversations = raw.get("conversations", [])
        if not isinstance(conversations, list):
            errors.append(f"{label}.conversations must be a list")
            continue
        active_primary = 0
        for conversation_index, conversation in enumerate(conversations):
            conversation_label = f"{label}.conversations[{conversation_index}]"
            if not isinstance(conversation, dict):
                errors.append(f"{conversation_label} must be an object")
                continue
            conversation_id = conversation.get("conversation_id")
            if not _nonempty(conversation_id):
                errors.append(f"{conversation_label}.conversation_id is required")
                continue
            conversation_id = conversation_id.strip()
            previous = conversation_owners.get(conversation_id)
            if previous:
                errors.append(
                    f"Web conversation {conversation_id!r} is repeated in "
                    f"{previous!r} and {lineage_id!r}"
                )
            else:
                conversation_owners[conversation_id] = lineage_id

            kind = conversation.get("kind")
            conversation_status = conversation.get("status")
            if kind not in CONVERSATION_KINDS:
                errors.append(f"{conversation_label}.kind is invalid: {kind!r}")
            if conversation_status not in CONVERSATION_STATUSES:
                errors.append(
                    f"{conversation_label}.status is invalid: {conversation_status!r}"
                )
            if (
                kind in PRIMARY_PATH_KINDS
                and conversation_status in LIVE_CONVERSATION_STATUSES
            ):
                active_primary += 1

            bridge_thread_id = conversation.get("bridge_thread_id")
            if _nonempty(bridge_thread_id):
                bridge_thread_id = bridge_thread_id.strip()
                previous = bridge_thread_owners.get(bridge_thread_id)
                if previous and previous != conversation_id:
                    errors.append(
                        f"Bridge Thread {bridge_thread_id!r} maps to both "
                        f"{previous!r} and {conversation_id!r}"
                    )
                bridge_thread_owners[bridge_thread_id] = conversation_id

            submission_id = conversation.get("active_submission_id")
            if submission_id is not None:
                if not _nonempty(submission_id):
                    errors.append(
                        f"{conversation_label}.active_submission_id must be null or non-empty"
                    )
                else:
                    submission_id = submission_id.strip()
                    if conversation_status not in LIVE_CONVERSATION_STATUSES:
                        errors.append(
                            f"{conversation_label} has an active submission but status "
                            f"is {conversation_status!r}"
                        )
                    if not _nonempty(bridge_thread_id):
                        errors.append(
                            f"{conversation_label} has an active submission without a Bridge Thread"
                        )
                    previous = active_submission_owners.get(submission_id)
                    if previous and previous != conversation_id:
                        errors.append(
                            f"active submission {submission_id!r} belongs to both "
                            f"{previous!r} and {conversation_id!r}"
                        )
                    active_submission_owners[submission_id] = conversation_id

        if active_primary > 1:
            errors.append(
                f"{label} has {active_primary} live primary-path conversations"
            )

    for track_id, count in sorted(track_wip.items()):
        if count > 3:
            warnings.append(f"Track {track_id!r} has {count} active Students; normal WIP is 1-3")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "bindings": len(bindings),
            "active_students": len(active_students),
            "pro_lineages": len(lineages),
            "web_conversations": len(conversation_owners),
            "active_submissions": len(active_submission_owners),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a non-canonical Auto Research local/Web Chat mapping snapshot."
    )
    parser.add_argument("mapping", help="JSON file path, or '-' for stdin")
    args = parser.parse_args()
    try:
        result = validate_mapping(_load(args.mapping))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
