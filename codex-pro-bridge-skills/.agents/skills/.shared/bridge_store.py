#!/usr/bin/env python3
"""Shared persistence and rendering for Codex Pro Bridge.

The JSONL event ledger is canonical. Markdown timelines and indexes are derived
views and can be regenerated at any time.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Sequence

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA_VERSION = 1
SEQUENCE_EVENT_LIMIT = 40
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
LEGACY_EVENT_MAP = {
    "codex-update": "codex-snapshot",
    "bundle": "legacy-bundle",
    "gpt-pro-turn": "gpt-exchange",
}


class BridgeError(ValueError):
    """Raised when bridge state would become ambiguous or unsafe."""


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def timestamp_slug() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def validate_id(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value:
        raise BridgeError(f"{field} is required")
    if len(value) > 80 or not ID_RE.fullmatch(value):
        raise BridgeError(
            f"{field} must be 1-80 lowercase letters, digits, or hyphens, "
            "and must start and end with a letter or digit"
        )
    return value


def default_codex_session_id(thread_id: str) -> str:
    return _derived_session_id(thread_id, "-codex", "Codex session id")


def default_gpt_session_id(thread_id: str) -> str:
    return _derived_session_id(thread_id, "-gpt-pro", "GPT Pro session id")


def _derived_session_id(thread_id: str, suffix: str, field: str) -> str:
    thread_id = validate_id(thread_id, "bridge thread id")
    raw = f"{thread_id}{suffix}"
    if len(raw) <= 80:
        return validate_id(raw, field)
    fingerprint = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:8]
    keep = 80 - len(suffix) - len(fingerprint) - 1
    shortened = thread_id[:keep].rstrip("-")
    return validate_id(f"{shortened}-{fingerprint}{suffix}", field)


def bridge_root(repo: Path) -> Path:
    return repo.resolve() / ".codex" / "codex-pro-bridge"


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def repo_relative(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise BridgeError(f"Path is outside repository root: {path}") from exc


def resolve_repo_path(value: str, repo: Path, *, must_exist: bool = True) -> Path:
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (repo / path).resolve()
    if not is_within(path, repo):
        raise BridgeError(f"Path is outside repository root: {value}")
    if must_exist and not path.exists():
        raise BridgeError(f"Path does not exist: {value}")
    return path


def file_sha256(path: Path) -> str:
    if not path.is_file():
        raise BridgeError(f"Digest source must be a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_quote(value: Any) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=False)


def parse_metadata(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    result: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") or line.startswith("## "):
            break
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        try:
            result[key] = str(json.loads(raw))
        except Exception:
            result[key] = raw.strip("'\"")
    return result


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        # Frozen artifact hashes are computed from UTF-8 text bytes. Windows
        # newline translation would otherwise change those bytes after hashing.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- Host-local browser ownership ---------------------------------------------
# The registry is shared across repositories and worktrees on this WSL host.
# Claims are scoped by Chrome profile and, when available, Project/conversation,
# so different conversations may mutate different dedicated tabs concurrently.
# A Project claim blocks conversation claims in that Project while shared
# Project state is being changed. Conversation bindings persist after a live
# claim is released: one Bridge Thread can never silently move to another chat.
DEFAULT_BROWSER_LEASE_TTL_SECONDS = 1800
DEFAULT_BROWSER_PROFILE = "chrome-stable-default"
BROWSER_PROFILE_ALIASES = {
    "chrome-stable-default": DEFAULT_BROWSER_PROFILE,
    "chrome-default": DEFAULT_BROWSER_PROFILE,
    "chrome-devtools": DEFAULT_BROWSER_PROFILE,
}
TAB_OWNER_STORAGE_KEY = "codex-pro-bridge.tab-owner.v1"
_BROWSER_STATE_DIR_ENV = "CODEX_PRO_BRIDGE_BROWSER_STATE_DIR"
_BROWSER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,240}$")
_BROWSER_CLAIM_SCOPES = {"auto", "profile", "project", "conversation"}


def _browser_state_root() -> Path:
    configured = os.environ.get(_BROWSER_STATE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".codex" / "codex-pro-bridge" / "browser-state"


def _browser_registry_path() -> Path:
    return _browser_state_root() / "ownership.json"


def _empty_browser_registry() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "bindings": [],
        "leases": [],
        "pending_bootstraps": [],
    }


def _read_browser_registry() -> Dict[str, Any]:
    path = _browser_registry_path()
    if not path.exists():
        return _empty_browser_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BridgeError(f"Cannot read browser ownership registry {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise BridgeError(f"Unsupported browser ownership registry: {path}")
    if not isinstance(data.get("bindings"), list) or not isinstance(data.get("leases"), list):
        raise BridgeError(f"Malformed browser ownership registry: {path}")
    # ``pending_bootstraps`` was added without changing schema v1 so existing
    # registries remain readable. A pending bootstrap is durable browser-tab
    # identity; unlike a lease, it must never disappear merely because time
    # elapsed while ChatGPT was generating or the local worker was interrupted.
    if "pending_bootstraps" not in data:
        data["pending_bootstraps"] = []
    if not isinstance(data.get("pending_bootstraps"), list):
        raise BridgeError(f"Malformed browser ownership registry: {path}")
    _materialize_legacy_browser_bootstraps(data)
    return data


def _write_browser_registry(registry: Mapping[str, Any]) -> None:
    path = _browser_registry_path()
    atomic_write_text(path, json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n")


def _lease_is_live(lease: Mapping[str, Any], *, now: dt.datetime) -> bool:
    expires_at = str(lease.get("expires_at", "")) if lease else ""
    if not expires_at:
        return False
    try:
        return dt.datetime.fromisoformat(expires_at) > now
    except ValueError:
        return False


def _live_browser_leases(registry: Mapping[str, Any], *, now: dt.datetime) -> List[Dict[str, Any]]:
    return [
        dict(lease)
        for lease in registry.get("leases", [])
        if isinstance(lease, dict) and _lease_is_live(lease, now=now)
    ]


def _validate_browser_identity(value: str, field: str, *, required: bool = False) -> str:
    normalized = (value or "").strip()
    if not normalized:
        if required:
            raise BridgeError(f"{field} is required")
        return ""
    if not _BROWSER_ID_RE.fullmatch(normalized):
        raise BridgeError(
            f"{field} must be 1-240 letters, digits, dots, underscores, or hyphens"
        )
    return normalized


def normalize_browser_profile(value: str) -> str:
    profile = _validate_browser_identity(value, "browser profile", required=True)
    try:
        return BROWSER_PROFILE_ALIASES[profile]
    except KeyError as exc:
        raise BridgeError(f"Unknown browser profile: {profile!r}") from exc


def _browser_profiles_match(left: Any, right: Any) -> bool:
    return normalize_browser_profile(str(left or "")) == normalize_browser_profile(
        str(right or "")
    )


def _resolve_browser_scope(
    requested_scope: str,
    *,
    conversation_id: str,
    project_id: str,
) -> str:
    scope = (requested_scope or "auto").strip().lower()
    if scope not in _BROWSER_CLAIM_SCOPES:
        raise BridgeError(f"Unknown browser claim scope: {requested_scope!r}")
    if scope == "auto":
        return "conversation" if conversation_id else "project" if project_id else "profile"
    if scope == "conversation" and not conversation_id:
        raise BridgeError("A conversation browser claim requires a conversation id")
    if scope == "project" and not project_id:
        raise BridgeError("A Project browser claim requires a remote Project id")
    return scope


def _browser_claims_conflict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if not _browser_profiles_match(
        left.get("browser_profile"), right.get("browser_profile")
    ):
        return False
    left_scope = left.get("scope")
    right_scope = right.get("scope")
    if "profile" in {left_scope, right_scope}:
        return True
    left_project = str(left.get("expected_remote_project_id", ""))
    right_project = str(right.get("expected_remote_project_id", ""))
    if left_project and left_project == right_project and "project" in {left_scope, right_scope}:
        return True
    return bool(
        left_scope == right_scope == "conversation"
        and left.get("expected_conversation_id") == right.get("expected_conversation_id")
    )


def _same_bootstrap_identity(
    item: Mapping[str, Any], requested: Mapping[str, Any]
) -> bool:
    return bool(
        item.get("bootstrap", False)
        and requested.get("bootstrap", False)
        and _browser_profiles_match(
            item.get("browser_profile"), requested.get("browser_profile")
        )
        and item.get("scope") == requested.get("scope")
        and item.get("thread_id", "") == requested.get("thread_id", "")
        and item.get("expected_conversation_id", "")
        == requested.get("expected_conversation_id", "")
        and item.get("expected_remote_project_id", "")
        == requested.get("expected_remote_project_id", "")
    )


def _pending_bootstrap_from_claim(
    claim: Mapping[str, Any], *, now: dt.datetime
) -> Dict[str, Any]:
    created_at = str(claim.get("acquired_at") or now.isoformat(timespec="seconds"))
    pending = {
        "browser_profile": normalize_browser_profile(
            str(claim.get("browser_profile", ""))
        ),
        "scope": str(claim.get("scope", "")),
        "thread_id": str(claim.get("thread_id", "")),
        "expected_conversation_id": "",
        "expected_remote_project_id": str(
            claim.get("expected_remote_project_id", "")
        ),
        "bootstrap": True,
        "tab_owner_token": str(claim.get("tab_owner_token", "")),
        "storage_key": str(claim.get("storage_key") or TAB_OWNER_STORAGE_KEY),
        "tab_bound": bool(claim.get("tab_bound", False)),
        "created_at": created_at,
        "last_claimed_at": now.isoformat(timespec="seconds"),
    }
    if claim.get("tab_bound_at"):
        pending["tab_bound_at"] = claim["tab_bound_at"]
    return pending


def _materialize_legacy_browser_bootstraps(registry: Dict[str, Any]) -> None:
    """Normalize retained pre-pending bootstrap leases without writing the registry.

    Every registry mutation filters expired leases. Materializing their durable
    identity during read ensures an unrelated later mutation cannot discard the
    only owner-token record before the original Bridge Thread returns.
    """
    now = dt.datetime.now().astimezone()
    for lease in registry.get("leases", []):
        if (
            not isinstance(lease, dict)
            or not lease.get("bootstrap", False)
            or not lease.get("tab_owner_token")
        ):
            continue
        matches = [
            pending
            for pending in registry["pending_bootstraps"]
            if isinstance(pending, dict)
            and _same_bootstrap_identity(pending, lease)
        ]
        owners = {
            str(item.get("tab_owner_token", ""))
            for item in matches + [lease]
            if str(item.get("tab_owner_token", ""))
        }
        if len(owners) != 1:
            raise BridgeError(
                "Ambiguous retained bootstrap owners for one browser identity; HOLD"
            )
        if not matches:
            registry["pending_bootstraps"].append(
                _pending_bootstrap_from_claim(lease, now=now)
            )
            continue
        if len(matches) > 1:
            raise BridgeError(
                "Multiple pending bootstraps for one browser identity; HOLD"
            )
        pending = matches[0]
        if lease.get("tab_bound", False):
            pending["tab_bound"] = True
            if lease.get("tab_bound_at"):
                pending["tab_bound_at"] = lease["tab_bound_at"]


def read_browser_lease(repo: Path) -> Dict[str, Any]:
    """Return the live host-local claim registry.

    ``repo`` remains in the signature for compatibility; ownership is no longer
    repository-local.
    """
    del repo
    path = _browser_registry_path()
    lock_path = path.with_name(f".{path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        return {
            "schema_version": 1,
            "registry_path": str(path),
            "bindings": [dict(item) for item in registry["bindings"] if isinstance(item, dict)],
            "leases": _live_browser_leases(registry, now=now),
            "pending_bootstraps": [
                dict(item)
                for item in registry["pending_bootstraps"]
                if isinstance(item, dict)
            ],
        }


def inspect_browser_tab_owner(
    repo: Path,
    *,
    browser_profile: str,
    tab_owner_token: str,
) -> Dict[str, Any]:
    """Classify an observed tab owner without changing browser or registry state."""
    del repo
    profile = normalize_browser_profile(browser_profile)
    owner_token = _validate_browser_identity(
        tab_owner_token, "tab owner token", required=True
    )
    path = _browser_registry_path()
    lock_path = path.with_name(f".{path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        live_claims = [
            dict(item)
            for item in _live_browser_leases(registry, now=now)
            if _browser_profiles_match(item.get("browser_profile"), profile)
            and item.get("tab_owner_token") == owner_token
        ]
        bindings = [
            dict(item)
            for item in registry["bindings"]
            if isinstance(item, dict)
            and _browser_profiles_match(item.get("browser_profile"), profile)
            and item.get("tab_owner_token") == owner_token
        ]
        pending_bootstraps = [
            dict(item)
            for item in registry["pending_bootstraps"]
            if isinstance(item, dict)
            and _browser_profiles_match(item.get("browser_profile"), profile)
            and item.get("tab_owner_token") == owner_token
        ]
    status = (
        "live-claim"
        if live_claims
        else "durable-binding"
        if bindings
        else "pending-bootstrap"
        if pending_bootstraps
        else "stale"
    )
    return {
        "status": status,
        "browser_profile": profile,
        "tab_owner_token": owner_token,
        "live_claims": live_claims,
        "bindings": bindings,
        "pending_bootstraps": pending_bootstraps,
    }


def mark_browser_tab_bound(
    repo: Path,
    *,
    token: str,
    observed_tab_owner_token: str,
) -> Dict[str, Any]:
    """Persist bootstrap tab binding only after its owner token was observed."""
    del repo
    token = (token or "").strip()
    owner_token = (observed_tab_owner_token or "").strip()
    if not token or not owner_token:
        raise BridgeError("Live claim and observed tab owner tokens are required")
    path = _browser_registry_path()
    lock_path = path.with_name(f".{path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        live = _live_browser_leases(registry, now=now)
        claim = next((item for item in live if item.get("token") == token), None)
        if claim is None:
            raise BridgeError("Browser claim token is not a live holder")
        if not claim.get("bootstrap", False):
            raise BridgeError("Only a bootstrap claim records tab_bound")
        if claim.get("tab_owner_token") != owner_token:
            raise BridgeError("Observed tab owner token does not match the bootstrap claim")
        if claim.get("tab_bound", False):
            return dict(claim)
        claim["tab_bound"] = True
        claim["tab_bound_at"] = now.isoformat(timespec="seconds")
        matching_pending = next(
            (
                item
                for item in registry["pending_bootstraps"]
                if isinstance(item, dict)
                and _same_bootstrap_identity(item, claim)
                and item.get("tab_owner_token") == owner_token
            ),
            None,
        )
        if matching_pending is None:
            matching_pending = _pending_bootstrap_from_claim(claim, now=now)
            registry["pending_bootstraps"].append(matching_pending)
        matching_pending["tab_bound"] = True
        matching_pending["tab_bound_at"] = claim["tab_bound_at"]
        matching_pending["last_claimed_at"] = claim["tab_bound_at"]
        registry["leases"] = [
            claim if item.get("token") == token else item for item in live
        ]
        registry["updated_at"] = claim["tab_bound_at"]
        _write_browser_registry(registry)
        return dict(claim)


def acquire_browser_lease(
    repo: Path,
    *,
    holder: str,
    thread_id: str = "",
    expected_conversation_id: str = "",
    expected_remote_project_id: str = "",
    browser_profile: str = DEFAULT_BROWSER_PROFILE,
    scope: str = "auto",
    bootstrap: bool = False,
    ttl_seconds: int = DEFAULT_BROWSER_LEASE_TTL_SECONDS,
) -> Dict[str, Any]:
    """Acquire one host-local, profile-aware browser claim.

    Different conversation claims may coexist. The same conversation cannot be
    claimed by another thread, and a Project/profile claim excludes narrower
    claims in its scope.
    """
    del repo
    holder = (holder or "").strip()
    if not holder:
        raise BridgeError("A browser claim requires a non-empty --holder")
    if ttl_seconds <= 0:
        raise BridgeError("Browser claim ttl-seconds must be positive")
    profile = normalize_browser_profile(browser_profile)
    conversation_id = _validate_browser_identity(
        expected_conversation_id, "conversation id"
    )
    project_id = _validate_browser_identity(expected_remote_project_id, "remote Project id")
    normalized_thread = validate_id(thread_id, "bridge thread id") if thread_id else ""
    resolved_scope = _resolve_browser_scope(
        scope, conversation_id=conversation_id, project_id=project_id
    )
    if resolved_scope == "conversation" and not normalized_thread:
        raise BridgeError("A conversation browser claim requires --bridge-thread-id")
    if bootstrap:
        if conversation_id or resolved_scope == "conversation":
            raise BridgeError("A bootstrap claim cannot already have a conversation id")
        if not normalized_thread:
            raise BridgeError("A bootstrap claim requires --bridge-thread-id")
        if resolved_scope not in {"project", "profile"}:
            raise BridgeError("A bootstrap claim must use Project or profile scope")

    requested = {
        "browser_profile": profile,
        "scope": resolved_scope,
        "thread_id": normalized_thread,
        "expected_conversation_id": conversation_id,
        "expected_remote_project_id": project_id,
        "bootstrap": bool(bootstrap),
    }
    registry_path = _browser_registry_path()
    lock_path = registry_path.with_name(f".{registry_path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        live = _live_browser_leases(registry, now=now)

        if bootstrap:
            for binding in registry["bindings"]:
                if not isinstance(binding, dict):
                    continue
                if (
                    _browser_profiles_match(binding.get("browser_profile"), profile)
                    and binding.get("thread_id") == normalized_thread
                ):
                    raise BridgeError(
                        f"Bridge Thread {normalized_thread!r} is already bound to conversation "
                        f"{binding.get('expected_conversation_id')!r}"
                    )

        pending_bootstrap = None
        for pending in registry["pending_bootstraps"]:
            if not isinstance(pending, dict):
                continue
            if (
                _browser_profiles_match(pending.get("browser_profile"), profile)
                and pending.get("thread_id") == normalized_thread
            ):
                if bootstrap:
                    if not _same_bootstrap_identity(pending, requested):
                        raise BridgeError(
                            f"Bridge Thread {normalized_thread!r} already has a pending "
                            "bootstrap for a different browser scope"
                        )
                    pending_bootstrap = pending
                elif _browser_claims_conflict(pending, requested):
                    raise BridgeError(
                        f"Bridge Thread {normalized_thread!r} has a pending bootstrap; "
                        "explicit release or promotion is required"
                    )
            elif _browser_claims_conflict(pending, requested):
                raise BridgeError(
                    "Browser claim conflicts with pending bootstrap for holder thread "
                    f"{pending.get('thread_id', '<unknown>')!r}, scope "
                    f"{pending.get('scope', '<unknown>')!r}; explicit release or "
                    "promotion is required"
                )

        for current in live:
            same_claim = all(
                _browser_profiles_match(current.get(key), value)
                if key == "browser_profile"
                else current.get(key, False) == value
                if key == "bootstrap"
                else current.get(key, "") == value
                for key, value in requested.items()
            )
            if same_claim and current.get("holder") == holder:
                if bootstrap and pending_bootstrap is None:
                    pending_bootstrap = _pending_bootstrap_from_claim(current, now=now)
                    registry["pending_bootstraps"].append(pending_bootstrap)
                if pending_bootstrap is not None:
                    current["tab_owner_token"] = pending_bootstrap["tab_owner_token"]
                    current["tab_bound"] = bool(pending_bootstrap.get("tab_bound", False))
                    if pending_bootstrap.get("tab_bound_at"):
                        current["tab_bound_at"] = pending_bootstrap["tab_bound_at"]
                    pending_bootstrap["last_claimed_at"] = now.isoformat(timespec="seconds")
                current["expires_at"] = (
                    now + dt.timedelta(seconds=ttl_seconds)
                ).isoformat(timespec="seconds")
                registry["leases"] = [
                    current if item.get("token") == current.get("token") else item
                    for item in live
                ]
                registry["updated_at"] = now.isoformat(timespec="seconds")
                _write_browser_registry(registry)
                return current
            if _browser_claims_conflict(current, requested):
                raise BridgeError(
                    "Browser claim conflicts with holder "
                    f"{current.get('holder', '<unknown>')!r}, scope "
                    f"{current.get('scope', '<unknown>')!r}, conversation "
                    f"{current.get('expected_conversation_id', '<none>')!r}, until "
                    f"{current.get('expires_at', '<unknown>')}"
                )

        tab_owner_token = (
            str(pending_bootstrap.get("tab_owner_token", ""))
            if pending_bootstrap is not None
            else uuid.uuid4().hex
            if bootstrap
            else ""
        )
        compatible_tab_owner_tokens: List[str] = []
        if resolved_scope == "conversation":
            matching_bindings = []
            for binding in registry["bindings"]:
                if not isinstance(binding, dict):
                    continue
                if (
                    _browser_profiles_match(binding.get("browser_profile"), profile)
                    and binding.get("expected_conversation_id") == conversation_id
                ):
                    if binding.get("thread_id") != normalized_thread:
                        raise BridgeError(
                            f"Conversation {conversation_id!r} belongs to Bridge Thread "
                            f"{binding.get('thread_id')!r}, not {normalized_thread!r}"
                        )
                    if binding.get("expected_remote_project_id", "") != project_id:
                        raise BridgeError("Conversation binding has a different ChatGPT Project id")
                    matching_bindings.append(binding)
                if (
                    _browser_profiles_match(binding.get("browser_profile"), profile)
                    and binding.get("thread_id") == normalized_thread
                    and binding.get("expected_conversation_id") != conversation_id
                ):
                    raise BridgeError(
                        f"Bridge Thread {normalized_thread!r} is already bound to conversation "
                        f"{binding.get('expected_conversation_id')!r}"
                    )
            matching_binding = (
                max(
                    matching_bindings,
                    key=lambda item: str(
                        item.get("last_claimed_at") or item.get("created_at") or ""
                    ),
                )
                if matching_bindings
                else None
            )
            if matching_binding:
                tab_owner_token = str(matching_binding.get("tab_owner_token", ""))
                compatible_tab_owner_tokens = sorted(
                    {
                        str(item.get("tab_owner_token", ""))
                        for item in matching_bindings
                        if str(item.get("tab_owner_token", ""))
                    }
                )
                matching_binding["last_claimed_at"] = now.isoformat(timespec="seconds")
            else:
                tab_owner_token = uuid.uuid4().hex
                compatible_tab_owner_tokens = [tab_owner_token]
                registry["bindings"].append(
                    {
                        "browser_profile": profile,
                        "thread_id": normalized_thread,
                        "expected_conversation_id": conversation_id,
                        "expected_remote_project_id": project_id,
                        "tab_owner_token": tab_owner_token,
                        "storage_key": TAB_OWNER_STORAGE_KEY,
                        "created_at": now.isoformat(timespec="seconds"),
                        "last_claimed_at": now.isoformat(timespec="seconds"),
                    }
                )

        acquired = now.isoformat(timespec="seconds")
        lease = {
            "token": uuid.uuid4().hex,
            "holder": holder,
            **requested,
            "tab_owner_token": tab_owner_token,
            "storage_key": TAB_OWNER_STORAGE_KEY if tab_owner_token else "",
            "acquired_at": acquired,
            "expires_at": (
                now + dt.timedelta(seconds=ttl_seconds)
            ).isoformat(timespec="seconds"),
        }
        if bootstrap:
            lease["tab_bound"] = bool(
                pending_bootstrap.get("tab_bound", False)
                if pending_bootstrap is not None
                else False
            )
            if pending_bootstrap and pending_bootstrap.get("tab_bound_at"):
                lease["tab_bound_at"] = pending_bootstrap["tab_bound_at"]
            if pending_bootstrap is None:
                pending_bootstrap = _pending_bootstrap_from_claim(lease, now=now)
                registry["pending_bootstraps"].append(pending_bootstrap)
            else:
                pending_bootstrap["last_claimed_at"] = acquired
        elif compatible_tab_owner_tokens:
            lease["compatible_tab_owner_tokens"] = compatible_tab_owner_tokens
        registry["leases"] = live + [lease]
        registry["updated_at"] = acquired
        _write_browser_registry(registry)
        return lease


def promote_browser_bootstrap(
    repo: Path,
    *,
    token: str,
    thread_id: str,
    conversation_id: str,
    expected_remote_project_id: str = "",
    browser_profile: str = DEFAULT_BROWSER_PROFILE,
    observed_tab_owner_token: str,
) -> Dict[str, Any]:
    """Atomically promote a first-Send claim to a durable conversation binding."""
    del repo
    token = (token or "").strip()
    if not token:
        raise BridgeError("Browser claim token is required")
    normalized_thread = validate_id(thread_id, "bridge thread id")
    normalized_conversation = _validate_browser_identity(
        conversation_id, "conversation id", required=True
    )
    normalized_project = _validate_browser_identity(
        expected_remote_project_id, "remote Project id"
    )
    profile = normalize_browser_profile(browser_profile)
    owner_token = (observed_tab_owner_token or "").strip()
    if not owner_token:
        raise BridgeError("Observed tab owner token is required")

    registry_path = _browser_registry_path()
    lock_path = registry_path.with_name(f".{registry_path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        live = _live_browser_leases(registry, now=now)
        claim = next((item for item in live if item.get("token") == token), None)
        if claim is None:
            raise BridgeError("Browser claim token is not a live holder")
        if claim.get("thread_id") != normalized_thread:
            raise BridgeError("Bootstrap claim belongs to a different Bridge Thread")
        if not _browser_profiles_match(claim.get("browser_profile"), profile):
            raise BridgeError("Bootstrap claim belongs to a different browser profile")
        if claim.get("expected_remote_project_id", "") != normalized_project:
            raise BridgeError("Bootstrap claim belongs to a different ChatGPT Project")
        if claim.get("tab_owner_token", "") != owner_token:
            raise BridgeError("The post-Send tab owner token does not match the bootstrap claim")

        if not claim.get("bootstrap", False):
            if (
                claim.get("scope") == "conversation"
                and claim.get("expected_conversation_id") == normalized_conversation
            ):
                return dict(claim)
            raise BridgeError("Browser claim is not an active conversation bootstrap")
        expected_scope = "project" if normalized_project else "profile"
        if claim.get("scope") != expected_scope:
            raise BridgeError(f"Bootstrap claim must have {expected_scope} scope")

        matching_binding = None
        for binding in registry["bindings"]:
            if not isinstance(binding, dict):
                continue
            same_profile = _browser_profiles_match(binding.get("browser_profile"), profile)
            if same_profile and binding.get("thread_id") == normalized_thread:
                if binding.get("expected_conversation_id") != normalized_conversation:
                    raise BridgeError(
                        f"Bridge Thread {normalized_thread!r} is already bound to conversation "
                        f"{binding.get('expected_conversation_id')!r}"
                    )
                matching_binding = binding
            if same_profile and binding.get("expected_conversation_id") == normalized_conversation:
                if binding.get("thread_id") != normalized_thread:
                    raise BridgeError(
                        f"Conversation {normalized_conversation!r} belongs to Bridge Thread "
                        f"{binding.get('thread_id')!r}"
                    )
                matching_binding = binding

        promoted_at = now.isoformat(timespec="seconds")
        if matching_binding is None:
            matching_binding = {
                "browser_profile": profile,
                "thread_id": normalized_thread,
                "expected_conversation_id": normalized_conversation,
                "expected_remote_project_id": normalized_project,
                "tab_owner_token": owner_token,
                "storage_key": TAB_OWNER_STORAGE_KEY,
                "created_at": promoted_at,
                "last_claimed_at": promoted_at,
            }
            registry["bindings"].append(matching_binding)
        elif (
            matching_binding.get("expected_remote_project_id", "") != normalized_project
            or matching_binding.get("tab_owner_token", "") != owner_token
        ):
            raise BridgeError("Existing conversation binding does not match the bootstrap claim")

        promoted = {
            **claim,
            "scope": "conversation",
            "expected_conversation_id": normalized_conversation,
            "bootstrap": False,
            "promoted_at": promoted_at,
        }
        promoted.pop("tab_bound", None)
        registry["pending_bootstraps"] = [
            item
            for item in registry["pending_bootstraps"]
            if not (
                isinstance(item, dict)
                and _same_bootstrap_identity(item, claim)
                and item.get("tab_owner_token") == owner_token
            )
        ]
        registry["leases"] = [
            promoted if item.get("token") == token else item for item in live
        ]
        registry["updated_at"] = promoted_at
        _write_browser_registry(registry)
        return dict(promoted)


def release_browser_lease(repo: Path, *, token: str) -> bool:
    """Release one live claim by exact token; persistent chat binding remains."""
    del repo
    token = (token or "").strip()
    if not token:
        raise BridgeError("Browser claim token is required")
    registry_path = _browser_registry_path()
    lock_path = registry_path.with_name(f".{registry_path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        live = _live_browser_leases(registry, now=now)
        released_claim = next((lease for lease in live if lease.get("token") == token), None)
        kept = [lease for lease in live if lease.get("token") != token]
        if len(kept) == len(live):
            raise BridgeError("Browser claim token is not a live holder")
        if released_claim and released_claim.get("bootstrap", False):
            registry["pending_bootstraps"] = [
                item
                for item in registry["pending_bootstraps"]
                if not (
                    isinstance(item, dict)
                    and _same_bootstrap_identity(item, released_claim)
                    and item.get("tab_owner_token")
                    == released_claim.get("tab_owner_token")
                )
            ]
        registry["leases"] = kept
        registry["updated_at"] = now.isoformat(timespec="seconds")
        _write_browser_registry(registry)
        return True


def assert_browser_lease_held(repo: Path, *, token: str) -> Dict[str, Any]:
    """Return the live host-local claim identified by ``token``."""
    del repo
    token = (token or "").strip()
    registry_path = _browser_registry_path()
    lock_path = registry_path.with_name(f".{registry_path.name}.lock")
    with file_lock(lock_path):
        registry = _read_browser_registry()
        now = dt.datetime.now().astimezone()
        for lease in _live_browser_leases(registry, now=now):
            if lease.get("token") == token:
                return lease
    raise BridgeError("Browser claim token is not a live holder")


def write_bound_metadata(
    path: Path,
    values: Mapping[str, Any],
    *,
    ordered_keys: Sequence[str],
    immutable_keys: Sequence[str],
) -> Dict[str, str]:
    """Write metadata while rejecting identity changes."""
    lock_path = path.with_name(f".{path.name}.lock")
    with file_lock(lock_path):
        previous = parse_metadata(path)
        merged: Dict[str, str] = dict(previous)
        for key, raw_value in values.items():
            value = str(raw_value or "")
            old = previous.get(key, "")
            if key in immutable_keys and old and value and old != value:
                raise BridgeError(f"Cannot change {key} from {old!r} to {value!r} in {path}")
            if value or key not in merged:
                merged[key] = value
        lines = [f"{key}: {json_quote(merged.get(key, ''))}" for key in ordered_keys]
        atomic_write_text(path, "\n".join(lines) + "\n")
        return merged


def unique_artifact_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-") or "artifact"
    for _ in range(10):
        candidate = directory / f"{timestamp_slug()}-{safe_stem}-{uuid.uuid4().hex[:8]}{suffix}"
        if not candidate.exists():
            return candidate
    raise BridgeError(f"Could not allocate a unique artifact path under {directory}")


def require_new_output(path: Path) -> None:
    if path.exists():
        raise BridgeError(f"Refusing to overwrite immutable artifact: {path}")


def _legacy_events(markdown_path: Path, thread_id: str) -> List[Dict[str, Any]]:
    if not markdown_path.exists():
        return []
    text = markdown_path.read_text(encoding="utf-8")
    marker = "## Timeline\n"
    if marker not in text:
        return []
    timeline = text.split(marker, 1)[1]
    meta = parse_metadata(markdown_path)
    raw_events: List[tuple[str, str, List[str]]] = []
    current: tuple[str, str, List[str]] | None = None
    for line in timeline.splitlines():
        if line.startswith("### "):
            if current:
                raw_events.append(current)
            header = line[4:].strip()
            if " - " in header:
                occurred_at, event_type = header.split(" - ", 1)
            else:
                occurred_at, event_type = "", header
            current = (occurred_at, event_type, [])
        elif current is not None:
            current[2].append(line)
    if current:
        raw_events.append(current)

    events: List[Dict[str, Any]] = []
    parent = ""
    for index, (occurred_at, legacy_type, lines) in enumerate(raw_events, start=1):
        fingerprint = hashlib.sha256(
            (occurred_at + legacy_type + "\n".join(lines)).encode("utf-8")
        ).hexdigest()[:10]
        event_id = f"legacy-{index:04d}-{fingerprint}"
        mapped = LEGACY_EVENT_MAP.get(legacy_type, f"legacy-{legacy_type}")
        details = "\n".join(lines)
        codex_match = re.search(r"Codex session:\s*`([^`]+)`", details)
        gpt_match = re.search(r"GPT Pro session:\s*`([^`]+)`", details)
        codex_session_id = codex_match.group(1) if codex_match else ""
        gpt_session_id = gpt_match.group(1) if gpt_match else ""
        if not codex_session_id:
            codex_session_id = (meta.get("codex_session_ids", "").split(",", 1)[0]).strip()
        if not gpt_session_id:
            gpt_session_id = (meta.get("gpt_pro_session_ids", "").split(",", 1)[0]).strip()
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "thread_id": thread_id,
            "event_type": mapped,
            "occurred_at": occurred_at or meta.get("created_at", ""),
            "actor": "gpt-pro" if mapped == "gpt-exchange" else "codex",
            "parent_event_id": parent,
            "thread_title": meta.get("title", thread_id),
            "codex_session_id": codex_session_id,
            "gpt_pro_session_id": gpt_session_id,
            "artifact": {},
            "data": {"legacy_event_type": legacy_type, "legacy_details": lines},
        }
        events.append(event)
        parent = event_id
    return events


def _jsonl_path(bridge_dir: Path, thread_id: str) -> Path:
    return bridge_dir / "threads" / f"{thread_id}.jsonl"


def _markdown_path(bridge_dir: Path, thread_id: str) -> Path:
    return bridge_dir / "threads" / f"{thread_id}.md"


def load_events(bridge_dir: Path, thread_id: str) -> List[Dict[str, Any]]:
    thread_id = validate_id(thread_id, "bridge thread id")
    jsonl_path = _jsonl_path(bridge_dir, thread_id)
    if not jsonl_path.exists():
        return _legacy_events(_markdown_path(bridge_dir, thread_id), thread_id)
    events: List[Dict[str, Any]] = []
    for line_number, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"Invalid JSONL at {jsonl_path}:{line_number}: {exc}") from exc
        if event.get("thread_id") != thread_id:
            raise BridgeError(f"Thread id mismatch in {jsonl_path}:{line_number}")
        events.append(event)
    return events


def _event_summary(event: Mapping[str, Any]) -> str:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    for key in ("summary", "question", "goal", "verification", "turn"):
        value = str(data.get(key, "")).strip()
        if value:
            return re.sub(r"\s+", " ", value)[:90]
    return event.get("event_type", "event")


def _sequence_diagram(events: Sequence[Mapping[str, Any]]) -> str:
    selected = list(events[-SEQUENCE_EVENT_LIMIT:])
    start = len(events) - len(selected) + 1
    lines = [
        "```mermaid",
        "sequenceDiagram",
        "  participant C as Codex",
        "  participant G as GPT Pro",
    ]
    for offset, event in enumerate(selected):
        number = start + offset
        event_type = str(event.get("event_type", "event"))
        label = re.sub(r"[\r\n:]+", " ", _event_summary(event)).replace('"', "'")
        label = f"{number:02d} {event_type}: {label}"[:150]
        if event_type == "gpt-exchange":
            lines.append(f"  C->>G: {label}")
            lines.append(f"  G-->>C: {number:02d} response captured")
        else:
            lines.append(f"  C->>C: {label}")
    lines.append("```")
    if len(events) > len(selected):
        return f"_View shows the latest {len(selected)} of {len(events)} events._\n\n" + "\n".join(lines)
    return "\n".join(lines)


def _format_value(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [f"{key}={item}" for key, item in value.items() if item not in (None, "", [], {})]
        return ", ".join(parts) or "-"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or "-"


def _thread_metadata(thread_id: str, events: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    title = next((str(event.get("thread_title")) for event in events if event.get("thread_title")), thread_id)
    project_ids = list(
        dict.fromkeys(
            str(event.get("bridge_project_id"))
            for event in events
            if event.get("bridge_project_id")
        )
    )
    codex_ids = list(dict.fromkeys(str(event.get("codex_session_id")) for event in events if event.get("codex_session_id")))
    gpt_ids = list(dict.fromkeys(str(event.get("gpt_pro_session_id")) for event in events if event.get("gpt_pro_session_id")))
    return {
        "bridge_thread_id": thread_id,
        "bridge_project_id": ", ".join(project_ids),
        "title": title,
        "created_at": str(events[0].get("occurred_at", "")) if events else "",
        "last_used_at": str(events[-1].get("occurred_at", "")) if events else "",
        "codex_session_ids": ", ".join(codex_ids),
        "gpt_pro_session_ids": ", ".join(gpt_ids),
        "latest_event": str(events[-1].get("event_type", "")) if events else "",
        "event_count": str(len(events)),
    }


def render_thread_markdown(thread_id: str, events: Sequence[Mapping[str, Any]]) -> str:
    meta = _thread_metadata(thread_id, events)
    header = "\n".join(f"{key}: {json_quote(value)}" for key, value in meta.items())
    timeline: List[str] = []
    for index, event in enumerate(events, start=1):
        timeline.extend(
            [
                f"### {index:03d} · {event.get('occurred_at', '-')} · {event.get('event_type', 'event')} · `{event.get('event_id', '-')}`",
                "",
                f"- Actor: `{event.get('actor', '-')}`",
                f"- Parent: `{event.get('parent_event_id') or '-'}`",
            ]
        )
        if event.get("codex_session_id"):
            timeline.append(f"- Codex session: `{event['codex_session_id']}`")
        if event.get("gpt_pro_session_id"):
            timeline.append(f"- GPT Pro session: `{event['gpt_pro_session_id']}`")
        if event.get("bridge_project_id"):
            timeline.append(f"- Bridge project: `{event['bridge_project_id']}`")
        artifact = event.get("artifact")
        if artifact:
            timeline.append(f"- Artifact: {_format_value(artifact)}")
        data = event.get("data")
        if isinstance(data, Mapping):
            for key, value in data.items():
                if value not in (None, "", [], {}):
                    timeline.append(f"- {key.replace('_', ' ').title()}: {_format_value(value)}")
        timeline.append("")
    body = "\n".join(timeline).rstrip() or "_No events recorded._"
    return (
        f"{header}\n\n# Bridge Thread: {meta['title']}\n\n"
        f"## Sequence\n\n{_sequence_diagram(events)}\n\n"
        f"## Timeline\n\n{body}\n"
    )


def _write_thread_index(bridge_dir: Path) -> None:
    threads_dir = bridge_dir / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(threads_dir / ".index.lock"):
        stems = {p.stem for p in threads_dir.glob("*.jsonl")}
        stems.update(p.stem for p in threads_dir.glob("*.md") if p.name != "index.md")
        rows: List[Dict[str, str]] = []
        for stem in stems:
            try:
                rows.append(_thread_metadata(stem, load_events(bridge_dir, stem)))
            except BridgeError as exc:
                rows.append(
                    {
                        "bridge_thread_id": stem,
                        "bridge_project_id": "",
                        "title": "INVALID LEDGER",
                        "created_at": "",
                        "last_used_at": "",
                        "codex_session_ids": "",
                        "gpt_pro_session_ids": "",
                        "latest_event": f"ERROR: {exc}",
                        "event_count": "?",
                    }
                )
        rows.sort(key=lambda row: row.get("last_used_at", ""), reverse=True)
        lines = [
            "# Codex Pro Bridge Threads",
            "",
            "| Bridge Thread | Project | Title | Codex Sessions | GPT Pro Sessions | Events | Last Used | Latest Event | File |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            escape = lambda value: (value or "-").replace("|", "\\|").replace("\n", " ")
            lines.append(
                "| `{}` | `{}` | {} | {} | {} | {} | {} | {} | [open]({}.md) |".format(
                    escape(row["bridge_thread_id"]),
                    escape(row["bridge_project_id"]),
                    escape(row["title"]),
                    escape(row["codex_session_ids"]),
                    escape(row["gpt_pro_session_ids"]),
                    escape(row["event_count"]),
                    escape(row["last_used_at"]),
                    escape(row["latest_event"]),
                    escape(row["bridge_thread_id"]),
                )
            )
        atomic_write_text(threads_dir / "index.md", "\n".join(lines) + "\n")


def append_event(
    repo: Path,
    *,
    thread_id: str,
    event_type: str,
    actor: str,
    thread_title: str = "",
    bridge_project_id: str = "",
    codex_session_id: str = "",
    gpt_pro_session_id: str = "",
    artifact: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    dedupe_key: str = "",
    occurred_at: str = "",
    expected_parent_event_id: str | None = None,
) -> Dict[str, Any]:
    repo = repo.resolve()
    thread_id = validate_id(thread_id, "bridge thread id")
    if bridge_project_id:
        bridge_project_id = validate_id(bridge_project_id, "bridge project id")
    if codex_session_id:
        codex_session_id = validate_id(codex_session_id, "Codex session id")
    if gpt_pro_session_id:
        gpt_pro_session_id = validate_id(gpt_pro_session_id, "GPT Pro session id")
    bridge_dir = bridge_root(repo)
    threads_dir = bridge_dir / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = _jsonl_path(bridge_dir, thread_id)
    lock_path = threads_dir / f".{thread_id}.lock"
    with file_lock(lock_path):
        events = load_events(bridge_dir, thread_id)
        existing_project_ids = {
            str(event.get("bridge_project_id"))
            for event in events
            if event.get("bridge_project_id")
        }
        if len(existing_project_ids) > 1:
            raise BridgeError(
                f"Bridge Thread {thread_id} has conflicting Project identities"
            )
        if (
            bridge_project_id
            and existing_project_ids
            and bridge_project_id not in existing_project_ids
        ):
            raise BridgeError(
                f"Bridge Thread {thread_id} belongs to "
                f"{next(iter(existing_project_ids))}, not {bridge_project_id}"
            )
        effective_project_id = bridge_project_id or (
            next(iter(existing_project_ids)) if existing_project_ids else ""
        )
        if dedupe_key:
            for event in events:
                if event.get("dedupe_key") == dedupe_key:
                    _verify_thread_events(repo, thread_id, events)
                    expected = {
                        "event_type": event_type, "actor": actor,
                        "bridge_project_id": effective_project_id,
                        "codex_session_id": codex_session_id,
                        "gpt_pro_session_id": gpt_pro_session_id,
                        "artifact": dict(artifact or {}), "data": dict(data or {}),
                    }
                    if any(event.get(key, "") != value for key, value in expected.items()):
                        raise BridgeError("Duplicate event key has changed identity or payload")
                    return event
        parent = str(events[-1].get("event_id", "")) if events else ""
        if expected_parent_event_id is not None and parent != expected_parent_event_id:
            raise BridgeError("Ledger changed before append; revalidate the repair")
        timestamp = occurred_at or now_iso()
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"{timestamp.replace(':', '').replace('+', '-')}-{uuid.uuid4().hex[:10]}",
            "thread_id": thread_id,
            "event_type": event_type,
            "occurred_at": timestamp,
            "actor": actor,
            "parent_event_id": parent,
            "thread_title": thread_title or (events[0].get("thread_title", "") if events else thread_id),
            "codex_session_id": codex_session_id,
            "gpt_pro_session_id": gpt_pro_session_id,
            "artifact": dict(artifact or {}),
            "data": dict(data or {}),
            "dedupe_key": dedupe_key,
        }
        if effective_project_id:
            event["bridge_project_id"] = effective_project_id
        # This is the sole append authority: qualify history and candidate under
        # the same lock. An evidenced late snapshot may repair its exact old
        # exchange; the combined verifier never permits unrelated bad history.
        _verify_thread_events(repo, thread_id, [*events, event], candidate_event_id=event["event_id"])
        if not jsonl_path.exists() and events:
            atomic_write_text(
                jsonl_path,
                "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in events),
            )
        with jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        events.append(event)
        atomic_write_text(_markdown_path(bridge_dir, thread_id), render_thread_markdown(thread_id, events))
    _write_thread_index(bridge_dir)
    return event


def compact_thread_context(
    repo: Path,
    thread_id: str,
    *,
    max_events: int = 24,
    max_chars: int = 20_000,
) -> str:
    if max_events <= 0 or max_chars <= 0:
        raise BridgeError("Thread event and character budgets must be positive")
    bridge_dir = bridge_root(repo)
    events = load_events(bridge_dir, thread_id)
    if not events:
        return "_No prior bridge thread events were available._"
    selected = events[-max_events:]
    omitted = len(events) - len(selected)
    while selected:
        lines = [
            f"- Bridge thread id: `{thread_id}`",
            f"- Total events: {len(events)}",
            f"- Included events: latest {len(selected)}",
            f"- Older events omitted: {omitted}",
            "",
            "## Recent Sequence",
            "",
            _sequence_diagram(selected),
            "",
            "## Recent Events",
            "",
        ]
        start = len(events) - len(selected) + 1
        for offset, event in enumerate(selected):
            lines.append(
                f"### {start + offset:03d} · {event.get('event_type')} · {event.get('occurred_at')}"
            )
            lines.append(f"- Event ID: `{event.get('event_id') or '-'}`")
            lines.append(f"- Parent: `{event.get('parent_event_id') or '-'}`")
            lines.append(f"- Summary: {_event_summary(event)}")
            artifact = event.get("artifact")
            if artifact:
                lines.append(f"- Artifact: {_format_value(artifact)}")
            data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            for key in (
                "bundle_sha256",
                "model_verification",
                "requested_model",
                "selected_ui_label",
            ):
                if data.get(key) not in (None, ""):
                    lines.append(f"- {key.replace('_', ' ').title()}: {_format_value(data[key])}")
            lines.append("")
        content = "\n".join(lines).strip()
        if len(content) <= max_chars:
            return content
        selected = selected[1:]
        omitted += 1
    return "_Bridge thread exists, but its compact view exceeded the configured budget._"


def recovered_snapshot_targets(repo: Path, events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Validate late snapshot receipts against notes in the actual sent bundle.

    A late receipt is not a backdated event or a snapshot for a future round.
    Unbundled exchanges cannot be repaired through this evidence route.
    """
    seen = {}
    recovered = set()
    for event in events:
        data = event.get("data") or {}
        target_id = data.get("recovery_for_exchange")
        if target_id:
            target = seen.get(target_id)
            if (event.get("event_type") != "codex-snapshot" or not target
                    or target.get("event_type") != "gpt-exchange" or target_id in recovered):
                raise BridgeError("Invalid or duplicate snapshot recovery target")
            if any(event.get(k, "") != target.get(k, "") for k in
                   ("thread_id", "codex_session_id", "bridge_project_id")):
                raise BridgeError("Snapshot recovery identity mismatch")
            source = target.get("data") or {}
            if not source.get("bundle") or data.get("source_bundle_sha256") != source.get("bundle_sha256"):
                raise BridgeError("Snapshot recovery needs the exact sent bundle")
            bundle = resolve_repo_path(source["bundle"], repo)
            if file_sha256(bundle) != source["bundle_sha256"]:
                raise BridgeError("Snapshot recovery bundle hash mismatch")
            member = "context/codex-session-notes.md"
            if data.get("source_member") != member:
                raise BridgeError("Snapshot recovery must use bundled Codex notes")
            artifact = event.get("artifact") or {}
            if not artifact.get("path"):
                raise BridgeError("Snapshot recovery artifact is missing")
            snapshot = resolve_repo_path(artifact["path"], repo)
            try:
                with zipfile.ZipFile(bundle) as archive:
                    if archive.namelist().count(member) != 1:
                        raise BridgeError("Snapshot recovery bundle notes are missing or ambiguous")
                    if snapshot.read_bytes() != archive.read(member):
                        raise BridgeError("Recovered snapshot differs from sent bundle notes")
            except (zipfile.BadZipFile, KeyError, OSError) as exc:
                raise BridgeError(f"Cannot validate snapshot recovery: {exc}") from exc
            recovered.add(target_id)
        seen[event.get("event_id")] = event
    return recovered


def verify_thread_integrity(
    repo: Path,
    thread_id: str,
    *,
    require_complete_rounds: bool = False,
) -> Dict[str, Any]:
    """Verify ledger links, artifact digests, bundle digests, and round ordering."""
    repo = repo.resolve()
    thread_id = validate_id(thread_id, "bridge thread id")
    events = load_events(bridge_root(repo), thread_id)
    return _verify_thread_events(repo, thread_id, events, require_complete_rounds=require_complete_rounds)


def snapshot_binding_metadata(path):
    labels = {"Bridge Thread ID":"bridge_thread_id","Bridge Project ID":"bridge_project_id",
              "Codex Session ID":"codex_session_id"}
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i,line in enumerate(lines[1:],1) if line.strip()),None)
    if not lines or lines[0] != "# Codex Session Notes" or start is None or lines[start] != "## Metadata":
        raise BridgeError("Legacy snapshot lacks its original Metadata section")
    values = {}
    for line in lines[start+1:]:
        if line.startswith("## "):
            break
        match = re.fullmatch(r"- (Bridge Thread ID|Bridge Project ID|Codex Session ID): `([^`]*)`",line)
        if match:
            key = labels[match[1]]
            if key in values:
                raise BridgeError("Legacy snapshot has duplicate binding metadata")
            values[key] = match[2]
    return values


def legacy_project_binding_source(repo, event):
    """Qualify a raw empty Project only at a legacy recovery boundary."""
    project = event.get("bridge_project_id", "")
    expected = {"bridge_project_id":project,"bridge_thread_id":event["thread_id"],
                "codex_session_id":event["codex_session_id"]}
    snapshot = resolve_repo_path(event["artifact"]["path"],repo)
    snapshot_meta = snapshot_binding_metadata(snapshot)
    if not project or any(snapshot_meta.get(k) != v for k,v in expected.items()):
        raise BridgeError("Legacy Project inference lacks matching immutable snapshot metadata")
    from project_store import BridgeProjectStore
    store = BridgeProjectStore(repo)
    bound = store.project_for_thread(event["thread_id"])
    if bound and bound != project:
        raise BridgeError("Legacy Project inference conflicts with the current unique store binding")
    session = bridge_root(repo)/"codex-sessions"/event["codex_session_id"]/"session.md"
    if session.exists():
        file_sha256(session)
        session_meta = parse_metadata(session)
        if any(session_meta.get(k) != v for k,v in expected.items()):
            raise BridgeError("Legacy Project inference conflicts with the current session binding")
        return "session-metadata/v1",session
    if bound == project:
        return "store-activity/v1",store.project_dir(project)/"activity.jsonl"
    raise BridgeError("Legacy Project inference has no existing session or unique store binding")


def verify_legacy_project_inference(repo, event, inference, *, candidate=False):
    required = {"schema_version","project","binding_kind","binding_source","binding"}
    if (not isinstance(inference,Mapping) or set(inference) != required
            or inference["schema_version"] != "legacy-project-inference/v1"
            or inference["project"] != event.get("bridge_project_id")
            or not inference["project"]):
        raise BridgeError("Legacy Project inference receipt is invalid")
    expected = {"bridge_project_id":event["bridge_project_id"],"bridge_thread_id":event["thread_id"],
                "codex_session_id":event["codex_session_id"]}
    snapshot = resolve_repo_path(event["artifact"]["path"],repo)
    snapshot_meta = snapshot_binding_metadata(snapshot)
    if any(snapshot_meta.get(k) != v for k,v in expected.items()):
        raise BridgeError("Legacy Project inference snapshot metadata disagrees with its event")
    kind = inference["binding_kind"]
    if kind == "session-metadata/v1":
        source = bridge_root(repo)/"codex-sessions"/event["codex_session_id"]/"session.md"
    elif kind == "store-activity/v1":
        source = bridge_root(repo)/"projects"/event["bridge_project_id"]/"activity.jsonl"
    else:
        raise BridgeError("Legacy Project inference binding kind is unsupported")
    for field in ("binding_source","binding"):
        value = inference[field]
        if not isinstance(value,Mapping) or set(value) != {"path","sha256"}:
            raise BridgeError("Legacy Project inference binding descriptor is invalid")
    if (inference["binding_source"]["path"] != repo_relative(source,repo)
            or inference["binding_source"]["sha256"] != inference["binding"]["sha256"]):
        raise BridgeError("Legacy Project inference source mapping mismatch")
    frozen = resolve_repo_path(inference["binding"]["path"],repo)
    if file_sha256(frozen) != inference["binding"]["sha256"]:
        raise BridgeError("Legacy Project inference frozen binding digest drift")
    if kind == "session-metadata/v1":
        frozen_meta = parse_metadata(frozen)
        if any(frozen_meta.get(k) != v for k,v in expected.items()):
            raise BridgeError("Legacy Project inference frozen session identity mismatch")
    else:
        activity = [json.loads(line) for line in frozen.read_text(encoding="utf-8").splitlines() if line.strip()]
        if (any(item.get("bridge_project_id") != event["bridge_project_id"] for item in activity)
                or not any(item.get("event_type") == "task-attached"
                    and item.get("data",{}).get("bridge_thread_id") == event["thread_id"] for item in activity)):
            raise BridgeError("Legacy Project inference frozen store identity mismatch")
    if candidate:
        actual_kind,actual_source = legacy_project_binding_source(repo,event)
        if actual_kind != kind or actual_source != source or file_sha256(source) != inference["binding_source"]["sha256"]:
            raise BridgeError("Legacy Project inference current source drift")


def verify_snapshot_inputs(repo, event, *, legacy_project_inference=None, candidate=False):
    proof = event.get("data", {}).get("input_proof")
    if not isinstance(proof, Mapping) or set(proof) != {"inputs", "source_notes", "request", "delivery_prompt"}:
        raise BridgeError("Snapshot lacks immutable input/source proof")
    values = {}
    for key, artifact in proof.items():
        if key == "request" and artifact is None:
            values[key] = None
            continue
        if not isinstance(artifact, Mapping) or set(artifact) != {"path", "sha256"}:
            raise BridgeError(f"Snapshot {key} proof descriptor is invalid")
        path = resolve_repo_path(artifact["path"], repo)
        if file_sha256(path) != artifact["sha256"]:
            raise BridgeError(f"Snapshot {key} proof digest drift")
        values[key] = path.read_bytes()
    inputs = json.loads(values["inputs"].decode("utf-8"))
    required = {"thread", "session", "project", "goal", "question", "summary", "raw_history", "history_source", "title"}
    if not isinstance(inputs, dict) or set(inputs) != required:
        raise BridgeError("Snapshot input receipt shape is invalid")
    digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    project_matches = inputs["project"] == event.get("bridge_project_id", "")
    if legacy_project_inference:
        if project_matches or inputs["project"] != "":
            raise BridgeError("Legacy Project inference only applies to an original empty Project input")
        verify_legacy_project_inference(repo,event,legacy_project_inference,candidate=candidate)
        project_matches = True
    if (digest != event["data"].get("inputs_sha256") or inputs["thread"] != event["thread_id"]
            or inputs["session"] != event["codex_session_id"] or not project_matches
            or inputs["summary"] != values["source_notes"].decode("utf-8").replace("\r\n", "\n").replace("\r","\n").strip()):
        raise BridgeError("Snapshot input/source identity or digest mismatch")
    request = json.loads(values["request"].decode("utf-8")) if values["request"] is not None else None
    if request is not None:
        if (request.get("repo") != str(repo) or request.get("bridge_thread_id") != event["thread_id"]
                or request.get("goal", "").strip() != inputs["goal"] or request.get("question", "").strip() != inputs["question"]):
            raise BridgeError("Snapshot frozen request disagrees with inputs")
    if request is None and values["delivery_prompt"].decode("utf-8").strip() != inputs["question"]:
        raise BridgeError("Snapshot delivery prompt disagrees with frozen question")
    if request is not None:
        from material_prompt import delivery_prompt
        if values["delivery_prompt"] != delivery_prompt(request).encode("utf-8"):
            raise BridgeError("Snapshot delivery prompt disagrees with the frozen request grammar")
    return {"inputs_sha256": digest, "source_notes": values["source_notes"], "request": request,
            "delivery_prompt": values["delivery_prompt"],
            "request_sha256": proof["request"]["sha256"] if proof["request"] else ""}


def _verify_thread_events(repo, thread_id, events, *, require_complete_rounds=False, candidate_event_id=""):
    """Shared verifier for stored ledgers and a proposed append-only repair."""
    if not events:
        raise BridgeError(f"Bridge thread has no events: {thread_id}")

    recovered = recovered_snapshot_targets(repo, events)
    used_recoveries = set()

    event_ids: set[str] = set()
    dedupe_keys: set[str] = set()
    expected_parent = ""
    round_has_snapshot = False
    pending_exchange = False
    complete_rounds = 0
    artifact_count = 0
    bundle_count = 0
    project_ids: set[str] = set()
    snapshot_identity = None
    snapshot_round_key = ""
    snapshot_proof = None
    snapshot_event = None
    snapshot_artifact = None
    exchange_identity = None
    exchange_turn = ""

    for index, event in enumerate(events, start=1):
        prefix = f"event {index}"
        event_type = str(event.get("event_type", ""))
        expected_actor = {
            "codex-snapshot": "codex",
            "gpt-exchange": "gpt-pro",
            "codex-verdict": "codex",
        }.get(event_type)
        if expected_actor and event.get("actor") != expected_actor:
            raise BridgeError(
                f"{prefix}: actor mismatch for {event_type}; expected {expected_actor}"
            )
        if event.get("schema_version") != SCHEMA_VERSION:
            raise BridgeError(f"{prefix}: unsupported schema version")
        if event.get("thread_id") != thread_id:
            raise BridgeError(f"{prefix}: thread id mismatch")
        if event.get("bridge_project_id"):
            project_id = validate_id(
                str(event["bridge_project_id"]), "bridge project id"
            )
            project_ids.add(project_id)
            if len(project_ids) > 1:
                raise BridgeError(f"{prefix}: conflicting Bridge Project identity")
        event_id = str(event.get("event_id", ""))
        if not event_id or event_id in event_ids:
            raise BridgeError(f"{prefix}: missing or duplicate event id")
        event_ids.add(event_id)
        if str(event.get("parent_event_id", "")) != expected_parent:
            raise BridgeError(
                f"{prefix}: parent chain mismatch; expected {expected_parent or '<root>'}"
            )
        expected_parent = event_id
        occurred_at = str(event.get("occurred_at", ""))
        try:
            parsed_time = dt.datetime.fromisoformat(occurred_at)
        except ValueError as exc:
            raise BridgeError(f"{prefix}: invalid occurred_at timestamp") from exc
        if parsed_time.tzinfo is None:
            raise BridgeError(f"{prefix}: occurred_at must include a timezone")
        dedupe_key = str(event.get("dedupe_key", ""))
        if dedupe_key:
            if dedupe_key in dedupe_keys:
                raise BridgeError(f"{prefix}: duplicate dedupe key")
            dedupe_keys.add(dedupe_key)

        artifact = event.get("artifact")
        if expected_actor and not artifact:
            raise BridgeError(f"{prefix}: {event_type} artifact is required")
        if expected_actor:
            validate_id(str(event.get("codex_session_id", "")), "Codex session id")
        if event_type in {"gpt-exchange", "codex-verdict"}:
            validate_id(str(event.get("gpt_pro_session_id", "")), "GPT Pro session id")
        if artifact:
            if not isinstance(artifact, Mapping):
                raise BridgeError(f"{prefix}: artifact must be an object")
            artifact_path = str(artifact.get("path", ""))
            expected_sha = str(artifact.get("sha256", ""))
            if not artifact.get("kind") or not artifact_path or not re.fullmatch(
                r"[0-9a-f]{64}", expected_sha
            ):
                raise BridgeError(f"{prefix}: artifact kind, path, and SHA-256 are required")
            expected_kind = {
                "codex-snapshot": "codex-notes",
                "gpt-exchange": "gpt-pro-turn",
                "codex-verdict": "codex-verdict",
            }.get(event_type)
            if expected_kind and artifact.get("kind") != expected_kind:
                raise BridgeError(
                    f"{prefix}: artifact kind mismatch for {event_type}; expected {expected_kind}"
                )
            try:
                resolved_artifact = resolve_repo_path(artifact_path, repo)
            except BridgeError as exc:
                raise BridgeError(f"{prefix}: artifact path is missing or unsafe: {artifact_path}") from exc
            if not resolved_artifact.is_file():
                raise BridgeError(f"{prefix}: artifact is not a file: {artifact_path}")
            if file_sha256(resolved_artifact) != expected_sha:
                raise BridgeError(f"{prefix}: artifact hash mismatch: {artifact_path}")
            artifact_count += 1

        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        for evidence_key in ("raw_answer", "capture_proof", "raw_prompt", "notes_reference", "snapshot_input_receipt"):
            if evidence_key not in data:
                continue
            evidence = data[evidence_key]
            if not isinstance(evidence, Mapping) or not evidence.get("path") or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256", ""))):
                raise BridgeError(f"{prefix}: invalid {evidence_key} artifact")
            if evidence_key == "capture_proof":
                from capture_provenance import private_proof
                raw_path = Path(evidence["path"])
                private_proof(raw_path if raw_path.is_absolute() else repo/raw_path)
            evidence_path = resolve_repo_path(evidence["path"], repo)
            if not evidence_path.is_file() or file_sha256(evidence_path) != evidence["sha256"]:
                raise BridgeError(f"{prefix}: {evidence_key} artifact digest drift")
            if evidence_key == "raw_answer" and evidence["sha256"] != data.get("answer_sha256"):
                raise BridgeError(f"{prefix}: raw answer and exchange digest disagree")
            if evidence_key == "raw_prompt" and evidence["sha256"] != data.get("prompt_sha256"):
                raise BridgeError(f"{prefix}: raw prompt and exchange digest disagree")
        if data.get("capture_route") == "browser-page-serialized" or data.get("answer_format") == "page-serialized-markdown":
            from capture_provenance import PAGE_FORMAT, PAGE_ROUTE, private_proof, validate_page_serialization
            from bridge_attempts import read_attempt
            if data.get("capture_route") != PAGE_ROUTE or data.get("answer_format") != PAGE_FORMAT:
                raise BridgeError(f"{prefix}: dishonest page serialization route/format")
            if not data.get("raw_answer") or not data.get("capture_proof") or not data.get("attempt_id"):
                raise BridgeError(f"{prefix}: page serialization lacks canonical raw/proof evidence")
            attempt = read_attempt(repo, thread_id, data["attempt_id"])
            proof_path = resolve_repo_path(data["capture_proof"]["path"], repo)
            private_proof(proof_path)
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            validate_page_serialization(proof, attempt=attempt, prompt=attempt["prompt"],
                answer=resolve_repo_path(data["raw_answer"]["path"], repo).read_bytes().decode("utf-8"))
        bundle_path = str(data.get("bundle", ""))
        bundle_sha = str(data.get("bundle_sha256", ""))
        if bundle_path:
            if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha):
                raise BridgeError(f"{prefix}: bundle SHA-256 is missing or invalid")
            try:
                bundle = resolve_repo_path(bundle_path, repo)
            except BridgeError as exc:
                raise BridgeError(f"{prefix}: bundle path is missing or unsafe: {bundle_path}") from exc
            if not bundle.is_file() or file_sha256(bundle) != bundle_sha:
                raise BridgeError(f"{prefix}: bundle hash mismatch: {bundle_path}")
            bundle_count += 1

        if event_type == "codex-snapshot":
            if data.get("recovery_for_exchange"):
                continue
            if pending_exchange:
                raise BridgeError(f"{prefix}: snapshot cannot precede the pending Codex verdict")
            if data.get("input_proof") or event_id == candidate_event_id:
                snapshot_proof = verify_snapshot_inputs(repo, event)
            else:
                snapshot_proof = None  # Read-only compatibility for qualified old history.
            identity = (event.get("codex_session_id", ""), event.get("bridge_project_id", ""))
            if round_has_snapshot and identity != snapshot_identity:
                raise BridgeError(f"{prefix}: round snapshot identity mismatch")
            round_key = data.get("round_key", "")
            if round_key:
                validate_id(round_key, "round key")
                if (dedupe_key != f"codex-snapshot-round:{round_key}" or not
                        re.fullmatch(r"[0-9a-f]{64}", str(data.get("inputs_sha256", "")))):
                    raise BridgeError(f"{prefix}: round snapshot key or input digest is invalid")
            if round_has_snapshot and (round_key or snapshot_round_key):
                raise BridgeError(f"{prefix}: another round snapshot is already available")
            snapshot_identity = identity
            snapshot_round_key = round_key
            snapshot_event = event
            snapshot_artifact = artifact
            round_has_snapshot = True
        elif event_type == "gpt-exchange":
            if event_id in recovered:
                if round_has_snapshot or pending_exchange:
                    raise BridgeError(f"{prefix}: snapshot recovery is redundant or overlaps an open round")
                round_has_snapshot = True
                snapshot_identity = (event.get("codex_session_id", ""), event.get("bridge_project_id", ""))
                used_recoveries.add(event_id)
            if not round_has_snapshot or pending_exchange:
                raise BridgeError(f"{prefix}: GPT exchange has no available Codex snapshot")
            if (event.get("codex_session_id", ""), event.get("bridge_project_id", "")) != snapshot_identity:
                raise BridgeError(f"{prefix}: exchange does not belong to the round snapshot")
            if data.get("snapshot_input_receipt"):
                receipt = json.loads(resolve_repo_path(data["snapshot_input_receipt"]["path"],repo).read_text(encoding="utf-8"))
                if (not snapshot_event or receipt.get("schema_version") != "snapshot-inputs/v1"
                        or receipt.get("snapshot_event_id") != snapshot_event["event_id"]
                        or receipt.get("snapshot_artifact") != snapshot_artifact
                        or receipt.get("round_key") != snapshot_round_key
                        or receipt.get("inputs_sha256") != snapshot_event["data"].get("inputs_sha256")):
                    raise BridgeError(f"{prefix}: prospective snapshot receipt identity mismatch")
                original_proof = snapshot_event["data"].get("input_proof")
                if original_proof and receipt.get("input_proof") != original_proof:
                    raise BridgeError(f"{prefix}: prospective receipt cannot replace immutable snapshot proof")
                if original_proof and receipt.get("project_inference"):
                    raise BridgeError(f"{prefix}: new snapshot cannot use legacy Project inference")
                if not original_proof:
                    handoff = receipt.get("job_handoff")
                    if not isinstance(handoff,dict) or set(handoff) != {"path","sha256"}:
                        raise BridgeError(f"{prefix}: legacy prospective proof lacks frozen job/handoff anchor")
                    handoff_path = resolve_repo_path(handoff["path"],repo)
                    if file_sha256(handoff_path) != handoff["sha256"]:
                        raise BridgeError(f"{prefix}: legacy prospective handoff digest drift")
                    expected_job = hashlib.sha256((str(repo)+"\n"+handoff["sha256"]).encode()).hexdigest()
                    frozen_handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
                    request_artifact = receipt.get("input_proof",{}).get("request") or {}
                    if (snapshot_round_key != expected_job or frozen_handoff.get("repo") != str(repo)
                            or frozen_handoff.get("bridge_thread_id") != thread_id
                            or frozen_handoff.get("request_sha256") != request_artifact.get("sha256")):
                        raise BridgeError(f"{prefix}: legacy prospective job/request identity mismatch")
                snapshot_proof = verify_snapshot_inputs(repo,{**snapshot_event,
                    "data":{**snapshot_event["data"],"input_proof":receipt.get("input_proof")}},
                    legacy_project_inference=receipt.get("project_inference"),candidate=event_id == candidate_event_id)
            if snapshot_round_key and (snapshot_proof or event_id == candidate_event_id) and data.get("round_key") != snapshot_round_key:
                raise BridgeError(f"{prefix}: exchange round key does not match its snapshot")
            if event_id == candidate_event_id and snapshot_proof is None:
                raise BridgeError(f"{prefix}: round snapshot lacks immutable input proof")
            if snapshot_proof:
                if (data.get("snapshot_inputs_sha256") != snapshot_proof["inputs_sha256"]
                        or data.get("snapshot_request_sha256", "") != snapshot_proof["request_sha256"]):
                    raise BridgeError(f"{prefix}: exchange snapshot input/request digest mismatch")
                if not data.get("raw_prompt") or not data.get("notes_reference"):
                    raise BridgeError(f"{prefix}: exchange lacks actual prompt/notes reference proof")
                prompt_bytes = resolve_repo_path(data["raw_prompt"]["path"], repo).read_bytes()
                frozen_prompt = snapshot_proof["delivery_prompt"]
                if (prompt_bytes != frozen_prompt if snapshot_proof["request"] is not None
                        else prompt_bytes not in (frozen_prompt,frozen_prompt+b"\n") and frozen_prompt != prompt_bytes+b"\n"):
                    raise BridgeError(f"{prefix}: actual prompt differs from frozen snapshot delivery")
                notes_bytes = resolve_repo_path(data["notes_reference"]["path"], repo).read_bytes()
                snapshot_bytes = resolve_repo_path(snapshot_artifact["path"], repo).read_bytes()
                if notes_bytes not in (snapshot_bytes, snapshot_proof["source_notes"]):
                    raise BridgeError(f"{prefix}: actual notes reference differs from the round snapshot")
                if bundle_path:
                    try:
                        with zipfile.ZipFile(bundle) as archive:
                            member = "context/codex-session-notes.md"
                            if archive.namelist().count(member) != 1 or archive.read(member) != snapshot_proof["source_notes"]:
                                raise BridgeError(f"{prefix}: sent bundle notes differ from frozen snapshot source")
                    except zipfile.BadZipFile as exc:
                        raise BridgeError(f"{prefix}: sent bundle is not a valid ZIP") from exc
            exchange_identity = (event.get("codex_session_id", ""), event.get("gpt_pro_session_id", ""),
                                 event.get("bridge_project_id", ""))
            exchange_turn = artifact["path"]
            pending_exchange = True
        elif event_type == "codex-verdict":
            if not pending_exchange:
                raise BridgeError(f"{prefix}: Codex verdict has no pending GPT exchange")
            if ((event.get("codex_session_id", ""), event.get("gpt_pro_session_id", ""),
                 event.get("bridge_project_id", "")) != exchange_identity or data.get("turn") != exchange_turn):
                raise BridgeError(f"{prefix}: verdict does not belong to the pending GPT exchange")
            pending_exchange = False
            round_has_snapshot = False
            snapshot_identity = None
            snapshot_round_key = ""
            snapshot_proof = None
            snapshot_event = None
            snapshot_artifact = None
            complete_rounds += 1
        elif not event_type.startswith("legacy-"):
            raise BridgeError(f"{prefix}: unsupported event type {event_type!r}")

    if used_recoveries != recovered:
        raise BridgeError("Snapshot recovery was not consumed by its exact exchange")
    if require_complete_rounds and (pending_exchange or round_has_snapshot):
        raise BridgeError("Bridge thread ends with an incomplete round")
    return {
        "valid": True,
        "thread_id": thread_id,
        "bridge_project_id": next(iter(project_ids)) if project_ids else "",
        "event_count": len(events),
        "complete_rounds": complete_rounds,
        "artifact_count": artifact_count,
        "bundle_count": bundle_count,
        "round_complete": not pending_exchange and not round_has_snapshot,
        "recovered_snapshot_count": len(used_recoveries),
    }


def write_session_index(sessions_dir: Path, *, kind: str) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(sessions_dir / ".index.lock"):
        rows = [parse_metadata(path) for path in sessions_dir.glob("*/session.md")]
        rows = [row for row in rows if row]
        rows.sort(key=lambda row: row.get("last_used_at", ""), reverse=True)
        escape = lambda value: (str(value or "-")).replace("|", "\\|").replace("\n", " ")
        if kind == "codex":
            lines = [
                "# Codex Bridge Sessions",
                "",
                "| Codex Session | Bridge Thread | Project | Title | Source | Last Used | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            for row in rows:
                notes = row.get("notes_path", "")
                link = f"[notes]({notes})" if notes else "-"
                lines.append(
                    f"| `{escape(row.get('codex_session_id'))}` | `{escape(row.get('bridge_thread_id'))}` | "
                    f"`{escape(row.get('bridge_project_id'))}` | "
                    f"{escape(row.get('title'))} | {escape(row.get('history_source'))} | "
                    f"{escape(row.get('last_used_at'))} | {link} |"
                )
        elif kind == "gpt-pro":
            lines = [
                "# GPT Pro Bridge Sessions",
                "",
                "| GPT Pro Session | Bridge Thread | Project | Remote Project | Title | Purpose | Last Used | Latest Turn | URL |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for row in rows:
                url = row.get("web_conversation_url", "")
                link = f"[open]({url})" if url.startswith(("https://", "http://")) else "-"
                lines.append(
                    f"| `{escape(row.get('gpt_pro_session_id'))}` | `{escape(row.get('bridge_thread_id'))}` | "
                    f"`{escape(row.get('bridge_project_id'))}` | "
                    f"`{escape(row.get('remote_project_id'))}` | "
                    f"{escape(row.get('web_title'))} | {escape(row.get('purpose'))} | "
                    f"{escape(row.get('last_used_at'))} | {escape(row.get('latest_turn'))} | {link} |"
                )
        else:
            raise BridgeError(f"Unknown session index kind: {kind}")
        atomic_write_text(sessions_dir / "index.md", "\n".join(lines) + "\n")


def _short(value: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[: limit - 3].rstrip() + "..." if len(text) > limit else text or "-"


def record_codex_verdict(
    repo: Path,
    *,
    thread_id: str,
    gpt_pro_session_id: str,
    codex_session_id: str,
    bridge_project_id: str = "",
    turn_path: Path,
    summary: str,
    verification: str,
    decision_trail: str = "",
    changes: str = "",
    tests: str = "",
    next_question: str = "",
    occurred_at: str = "",
) -> Path:
    """Write an immutable Codex verdict artifact and append its event."""
    repo = repo.resolve()
    thread_id = validate_id(thread_id, "bridge thread id")
    gpt_pro_session_id = validate_id(gpt_pro_session_id, "GPT Pro session id")
    codex_session_id = validate_id(codex_session_id, "Codex session id")
    turn_path = turn_path.resolve()
    if not is_within(turn_path, repo) or not turn_path.is_file():
        raise BridgeError("Verdict turn file must exist under the repository root")
    if not verification.strip():
        raise BridgeError("Codex verification is required before recording a verdict")
    session_meta = parse_metadata(
        bridge_root(repo) / "gpt-pro-sessions" / gpt_pro_session_id / "session.md"
    )
    bridge_project_id = (
        bridge_project_id or session_meta.get("bridge_project_id", "")
    )
    if bridge_project_id:
        bridge_project_id = validate_id(bridge_project_id, "bridge project id")
    if session_meta.get("bridge_project_id") not in (
        None,
        "",
        bridge_project_id,
    ):
        raise BridgeError(
            f"GPT Pro session belongs to {session_meta['bridge_project_id']}, "
            f"not {bridge_project_id}"
        )
    if session_meta.get("bridge_thread_id") not in (None, "", thread_id):
        raise BridgeError(
            f"GPT Pro session {gpt_pro_session_id} is bound to "
            f"{session_meta['bridge_thread_id']}, not {thread_id}"
        )
    if session_meta.get("codex_session_id") not in (None, "", codex_session_id):
        raise BridgeError(
            f"GPT Pro session {gpt_pro_session_id} is linked to Codex session "
            f"{session_meta['codex_session_id']}, not {codex_session_id}"
        )
    expected_session_dir = (
        bridge_root(repo) / "gpt-pro-sessions" / gpt_pro_session_id
    ).resolve()
    turn_rel = repo_relative(turn_path, repo)
    if turn_path.parent != expected_session_dir:
        raise BridgeError("Verdict turn must stay inside the bound GPT Pro session")
    matching_exchange = next(
        (
            event
            for event in load_events(bridge_root(repo), thread_id)
            if event.get("event_type") == "gpt-exchange"
            and isinstance(event.get("artifact"), Mapping)
            and event["artifact"].get("path") == turn_rel
            and event.get("gpt_pro_session_id") == gpt_pro_session_id
            and event.get("codex_session_id") == codex_session_id
        ),
        None,
    )
    if matching_exchange is None:
        raise BridgeError("Verdict turn is not a captured exchange in the bound GPT Pro session")
    saved_at = occurred_at or now_iso()
    payload_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "turn": turn_rel,
                "summary": summary,
                "verification": verification,
                "decision_trail": decision_trail,
                "changes": changes,
                "tests": tests,
                "next_question": next_question,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    verdict_path = (
        turn_path.parent
        / "verdicts"
        / f"{turn_path.stem}-verdict-{payload_fingerprint[:12]}.md"
    )
    content = "\n".join(
        [
            f"# Codex Verdict for {turn_path.stem}",
            "",
            "## Metadata",
            f"- Bridge Thread ID: `{thread_id}`",
            f"- Bridge Project ID: `{bridge_project_id or '-'}`",
            f"- Codex Session ID: `{codex_session_id}`",
            f"- GPT Pro Session ID: `{gpt_pro_session_id}`",
            f"- GPT Pro Turn: `{repo_relative(turn_path, repo)}`",
            f"- Recorded at: {saved_at}",
            "",
            "## Codex Summary",
            "",
            summary.strip() or "_Not recorded._",
            "",
            "## Codex Verification",
            "",
            verification.strip(),
            "",
            "## Decision Trail",
            "",
            decision_trail.strip() or "_Not recorded._",
            "",
            "## Implemented or Proposed Changes",
            "",
            changes.strip() or "_None recorded._",
            "",
            "## Tests and Validation",
            "",
            tests.strip() or "_None recorded._",
            "",
            "## Next GPT Pro Question",
            "",
            next_question.strip() or "_No next question recorded._",
            "",
        ]
    )
    if not verdict_path.exists():
        atomic_write_text(verdict_path, content)
    verdict_rel = repo_relative(verdict_path, repo)
    append_event(
        repo,
        thread_id=thread_id,
        event_type="codex-verdict",
        actor="codex",
        thread_title=session_meta.get("purpose", "") or session_meta.get("web_title", "") or thread_id,
        bridge_project_id=bridge_project_id,
        codex_session_id=codex_session_id,
        gpt_pro_session_id=gpt_pro_session_id,
        artifact={"kind": "codex-verdict", "path": verdict_rel, "sha256": file_sha256(verdict_path)},
        data={
            "turn": repo_relative(turn_path, repo),
            "summary": _short(summary),
            "verification": _short(verification),
            "next_question": _short(next_question),
        },
        dedupe_key=f"codex-verdict:{turn_rel}:{payload_fingerprint}",
        occurred_at=saved_at,
    )
    return verdict_path
