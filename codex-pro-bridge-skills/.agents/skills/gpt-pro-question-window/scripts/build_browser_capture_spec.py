#!/usr/bin/env python3
"""Build a safely escaped evaluate_script call for one pinned assistant UID."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the exact DevTools evaluate_script payload for browser fallback."
    )
    parser.add_argument("--assistant-uid", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--remote-turn-id", required=True)
    parser.add_argument("--prompt-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        uid = args.assistant_uid.strip()
        if not UID_RE.fullmatch(uid):
            raise ValueError("--assistant-uid has an invalid format")
        conversation_id = args.conversation_id.strip()
        remote_turn_id = args.remote_turn_id.strip()
        if not conversation_id or not remote_turn_id:
            raise ValueError("Conversation and remote turn IDs are required")
        prompt_sha = args.prompt_sha256.strip().lower()
        if not SHA256_RE.fullmatch(prompt_sha):
            raise ValueError("--prompt-sha256 must be a lowercase SHA-256")
        output = Path(args.output).expanduser().resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        try:
            output.relative_to(temp_root)
        except ValueError as exc:
            raise ValueError(f"--output must stay below the OS temp root: {temp_root}") from exc
        if output.suffix.lower() != ".json":
            raise ValueError("--output must end in .json")
        literals = {
            "conversation_id": conversation_id,
            "remote_turn_id": remote_turn_id,
            "prompt_sha256": prompt_sha,
        }
        function = (
            "(el) => ({schema_version: 1, "
            f"conversation_id: {json.dumps(conversation_id)}, "
            f"remote_turn_id: {json.dumps(remote_turn_id)}, "
            f"prompt_sha256: {json.dumps(prompt_sha)}, "
            "html: el.innerHTML})"
        )
        print(
            json.dumps(
                {
                    "tool": "mcp__chrome_devtools__evaluate_script",
                    "args": {"function": function, "args": [uid], "filePath": str(output)},
                    "identity": literals,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
