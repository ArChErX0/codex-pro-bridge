#!/usr/bin/env python3
"""Persist ChatGPT's Copy reply Markdown from the Windows clipboard."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class CaptureError(RuntimeError):
    pass


FENCE = re.compile(r"^\s*```")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[([^\]]*)\]:\s*\S", re.MULTILINE)
INLINE_LINK = re.compile(r"(?<!!)\[[^\]]+\]\([^\n)]+\)")
REFERENCE_USAGE = re.compile(r"(?<!!)\[([^\]]*)\]\[([^\]]*)\]")


def link_count(text: str) -> int:
    """Count rendered links, ignoring fenced code blocks.

    ChatGPT's Copy reply emits inline links and full reference links
    (``[text][id]``) whose ``[id]:`` definitions are appended after the
    prose. Only a reference with a definition is a link, each usage counts
    once, and the definition lines themselves are not usages.
    """
    visible = []
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            visible.append("")
        else:
            visible.append("" if fenced else line)
    body = "\n".join(visible)
    defined = {label.strip().casefold() for label in REFERENCE_DEFINITION.findall(body)}
    references = sum(
        1
        for label, identifier in REFERENCE_USAGE.findall(body)
        if (identifier.strip() or label.strip()).casefold() in defined
    )
    return len(INLINE_LINK.findall(body)) + references


def markdown_metrics(text: str) -> dict[str, int | str]:
    lines = text.splitlines()
    visible = []
    fence = None
    code_blocks = 0
    for line in lines:
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            code_blocks += 1
            continue
        visible.append(line)
    body = "\n".join(visible)
    table_count = 0
    for line in visible:
        if "|" not in line:
            continue
        cells = line.strip().strip("|").split("|")
        if len(cells) >= 2 and all(
            re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells
        ):
            table_count += 1
    display_math = len(re.findall(r"\\\[[\s\S]*?\\\]", body)) + len(re.findall(r"\$\$[\s\S]*?\$\$", body))
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "characters": len(text),
        "lines": len(lines),
        "headings": sum(bool(re.match(r"^#{1,6}\s+", line)) for line in visible),
        "display_math": display_math,
        "inline_math": len(re.findall(r"\\\([\s\S]*?\\\)", body)),
        "tables": table_count,
        "code_blocks": code_blocks,
        "links": link_count(body),
    }


def read_windows_clipboard() -> str:
    executable = shutil.which("powershell.exe")
    if not executable:
        raise CaptureError("powershell.exe is unavailable; the Windows clipboard cannot be read")
    command = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "$value=Get-Clipboard -Raw; "
        "if ($null -eq $value) { exit 3 }; "
        "[Console]::Out.Write($value)"
    )
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        # With terminating PowerShell errors, exit 3 without diagnostics is
        # exclusively the explicit null-text branch, not a failed clipboard API.
        if completed.returncode == 3 and not detail:
            raise CaptureError("Windows clipboard is empty")
        raise CaptureError(
            f"Windows clipboard read failed with exit {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    text = completed.stdout.decode("utf-8-sig").replace("\r\n", "\n")
    if not text.strip():
        raise CaptureError("Windows clipboard is empty")
    return text.rstrip() + "\n"


def atomic_write(path: Path, text: str, *, replace: bool) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise CaptureError(f"Refusing to overwrite existing capture: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Save Markdown produced by ChatGPT's visible Copy reply control."
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    for name in (
        "headings",
        "display-math",
        "inline-math",
        "tables",
        "code-blocks",
        "links",
    ):
        parser.add_argument(f"--expected-{name}", type=int)
    args = parser.parse_args()

    try:
        text = read_windows_clipboard()
        metrics = markdown_metrics(text)
        expected = {
            "headings": args.expected_headings,
            "display_math": args.expected_display_math,
            "inline_math": args.expected_inline_math,
            "tables": args.expected_tables,
            "code_blocks": args.expected_code_blocks,
            "links": args.expected_links,
        }
        mismatches = {
            name: {"expected": value, "observed": metrics[name]}
            for name, value in expected.items()
            if value is not None and metrics[name] != value
        }
        if mismatches:
            raise CaptureError(
                "Copied Markdown does not match the target reply structure: "
                + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
            )
        atomic_write(Path(args.output), text, replace=args.replace)
        print(
            json.dumps(
                {"path": str(Path(args.output).expanduser().resolve()), **metrics},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (CaptureError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
