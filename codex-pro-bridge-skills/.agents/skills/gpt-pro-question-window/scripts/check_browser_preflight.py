#!/usr/bin/env python3
"""Fail-closed browser observations before a ChatGPT prompt is submitted."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[2] / ".shared"
sys.path.insert(0, str(SHARED_DIR))

from bridge_store import (  # noqa: E402
    BridgeError,
    assert_browser_lease_held,
    file_sha256,
    now_iso,
)
from browser_host import verify_staged_file  # noqa: E402
from project_store import REMOTE_PROJECT_ID_RE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact visible model and attachment state before browser submission."
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--bridge-thread-id", default="")
    parser.add_argument(
        "--browser-lease-token",
        default="",
        help="Optional token from manage_browser_lease.py; verified before Send.",
    )
    parser.add_argument("--requested-model", required=True)
    parser.add_argument("--selected-ui-label", required=True)
    parser.add_argument(
        "--source-bundle",
        default="",
        help="Original bundle before optional browser-host staging.",
    )
    parser.add_argument(
        "--bundle",
        default="",
        help="Absolute execution-host path uploaded directly or returned by staging.",
    )
    parser.add_argument(
        "--browser-upload-path",
        default="",
        help="Browser-host path returned by manage_browser_staging.py.",
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
        requested = args.requested_model.strip()
        selected = args.selected_ui_label.strip()
        if not requested or not selected:
            raise BridgeError("Requested and selected model labels must both be non-empty")
        if requested != selected:
            raise BridgeError(
                f"Selected UI label {selected!r} does not exactly match requested model {requested!r}"
            )

        raw_bundle = Path(args.bundle).expanduser() if args.bundle else None
        raw_source = Path(args.source_bundle).expanduser() if args.source_bundle else None
        if raw_bundle and not raw_bundle.is_absolute():
            raise BridgeError("--bundle must be an absolute path for Chrome upload")
        bundle_path = raw_bundle.resolve() if raw_bundle else None
        attachment_name = args.attachment_name.strip()
        upload_control = args.upload_control.strip()
        attachment_sha256 = ""
        attachment_verification = "not-required"
        browser_upload_path = args.browser_upload_path.strip()
        staging_topology = "direct"
        if bundle_path:
            if not bundle_path.is_file():
                raise BridgeError(f"Bundle does not exist: {bundle_path}")
            if raw_source or browser_upload_path:
                if not raw_source or not raw_source.is_absolute():
                    raise BridgeError(
                        "--source-bundle must be absolute when browser staging is used"
                    )
                verified = verify_staged_file(
                    bundle_path,
                    source_path=raw_source,
                    expected_browser_path=browser_upload_path,
                )
                expected_name = verified["attachment_name"]
                attachment_sha256 = verified["staged_sha256"]
                browser_upload_path = verified["staged_browser_path"]
                staging_topology = verified["topology"]
            else:
                expected_name = bundle_path.name
                attachment_sha256 = file_sha256(bundle_path)
                browser_upload_path = str(bundle_path)
            if attachment_name != expected_name:
                raise BridgeError(
                    f"Visible attachment {attachment_name!r} does not match bundle {expected_name!r}"
                )
            if not upload_control:
                raise BridgeError("--upload-control is required when a bundle is attached")
            if "hidden" in upload_control.lower():
                raise BridgeError("Direct hidden-input clicks are not an accepted upload control")
            attachment_verification = "verified"
        elif raw_source or browser_upload_path or attachment_name or upload_control:
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
                    "requested_model": requested,
                    "selected_ui_label": selected,
                    "model_verification": "verified",
                    "attachment_name": attachment_name,
                    "attachment_sha256": attachment_sha256,
                    "attachment_verification": attachment_verification,
                    "source_bundle": str(raw_source.resolve()) if raw_source else "",
                    "bundle": str(bundle_path) if bundle_path else "",
                    "browser_upload_path": browser_upload_path,
                    "staging_topology": staging_topology,
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
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (BridgeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
