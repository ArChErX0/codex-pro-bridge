"""Validate copied Markdown links against the pinned new-UI source metadata.

The visible DOM exposes structural citation/resource controls.  The new UI's
message item exposes the logical source references needed to explain the
copied text.  This module keeps those domains separate and fails closed when
the source shape, identity, or occurrence mapping is incomplete.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from bridge_store import BridgeError
from capture_copied_reply import INLINE_LINK
from bridge_runtime.submission_text import copied_prompt_codec


SCHEMA = "copied-link-source-proof/v1"
SOURCE_SCHEMA = "copied-link-source/v1"
SOURCE_ROUTE = "new-ui-fiber-item"
CONTENT_REFERENCE_TOKEN = re.compile(r':chatgpt-content-reference\{index="(\d+)"\}')


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value):
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _fail(message):
    raise BridgeError("Copied-link source proof: " + message)


def require_source_proof_status(*, route, answer_format, status, proof_present):
    """Require an immutable source proof whenever capture observed availability."""
    if route == "browser-fallback" and answer_format == "copied-markdown":
        if status == "available" and not proof_present:
            raise BridgeError("Available copied-link source requires its proof receipt")
        if proof_present and status != "available":
            raise BridgeError("Copied-link source proof must declare available status")


def _occurrences(text):
    if not isinstance(text, str):
        _fail("copied text must be a string")
    result = []
    for ordinal, match in enumerate(INLINE_LINK.finditer(text), 1):
        value = match.group(0)
        split = value.find("](")
        label = value[1:split]
        target = value[split + 2 : -1]
        parsed = urlsplit(target)
        result.append({
            "ordinal": ordinal,
            "label": label,
            "target": target,
            "base_target": target.split("#", 1)[0],
            "fragment": parsed.fragment or "",
            "text": value,
            "line": text[: match.start()].count("\n") + 1,
        })
    return result


def _source_identity(source):
    if not isinstance(source, dict):
        _fail("source observation must be an object")
    ignored = {"observed_at", "dom", "readiness"}
    return {key: value for key, value in source.items() if key not in ignored}


def source_identity_digest(source):
    """Digest stable source identity while excluding observation timestamps."""
    return _json_digest(_source_identity(source))


def _require_text(value, field):
    if not isinstance(value, str) or not value:
        _fail(f"{field} must be non-empty text")
    return value


def _validate_turn_summary(turn, *, user_id, assistant_id, field):
    if not isinstance(turn, dict):
        _fail(f"{field} is missing")
    turn_key = _require_text(turn.get("key"), f"{field}.key")
    entry_id = _require_text(turn.get("entry_id"), f"{field}.entry_id")
    if entry_id != turn_key:
        _fail(f"{field}.entry_id does not match turn key")
    if turn.get("entry_turn_key") != user_id:
        _fail(f"{field}.entry_turn_key does not match pinned user")
    if turn.get("status") != "complete":
        _fail(f"{field} is not complete")
    items = turn.get("items")
    if not isinstance(items, list) or len(items) != 3:
        _fail(f"{field}.items must contain exactly user, reasoning-group, assistant")
    types = [item.get("type") if isinstance(item, dict) else None for item in items]
    if types != ["user-message", "chatgpt-reasoning-group", "assistant-message"]:
        _fail(f"{field}.items order/type is not canonical: {types!r}")
    user, reasoning, assistant = items
    if user.get("message_id") != user_id or assistant.get("message_id") != assistant_id:
        _fail(f"{field} item IDs do not match pinned records")
    if reasoning.get("completed") is not True:
        _fail(f"{field} reasoning group is not complete")
    if assistant.get("completed") is not True:
        _fail(f"{field} assistant item is not complete")
    return turn_key


def _validate_turn(source, *, user_id, assistant_id):
    assistant_turn = source.get("turn")
    user_turn = source.get("user_turn")
    turn_key = _validate_turn_summary(assistant_turn, user_id=user_id, assistant_id=assistant_id, field="turn")
    user_turn_key = _validate_turn_summary(user_turn, user_id=user_id, assistant_id=assistant_id, field="user_turn")
    fields = ("key", "entry_id", "entry_turn_key", "status", "items")
    if {field: assistant_turn.get(field) for field in fields} != {field: user_turn.get(field) for field in fields}:
        _fail("user/assistant turn or entry relation mismatch")
    if turn_key != user_turn_key:
        _fail("user/assistant turn keys differ")
    return turn_key


_SUBMISSION_COPY_FIELDS = (
    "comparison_codec", "conversation_url", "copy_provenance", "copy_sha256",
    "prompt_sha256", "remote_turn_id", "tab_owner_token",
)


def _submission_copy_summary(value):
    if not isinstance(value, dict):
        _fail("submission-copy artifact summary is missing")
    summary = {key: value.get(key) for key in _SUBMISSION_COPY_FIELDS}
    if any(not isinstance(summary[key], str) or not summary[key] for key in _SUBMISSION_COPY_FIELDS):
        _fail("submission-copy artifact summary is incomplete")
    if summary["copy_provenance"] != "visible-copy-write/v1":
        _fail("submission-copy artifact provenance is not visible-copy-write/v1")
    if summary["comparison_codec"] not in {"exact", "literal-user-markdown/v1"}:
        _fail("submission-copy artifact comparison codec is unknown")
    return summary


def _validate_prompt_binding(source, *, user_id, expected_url, expected_owner, expected_prompt, submission_copy):
    if not isinstance(expected_prompt, str) or not expected_prompt:
        _fail("canonical prompt is unavailable for user-source binding")
    summary = _submission_copy_summary(submission_copy)
    user = source.get("user")
    user_message = user.get("message") if isinstance(user, dict) else None
    _require_text(user_message, "source user escaped message")
    codec = copied_prompt_codec(user_message, expected_prompt, literal_user_text=True)
    if codec is None:
        _fail("source user message differs from the frozen prompt")
    canonical_prompt_sha = _digest(expected_prompt)
    expected_summary = {**summary, "remote_turn_id": user_id, "conversation_url": expected_url,
                        "tab_owner_token": expected_owner, "prompt_sha256": canonical_prompt_sha,
                        "copy_sha256": _digest(user_message), "comparison_codec": codec}
    if summary != expected_summary:
        _fail("submission-copy artifact summary does not bind the source user message")
    return {
        "prompt_sha256": canonical_prompt_sha,
        "prompt_codec": codec,
        "submission_copy_summary": summary,
        "submission_copy_sha256": _json_digest(summary),
    }


def _validate_source_shape(source, *, user_id, assistant_id, expected_url, expected_owner, expected_page_id):
    if source.get("status") != "available":
        _fail("source status is not available")
    if source.get("schema") != SOURCE_SCHEMA or source.get("source_route") != SOURCE_ROUTE:
        _fail("unknown or unsupported source schema")
    if source.get("conversation_url") != expected_url or source.get("owner_token") != expected_owner:
        _fail("source owner/URL identity mismatch")
    if str(source.get("page_id")) != str(expected_page_id):
        _fail("source page identity mismatch")
    if source.get("complete") is not True or source.get("truncated") is not False:
        _fail("source observation is incomplete or truncated")
    records = source.get("records")
    if records != {"user": user_id, "assistant": assistant_id}:
        _fail("source fixed records do not match pinned user/assistant")
    user = source.get("user")
    assistant = source.get("assistant")
    if not isinstance(user, dict) or not isinstance(assistant, dict):
        _fail("source user/assistant payload is missing")
    if user.get("id") != user_id or user.get("messageId") != user_id or user.get("type") != "user-message":
        _fail("source user record mismatch")
    if assistant.get("id") != assistant_id or assistant.get("type") != "assistant-message":
        _fail("source assistant record mismatch")
    if assistant.get("completed") is not True or assistant.get("phase") != "final_answer":
        _fail("source assistant is not a completed final answer")
    if assistant.get("latest_message_id") != assistant_id:
        _fail("source latest message ID mismatch")
    if assistant.get("source_message_ids") != [assistant_id]:
        _fail("sourceMessageIds are not the singleton pinned assistant")
    _require_text(assistant.get("content"), "source assistant content")
    reference_ids = assistant.get("content_reference_message_ids")
    statuses = assistant.get("content_reference_message_statuses")
    if not isinstance(reference_ids, list) or len(reference_ids) != 32 or any(value != assistant_id for value in reference_ids):
        _fail("contentReferenceMessageIds are not the expected 32 singleton assistant refs")
    if not isinstance(statuses, list) or len(statuses) != 32 or any(value != "finished_successfully" for value in statuses):
        _fail("content-reference statuses are incomplete or unsuccessful")
    turn_key = _validate_turn(source, user_id=user_id, assistant_id=assistant_id)
    refs = assistant.get("content_references")
    if not isinstance(refs, list) or len(refs) != 32:
        _fail("contentReferences must contain exactly 32 entries")
    allowed = {"file", "grouped_webpages", "sources_footnote"}
    if any(not isinstance(ref, dict) or ref.get("type") not in allowed for ref in refs):
        _fail("contentReferences contains an unknown type")
    counts = {kind: sum(ref.get("type") == kind for ref in refs) for kind in allowed}
    if counts != {"file": 30, "grouped_webpages": 1, "sources_footnote": 1}:
        _fail(f"contentReferences type counts drifted: {counts!r}")
    grouped = [ref for ref in refs if ref.get("type") == "grouped_webpages"][0]
    safe_urls = grouped.get("safe_urls")
    if not isinstance(safe_urls, list) or not safe_urls or any(not isinstance(url, str) or not url.startswith("https://") for url in safe_urls):
        _fail("grouped web safe_urls are missing or malformed")
    _require_text(grouped.get("alt"), "grouped web alt")
    if not isinstance(grouped.get("matched_text"), str) or not isinstance(grouped.get("items"), list) or len(grouped.get("items")) != 1:
        _fail("grouped web citation shape is incomplete")
    grouped_item = grouped["items"][0]
    if not isinstance(grouped_item, dict) or not isinstance(grouped_item.get("supporting_websites"), list):
        _fail("grouped web supporting source shape is incomplete")
    for ref in refs:
        if ref.get("type") == "file" and not isinstance(ref.get("matched_text"), str):
            _fail("file content reference shape is incomplete")
        if ref.get("type") == "sources_footnote" and ref.get("safe_urls") not in (None, []):
            _fail("sources footnote unexpectedly carries logical URLs")
    if grouped.get("status") not in (None, "done"):
        _fail("grouped web source is not done")
    return turn_key, refs


def _link_from_alt(alt):
    matches = list(INLINE_LINK.finditer(alt))
    if len(matches) != 1:
        _fail("grouped web alt must contain exactly one Markdown link")
    value = matches[0].group(0)
    split = value.find("](")
    return {"label": value[1:split], "target": value[split + 2 : -1], "text": value}


def _source_occurrence(value, *, ordinal, kind, source_ref_index):
    matches = _occurrences(value)
    if len(matches) != 1:
        _fail("source reference must contain exactly one Markdown link")
    item = matches[0]
    return {key: item[key] for key in ("ordinal", "label", "target", "base_target", "fragment", "text")} | {
        "ordinal": ordinal, "kind": kind, "source_ref_index": source_ref_index,
    }


def _validate_file_reference(ref, *, index, assistant_id):
    if ref.get("message_id") != assistant_id:
        _fail(f"file reference {index} is bound to another assistant")
    matched = ref.get("matched_text")
    if not isinstance(matched, str) or not matched or ref.get("link_markdown") != matched:
        _fail(f"file reference {index} link text is not canonical")
    occurrence = _source_occurrence(matched, ordinal=1, kind="file", source_ref_index=index)
    if not occurrence["target"].startswith("sandbox:"):
        _fail(f"file reference {index} is not a sandbox link")
    basename = occurrence["target"].split("/", 3)[-1]
    sandbox_path = occurrence["target"][len("sandbox:"):]
    if ref.get("name") != basename or ref.get("file_name") != basename or ref.get("sandbox_path") != sandbox_path:
        _fail(f"file reference {index} basename/path fields do not match link target")
    return occurrence


def _source_ordered_links(source, refs, *, assistant_id):
    content = source["assistant"]["content"]
    tokens = list(CONTENT_REFERENCE_TOKEN.finditer(content))
    expected_indices = {0, *(index for index, ref in enumerate(refs) if ref.get("type") == "file")}
    observed_indices = [int(match.group(1)) for match in tokens]
    if len(observed_indices) != len(expected_indices) or len(set(observed_indices)) != len(observed_indices):
        _fail("assistant content reference tokens are missing or duplicated")
    if set(observed_indices) != expected_indices:
        _fail("assistant content reference token indices do not match source refs")
    grouped = refs[0]
    if grouped.get("type") != "grouped_webpages":
        _fail("grouped web reference is not the indexed source ref")
    alt = _link_from_alt(grouped.get("alt", ""))
    safe_urls = grouped.get("safe_urls", [])
    supporting = grouped.get("items", [{}])[0].get("supporting_websites", [])
    supporting_urls = {item.get("url") for item in supporting if isinstance(item, dict)}
    if alt["target"] not in safe_urls or alt["target"] not in supporting_urls:
        _fail("grouped web alt target is not present in safe/supporting source URLs")
    if alt["text"] in content:
        _fail("grouped web token unexpectedly has an inline Markdown literal")
    token_entries = []
    expanded_parts = []
    cursor = 0
    for ordinal, match in enumerate(tokens, 1):
        index = int(match.group(1))
        ref = refs[index]
        expanded_parts.append(content[cursor:match.start()])
        if index == 0:
            expanded_parts.append(alt["text"])
            token_entries.append((alt["text"], "grouped_web_alt", index))
            cursor = match.end()
            continue
        if ref.get("type") != "file":
            _fail(f"content token {index} does not point to a file source ref")
        occurrence = _validate_file_reference(ref, index=index, assistant_id=assistant_id)
        if not content[match.end():].startswith(occurrence["text"]):
            _fail(f"content token {index} is not immediately followed by matched_text")
        token_entries.append((occurrence["text"], "file", index))
        cursor = match.end()
    expanded_parts.append(content[cursor:])
    expanded = "".join(expanded_parts)
    expanded_occurrences = _occurrences(expanded)
    if len(expanded_occurrences) != len(token_entries):
        _fail("source logical-link inventory is incomplete")
    ordered = []
    fields = ("label", "target", "base_target", "fragment", "text")
    for ordinal, (actual, (expected_text, kind, index)) in enumerate(zip(expanded_occurrences, token_entries), 1):
        if actual["text"] != expected_text:
            _fail("expanded source logical-link occurrence differs from token metadata")
        ordered.append({key: actual[key] for key in ("ordinal", *fields)} | {
            "ordinal": ordinal, "kind": kind, "source_ref_index": index,
        })
    return ordered


def _validate_dom_source_mapping(source, refs):
    dom = source.get("dom")
    if not isinstance(dom, dict):
        _fail("source DOM mapping is missing")
    grouped = refs[0]
    safe_urls = set(grouped.get("safe_urls", []))
    anchors = dom.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        _fail("source DOM citation anchors are missing")
    hrefs = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("href"), str) or anchor["href"] not in safe_urls:
            _fail("source DOM citation anchor is not a grouped safe URL")
        hrefs.append(anchor["href"])
    alt_target = _link_from_alt(grouped["alt"])["target"]
    if alt_target not in hrefs:
        _fail("source DOM citation anchors omit the grouped alt target")
    names = {ref.get("name") for ref in refs if ref.get("type") == "file"}
    rows = dom.get("resource_rows")
    if not isinstance(rows, list):
        _fail("source DOM resource rows are missing")
    for row in rows:
        if not isinstance(row, dict):
            _fail("source DOM resource row is malformed")
        row_name = row.get("filename")
        if not row_name and isinstance(row.get("titles"), list) and row["titles"]:
            row_name = row["titles"][0]
        if row_name not in names:
            _fail("source DOM resource row is not a declared file reference")
    sandbox_buttons = dom.get("sandbox_buttons")
    if not isinstance(sandbox_buttons, int) or sandbox_buttons < 0:
        _fail("source DOM sandbox button count is malformed")
    structural_links = dom.get("structural_links")
    if structural_links != len(anchors) + len(rows) + sandbox_buttons:
        _fail("source DOM structural link count does not equal its visible components")
    if len(anchors) != 1 or len(rows) != 2 or sandbox_buttons != 0 or structural_links != 3:
        _fail("source DOM structural shape is not the pinned 1-anchor/2-resource/0-button layout")


def _source_links(source, copied_text, *, user_id, assistant_id, expected_url, expected_owner, expected_page_id,
                  expected_prompt, submission_copy):
    turn_key, refs = _validate_source_shape(
        source, user_id=user_id, assistant_id=assistant_id, expected_url=expected_url,
        expected_owner=expected_owner, expected_page_id=expected_page_id,
    )
    binding = _validate_prompt_binding(
        source, user_id=user_id, expected_url=expected_url, expected_owner=expected_owner,
        expected_prompt=expected_prompt, submission_copy=submission_copy,
    )
    _validate_dom_source_mapping(source, refs)
    copied = _occurrences(copied_text)
    logical = _source_ordered_links(source, refs, assistant_id=assistant_id)
    if len(copied) != len(logical):
        _fail("copied and source logical-link inventories have different lengths")
    fields = ("ordinal", "label", "target", "base_target", "fragment", "text")
    if any(tuple(item.get(key) for key in fields) != tuple(source_item.get(key) for key in fields)
           for item, source_item in zip(copied, logical)):
        _fail("copied logical-link occurrences differ from source token order/identity")
    grouped_index, grouped = next((index, ref) for index, ref in enumerate(refs)
                                  if ref.get("type") == "grouped_webpages")
    return turn_key, logical, copied, grouped, binding


def build_source_link_proof(
    source_before,
    source_after,
    copied_text,
    *,
    assistant_id,
    user_id,
    expected_url,
    expected_owner,
    expected_page_id,
    structural_metrics,
    copied_metrics,
    expected_prompt,
    submission_copy,
):
    before_key, before_links, _, before_group, before_binding = _source_links(
        source_before, copied_text, user_id=user_id, assistant_id=assistant_id,
        expected_url=expected_url, expected_owner=expected_owner, expected_page_id=expected_page_id,
        expected_prompt=expected_prompt, submission_copy=submission_copy,
    )
    after_key, after_links, _, after_group, after_binding = _source_links(
        source_after, copied_text, user_id=user_id, assistant_id=assistant_id,
        expected_url=expected_url, expected_owner=expected_owner, expected_page_id=expected_page_id,
        expected_prompt=expected_prompt, submission_copy=submission_copy,
    )
    if source_identity_digest(source_before) != source_identity_digest(source_after):
        _fail("source identity changed across Copy")
    if source_before.get("dom") != source_after.get("dom"):
        _fail("DOM logical-link structure changed across Copy")
    if before_key != after_key or before_links != after_links:
        _fail("source turn or logical-link inventory changed across Copy")
    if before_binding != after_binding:
        _fail("source prompt binding changed across Copy")
    if not isinstance(structural_metrics, dict) or not isinstance(copied_metrics, dict):
        _fail("capture metrics must be objects")
    if not isinstance(structural_metrics.get("links"), int) or not isinstance(copied_metrics.get("links"), int):
        _fail("capture link metrics are not integers")
    if copied_metrics["links"] != len(before_links):
        _fail("copied link count differs from the source occurrence inventory")
    dom = source_before.get("dom")
    if not isinstance(dom, dict) or dom.get("structural_links") != structural_metrics["links"]:
        _fail("DOM structural link count does not match the existing metric")
    proof = {
        "schema": SCHEMA,
        "source_route": SOURCE_ROUTE,
        "status": "available",
        "conversation_url": expected_url,
        "owner_token": expected_owner,
        "page_id": str(expected_page_id),
        "user_id": user_id,
        "assistant_id": assistant_id,
        "turn_key": before_key,
        "prompt_sha256": before_binding["prompt_sha256"],
        "prompt_codec": before_binding["prompt_codec"],
        "submission_copy_summary": before_binding["submission_copy_summary"],
        "submission_copy_sha256": before_binding["submission_copy_sha256"],
        "source_before_sha256": source_identity_digest(source_before),
        "source_after_sha256": source_identity_digest(source_after),
        "source_sha256": _json_digest(source_before),
        "source_link_count": len(before_links),
        "copied_link_count": copied_metrics["links"],
        "structural_link_count": structural_metrics["links"],
        "structural_metrics": structural_metrics,
        "copied_metrics": copied_metrics,
        "grouped_web_safe_urls": before_group.get("safe_urls", []),
        "logical_links": before_links,
        "source_before": source_before,
        "source_after": source_after,
        "copied_text_sha256": _digest(copied_text),
        "copied_characters": len(copied_text),
    }
    proof["proof_sha256"] = _json_digest({key: value for key, value in proof.items() if key != "proof_sha256"})
    return proof


def validate_source_link_proof(
    proof,
    copied_text,
    *,
    assistant_id=None,
    user_id=None,
    expected_url=None,
    expected_owner=None,
    expected_page_id=None,
    expected_prompt=None,
):
    if not isinstance(proof, dict) or proof.get("schema") != SCHEMA:
        _fail("proof schema is unknown")
    if proof.get("status") != "available":
        _fail("proof status is not available")
    stable = {key: value for key, value in proof.items() if key != "proof_sha256"}
    if proof.get("proof_sha256") != _json_digest(stable):
        _fail("proof digest mismatch")
    if proof.get("copied_text_sha256") != _digest(copied_text):
        _fail("proof copied-text digest mismatch")
    actual_assistant = assistant_id or proof.get("assistant_id")
    actual_user = user_id or proof.get("user_id")
    actual_url = expected_url or proof.get("conversation_url")
    actual_owner = expected_owner or proof.get("owner_token")
    actual_page = expected_page_id or proof.get("page_id")
    if proof.get("assistant_id") != actual_assistant or proof.get("user_id") != actual_user:
        _fail("proof fixed message identity mismatch")
    if proof.get("conversation_url") != actual_url or proof.get("owner_token") != actual_owner:
        _fail("proof owner/URL mismatch")
    if str(proof.get("page_id")) != str(actual_page):
        _fail("proof page identity mismatch")
    if proof.get("source_before_sha256") != source_identity_digest(proof.get("source_before", {})) or proof.get("source_after_sha256") != source_identity_digest(proof.get("source_after", {})):
        _fail("proof source observation digest mismatch")
    rebuilt = build_source_link_proof(
        proof["source_before"], proof["source_after"], copied_text,
        assistant_id=actual_assistant, user_id=actual_user, expected_url=actual_url,
        expected_owner=actual_owner, expected_page_id=actual_page,
        structural_metrics=proof.get("structural_metrics", {}), copied_metrics=proof.get("copied_metrics", {}),
        expected_prompt=expected_prompt,
        submission_copy=proof.get("submission_copy_summary"),
    )
    for key in ("logical_links", "source_sha256", "prompt_sha256", "prompt_codec",
                "submission_copy_summary", "submission_copy_sha256"):
        if rebuilt[key] != proof.get(key):
            _fail("proof logical-link or prompt-binding inventory drift")
    if expected_prompt is None:
        _fail("canonical prompt is required to validate source proof")
    return proof
