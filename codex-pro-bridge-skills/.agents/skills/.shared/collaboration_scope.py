"""Codex root/owner selection on top of the existing Project and Thread stores."""
from __future__ import annotations

import hashlib
from pathlib import Path

from bridge_store import BridgeError, validate_id
from project_store import BridgeProjectStore, validate_owner_agent_id

SCOPE_FIELDS = ("codex_root_thread_id", "owner_agent_id")


def scope_fields(data) -> dict[str, str]:
    values = {key: data.get(key, "") for key in SCOPE_FIELDS}
    if not any(values.values()):
        return {}
    root, owner = (values[key] for key in SCOPE_FIELDS)
    if not isinstance(root, str) or not isinstance(owner, str) or not root or not owner:
        raise BridgeError("Codex scope requires both codex_root_thread_id and owner_agent_id")
    validate_id(root, "Codex root thread id")
    validate_owner_agent_id(owner)
    return values


def scoped_project(store: BridgeProjectStore, root: str, explicit: str = "") -> str:
    """Never fall back to the repository's legacy or another root's Project."""
    project_id = explicit or store.root_project_id(root)
    if project_id:
        if store.load_project(project_id).get("codex_root_thread_id") != root:
            raise BridgeError("Bridge Project belongs to a different Codex root thread")
        return project_id
    project_id = "codex-" + hashlib.sha256(root.encode()).hexdigest()[:24]
    store.create_project(project_id, title=f"Codex {root}", codex_root_thread_id=root)
    return project_id


def owner_thread_id(root: str, owner: str, remote_project_id: str) -> str:
    # Project rebinds retain old history, but must not reuse its conversation.
    key = "\0".join((root, owner, remote_project_id))
    return "codex-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def validate_scope_binding(data) -> None:
    scope = scope_fields(data)
    project_id = data.get("bridge_project_id", "")
    if not project_id:
        if scope:
            raise BridgeError("Codex root/owner scope requires a Project binding")
        return
    store = BridgeProjectStore(Path(data["repo"]))
    if not scope and not (store.project_dir(project_id) / "project.json").exists():
        return  # Legacy request preparation did not require a local Project record.
    project = store.load_project(project_id)
    if not scope and not project.get("codex_root_thread_id"):
        return
    if not scope or project.get("codex_root_thread_id") != scope["codex_root_thread_id"]:
        raise BridgeError("Request Codex root does not match its Project")
    task = store.task_states(project_id).get(data["bridge_thread_id"], {})
    if task.get("owner_agent_id") != scope["owner_agent_id"]:
        raise BridgeError("Request owner does not match its Bridge Thread")
