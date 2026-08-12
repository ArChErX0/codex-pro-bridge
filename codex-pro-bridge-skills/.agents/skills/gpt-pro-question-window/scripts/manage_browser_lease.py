#!/usr/bin/env python3
"""Acquire, release, or inspect the repository-local browser lease.

The lease serializes browser-mutating windows (attach/upload/preflight/send and
any later full-answer browser fallback) so that several parallel Review Probes
never drive the one signed-in browser at once. Release it while ChatGPT
generates and reacquire it only if browser fallback is required.
It is an advisory, repository-local lease for a host-local Chrome profile. It
does not coordinate separate worktrees/repositories and is NOT a distributed
lock. Use one declared dispatcher outside this repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[2] / ".shared"
sys.path.insert(0, str(SHARED_DIR))

from bridge_store import (  # noqa: E402
    DEFAULT_BROWSER_LEASE_TTL_SECONDS,
    BridgeError,
    acquire_browser_lease,
    read_browser_lease,
    release_browser_lease,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the repository-local browser lease.")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    acquire = sub.add_parser("acquire", help="Take the lease or fail if one is live.")
    acquire.add_argument("--holder", required=True, help="Identifier for the acquiring worker/agent.")
    acquire.add_argument("--bridge-thread-id", default="", help="Probe thread this lease is for.")
    acquire.add_argument("--expected-conversation-id", default="")
    acquire.add_argument("--expected-remote-project-id", default="")
    acquire.add_argument(
        "--ttl-seconds", type=int, default=DEFAULT_BROWSER_LEASE_TTL_SECONDS
    )

    release = sub.add_parser("release", help="Release the lease held by --token.")
    release.add_argument("--token", required=True)

    sub.add_parser("status", help="Print the current lease record (or {} if free).")

    args = parser.parse_args()
    try:
        repo = Path(args.repo).resolve()
        if not repo.is_dir():
            raise BridgeError(f"Repository root is not a directory: {repo}")
        if args.command == "acquire":
            lease = acquire_browser_lease(
                repo,
                holder=args.holder,
                thread_id=args.bridge_thread_id,
                expected_conversation_id=args.expected_conversation_id,
                expected_remote_project_id=args.expected_remote_project_id,
                ttl_seconds=args.ttl_seconds,
            )
            print(json.dumps(lease, ensure_ascii=False, sort_keys=True))
        elif args.command == "release":
            cleared = release_browser_lease(repo, token=args.token)
            print(json.dumps({"released": cleared}, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(read_browser_lease(repo), ensure_ascii=False, sort_keys=True))
        return 0
    except (BridgeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
