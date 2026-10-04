"""One completion authority shared by workers and read-only status/result calls."""
import hashlib
import json
from pathlib import Path

from bridge_attempts import read_attempt
from bridge_store import (BridgeError, bridge_root, file_sha256, load_events,
                          resolve_repo_path, verify_thread_integrity)
from capture_provenance import PAGE_FORMAT, PAGE_ROUTE, validate_page_serialization
from copied_link_source import require_source_proof_status, validate_source_link_proof


def require_capture_page_id(source_link_proof, observed_page_id):
    capture_page_id = source_link_proof.get("page_id") if isinstance(source_link_proof, dict) else None
    if capture_page_id in (None, ""):
        raise BridgeError("Copied-link source proof lacks the capture page identity")
    if observed_page_id in (None, "") or str(observed_page_id) != str(capture_page_id):
        raise BridgeError("Copied-link source proof page disagrees with canonical capture observation")
    return observed_page_id


def validated_completion(job):
    repo = Path(job["repo"]).resolve()
    thread = job["bridge_thread_id"]
    if not job.get("attempt_id"):
        raise BridgeError("Completion lacks canonical attempt identity")
    attempt = read_attempt(repo, thread, job["attempt_id"])
    if attempt["state"] != "captured":
        raise BridgeError("Canonical attempt has not captured a reply")
    if attempt["prompt_sha256"] != job.get("preparation", {}).get("prompt_sha256"):
        raise BridgeError("Captured attempt belongs to another prepared question")
    # Pro capture may legitimately await the local verdict. Verify all history
    # while reporting that boundary separately from response completion.
    ledger = verify_thread_integrity(repo, thread)
    exchanges = [e for e in load_events(bridge_root(repo), thread)
                 if e.get("event_type") == "gpt-exchange"
                 and e.get("data", {}).get("attempt_id") == attempt["attempt_id"]]
    if len(exchanges) != 1:
        raise BridgeError("Captured attempt needs one exact ledger exchange")
    exchange = exchanges[0]
    data = exchange["data"]
    route, answer_format = data.get("capture_route"), data.get("answer_format")
    if (route, answer_format) not in {("browser-fallback", "copied-markdown"),
                                     ("native-read-thread", "native-raw"), (PAGE_ROUTE, PAGE_FORMAT)}:
        raise BridgeError("Captured exchange has unsupported raw provenance route/format")
    receipt = job.get("capture") or job.get("result")
    canonical_raw = data.get("raw_answer")
    recovered = not isinstance(receipt, dict)
    if recovered:
        if not isinstance(canonical_raw, dict):
            raise BridgeError("Captured attempt has no raw answer receipt or canonical raw evidence")
        receipt = {"answer_path": canonical_raw["path"], "answer_sha256": canonical_raw["sha256"],
                   "completed_at": data.get("response_completed_at", "")}
    for key in ("answer_path","answer_sha256","completed_at","assistant_turn_id","copy_provenance",
                "capture_proof_path","capture_proof_sha256"):
        if key in receipt and not isinstance(receipt[key],str):
            raise BridgeError(f"Captured receipt metadata must be a string: {key}")
    if "copy_provenance" in receipt and (route != "browser-fallback" or receipt["copy_provenance"] != "visible-copy-write/v1"):
        raise BridgeError("Captured receipt has invalid Copy provenance")
    completed_at = data.get("response_completed_at", "")
    if not isinstance(completed_at,str):
        raise BridgeError("Canonical capture completion time must be a string")
    answer = resolve_repo_path(receipt.get("answer_path", ""), repo)
    turn = resolve_repo_path(attempt["capture"]["path"], repo)
    if (not answer.is_file() or file_sha256(answer) != receipt.get("answer_sha256")
            or not turn.is_file() or file_sha256(turn) != attempt["capture"]["sha256"]):
        raise BridgeError("Captured answer or canonical turn digest drift")
    if canonical_raw:
        raw = resolve_repo_path(canonical_raw["path"], repo)
        if raw.read_bytes() != answer.read_bytes() or canonical_raw["sha256"] != receipt["answer_sha256"]:
            raise BridgeError("Envelope raw answer disagrees with canonical evidence")
    if (exchange.get("thread_id") != thread or exchange.get("artifact") !=
            {"kind": "gpt-pro-turn", **attempt["capture"]}):
        raise BridgeError("Ledger exchange and canonical captured turn disagree")
    for key, expected in (("answer_sha256", receipt["answer_sha256"]),
                          ("prompt_sha256", attempt["prompt_sha256"]),
                          ("remote_turn_id", attempt["remote_turn_id"]),
                          ("observed_conversation_id", attempt["conversation_id"])):
        if data.get(key) != expected:
            raise BridgeError(f"Captured exchange mismatch: {key}")
    answer_text = answer.read_bytes().decode("utf-8") if route == PAGE_ROUTE else answer.read_text(encoding="utf-8")
    source_link_proof = data.get("source_link_proof")
    require_source_proof_status(route=route, answer_format=answer_format,
                                status=data.get("source_link_status", "unsupported"),
                                proof_present=source_link_proof is not None)
    if source_link_proof is not None:
        if not isinstance(source_link_proof, dict):
            raise BridgeError("Copied-link source proof receipt is malformed")
        source_path = resolve_repo_path(source_link_proof.get("path", ""), repo)
        if (not source_path.is_file()
                or file_sha256(source_path) != source_link_proof.get("sha256")):
            raise BridgeError("Copied-link source proof digest drift")
        proof = json.loads(source_path.read_text(encoding="utf-8"))
        observed_page_id = data.get("observed_page_id")
        capture_page_id = require_capture_page_id(source_link_proof, observed_page_id)
        checked_source = validate_source_link_proof(
            proof, answer_text,
            assistant_id=source_link_proof.get("assistant_id", proof.get("assistant_id")),
            user_id=attempt["remote_turn_id"], expected_url=attempt["conversation_url"],
            expected_owner=attempt.get("tab_owner_token") or source_link_proof.get("owner_token", proof.get("owner_token")),
            expected_page_id=capture_page_id,
            expected_prompt=attempt["prompt"],
        )
        for key in ("source_sha256", "structural_link_count", "source_link_count", "copied_link_count"):
            if source_link_proof.get(key) != checked_source.get(key):
                raise BridgeError("Copied-link source proof summary disagrees with canonical proof")
    if route == PAGE_ROUTE:
        proof_artifact = data.get("capture_proof")
        if not isinstance(proof_artifact, dict):
            raise BridgeError("Captured page serialization lacks canonical proof")
        proof_path = resolve_repo_path(proof_artifact["path"], repo)
        if file_sha256(proof_path) != proof_artifact["sha256"]:
            raise BridgeError("Captured page serialization proof digest drift")
        checked = validate_page_serialization(json.loads(proof_path.read_text(encoding="utf-8")),
            attempt=attempt, prompt=attempt["prompt"], answer=answer_text)
        completed_at = checked["completed_at"]
    fingerprint = hashlib.sha256(json.dumps({
        "attempt_id": attempt["attempt_id"], "thread_id": thread,
        "remote_turn_id": attempt["remote_turn_id"], "prompt": attempt["prompt"],
        "answer": answer_text, "answer_format": answer_format,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if fingerprint != attempt.get("capture_fingerprint"):
        raise BridgeError("Raw answer does not match canonical capture fingerprint")
    # Optional public identities come from canonical source evidence. A typed
    # but unverified envelope string cannot invent a proof path, turn or time.
    receipt = {key: value for key, value in receipt.items() if key in {
        "answer_path", "answer_sha256", "copy_provenance"}}
    receipt["completed_at"] = completed_at
    if route == PAGE_ROUTE:
        receipt.update(capture_proof_path=str(proof_path),capture_proof_sha256=proof_artifact["sha256"],
                       assistant_turn_id=checked["assistant_turn_id"])
    if source_link_proof is not None:
        receipt["source_link_proof"] = source_link_proof
    verdicts = [e for e in load_events(bridge_root(repo), thread) if e.get("event_type") == "codex-verdict"
                and e.get("data", {}).get("turn") == attempt["capture"]["path"]]
    # The full-history verifier validates each verdict's exact exchange link.
    # A later pending round must not reopen this attempt's completed round.
    result = {**receipt, "answer_path": str(answer), "turn_path": str(turn),
              "turn_sha256": attempt["capture"]["sha256"], "attempt_id": attempt["attempt_id"],
              "remote_turn_id": attempt["remote_turn_id"], "conversation_url": attempt["conversation_url"],
              "capture_route": route, "answer_format": answer_format,
              "local_verdict": "recorded" if verdicts else "pending",
              "ledger_round_complete": bool(verdicts), "ledger_thread_complete": ledger["round_complete"]}
    for key, value in result.items():
        if key in job.get("result", {}) and job["result"][key] != value:
            if key not in {"local_verdict", "ledger_round_complete", "ledger_thread_complete"}:
                raise BridgeError(f"Cached job result disagrees with captured evidence: {key}")
    if recovered:
        result["receipt_recovered_from"] = "canonical-exchange-raw-evidence"
    return result
