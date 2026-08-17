#!/usr/bin/env python3
"""Audit or gracefully stop idle chrome-devtools-mcp trees for one app server."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass


WRAPPER_RE = re.compile(
    r"^(?:npm exec|[^ ]*/npx|npx) chrome-devtools-mcp@1\.7\.0(?:\s|$)"
)
SERVER_RE = re.compile(r"^(?:[^ ]*/)?chrome-devtools-mcp(?:\s|$)")


class ReaperError(ValueError):
    """Raised when process ownership cannot be proved safely."""


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    command: str


def parse_process_table(value: str) -> list[Process]:
    processes: list[Process] = []
    for line in value.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if match:
            processes.append(
                Process(int(match.group(1)), int(match.group(2)), match.group(3).strip())
            )
    return processes


def process_table() -> list[Process]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_process_table(result.stdout)


def select_tree(processes: list[Process], parent_pid: int) -> tuple[list[Process], list[Process]]:
    wrappers = [
        process
        for process in processes
        if process.ppid == parent_pid and WRAPPER_RE.match(process.command)
    ]
    wrapper_ids = {process.pid for process in wrappers}
    servers = [
        process
        for process in processes
        if process.ppid in wrapper_ids and SERVER_RE.match(process.command)
    ]
    unknown_children = [
        process
        for process in processes
        if process.ppid in wrapper_ids and process not in servers
    ]
    if unknown_children:
        raise ReaperError(
            "Refusing to manage wrappers with unexpected children: "
            + ", ".join(str(process.pid) for process in unknown_children)
        )
    return wrappers, servers


def established_tcp(pids: list[int]) -> list[int]:
    active: list[int] = []
    for pid in pids:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP"],
            check=False,
            capture_output=True,
            text=True,
        )
        if "ESTABLISHED" in result.stdout:
            active.append(pid)
    return active


def still_alive(pids: list[int]) -> list[int]:
    alive: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise ReaperError(f"Cannot inspect PID {pid}") from exc
        alive.append(pid)
    return alive


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or gracefully stop exact idle chrome-devtools-mcp 1.7.0 trees."
    )
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--keep-active",
        action="store_true",
        help="Preserve one ESTABLISHED MCP tree and terminate only the idle trees.",
    )
    args = parser.parse_args()
    try:
        processes = process_table()
        parent = next((p for p in processes if p.pid == args.parent_pid), None)
        if not parent or "codex" not in parent.command.lower():
            raise ReaperError("--parent-pid is not a live Codex process")
        wrappers, servers = select_tree(processes, args.parent_pid)
        all_pids = [process.pid for process in servers + wrappers]
        active = established_tcp(all_pids)
        active_ids = set(active)
        active_wrapper_ids = {
            wrapper.pid
            for wrapper in wrappers
            if wrapper.pid in active_ids
            or any(server.ppid == wrapper.pid and server.pid in active_ids for server in servers)
        }
        if len(active_wrapper_ids) > 1:
            raise ReaperError(
                "Refusing cleanup with more than one ESTABLISHED MCP tree: "
                + ", ".join(map(str, sorted(active_wrapper_ids)))
            )
        if args.apply and active_wrapper_ids and not args.keep_active:
            raise ReaperError(
                "Refusing cleanup while an MCP tree is ESTABLISHED; use --keep-active "
                "to preserve that one tree and reap only idle trees"
            )
        preserved = {
            process.pid
            for process in wrappers + servers
            if process.pid in active_wrapper_ids or process.ppid in active_wrapper_ids
        }
        targets = [pid for pid in all_pids if pid not in preserved]
        result = {
            "parent_pid": args.parent_pid,
            "wrapper_count": len(wrappers),
            "server_count": len(servers),
            "target_pids": targets,
            "established_tcp_pids": active,
            "preserved_pids": sorted(preserved),
            "applied": args.apply,
        }
        if args.apply:
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 3.0
            remaining = still_alive(targets)
            while remaining and time.monotonic() < deadline:
                time.sleep(0.1)
                remaining = still_alive(targets)
            result["terminated_count"] = len(targets) - len(remaining)
            result["remaining_pids"] = remaining
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ReaperError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
