"""Raw page serialization contract shared by producer, saver and completion.

The schema is the shape authority. No rendered text or inferred DOM parent is
accepted as raw source. Proof files contain owner tokens and stay private.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from bridge_store import BridgeError

PAGE_ROUTE = "browser-page-serialized"
PAGE_FORMAT = "page-serialized-markdown"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "gpt-pro-question-window/schemas/page_serialization.schema.json"


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_digest(messages):
    return text_digest(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def private_proof(path):
    mode = path.stat().st_mode
    if path.is_symlink() or not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
        raise BridgeError("Raw serialization proof must be a private regular artifact")


def _shape(value, spec):
    # Only this schema's small Draft7 subset is evaluated, without adding a
    # runtime dependency. Full Draft7 parity is exercised in the local tests.
    if "const" in spec and (value != spec["const"] or type(value) is not type(spec["const"])):
        raise BridgeError("Page serialization schema constant mismatch")
    kinds = spec.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "null": value is None}
    if kinds and not any(matches[kind] for kind in kinds):
        raise BridgeError("Page serialization schema type mismatch")
    if isinstance(value, dict):
        props = spec.get("properties", {})
        if set(spec.get("required", [])) - set(value) or (spec.get("additionalProperties") is False and set(value) - set(props)):
            raise BridgeError("Page serialization schema fields mismatch")
        for key in set(value) & set(props):
            _shape(value[key], props[key])
    if isinstance(value, list):
        if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", len(value)):
            raise BridgeError("Page serialization schema item count mismatch")
        for item in value:
            _shape(item, spec.get("items", {}))
    if isinstance(value, str) and (len(value) < spec.get("minLength", 0) or
                                  ("pattern" in spec and not re.fullmatch(spec["pattern"], value))):
        raise BridgeError("Page serialization schema string mismatch")


def _raw_text(message):
    content = message.get("content", {})
    if not isinstance(content, dict):
        raise BridgeError("Raw serialization content must be an object")
    parts = content.get("parts") if isinstance(content, dict) else None
    if (content.get("content_type") != "text" or not isinstance(parts, list)
            or len(parts) != 1 or not isinstance(parts[0], str) or not parts[0]):
        raise BridgeError("Raw serialization requires one complete text/Markdown part")
    return parts[0]


def _role(record):
    author = record.get("message", {}).get("author", {})
    if not isinstance(author, dict):
        raise BridgeError("Raw serialization author must be an object")
    return author.get("role")


def _parent(record):
    message, node = record["message"], record["node"]
    direct = message.get("parent_id")
    mapped = None
    if node is not None:
        if node.get("id") != message.get("id") or node.get("message") != message:
            raise BridgeError("Serialized node/message identity mismatch")
        mapped = node.get("parent")
    if direct and mapped and direct != mapped:
        raise BridgeError("Ambiguous serialized assistant parent")
    parent = direct or mapped
    if not isinstance(parent, str) or not parent:
        raise BridgeError("Raw serialization lacks assistant parent; DOM adjacency cannot supply it")
    return parent


def validate_page_serialization(proof, *, attempt, prompt, answer=None, page_id=None, owner=None, url=None):
    _shape(proof, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    expected_url = url or attempt["conversation_url"]
    expected_owner = owner or attempt["tab_owner_token"]
    if proof["conversation_url"] != expected_url or proof["owner_token"] != expected_owner:
        raise BridgeError("Raw serialization owner/URL mismatch")
    if page_id is not None and proof["page_id"] != str(page_id):
        raise BridgeError("Raw serialization page identity mismatch")
    canonical_page = attempt.get("preflight", {}).get("observed_page_id")
    if not canonical_page or proof["page_id"] != str(canonical_page):
        raise BridgeError("Raw serialization page disagrees with canonical preflight")
    try:
        observed = dt.datetime.fromisoformat(proof["observed_at"].removesuffix("Z") +
                                           ("+00:00" if proof["observed_at"].endswith("Z") else ""))
    except ValueError as exc:
        raise BridgeError("Raw serialization observation timestamp is invalid") from exc
    if observed.tzinfo is None:
        raise BridgeError("Raw serialization observation timestamp lacks timezone")
    records = proof["messages"]
    users = [r for r in records if _role(r) == "user"]
    assistants = [r for r in records if _role(r) == "assistant"]
    if len(users) != 1 or len(assistants) != 1:
        raise BridgeError("Raw serialization requires one user and one unique assistant")
    user, assistant = users[0]["message"], assistants[0]["message"]
    user_node = users[0]["node"]
    if (not isinstance(user_node, dict) or user_node.get("id") != user.get("id")
            or user_node.get("message") != user or user_node.get("children") != [assistant.get("id")]):
        raise BridgeError("Raw serialization lacks the unique complete user-node children relationship")
    if user.get("id") != attempt["remote_turn_id"] or assistant.get("id") == user.get("id"):
        raise BridgeError("Raw serialization pinned user identity mismatch")
    if not isinstance(assistant.get("id"), str) or not assistant["id"] or _parent(assistants[0]) != user["id"]:
        raise BridgeError("Raw serialization assistant parent mismatch")
    if (assistant.get("channel") != "final" or assistant.get("status") != "finished_successfully"
            or assistant.get("end_turn") is not True):
        raise BridgeError("Raw serialization assistant is not a completed final answer")
    user_text, raw = _raw_text(user), _raw_text(assistant)
    if user_text != prompt and user_text + "\n" != prompt:
        raise BridgeError("Raw serialization pinned user prompt mismatch")
    if answer is not None and raw != answer:
        raise BridgeError("Raw serialization differs from saved Markdown")
    hashes = {"source_sha256": source_digest(records), "prompt_sha256": text_digest(prompt),
              "user_sha256": text_digest(user_text), "answer_sha256": text_digest(raw)}
    if any(proof[key] != value for key, value in hashes.items()):
        raise BridgeError("Raw serialization source/Markdown digest mismatch")
    return {"answer": raw, "assistant_turn_id": assistant["id"], "completed_at": proof["observed_at"],
            "answer_sha256": hashes["answer_sha256"], "capture_route": PAGE_ROUTE, "answer_format": PAGE_FORMAT}


def freeze_page_serialization(observation, *, attempt, prompt, page_id, owner, url):
    proof = {**observation, "schema_version": "page-message-serialization/v1",
             "page_id": str(page_id)}
    messages = proof.get("messages", [])
    users = [r["message"] for r in messages if _role(r) == "user"]
    assistants = [r["message"] for r in messages if _role(r) == "assistant"]
    if len(users) != 1 or len(assistants) != 1:
        raise BridgeError("Raw page serialization unavailable or ambiguous")
    proof.update(source_sha256=source_digest(messages), prompt_sha256=text_digest(prompt),
                 user_sha256=text_digest(_raw_text(users[0])), answer_sha256=text_digest(_raw_text(assistants[0])))
    validate_page_serialization(proof, attempt=attempt, prompt=prompt, page_id=page_id, owner=owner, url=url)
    return proof
