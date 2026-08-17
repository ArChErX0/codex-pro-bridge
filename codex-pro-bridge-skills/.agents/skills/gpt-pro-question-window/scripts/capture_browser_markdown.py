#!/usr/bin/env python3
"""Convert a pinned ChatGPT assistant turn's rendered HTML to stable Markdown.

The DevTools caller must identify the exact conversation and remote turn before
exporting ``element.innerHTML``.  This script deliberately rejects ``innerText``
captures because they destroy headings, tables, code fences, links, and citation
anchors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}


class CaptureError(ValueError):
    """Raised when browser fallback identity or content is not trustworthy."""


class Node:
    def __init__(self, tag: str, attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []


class TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _text(node: Node | str) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node.children)


def _inline(node: Node | str) -> str:
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node)
    body = "".join(_inline(child) for child in node.children)
    if node.tag in {"strong", "b"}:
        return f"**{body.strip()}**" if body.strip() else ""
    if node.tag in {"em", "i"}:
        return f"*{body.strip()}*" if body.strip() else ""
    if node.tag == "del":
        return f"~~{body.strip()}~~" if body.strip() else ""
    if node.tag == "code":
        raw = _text(node).strip()
        fence = "`" * max(1, max((len(run) for run in re.findall(r"`+", raw)), default=0) + 1)
        return f"{fence}{raw}{fence}"
    if node.tag == "a":
        label = body.strip() or node.attrs.get("href", "")
        href = node.attrs.get("href", "").strip()
        return f"[{label}]({href})" if href else label
    if node.tag == "br":
        return "\n"
    if node.tag == "img":
        alt = node.attrs.get("alt", "").strip()
        src = node.attrs.get("src", "").strip()
        return f"![{alt}]({src})" if src else alt
    return body


def _table(node: Node) -> str:
    rows: list[list[str]] = []
    header_flags: list[bool] = []

    def visit(candidate: Node | str) -> None:
        if isinstance(candidate, str):
            return
        if candidate.tag == "tr":
            cells = [child for child in candidate.children if isinstance(child, Node) and child.tag in {"th", "td"}]
            if cells:
                rows.append([_inline(cell).strip().replace("|", "\\|") for cell in cells])
                header_flags.append(any(cell.tag == "th" for cell in cells))
            return
        for child in candidate.children:
            visit(child)

    visit(node)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    body = padded[1:]
    if not header_flags[0]:
        header = [f"Column {index + 1}" for index in range(width)]
        body = padded
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines) + "\n\n"


def _list(node: Node, *, ordered: bool, depth: int = 0) -> str:
    lines: list[str] = []
    counter = 1
    for child in node.children:
        if not isinstance(child, Node) or child.tag != "li":
            continue
        inline_parts: list[str] = []
        nested: list[Node] = []
        for item in child.children:
            if isinstance(item, Node) and item.tag in {"ul", "ol"}:
                nested.append(item)
            else:
                inline_parts.append(_render(item).strip())
        marker = f"{counter}." if ordered else "-"
        content = " ".join(part for part in inline_parts if part).strip()
        lines.append(f"{'  ' * depth}{marker} {content}".rstrip())
        for nested_list in nested:
            lines.append(
                _list(nested_list, ordered=nested_list.tag == "ol", depth=depth + 1).rstrip()
            )
        counter += 1
    return "\n".join(lines) + "\n\n"


def _render(node: Node | str) -> str:
    if isinstance(node, str):
        return node
    tag = node.tag
    if tag in {"script", "style", "button", "svg"}:
        return ""
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"{'#' * int(tag[1])} {_inline(node).strip()}\n\n"
    if tag == "p":
        return _inline(node).strip() + "\n\n"
    if tag == "pre":
        code_node = next(
            (child for child in node.children if isinstance(child, Node) and child.tag == "code"),
            None,
        )
        raw = _text(code_node or node).strip("\n")
        language = ""
        if code_node:
            classes = code_node.attrs.get("class", "").split()
            language = next((value[9:] for value in classes if value.startswith("language-")), "")
        fence_len = max(3, max((len(run) for run in re.findall(r"`+", raw)), default=0) + 1)
        fence = "`" * fence_len
        return f"{fence}{language}\n{raw}\n{fence}\n\n"
    if tag == "table":
        return _table(node)
    if tag in {"ul", "ol"}:
        return _list(node, ordered=tag == "ol")
    if tag == "blockquote":
        body = "".join(_render(child) for child in node.children).strip()
        return "\n".join(f"> {line}" if line else ">" for line in body.splitlines()) + "\n\n"
    if tag == "hr":
        return "---\n\n"
    if tag in {"strong", "b", "em", "i", "del", "code", "a", "br", "img"}:
        return _inline(node)
    return "".join(_render(child) for child in node.children)


def html_to_markdown(value: str) -> str:
    parser = TreeParser()
    parser.feed(value)
    parser.close()
    rendered = _render(parser.root).replace("\u00a0", " ")
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{4,}", "\n\n\n", rendered)
    result = rendered.strip() + "\n"
    if not result.strip():
        raise CaptureError("Rendered assistant HTML produced an empty Markdown answer")
    return result


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.read_text(encoding="utf-8") == value:
            return
        raise CaptureError(f"Refusing to overwrite a different capture: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _required_identity(payload: dict[str, Any], key: str, expected: str) -> str:
    observed = str(payload.get(key, "")).strip()
    if not observed or observed != expected.strip():
        raise CaptureError(f"{key} does not match the expected pinned target")
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert an exact pinned assistant turn's rendered HTML to Markdown."
    )
    parser.add_argument("--payload-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-conversation-id", required=True)
    parser.add_argument("--expected-remote-turn-id", required=True)
    parser.add_argument("--expected-prompt-sha256", required=True)
    args = parser.parse_args()
    try:
        payload_path = Path(args.payload_file).expanduser().resolve(strict=True)
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise CaptureError("Browser capture payload must be a schema_version=1 object")
        _required_identity(payload, "conversation_id", args.expected_conversation_id)
        _required_identity(payload, "remote_turn_id", args.expected_remote_turn_id)
        expected_prompt_sha = args.expected_prompt_sha256.strip().lower()
        if not SHA256_RE.fullmatch(expected_prompt_sha):
            raise CaptureError("--expected-prompt-sha256 must be a lowercase SHA-256")
        _required_identity(payload, "prompt_sha256", expected_prompt_sha)
        html = payload.get("html")
        if not isinstance(html, str) or not html.strip():
            raise CaptureError("Payload must contain the exact assistant element innerHTML")
        markdown = html_to_markdown(html)
        output = Path(args.output).expanduser().resolve()
        _atomic_write(output, markdown)
        print(
            json.dumps(
                {
                    "valid": True,
                    "capture_format": "rendered-html-to-markdown-v1",
                    "conversation_id": args.expected_conversation_id,
                    "remote_turn_id": args.expected_remote_turn_id,
                    "prompt_sha256": expected_prompt_sha,
                    "answer_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                    "output": str(output),
                    "bytes": len(markdown.encode("utf-8")),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (CaptureError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
