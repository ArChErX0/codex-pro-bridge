#!/usr/bin/env python3
"""Fail-closed browser observations before a ChatGPT prompt is submitted."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[2] / ".shared"
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from bridge_store import (  # noqa: E402
    BridgeError,
    assert_browser_lease_held,
    file_sha256,
    now_iso,
)
from project_store import REMOTE_PROJECT_ID_RE  # noqa: E402
from manage_bridge_dispatcher import (  # noqa: E402
    DispatcherError,
    assert_dispatcher_held,
)


ALLOWED_UPLOAD_CONTROLS = {
    "devtools-mcp-upload-file",
    "devtools-mcp-associated-file-input",
    "devtools-mcp-temporary-exposed-file-input-restored",
    "codex-chrome-visible-menu",
    # Compatibility values emitted by rounds captured before the DevTools
    # adapter was standardized.
    "visible-menu",
    "visible-file-input",
}


def verify_model_selection(args: argparse.Namespace) -> dict[str, str]:
    """Verify either the modern model-family/effort pair or the legacy label."""
    requested_family = args.requested_model_family.strip()
    selected_family = args.selected_model_family.strip()
    requested_effort = args.requested_effort.strip()
    selected_effort = args.selected_effort.strip()
    requested_legacy = args.requested_model.strip()
    selected_legacy = args.selected_ui_label.strip()
    modern = any((requested_family, selected_family, requested_effort, selected_effort))
    if modern:
        if requested_legacy or selected_legacy:
            raise BridgeError(
                "Use model-family/effort flags or legacy model flags, not both"
            )
        if not requested_family or not selected_family:
            raise BridgeError(
                "--requested-model-family and --selected-model-family are both required"
            )
        if not requested_effort or not selected_effort:
            raise BridgeError(
                "--requested-effort and --selected-effort are both required"
            )
        if requested_family != selected_family:
            raise BridgeError(
                f"Selected model family {selected_family!r} does not exactly match "
                f"requested family {requested_family!r}"
            )
        if requested_effort != selected_effort:
            raise BridgeError(
                f"Selected effort {selected_effort!r} does not exactly match "
                f"requested effort {requested_effort!r}"
            )
        return {
            "requested_model": requested_family,
            "selected_ui_label": selected_effort,
            "requested_model_family": requested_family,
            "selected_model_family": selected_family,
            "requested_effort": requested_effort,
            "selected_effort": selected_effort,
            "model_verification": "verified",
        }
    if not requested_legacy or not selected_legacy:
        raise BridgeError(
            "Provide both legacy --requested-model/--selected-ui-label or the modern effort pair"
        )
    if requested_legacy != selected_legacy:
        raise BridgeError(
            f"Selected UI label {selected_legacy!r} does not exactly match "
            f"requested model {requested_legacy!r}"
        )
    return {
        "requested_model": requested_legacy,
        "selected_ui_label": selected_legacy,
        "requested_model_family": "",
        "selected_model_family": "",
        "requested_effort": requested_legacy,
        "selected_effort": selected_legacy,
        "model_verification": "verified",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact visible model and attachment state before browser submission."
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--bridge-thread-id", default="")
    parser.add_argument(
        "--dispatcher-thread-id",
        required=True,
        help="Exact Codex task ID that owns the host-global dispatcher claim.",
    )
    parser.add_argument(
        "--dispatcher-token",
        required=True,
        help="Host-global dispatcher claim token; prevents non-dispatcher tasks from using Chrome.",
    )
    parser.add_argument(
        "--browser-lease-token",
        default="",
        help="Optional token from manage_browser_lease.py; verified before Send.",
    )
    parser.add_argument("--requested-model", default="")
    parser.add_argument("--selected-ui-label", default="")
    parser.add_argument("--requested-model-family", default="")
    parser.add_argument("--selected-model-family", default="")
    parser.add_argument("--requested-effort", default="")
    parser.add_argument("--selected-effort", default="")
    parser.add_argument(
        "--service-warning",
        default="",
        help="Visible rate-limit, downgrade, service, or account-protection warning.",
    )
    parser.add_argument("--bundle", default="")
    parser.add_argument(
        "--staged-file",
        default="",
        help="Exact OS-temp file passed to upload_file; permits a verified filename alias.",
    )
    parser.add_argument("--attachment-name", default="")
    parser.add_argument("--upload-control", default="")
    parser.add_argument("--expected-project-id", default="")
    parser.add_argument("--observed-project-id", default="")
    parser.add_argument("--expected-workspace", default="")
    parser.add_argument("--observed-workspace", default="")
    parser.add_argument("--expected-account-label", default="")
    parser.add_argument("--observed-account-label", default="")
    parser.add_argument("--binding-status", default="")
    parser.add_argument(
        "--expected-conversation-id",
        default="",
        help="Canonical conversation id reserved for this thread's probe.",
    )
    parser.add_argument(
        "--observed-conversation-id",
        default="",
        help="Conversation id currently visible in the browser before Send.",
    )
    args = parser.parse_args()

    try:
        dispatcher = assert_dispatcher_held(
            token=args.dispatcher_token,
            expected_thread_id=args.dispatcher_thread_id,
        )
        model = verify_model_selection(args)
        service_warning = args.service_warning.strip()
        if service_warning:
            raise BridgeError(
                "Visible service/account warning blocks submission: "
                + service_warning[:240]
            )

        raw_bundle = Path(args.bundle).expanduser() if args.bundle else None
        if raw_bundle and not raw_bundle.is_absolute():
            raise BridgeError("--bundle must be an absolute path for Chrome upload")
        bundle_path = raw_bundle.resolve() if raw_bundle else None
        raw_staged = Path(args.staged_file).expanduser() if args.staged_file else None
        if raw_staged and not raw_staged.is_absolute():
            raise BridgeError("--staged-file must be an absolute path")
        staged_path = raw_staged.resolve() if raw_staged else None
        attachment_name = args.attachment_name.strip()
        upload_control = args.upload_control.strip()
        attachment_sha256 = ""
        attachment_verification = "not-required"
        if bundle_path:
            if not bundle_path.is_file():
                raise BridgeError(f"Bundle does not exist: {bundle_path}")
            upload_path = staged_path or bundle_path
            if not upload_path.is_file():
                raise BridgeError(f"Staged upload file does not exist: {upload_path}")
            if attachment_name != upload_path.name:
                raise BridgeError(
                    f"Visible attachment {attachment_name!r} does not match uploaded file "
                    f"{upload_path.name!r}"
                )
            if not upload_control:
                raise BridgeError("--upload-control is required when a bundle is attached")
            if upload_control not in ALLOWED_UPLOAD_CONTROLS:
                raise BridgeError(
                    f"Unsupported upload control {upload_control!r}; use a verified semantic "
                    "DevTools/connector route"
                )
            bundle_sha256 = file_sha256(bundle_path)
            attachment_sha256 = file_sha256(upload_path)
            if attachment_sha256 != bundle_sha256:
                raise BridgeError("Staged upload SHA-256 does not match the canonical bundle")
            attachment_verification = "verified"
        elif attachment_name or upload_control or staged_path:
            raise BridgeError("Attachment observations require --bundle")

        expected_project_id = args.expected_project_id.strip()
        observed_project_id = args.observed_project_id.strip()
        expected_workspace = args.expected_workspace.strip()
        observed_workspace = args.observed_workspace.strip()
        expected_account_label = args.expected_account_label.strip()
        observed_account_label = args.observed_account_label.strip()
        binding_status = args.binding_status.strip()
        project_verification = "not-required"
        if expected_project_id:
            if not REMOTE_PROJECT_ID_RE.fullmatch(expected_project_id):
                raise BridgeError("--expected-project-id must look like g-p-<id>")
            if binding_status != "active":
                raise BridgeError("Project-bound submission requires an active binding")
            if observed_project_id != expected_project_id:
                raise BridgeError(
                    f"Observed ChatGPT Project {observed_project_id!r} does not match "
                    f"the binding {expected_project_id!r}"
                )
            if not expected_workspace or not expected_account_label:
                raise BridgeError(
                    "Project-bound submission requires expected workspace and account labels"
                )
            if observed_workspace != expected_workspace:
                raise BridgeError(
                    f"Observed workspace {observed_workspace!r} does not match "
                    f"the binding {expected_workspace!r}"
                )
            if observed_account_label != expected_account_label:
                raise BridgeError(
                    f"Observed account {observed_account_label!r} does not match "
                    f"the binding {expected_account_label!r}"
                )
            project_verification = "verified"
        elif (
            observed_project_id
            or expected_workspace
            or observed_workspace
            or expected_account_label
            or observed_account_label
            or binding_status
        ):
            raise BridgeError(
                "Standalone submission cannot include Project binding observations"
            )

        expected_conversation_id = args.expected_conversation_id.strip()
        observed_conversation_id = args.observed_conversation_id.strip()
        conversation_verification = "not-required"
        if expected_conversation_id:
            if not observed_conversation_id:
                raise BridgeError(
                    "--observed-conversation-id is required when a conversation is reserved"
                )
            if observed_conversation_id != expected_conversation_id:
                raise BridgeError(
                    f"Observed conversation {observed_conversation_id!r} does not match "
                    f"the reserved conversation {expected_conversation_id!r}; the browser "
                    "is on the wrong chat"
                )
            conversation_verification = "verified"
        elif observed_conversation_id:
            raise BridgeError(
                "--observed-conversation-id requires --expected-conversation-id"
            )

        browser_lease_verification = "not-provided"
        browser_lease_thread_id = ""
        if args.browser_lease_token:
            repo = Path(args.repo).resolve()
            if not repo.is_dir():
                raise BridgeError(f"Repository root is not a directory: {repo}")
            lease = assert_browser_lease_held(repo, token=args.browser_lease_token)
            browser_lease_thread_id = str(lease.get("thread_id", ""))
            expected_thread_id = args.bridge_thread_id.strip()
            if expected_thread_id and browser_lease_thread_id != expected_thread_id:
                raise BridgeError(
                    f"Browser lease belongs to thread {browser_lease_thread_id or '<none>'!r}, "
                    f"not {expected_thread_id!r}"
                )
            lease_conversation_id = str(lease.get("expected_conversation_id", ""))
            if lease_conversation_id and lease_conversation_id != expected_conversation_id:
                raise BridgeError(
                    "Browser lease conversation does not match the preflight conversation"
                )
            lease_project_id = str(lease.get("expected_remote_project_id", ""))
            if lease_project_id and lease_project_id != expected_project_id:
                raise BridgeError(
                    "Browser lease Project does not match the preflight Project"
                )
            browser_lease_verification = "verified"

        print(
            json.dumps(
                {
                    "ready": True,
                    "verified_at": now_iso(),
                    **model,
                    "service_warning": "",
                    "attachment_name": attachment_name,
                    "attachment_sha256": attachment_sha256,
                    "staged_file": str(staged_path) if staged_path else "",
                    "attachment_verification": attachment_verification,
                    "upload_control": upload_control,
                    "expected_project_id": expected_project_id,
                    "observed_project_id": observed_project_id,
                    "expected_workspace": expected_workspace,
                    "observed_workspace": observed_workspace,
                    "expected_account_label": expected_account_label,
                    "observed_account_label": observed_account_label,
                    "project_verification": project_verification,
                    "binding_status": binding_status,
                    "expected_conversation_id": expected_conversation_id,
                    "observed_conversation_id": observed_conversation_id,
                    "conversation_verification": conversation_verification,
                    "browser_lease_thread_id": browser_lease_thread_id,
                    "browser_lease_verification": browser_lease_verification,
                    "dispatcher_thread_id": dispatcher.get("thread_id", ""),
                    "dispatcher_verification": "verified",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (BridgeError, DispatcherError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
