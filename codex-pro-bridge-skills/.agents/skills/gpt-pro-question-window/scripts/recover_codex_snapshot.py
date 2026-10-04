#!/usr/bin/env python3
"""Recover one missing snapshot from the hash-verified sent ZIP, append-only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".shared"))
from bridge_store import (BridgeError, SCHEMA_VERSION, _verify_thread_events,
                          append_event, atomic_write_text, bridge_root, file_lock,
                          file_sha256, load_events, now_iso, repo_relative,
                          resolve_repo_path, validate_id, verify_thread_integrity)


def recover(repo: Path, thread: str, exchange_id: str, *, apply=False, expected_sha=""):
    repo = repo.resolve()
    thread = validate_id(thread, "bridge thread id")
    root = bridge_root(repo)
    ledger = root / "threads" / f"{thread}.jsonl"
    with file_lock(root / "threads" / f".{thread}.snapshot.lock"):
        before_sha = file_sha256(ledger)
        if apply and (not expected_sha or before_sha != expected_sha):
            raise BridgeError("Repair requires the reviewed current ledger SHA-256")
        events = load_events(root, thread)
        prior = [e for e in events if e.get("data", {}).get("recovery_for_exchange") == exchange_id]
        if prior:
            return {"status": "already-recovered", **verify_thread_integrity(repo, thread, require_complete_rounds=True)}
        matches = [e for e in events if e["event_id"] == exchange_id and e["event_type"] == "gpt-exchange"]
        if len(matches) != 1:
            raise BridgeError("Expected one exact GPT exchange")
        target = matches[0]
        source = target.get("data") or {}
        bundle = resolve_repo_path(source.get("bundle", ""), repo)
        if not source.get("bundle_sha256") or file_sha256(bundle) != source["bundle_sha256"]:
            raise BridgeError("Sent bundle digest does not match exchange")
        member = "context/codex-session-notes.md"
        with zipfile.ZipFile(bundle) as archive:
            if archive.namelist().count(member) != 1:
                raise BridgeError("Sent bundle must contain one exact Codex notes member")
            notes = archive.read(member)
        notes.decode("utf-8")
        # Stable artifact path allows recovery from interruption after extraction.
        key = hashlib.sha256(exchange_id.encode()).hexdigest()
        path = root / "snapshot-recoveries" / f"{key}.md"
        report = {"status": "planned", "thread_id": thread, "exchange_id": exchange_id,
                  "ledger_sha256": before_sha, "source_bundle_sha256": source["bundle_sha256"],
                  "snapshot_sha256": hashlib.sha256(notes).hexdigest(),
                  "snapshot_path": repo_relative(path, repo), "source_member": member}
        if not apply:
            return report
        # Keep the exact original ledger for independent audit/recovery. Do not
        # replace existing history or any answer/verdict, or backdate the receipt.
        backup = root / "snapshot-recoveries" / f"{thread}-{before_sha}.ledger.jsonl"
        original = ledger.read_bytes()
        if backup.exists() and backup.read_bytes() != original:
            raise BridgeError("Recovery backup drift")
        if not backup.exists():
            atomic_write_text(backup, original.decode("utf-8"))
        if path.exists() and path.read_bytes() != notes:
            raise BridgeError("Recovery snapshot drift")
        if not path.exists():
            atomic_write_text(path, notes.decode("utf-8"))
        timestamp = now_iso()
        data = {"recovery_for_exchange": exchange_id, "source_member": member,
                "source_bundle_sha256": source["bundle_sha256"],
                "history_source": "recovered-from-sent-bundle",
                "original_ledger_sha256": before_sha, "original_ledger_backup": repo_relative(backup, repo),
                "summary": "Late recovery of omitted round snapshot; original events unchanged."}
        artifact = {"kind": "codex-notes", "path": repo_relative(path, repo), "sha256": file_sha256(path)}
        kwargs = dict(thread_id=thread, event_type="codex-snapshot", actor="codex",
                      codex_session_id=target.get("codex_session_id", ""),
                      bridge_project_id=target.get("bridge_project_id", ""),
                      artifact=artifact, data=data, dedupe_key=f"snapshot-recovery:{exchange_id}",
                      occurred_at=timestamp)
        candidate = dict(kwargs, schema_version=SCHEMA_VERSION, event_id=f"recovery-preview-{key}",
                         parent_event_id=events[-1]["event_id"])
        # Run the unchanged identity/hash/order checks plus strict recovery proof
        # before the sole ledger append. Other invalid history is not repaired.
        _verify_thread_events(repo, thread, [*events, candidate], require_complete_rounds=True)
        event = append_event(repo, **kwargs, expected_parent_event_id=events[-1]["event_id"])
        return {**report, "status": "recovered", "recovery_event_id": event["event_id"],
                "verification": verify_thread_integrity(repo, thread, require_complete_rounds=True)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--bridge-thread-id", required=True)
    parser.add_argument("--exchange-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-ledger-sha256", default="")
    args = parser.parse_args()
    try:
        print(json.dumps(recover(args.repo, args.bridge_thread_id, args.exchange_id,
                                 apply=args.apply, expected_sha=args.expected_ledger_sha256), ensure_ascii=False, indent=2))
    except (BridgeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
