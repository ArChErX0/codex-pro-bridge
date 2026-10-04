"""Mechanical round runner. Browser/attempt/ledger gates are reused verbatim."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from bridge_attempts import (active_attempt, browser_wait_script, digest, mark_send_started,
                             prepare_attempt, read_attempt, record_submission)
from bridge_store import (BridgeError, DEFAULT_BROWSER_PROFILE, TAB_OWNER_STORAGE_KEY,
                          acquire_browser_lease, assert_browser_lease_held, file_lock,
                          file_sha256, now_iso, parse_metadata, release_browser_lease,
                          default_gpt_session_id, bridge_root)
from browser_host import cleanup_staged_file, staging_windows_root
from browser_identity import (conversation_identity_from_url, project_url_matches,
                              resolve_owned_tab, promote_bootstrap_tab)
from project_store import BridgeProjectStore
from .browser import Browser, send_button_uid, message_dom_source
from .jobs import validate_inputs, write_json
from .rpc import Client
from copied_link_source import build_source_link_proof

SCRIPTS = Path(__file__).resolve().parents[1]
SKILLS = SCRIPTS.parents[1]
ACCOUNT_WORKSPACE_PREFLIGHT_ERROR = (
    "BridgeError: Observed account/workspace differs from Project binding"
)
UI_PROFILE_PREFLIGHT_ERROR = (
    "BridgeError: Control did not become ready: ('button',) ['打开个人资料菜单']"
)
UI_PROFILE_OVERLAY_SCHEMA = "ui-profile-overlay/v1"
UI_PROFILE_OBSERVATION_SCHEMA = "live-ui-observation/v2-controlled-draft"
UI_PROFILE_OVERLAY_FILE = "ui-profile-overlay.json"
UI_PROFILE_OVERLAY_UI_KEYS = frozenset({
    "account", "account_menu_labels", "attachment_chip", "attachment_labels",
    "composer", "control_layout", "model", "model_menu_labels", "send_labels",
    "thinking", "thinking_keyboard_control", "thinking_label_location",
    "thinking_menu_labels", "thinking_positions", "thinking_placeholder_labels",
    "thinking_slider", "upload_labels", "workspace",
})


def identity_preflight_resume_allowed(job, output, handoff):
    """Allow only the known read-only Project identity mismatch checkpoint."""
    prep = job.data.get("preparation")
    if not (
        handoff.get("remote_project_id")
        and job.data.get("state") == "blocked"
        and job.data.get("stage") == "prepared-materials"
        and job.data.get("browser_phase") == "connected"
        and job.data.get("error") == ACCOUNT_WORKSPACE_PREFLIGHT_ERROR
        and isinstance(prep, dict)
        and prep.get("ready") is True
        and not job.data.get("attempt_id")
        and not job.data.get("capture")
        and not job.data.get("result")
        and output.is_dir()
        and not output.is_symlink()
    ):
        return False
    try:
        expected = {
            Path(prep[key]).resolve(strict=True)
            for key in ("bundle", "prompt_file", "materials_file")
        }
        if prep.get("packaging_receipt"):
            receipt = Path(prep["packaging_receipt"])
            expected.update((receipt.resolve(strict=True), receipt.with_suffix(".staging.json").resolve(strict=True)))
        if job.data.get("snapshot_input_receipt"):
            input_receipt = Path(job.data["snapshot_input_receipt"]["path"])
            expected.add(input_receipt.resolve(strict=True))
            if input_receipt.with_suffix(".handoff.json").exists():
                expected.add(input_receipt.with_suffix(".handoff.json").resolve(strict=True))
        actual = {path.resolve(strict=True) for path in output.iterdir()}
    except (KeyError, OSError):
        return False
    if actual != expected or any(not path.is_file() or not path.is_relative_to(output.resolve())
                                 for path in expected):
        return False
    return (
        file_sha256(Path(prep["bundle"])) == prep.get("bundle_sha256")
        and file_sha256(Path(prep["prompt_file"])) == prep.get("prompt_sha256")
        and prep.get("staging", {}).get("source_sha256") == prep.get("bundle_sha256")
    )


def _ui_overlay_expected_project_url(handoff):
    target = str(handoff.get("target_project_url", "") or "").strip()
    if target:
        return target
    project_id = str(handoff.get("remote_project_id", "") or "").strip()
    return f"https://chatgpt.com/g/{project_id}/project" if project_id else ""


def _validate_overlay_ui(ui):
    if not isinstance(ui, dict) or set(ui) - UI_PROFILE_OVERLAY_UI_KEYS:
        raise BridgeError("UI profile overlay contains a non-UI or unknown key")
    required_strings = (
        "account", "workspace", "model", "thinking", "composer", "attachment_chip",
        "thinking_slider", "thinking_keyboard_control",
    )
    for key in required_strings:
        if not isinstance(ui.get(key), str) or not ui[key].strip() or "<" in ui[key]:
            raise BridgeError(f"UI profile overlay requires observed selector for {key}")
    for key in ("account_menu_labels", "attachment_labels", "model_menu_labels",
                "send_labels", "thinking_menu_labels", "thinking_placeholder_labels",
                "upload_labels"):
        value = ui.get(key)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise BridgeError(f"UI profile overlay requires observed labels for {key}")
    if ui.get("control_layout") != "nested-slider":
        raise BridgeError("UI profile overlay requires the observed nested-slider layout")
    if ui.get("thinking_label_location") != "outer-menu":
        raise BridgeError("UI profile overlay requires the observed outer-menu thinking label")
    positions = ui.get("thinking_positions")
    if not isinstance(positions, dict) or not positions or any(
            not isinstance(k, str) or not k.strip() or isinstance(v, bool)
            or not isinstance(v, int) or v < 0 for k, v in positions.items()):
        raise BridgeError("UI profile overlay requires observed thinking_positions")


def _load_overlay_observation(overlay, job, handoff, prep):
    """Validate the small, known live observation that makes an overlay adoptable."""
    observation_path = Path(str(overlay.get("observation_artifact_path", ""))).expanduser()
    repo = Path(handoff["repo"]).resolve(strict=True)
    if (not observation_path.is_absolute() or observation_path.is_symlink()
            or not observation_path.resolve().is_relative_to(repo)
            or not observation_path.is_file()):
        raise BridgeError("UI profile observation artifact must be a repository-local regular file")
    if file_sha256(observation_path) != overlay.get("observation_artifact_sha256"):
        raise BridgeError("UI profile observation artifact digest drift")
    try:
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BridgeError("UI profile observation artifact is not strict UTF-8 JSON") from exc
    if observation.get("schema") != UI_PROFILE_OBSERVATION_SCHEMA:
        raise BridgeError("Unsupported UI profile observation schema")
    job_info = observation.get("job")
    identity = observation.get("identity")
    if (not isinstance(job_info, dict) or job_info.get("job_id") != job.data["job_id"]
            or job_info.get("bridge_thread_id") != handoff["bridge_thread_id"]
            or job_info.get("original_error") != UI_PROFILE_PREFLIGHT_ERROR):
        raise BridgeError("UI profile observation job or readiness drift")
    expected_url = _ui_overlay_expected_project_url(handoff)
    if (not isinstance(identity, dict) or identity.get("remote_project_id") != handoff.get("remote_project_id")
            or identity.get("project_url") != expected_url
            or identity.get("bootstrap") is not True
            or str(identity.get("expected_conversation_id", "")) != ""
            or identity.get("conversation_policy") != "create"):
        raise BridgeError("UI profile observation bootstrap identity drift")
    frozen = observation.get("frozen_prompt")
    prompt_path = Path(str(frozen.get("path", ""))).expanduser() if isinstance(frozen, dict) else None
    if (prompt_path is None or prompt_path.is_symlink() or not prompt_path.is_file()
            or not prompt_path.resolve().is_relative_to(repo)
            or frozen.get("sha256") != prep.get("prompt_sha256")
            or file_sha256(prompt_path) != frozen.get("sha256")):
        raise BridgeError("UI profile observation frozen prompt drift")
    ui = overlay.get("ui")
    if not isinstance(ui, dict) or observation.get("ui") != ui:
        raise BridgeError("UI profile observation UI map differs from overlay")
    send = observation.get("send_observation")
    if (not isinstance(send, dict) or not isinstance(send.get("accessible_name"), str)
            or not send["accessible_name"].strip() or send.get("count") != 1
            or send.get("type") != "submit" or send.get("disabled") is not False
            or send.get("aria_disabled") not in (None, "false")
            or send.get("same_form_as_composer") is not True or send.get("send_clicked") is not False
            or ui.get("send_labels") != [send["accessible_name"]]):
        raise BridgeError("UI profile observation lacks one verified Send control")
    model = observation.get("model")
    if (not isinstance(model, dict) or model.get("latest_checked_prior_receipt") is not True
            or model.get("outer_visible_label") != "6 Pro"
            or model.get("power_description") != "Pro, 5 of 5. Use Left and Right arrow keys to adjust power"
            or model.get("value") != 4 or ui.get("thinking_positions", {}).get("6 Pro") != 4):
        raise BridgeError("UI profile observation lacks the frozen 6 Pro target receipt")
    before = observation.get("before_draft")
    probe = observation.get("controlled_fill")
    after = observation.get("after_clear")
    if (not isinstance(before, dict) or before.get("composer_empty") is not True
            or before.get("attachment_count") != 0
            or not isinstance(probe, dict) or probe.get("semantic_draft_equal_frozen_prompt_stripped") is not True
            or probe.get("send_not_clicked") is not True
            or not isinstance(after, dict) or after.get("composer_empty") is not True
            or after.get("attachment_count") != 0 or after.get("send_count") != 0
            or after.get("no_new_conversation") is not True):
        raise BridgeError("UI profile observation controlled draft proof is incomplete")
    binding = observation.get("account_binding")
    profile = observation.get("profile")
    if (not isinstance(binding, dict) or not str(binding.get("workspace", "")).strip()
            or not str(binding.get("account_label", "")).strip()
            or not isinstance(profile, dict)
            or profile.get("account_workspace_label") not in {binding["workspace"], binding["account_label"]}):
        raise BridgeError("UI profile observation account binding proof is incomplete")
    source_receipts = observation.get("source_receipts")
    if (not isinstance(source_receipts, dict)
            or not str(source_receipts.get("target6pro", "")).strip()
            or not str(source_receipts.get("source_ui_labels", "")).strip()):
        raise BridgeError("UI profile observation is missing the approved raw UI receipts")
    return observation


def load_ui_profile_overlay(job, output, handoff):
    """Validate the one immutable same-job UI overlay and return its UI keys.

    The overlay is an in-memory adapter only. All identity, payload, route and
    digest fields remain anchored to the frozen job and handoff.
    """
    prep = job.data.get("preparation")
    if not (
        handoff.get("remote_project_id")
        and job.data.get("job_id")
        and job.data.get("state") == "blocked"
        and job.data.get("stage") == "prepared-materials"
        and job.data.get("browser_phase") == "connected"
        and job.data.get("error") == UI_PROFILE_PREFLIGHT_ERROR
        and job.data.get("may_resend") is False
        and isinstance(prep, dict)
        and prep.get("ready") is True
        and prep.get("sent") is False
        and not job.data.get("attempt_id")
        and not job.data.get("capture")
        and not job.data.get("result")
        and output.is_dir()
        and not output.is_symlink()
    ):
        raise BridgeError("UI profile overlay checkpoint is not an eligible prepared connected job")
    if active_attempt(Path(handoff["repo"]), handoff["bridge_thread_id"]):
        raise BridgeError("UI profile overlay cannot resume with an active canonical attempt")
    try:
        expected_files = {
            Path(prep[key]).resolve(strict=True)
            for key in ("bundle", "prompt_file", "materials_file")
        }
        if prep.get("packaging_receipt"):
            receipt = Path(prep["packaging_receipt"])
            expected_files.update((receipt.resolve(strict=True), receipt.with_suffix(".staging.json").resolve(strict=True)))
        if job.data.get("snapshot_input_receipt"):
            input_receipt = Path(job.data["snapshot_input_receipt"]["path"])
            expected_files.add(input_receipt.resolve(strict=True))
            if input_receipt.with_suffix(".handoff.json").exists():
                expected_files.add(input_receipt.with_suffix(".handoff.json").resolve())
        actual = {path.resolve(strict=True) for path in output.iterdir()}
    except (KeyError, OSError):
        raise BridgeError("UI profile overlay preparation artifacts are not readable")
    overlay_path = output / UI_PROFILE_OVERLAY_FILE
    if overlay_path.is_symlink() or not overlay_path.is_file() or actual != expected_files | {overlay_path.resolve()}:
        raise BridgeError("UI profile overlay output contains unexpected upload/draft/control artifacts")
    try:
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("UI profile overlay is not valid UTF-8 JSON") from exc
    if not isinstance(overlay, dict) or overlay.get("schema_version") != UI_PROFILE_OVERLAY_SCHEMA:
        raise BridgeError("Unsupported UI profile overlay schema")
    if overlay.get("job_id") != job.data["job_id"] or overlay.get("bridge_thread_id") != handoff["bridge_thread_id"]:
        raise BridgeError("UI profile overlay belongs to another job or Bridge Thread")
    if overlay.get("project_url") != _ui_overlay_expected_project_url(handoff):
        raise BridgeError("UI profile overlay Project URL drift")
    if overlay.get("bootstrap") is not True or str(overlay.get("expected_conversation_id", "__missing__")) != "":
        raise BridgeError("UI profile overlay conversation-create policy drift")
    if overlay.get("original_error") != UI_PROFILE_PREFLIGHT_ERROR:
        raise BridgeError("UI profile overlay original error drift")
    if overlay.get("handoff_sha256") != job.data.get("handoff_sha256"):
        raise BridgeError("UI profile overlay handoff digest drift")
    if overlay.get("request_sha256") != handoff.get("request_sha256"):
        raise BridgeError("UI profile overlay request digest drift")
    config_path = job.directory / "config.json"
    if overlay.get("base_config_sha256") != job.data.get("config_sha256") or file_sha256(config_path) != job.data.get("config_sha256"):
        raise BridgeError("UI profile overlay base config digest drift")
    if overlay.get("package_sha256") != prep.get("bundle_sha256") or file_sha256(Path(prep["bundle"])) != prep.get("bundle_sha256"):
        raise BridgeError("UI profile overlay package digest drift")
    staging = prep.get("staging")
    if not isinstance(staging, dict) or overlay.get("staging_sha256") != staging.get("source_sha256"):
        raise BridgeError("UI profile overlay staging digest drift")
    for key in ("source_path", "staged_execution_path"):
        if key in staging and file_sha256(Path(staging[key])) != staging.get("source_sha256"):
            raise BridgeError("UI profile overlay staged artifact digest drift")
    _validate_overlay_ui(overlay.get("ui"))
    observation = _load_overlay_observation(overlay, job, handoff, prep)
    if overlay.get("account_binding") != observation.get("account_binding"):
        raise BridgeError("UI profile overlay account binding drift")
    return overlay


def ui_profile_resume_allowed(job, output, handoff):
    try:
        load_ui_profile_overlay(job, output, handoff)
    except (BridgeError, OSError, KeyError, ValueError):
        return False
    return True


def load_adopted_ui_profile_overlay(job, handoff):
    """Revalidate an already-adopted overlay for Send/capture recovery."""
    receipt = job.data.get("ui_profile_overlay_receipt")
    if not isinstance(receipt, dict):
        return None
    overlay_path = Path(str(receipt.get("overlay_path", ""))).expanduser()
    output = Path(handoff["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
    if (not overlay_path.is_absolute() or overlay_path.is_symlink() or not overlay_path.is_file()
            or not overlay_path.resolve().is_relative_to(output.resolve())):
        raise BridgeError("Adopted UI profile overlay path is not the frozen job artifact")
    if file_sha256(overlay_path) != receipt.get("overlay_sha256"):
        raise BridgeError("Adopted UI profile overlay digest drift")
    try:
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BridgeError("Adopted UI profile overlay is not strict UTF-8 JSON") from exc
    if overlay.get("schema_version") != UI_PROFILE_OVERLAY_SCHEMA:
        raise BridgeError("Unsupported adopted UI profile overlay schema")
    if overlay.get("job_id") != job.data["job_id"] or overlay.get("bridge_thread_id") != handoff["bridge_thread_id"]:
        raise BridgeError("Adopted UI profile overlay belongs to another job or Bridge Thread")
    if overlay.get("project_url") != _ui_overlay_expected_project_url(handoff):
        raise BridgeError("Adopted UI profile overlay Project URL drift")
    if overlay.get("bootstrap") is not True or str(overlay.get("expected_conversation_id", "__missing__")) != "":
        raise BridgeError("Adopted UI profile overlay conversation-create policy drift")
    prep = job.data.get("preparation")
    _validate_overlay_ui(overlay.get("ui"))
    observation = _load_overlay_observation(overlay, job, handoff, prep)
    if overlay.get("account_binding") != observation.get("account_binding"):
        raise BridgeError("Adopted UI profile overlay account binding drift")
    if (receipt.get("observation_artifact_path") != overlay.get("observation_artifact_path")
            or receipt.get("observation_artifact_sha256") != overlay.get("observation_artifact_sha256")):
        raise BridgeError("Adopted UI profile observation receipt drift")
    return overlay


def apply_ui_profile_overlay(config, overlay):
    """Merge only observed UI selectors into an in-memory Browser config."""
    merged = dict(config)
    merged["ui"] = {**config["ui"], **overlay["ui"]}
    return merged


def uploaded_resume_allowed(job, output):
    """Allow only a read-only resume after one verified upload and before any Send.

    The failure happened after the completed DevTools upload, so the frozen
    receipts are the evidence: nothing is replayed, re-bundled, or re-derived.
    Anything unproven here stays blocked for inspection.
    """
    prep = job.data.get("preparation")
    if not (job.data.get("state") == "blocked"
            and job.data.get("stage") == "uploaded"
            and job.data.get("browser_phase") == "connected"
            and not job.data.get("attempt_id")
            and not job.data.get("capture")
            and isinstance(prep, dict)
            and prep.get("ready") is True
            and isinstance(prep.get("staging"), dict)
            and output.is_dir() and not output.is_symlink()):
        return False
    try:
        artifacts = [Path(prep[key]).resolve(strict=True) for key in ("bundle", "prompt_file")]
        artifacts += [Path(prep["staging"][key]).resolve(strict=True)
                      for key in ("source_path", "staged_execution_path")]
        artifacts += [(output / name).resolve(strict=True) for name in (
            "model-controls.json", "upload-plan.json",
            "upload-result.transport.json", "upload-result.json")]
    except (KeyError, OSError):
        return False
    return all(path.is_file() for path in artifacts)


def verify_completed_upload(output, prep, browser):
    """Re-verify one finished upload from its own artifacts and the live tab.

    Read-only: the upload is never replayed. The recorded plan, transport, and
    result receipts must agree with the frozen staged bundle and with the exact
    page, owner, and conversation that is about to be used.
    """
    from devtools_upload import validate_upload_action_plan, verify_upload_result
    staging = prep["staging"]
    for key in ("source_path", "staged_execution_path"):
        if file_sha256(Path(staging[key])) != staging["source_sha256"]:
            raise BridgeError("Frozen staged file digest drift")
    if staging["source_sha256"] != prep.get("bundle_sha256"):
        raise BridgeError("Frozen staging digest disagrees with the prepared bundle")
    if file_sha256(Path(prep["bundle"])) != prep.get("bundle_sha256"):
        raise BridgeError("Prepared bundle digest drift")
    plan = validate_upload_action_plan(
        json.loads((output / "upload-plan.json").read_text(encoding="utf-8")))
    transport = json.loads((output / "upload-result.transport.json").read_text(encoding="utf-8"))
    recorded = json.loads((output / "upload-result.json").read_text(encoding="utf-8"))
    if (transport.get("status") != "accepted" or transport.get("plan") != plan
            or plan["windows_path"] != staging["staged_browser_path"]
            or plan["staged_sha256"] != staging["source_sha256"]
            or plan["expected_attachment_name"] != staging["attachment_name"]):
        raise BridgeError("Completed upload receipts disagree with the frozen staged bundle")
    try:
        _, recorded_conversation = conversation_identity_from_url(str(transport.get("url", "")))
        _, live_conversation = conversation_identity_from_url(browser.url)
    except BridgeError as exc:
        # A bootstrap round has no conversation until Send; this resume only
        # covers an exact, already-conversation round.
        raise BridgeError(
            "Completed upload is not pinned to one exact conversation; inspection required") from exc
    if (transport.get("page_id") != str(browser.page_id) or transport.get("owner") != browser.owner
            or recorded_conversation != live_conversation):
        raise BridgeError("Completed upload receipts belong to another page, owner, or conversation")
    expected = verify_upload_result(plan, {"status": "accepted", "attachment_chip": True,
                                           "attachment_name": staging["attachment_name"]})
    if recorded != expected:
        raise BridgeError("Completed upload result receipt is not the verified receipt for its plan")
    if not browser.attachment_ready(staging["attachment_name"]):
        raise BridgeError("Composer does not hold the unique ready attachment of the completed upload")
    return recorded


def resumed_model_controls(control_path, h, browser):
    """Reuse the saved pre-upload control observation instead of replaying it.

    Re-running the control UI would rewrite the frozen observation, and the
    nested-slider model label is only visible inside an opened menu. The saved
    transaction is re-validated and must still describe this exact tab and
    handoff; the Pre-Send gate keeps checking the same receipt.
    """
    from model_controls import validate_model_control_trace
    control = validate_model_control_trace(
        json.loads(control_path.read_text(encoding="utf-8")))
    _, recorded_conversation = conversation_identity_from_url(control["observed_page_url"])
    _, live_conversation = conversation_identity_from_url(browser.url)
    if (control["page_id"] != str(browser.page_id) or control["tab_owner_token"] != browser.owner
            or recorded_conversation != live_conversation):
        raise BridgeError("Saved model-control receipt belongs to another page, owner, or conversation")
    for field in ("requested_model", "model_selection_kind", "requested_thinking_intensity"):
        if control[field] != h[field]:
            raise BridgeError(f"Saved model-control receipt does not match this handoff: {field}")
    return control


def run_helper(script, args, repo, *, json_output=True):
    result = subprocess.run([sys.executable, str(script), *map(str, args)], cwd=repo,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=180)
    if result.returncode:
        raise BridgeError(f"{script.name} failed: {(result.stderr or result.stdout)[-6000:]}")
    return json.loads(result.stdout) if json_output else result.stdout.strip()


def clipboard_baseline(reader, capture_error_type):
    """Permit only a proven empty clipboard before Copy; preserve real failures."""
    try:
        return reader()
    except capture_error_type as exc:
        if str(exc) == "Windows clipboard is empty":
            return ""
        raise


def session_conversation_id(meta):
    """Resolve the conversation pinned by GPT Pro session metadata.

    `save_bridge_turn` records the canonical `web_conversation_url`. Older
    aliases stay readable, but metadata naming two different conversations must
    fail instead of silently degrading into a new-conversation bootstrap claim.
    """
    pinned = []
    for key in ("web_conversation_url", "web_url"):
        value = str(meta.get(key, "") or "").strip()
        if not value:
            continue
        try:
            _, conversation_id = conversation_identity_from_url(value)
        except BridgeError as exc:
            raise BridgeError(f"GPT Pro session metadata {key} is unusable: {exc}") from exc
        pinned.append((key, conversation_id))
    reserved = str(meta.get("expected_conversation_id", "") or "").strip()
    if reserved:
        pinned.append(("expected_conversation_id", reserved))
    if len({conversation_id for _, conversation_id in pinned}) > 1:
        raise BridgeError(
            "GPT Pro session metadata pins conflicting conversations: "
            + ", ".join(f"{key}={conversation_id!r}" for key, conversation_id in pinned)
        )
    return pinned[0][1] if pinned else ""


def frozen_bootstrap_conversation_id(metadata_conversation_id, overlay):
    """Keep a create overlay pinned to its frozen empty conversation identity."""
    frozen = str(overlay.get("expected_conversation_id", ""))
    if frozen or overlay.get("bootstrap") is not True:
        raise BridgeError("UI profile overlay cannot alter the frozen bootstrap conversation policy")
    if metadata_conversation_id != frozen:
        raise BridgeError("Frozen bootstrap overlay conflicts with mutable session metadata")
    return frozen


def resolve(browser, repo, h, claim, *, allow_prepare):
    for _ in range(4):
        pages = browser.pages()
        owners = browser.owners(pages)
        result = resolve_owned_tab(repo, claim_token=claim["token"], thread_id=h["bridge_thread_id"],
            browser_profile=claim["browser_profile"], expected_project_id=h.get("remote_project_id", ""),
            pages=pages, owners=owners)
        action = result["action"]
        if action in {"reuse-owned-tab", "promote-ready"}:
            browser.page_id, browser.url = result["page_id"], result["url"]
            browser.owner = result["tab_owner_token"]
            return result, pages, owners
        if allow_prepare and action == "open-canonical-tab":
            browser.call("new_page", url=result["canonical_url"])
            continue
        if allow_prepare and action in {"set-owner-token", "replace-stale-owner-token", "replace-legacy-owner-token"}:
            browser.page_id = result["page_id"]
            # Compare-and-set against the exact observation; never steal a tab.
            browser.evaluate("() => { const key = " + json.dumps(TAB_OWNER_STORAGE_KEY) +
                "; if (location.href !== " + json.dumps(result["url"]) +
                " || (sessionStorage.getItem(key) || '') !== " +
                json.dumps(result.get("previous_tab_owner_token", "")) +
                ") throw Error('Owner changed'); sessionStorage.setItem(key, " +
                json.dumps(result["tab_owner_token"]) + "); return true; }")
            continue
        raise BridgeError(f"Tab resolution HOLD: {result}")
    raise BridgeError("Tab resolution did not stabilize within its action budget")


def assert_bootstrap_resolution(result, handoff):
    """Require the exact handoff Project home before any composer/control action."""
    expected_url = _ui_overlay_expected_project_url(handoff) or "https://chatgpt.com/"
    if (result.get("action") != "reuse-owned-tab"
            or result.get("url") != expected_url
            or result.get("canonical_url") not in (None, expected_url)
            or result.get("matching_page_count") != 1
            or result.get("owner_selected_count") != 1):
        raise BridgeError("Bootstrap resolution is not the unique exact canonical Project home or standalone home")


def bootstrap_preflight_observation(browser, result, handoff, *, bootstrap):
    """Perform the side-effect-free bootstrap gate before account/model/upload/fill."""
    if result.get("action") != "reuse-owned-tab":
        raise BridgeError("Existing bootstrap conversation requires recovery")
    if not bootstrap:
        return None
    assert_bootstrap_resolution(result, handoff)
    observed = browser.messages()
    if observed.get("messages"):
        raise BridgeError("Bootstrap page unexpectedly contains conversation messages")
    return observed


def submission_candidate(observation, attempt):
    """Find one user message after the boundary, without trusting rendered text."""
    if observation["owner"] != attempt["tab_owner_token"]:
        raise BridgeError("Submitted page owner mismatch")
    project, conversation = conversation_identity_from_url(observation["url"])
    if not project_url_matches(project, attempt["project_id"]):
        raise BridgeError("Submitted conversation Project mismatch")
    if attempt["conversation_id"] and attempt["conversation_id"] != conversation:
        raise BridgeError("Submitted conversation changed")
    messages = observation["messages"]
    boundary = attempt["pre_submit_boundary"]
    start = 0
    if boundary != "new-conversation":
        found = [i for i, msg in enumerate(messages) if msg["id"] == boundary]
        if len(found) != 1:
            raise BridgeError("Pre-Send boundary is not uniquely visible")
        start = found[0] + 1
    users = [msg for msg in messages[start:] if msg["role"] == "user"]
    if len(users) != 1:
        raise BridgeError("Exact submitted prompt is not uniquely visible; no resend")
    if not users[0]["id"]:
        raise BridgeError("Submitted user turn lacks a stable ID")
    return users[0]


def observed_submission(observation, attempt):
    user = submission_candidate(observation, attempt)
    if user["text"].strip() != attempt["prompt"].strip():
        raise BridgeError("Exact submitted prompt is not uniquely visible; no resend")
    return user["id"]


def verify_copied_submission(browser, observation, attempt, output, config):
    """Rendered Markdown is lossy; Copy message must match the frozen raw text."""
    from bridge_store import atomic_write_text
    user = submission_candidate(observation, attempt)
    browser.url = observation["url"]
    with file_lock(Path(config["state_dir"]) / "clipboard.lock"):
        proof = browser.copy_with_proof(user["id"], "user")
        copied = proof["text"]
        browser.assert_identity()
        fresh = submission_candidate(browser.messages(), attempt)
        from .submission_text import copied_prompt_codec
        codec = copied_prompt_codec(copied, attempt["prompt"], literal_user_text=proof.get("literal_user_text") is True)
        if fresh["id"] != user["id"] or codec is None:
            diagnostic = output / ("copied-user-mismatch-" + digest(copied)[:16] + ".md")
            if not diagnostic.exists():
                atomic_write_text(diagnostic, copied)
            raise BridgeError("Copied user message differs from frozen prompt; no resend")
        path = output / "copied-user-prompt.md"
        if path.exists() and path.read_text(encoding="utf-8") != copied:
            raise BridgeError("Copied user prompt artifact drift")
        atomic_write_text(path, copied)
        write_json(output / "submission-copy.json", {"remote_turn_id": user["id"],
            "conversation_url": browser.url, "tab_owner_token": browser.owner,
            "prompt_sha256": attempt["prompt_sha256"], "copy_sha256": file_sha256(path),
            "copy_provenance": proof["provenance"], "comparison_codec": codec})
        return user["id"]


def identity_args(browser, claim, resolved, pages, owners):
    return ["--browser-lease-token", claim["token"], "--browser-profile", claim["browser_profile"],
        "--observed-page-url", browser.url, "--matching-page-count", resolved["matching_page_count"],
        "--pages-json", json.dumps(pages), "--owners-json", json.dumps(owners),
        "--observed-page-id", browser.page_id, "--snapshot-page-id", browser.page_id,
        "--observed-tab-owner-token", browser.owner]


def cleanup(job):
    staging = job.data.get("preparation", {}).get("staging")
    if staging and not job.data.get("staging_cleaned"):
        path = Path(staging["staged_execution_path"])
        if not path.exists() and not path.is_symlink():
            # A crash can occur after deletion but before its receipt. Observe
            # absence explicitly; do not invent a successful deletion receipt.
            job.update(staging_cleaned={"status": "already-absent", "cleaned": False,
                       "path": str(path), "expected_sha256": staging["source_sha256"]})
            return
        receipt = cleanup_staged_file(staging["staged_execution_path"], expected_sha256=staging["source_sha256"])
        job.update(staging_cleaned=receipt)


def capture(browser, attempt, output, config):
    if config.get("capture_route") == "browser-page-serialized":
        from capture_provenance import freeze_page_serialization, validate_page_serialization
        from bridge_store import atomic_write_text
        event = browser.evaluate(browser_wait_script(attempt, browser.url)["function"])
        if event["status"] != "ready-for-capture":
            raise BridgeError("Pinned answer is not ready for raw serialization")
        proof = freeze_page_serialization(browser.page_serialization(attempt, event["assistant_turn_id"]),
            attempt=attempt, prompt=attempt["prompt"], page_id=browser.page_id, owner=browser.owner, url=browser.url)
        validated = validate_page_serialization(proof, attempt=attempt, prompt=attempt["prompt"])
        if validated["assistant_turn_id"] != event["assistant_turn_id"]:
            raise BridgeError("Raw serialization assistant differs from pinned wait result")
        browser.assert_identity()
        proof_path = output.with_suffix(".serialization.json")
        proof_text = json.dumps(proof, ensure_ascii=False, sort_keys=True) + "\n"
        for path, value in ((output, validated["answer"]), (proof_path, proof_text)):
            if path.exists() and path.read_bytes().decode("utf-8") != value:
                raise BridgeError("Immutable raw serialization artifact drift")
            if not path.exists():
                atomic_write_text(path, value)
        return {key: value for key, value in {**validated, "answer_path": str(output),
                "capture_proof_path": str(proof_path), "capture_proof_sha256": file_sha256(proof_path)}.items()
                if key != "answer"}
    from capture_copied_reply import markdown_metrics
    # The clipboard is a host-wide shared resource even across separate tabs.
    with file_lock(Path(config["state_dir"]) / "clipboard.lock"):
        event = browser.evaluate(browser_wait_script(attempt, browser.url)["function"])
        if event["status"] != "ready-for-capture":
            raise BridgeError(f"Answer changed before capture: {event}")
        assistant_id = event["assistant_turn_id"]
        try:
            source_before = browser.copied_link_source(attempt, assistant_id)
        except BridgeError as exc:
            # Source probing is supplemental.  Preserve the legacy DOM equality
            # route for equal link counts, while retaining a concrete HOLD reason
            # if a mismatch later needs the new source proof.
            source_before = {"status": "unsupported", "reason": "source-observation-error",
                             "error": str(exc)}
        source_supported = source_before.get("status") == "available"
        metrics = browser.evaluate("() => {" + message_dom_source() + " const id = " + json.dumps(assistant_id) + "; " + r'''
            const record = bridgePinnedMessage(id, 'assistant');
            const body = bridgeMessageBody(record), button = bridgeCopyButton(record);
            if (!body || !button || button.disabled || !button.getClientRects().length) throw Error('Copy reply unavailable');
            // KaTeX may mark its own root inside ChatGPT's role=math wrapper; keep only the
            // outermost role=math so one expression counts once, then classify it by whether a
            // .katex-display is an ancestor/self or a descendant.
            const math = [...body.querySelectorAll('[role="math"], [data-math-source]')]
              .filter(n => !n.parentElement?.closest('[role="math"], [data-math-source]'));
            const display = math.filter(n => n.getAttribute('data-math-display') === 'true' ||
              !!n.closest('.katex-display') || !!n.querySelector('.katex-display')).length;
            // A code block may nest a second pre (e.g. CodeMirror); count only the outermost one.
            const codeSelector = 'pre, [data-markdown-copy="code-block"]';
            const codeBlocks = [...body.querySelectorAll(codeSelector)]
              .filter(n => !n.parentElement?.closest(codeSelector)).length;
            // Sandbox file downloads render as icon-bearing buttons instead of anchors; a button
            // already inside an anchor is part of that anchor, so it is not a second link.
            const sandboxLinks = [...body.querySelectorAll('button')]
              .filter(n => !n.closest('a') && n.querySelector('[data-testid="library-file-icon"]')).length;
            // New UI renders one file link as a resource row with separate
            // preview/download buttons. Count the row, not both controls.
            const resourceLinks = [...body.querySelectorAll('[class~="group/resource-row"]')]
              .filter(n => !n.closest('a') && n.querySelector('[title]') &&
                n.querySelector('button[aria-label="下载文件"],button[aria-label="Download file"]') &&
                !n.querySelector('[data-testid="library-file-icon"]')).length;
            return {headings: body.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
              display_math: display, inline_math: math.length - display,
              tables: body.querySelectorAll('table').length, code_blocks: codeBlocks,
              links: body.querySelectorAll('a').length + sandboxLinks + resourceLinks};
        }''')
        proof = browser.copy_with_proof(assistant_id, "assistant")
        text = proof["text"]
        browser.assert_identity()
        after = browser.evaluate(browser_wait_script(attempt, browser.url)["function"])
        if after["status"] != "ready-for-capture" or after["assistant_turn_id"] != assistant_id:
            raise BridgeError("Pinned answer changed after Copy")
        actual = markdown_metrics(text)
        non_link_mismatches = {
            key: {"expected": expected, "actual": actual[key]}
            for key, expected in metrics.items() if key != "links" and actual[key] != expected
        }
        if non_link_mismatches:
            raise BridgeError("Copied Markdown structure differs from pinned answer: "
                              + json.dumps({"expected": metrics, "actual": actual}, sort_keys=True))
        source_proof = None
        links_mismatch = actual["links"] != metrics["links"]
        if links_mismatch and source_supported:
            submission_copy_path = output.parent / "submission-copy.json"
            if not submission_copy_path.is_file():
                raise BridgeError("Copied-link source requires the saved submission-copy artifact")
            try:
                submission_copy = json.loads(submission_copy_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise BridgeError("Saved submission-copy artifact is unreadable") from exc
            source_after = browser.copied_link_source(attempt, assistant_id)
            if source_after.get("status") != "available":
                raise BridgeError("Copied-link source disappeared after Copy; HOLD")
            source_proof = build_source_link_proof(
                source_before, source_after, text, assistant_id=assistant_id,
                user_id=attempt["remote_turn_id"], expected_url=browser.url,
                expected_owner=browser.owner, expected_page_id=browser.page_id,
                structural_metrics=metrics, copied_metrics=actual,
                expected_prompt=attempt["prompt"], submission_copy=submission_copy,
            )
        elif links_mismatch:
            # Preserve the legacy DOM equality route exactly; only the known
            # new-UI source proof may explain a structural/raw link-domain split.
            reason = source_before.get("reason", "source-observation-unavailable")
            raise BridgeError("Copied Markdown link mismatch requires complete source proof: "
                              + json.dumps({"expected": metrics, "actual": actual,
                                            "source_reason": reason}, sort_keys=True))
        from bridge_store import atomic_write_text
        source_proof_path = output.with_suffix(".source-links.json") if source_proof is not None else None
        if source_proof_path is not None:
            source_proof_text = json.dumps(source_proof, ensure_ascii=False, sort_keys=True) + "\n"
            if source_proof_path.exists() and source_proof_path.read_text(encoding="utf-8") != source_proof_text:
                raise BridgeError("Immutable copied-link source proof drift")
            if not source_proof_path.exists():
                atomic_write_text(source_proof_path, source_proof_text)
        if output.exists() and output.read_text(encoding="utf-8") != text:
            raise BridgeError("Immutable answer already exists with different contents")
        atomic_write_text(output, text)
        receipt = {"answer_path": str(output), "answer_sha256": file_sha256(output),
                   "assistant_turn_id": assistant_id, "completed_at": event["observed_at"],
                   "copy_provenance": proof["provenance"],
                   "source_link_status": "available" if source_proof is not None else "unsupported"}
        if source_proof_path is not None:
            receipt.update({"source_link_proof_path": str(source_proof_path),
                            "source_link_proof_sha256": file_sha256(source_proof_path),
                            "structural_link_count": source_proof["structural_link_count"],
                            "source_link_count": source_proof["source_link_count"],
                            "copied_link_count": source_proof["copied_link_count"],
                            "source_link_source_sha256": source_proof["source_sha256"]})
        return receipt


def execute(job, config):
    started = time.monotonic()
    h, request = validate_inputs(job.data["handoff_path"], job.data["handoff_sha256"], config)
    repo, thread = Path(h["repo"]), h["bridge_thread_id"]
    attempt = read_attempt(repo, thread, job.data["attempt_id"]) if job.data.get("attempt_id") else active_attempt(repo, thread)
    recovery = attempt is not None
    from types import SimpleNamespace
    checkpoint = job.data.get("resume_checkpoint") or {}
    if checkpoint.get("request_id") != job.data.get("continuation", {}).get("request_id"):
        checkpoint = {}
    recovery_view = SimpleNamespace(
        data={**job.data, "state": checkpoint.get("state", job.data.get("state")),
              "error": checkpoint.get("error", job.data.get("error"))},
        directory=job.directory,
    )
    output = Path(h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
    from prepare_review import admit_request, verify_packaging
    if not recovery:
        admit_request(request, stage=h.get("attachment_policy") == "bundle")
    packaging_path = output / "packaging.json"
    packaging_resume = (not recovery and job.data.get("stage") == "preparing"
                        and not job.data.get("preparation") and not job.data.get("claim_token")
                        and not job.data.get("browser_phase") and packaging_path.is_file())
    if packaging_resume:
        verify_packaging(json.loads(packaging_path.read_text(encoding="utf-8")),
                         Path(h["request_file"]), request)
    preparing_resume = (
        not recovery
        and recovery_view.data.get("state") == "blocked"
        and job.data.get("stage") == "preparing"
        and bool(recovery_view.data.get("error"))
        and not job.data.get("attempt_id")
        and not job.data.get("preparation")
        and not job.data.get("claim_token")
        and job.data.get("browser_phase") is None
        and (not output.exists() or (output.is_dir() and {p.name for p in output.iterdir()} <= {"snapshot-inputs.json","snapshot-inputs.handoff.json"}))
    )
    identity_preflight_resume = (
        not recovery and identity_preflight_resume_allowed(recovery_view, output, h)
    )
    ui_profile_resume = (
        not recovery and ui_profile_resume_allowed(recovery_view, output, h)
    )
    ui_profile_overlay = (
        load_ui_profile_overlay(recovery_view, output, h) if ui_profile_resume else None
    )
    adopted_ui_profile_overlay = (
        load_adopted_ui_profile_overlay(recovery_view, h)
        if job.data.get("ui_profile_overlay_receipt") else None
    )
    upload_resume = (not recovery and job.data["stage"] == "upload-started"
                     and (output / "upload-result.transport.json").is_file())
    uploaded_resume = not recovery and uploaded_resume_allowed(recovery_view, output)
    connection_resume = (not recovery and job.data["stage"] == "prepared-materials"
                         and job.data.get("browser_phase") in (None, "acquiring-claim", "connecting-browser"))
    if recovery and attempt["prompt_sha256"] != job.data.get("preparation", {}).get("prompt_sha256"):
        raise BridgeError("Unresolved attempt is not owned by this job")
    if not recovery and (h["mode"] == "recover-only" or
                         (job.data["stage"] != "queued" and not preparing_resume and not packaging_resume
                          and not identity_preflight_resume
                          and not ui_profile_resume
                          and not connection_resume and not upload_resume
                          and not uploaded_resume)):
        raise BridgeError("Interrupted pre-Send work requires inspection; upload/draft actions will not be replayed")
    if recovery and attempt["state"] == "prepared":
        raise BridgeError("Interrupted prepared attempt requires fresh preflight; no automatic Send")
    if recovery and attempt["state"] == "captured":
        finish(job, repo, attempt)
        return
    output = Path(h["expected_output_dir"]) / ("runtime-" + job.data["job_id"][:16])
    output.mkdir(parents=True, exist_ok=True)
    project_id = h.get("remote_project_id", "")
    store = BridgeProjectStore(repo)
    binding = store.load_binding(h["bridge_project_id"]) if project_id else {}
    if project_id and (binding.get("status") != "active" or binding.get("remote_project_id") != project_id):
        raise BridgeError("Fast path requires verified Project binding; run Project verification before dispatch")
    effective_overlay = ui_profile_overlay or adopted_ui_profile_overlay
    if project_id and effective_overlay:
        observed_binding = effective_overlay.get("account_binding", {})
        if (observed_binding.get("workspace") != binding.get("workspace")
                or observed_binding.get("account_label") != binding.get("account_label")):
            raise BridgeError("UI profile overlay account binding differs from Project binding")
    if project_id:
        report = store.verify(h["bridge_project_id"])
        if report["project_status"] != "active" or store.project_for_thread(thread) != h["bridge_project_id"]:
            raise BridgeError("Thread must belong to the active Project")
        if report["unsynced_source_count"] or not report["inventory_verified"]:
            raise BridgeError("Project Sources require reconciliation before dispatch")
    if not recovery and not identity_preflight_resume and not ui_profile_resume:
        notes_args = ["--repo", repo, "--bridge-thread-id", thread, "--goal", request["goal"],
                      "--summary-file", request["notes"], "--gpt-pro-question", request["question"],
                      "--round-request-file", h["request_file"], "--input-receipt-out", output/"snapshot-inputs.json",
                      "--round-handoff-file", job.data["handoff_path"],
                      "--round-key", job.data["job_id"]]
        if h.get("bridge_project_id"):
            notes_args += ["--bridge-project-id", h["bridge_project_id"]]
        run_helper(SKILLS / "bundle-algorithm-context/scripts/prepare_codex_session_notes.py", notes_args,
                   repo, json_output=False)
        snapshot_receipt = output/"snapshot-inputs.json"
        job.update(snapshot_input_receipt={"path":str(snapshot_receipt),"sha256":file_sha256(snapshot_receipt)})
    if not recovery and not identity_preflight_resume and not ui_profile_resume and not connection_resume and not upload_resume \
            and not uploaded_resume:
        if preparing_resume:
            job.data.pop("error", None)
        job.update(state="running", stage="preparing")
        args = ["--request", h["request_file"], "--packaging-receipt", packaging_path]
        if packaging_resume:
            args += ["--reuse-packaging"]
        if h["attachment_policy"] == "bundle":
            args += ["--stage", "--out", output / "bundle.zip"]
        prep = run_helper(SCRIPTS / "prepare_review.py", args, repo)
        if not prep.get("ready"):
            raise BridgeError("Preparation failed")
        prep["packaging_receipt"] = str(packaging_path)
        job.update(preparation=prep, stage="prepared-materials")
    prep = job.data["preparation"]
    if not recovery:
        from prepare_review import verify_preparation
        prompt_evidence = verify_preparation(prep, Path(h["request_file"]), request,packaging_receipt_path=packaging_path)
        job.update(preparation_prompt_codec=prompt_evidence["codec"])
    if identity_preflight_resume or ui_profile_resume or uploaded_resume:
        job.update(error="")
    if ui_profile_resume:
        overlay_path = output / UI_PROFILE_OVERLAY_FILE
        job.update(ui_profile_overlay_receipt={
            "overlay_path": str(overlay_path.resolve()),
            "overlay_sha256": file_sha256(overlay_path),
            "observation_artifact_path": ui_profile_overlay["observation_artifact_path"],
            "observation_artifact_sha256": ui_profile_overlay["observation_artifact_sha256"],
        })
    prompt = attempt["prompt"] if recovery else prompt_evidence["prompt"]
    if digest(prompt) != prep["prompt_sha256"]:
        raise BridgeError("Prepared prompt digest drift")
    meta = parse_metadata(bridge_root(repo) / "gpt-pro-sessions" / default_gpt_session_id(thread) / "session.md")
    # A recovered attempt stays authoritative on its recorded conversation; only a
    # fresh round takes the pinned conversation from the saved session metadata.
    metadata_conversation_id = session_conversation_id(meta)
    conversation_id = attempt["conversation_id"] if recovery else metadata_conversation_id
    if ui_profile_resume:
        conversation_id = frozen_bootstrap_conversation_id(metadata_conversation_id, ui_profile_overlay)
    profile = config.get("browser_profile", DEFAULT_BROWSER_PROFILE)
    holder = "bridge-mcp-" + job.data["job_id"][:24]
    # Reuse a surviving short claim when the process died. Otherwise reacquire
    # through the canonical registry, which restores the durable owner token.
    claim = None
    if job.data.get("claim_token") and not ui_profile_resume:
        try:
            claim = assert_browser_lease_held(repo, token=job.data["claim_token"])
        except BridgeError:
            pass
    if not claim:
        # This phase precedes every browser action. Contention can be resolved
        # externally without forcing a fresh bundle or a new job on resume.
        job.update(browser_phase="acquiring-claim")
        claim = acquire_browser_lease(repo, holder=holder, thread_id=thread,
            expected_conversation_id=conversation_id, expected_remote_project_id=project_id,
            browser_profile=profile, bootstrap=not bool(conversation_id))
    job.update(state="running", claim_token=claim["token"], browser_phase="connecting-browser")
    env = {**os.environ, **config.get("browser_env", {})}
    with (job.directory / "browser.log").open("ab") as log:
        client = None
        try:
            if config.get("browser_transport", "stdio") == "persistent":
                from .connection_service import SharedClient
                client = SharedClient(config)
            else:
                client = Client(config["browser_command"], env=env, stderr=log,
                                timeout=config.get("browser_tool_timeout_seconds", 90),
                                connect_timeout=config.get("browser_connect_timeout_seconds", 300))
            effective_overlay = ui_profile_overlay or adopted_ui_profile_overlay
            browser_config = apply_ui_profile_overlay(config, effective_overlay) if effective_overlay else config
            browser = Browser(client, browser_config["ui"])
            resolved, pages, owners = resolve(browser, repo, h, claim,
                allow_prepare=not recovery and not upload_resume and not uploaded_resume)
            job.update(browser_phase="connected")
            if not recovery:
                bootstrap_observed = bootstrap_preflight_observation(
                    browser, resolved, h, bootstrap=claim.get("bootstrap", False))
                draft = browser.composer_text()
                # A failed read-back after fill may leave precisely our frozen
                # draft. Adopt it only in verified pre-Send uploaded recovery;
                # never overwrite a different draft or fill the same one again.
                adopt_uploaded_draft = uploaded_resume and bool(draft) and draft == prompt.strip()
                if (draft and not adopt_uploaded_draft) or (not upload_resume and not uploaded_resume
                                                            and browser.attachment_count()):
                    raise BridgeError("Existing draft or attachments block dispatch")
                if upload_resume:
                    browser.recover_upload(prep["staging"], output / "upload-plan.json",
                                           output / "upload-result.json", write_json)
                if uploaded_resume:
                    verify_completed_upload(output, prep, browser)
                labels = browser.read_labels(("workspace", "account")) if project_id else {}
                if project_id and (labels["workspace"] != binding["workspace"] or labels["account"] != binding["account_label"]):
                    raise BridgeError("Observed account/workspace differs from Project binding")
                control_path = output / "model-controls.json"
                if uploaded_resume:
                    control = resumed_model_controls(control_path, h, browser)
                else:
                    control = browser.adjust_controls(h)
                    write_json(control_path, control)
                if prep.get("staging") and not upload_resume and not uploaded_resume:
                    browser.upload(prep["staging"], output / "upload-plan.json", output / "upload-result.json",
                        write_json, lambda: job.update(stage="upload-started"))
                    job.update(stage="uploaded")
                observed = bootstrap_observed if bootstrap_observed is not None else browser.messages()
                boundary = observed["messages"][-1]["id"] if observed["messages"] else "new-conversation"
                if not adopt_uploaded_draft:
                    browser.fill_prompt(prompt)
                if browser.attachment_count() != (1 if prep.get("staging") else 0):
                    raise BridgeError("Unexpected composer attachment count")
                resolved, pages, owners = resolve(browser, repo, h, claim, allow_prepare=False)
                if claim.get("bootstrap"):
                    assert_bootstrap_resolution(resolved, h)
                browser.snapshot()
                args = ["--repo", repo, "--bridge-thread-id", thread,
                    *identity_args(browser, claim, resolved, pages, owners),
                    "--requested-model", h["requested_model"], "--selected-ui-label", control["selected_model"],
                    "--model-selection-kind", h["model_selection_kind"],
                    "--requested-thinking-intensity", h["requested_thinking_intensity"],
                    "--selected-thinking-intensity", control["selected_thinking_intensity"],
                    "--model-control-receipt", control_path, "--pre-submit-boundary", boundary,
                    "--prompt-sha256", digest(prompt), "--mcp-host-os", "windows",
                    "--browser-host-os", "windows", "--mcp-temp-root", staging_windows_root()]
                if project_id:
                    args += ["--expected-project-id", project_id, "--observed-project-id", project_id,
                        "--expected-workspace", binding["workspace"], "--observed-workspace", labels["workspace"],
                        "--expected-account-label", binding["account_label"], "--observed-account-label", labels["account"],
                        "--binding-status", binding["status"]]
                if claim.get("bootstrap"):
                    args += ["--conversation-bootstrap"]
                else:
                    args += ["--expected-conversation-id", conversation_id, "--observed-conversation-id", conversation_id]
                if prep.get("staging"):
                    args += ["--source-bundle", prep["bundle"], "--bundle", prep["staging"]["staged_execution_path"],
                        "--attachment-name", prep["staging"]["attachment_name"], "--upload-control", "devtools-direct-menu-upload",
                        "--upload-action-plan", output / "upload-plan.json", "--upload-result", output / "upload-result.json"]
                preflight = run_helper(SCRIPTS / "check_browser_preflight.py", args, repo)
                write_json(output / "preflight.json", preflight)
                attempt = prepare_attempt(repo, thread, prompt, preflight, h.get("business_deadline"))
                job.update(attempt_id=attempt["attempt_id"], stage="preflight")
                # Browser carries the effective in-memory UI profile.  A same-job
                # English overlay must therefore feed the Send lookup too; using
                # frozen config here would regress to the old Chinese label only
                # after upload/fill had already completed.
                send_uid = send_button_uid(browser, browser.ui["send_labels"])
                browser.assert_identity()
                if browser.composer_text() != prompt.strip():
                    raise BridgeError("Prompt changed before Send")
                attempt = mark_send_started(repo, thread, attempt["attempt_id"])
                job.update(stage="send-started")
                browser.call("click", pageId=int(browser.page_id), uid=send_uid)
            if attempt["state"] in {"send-started", "failed"}:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        observed = browser.messages(check_identity=False)
                        submission_candidate(observed, attempt)
                        break
                    except BridgeError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.5)
                if submission_candidate(observed, attempt)["text"].strip() == attempt["prompt"].strip():
                    user_id = observed_submission(observed, attempt)
                else:
                    user_id = verify_copied_submission(browser, observed, attempt, output, config)
                browser.url = observed["url"]
                browser.assert_identity()
                if claim.get("bootstrap"):
                    browser.snapshot()
                    new_pages = browser.pages()
                    count = sum(p["url"] == browser.url for p in new_pages)
                    promoted = promote_bootstrap_tab(repo, claim_token=claim["token"], thread_id=thread,
                        browser_profile=profile, expected_project_id=project_id, observed_page_url=browser.url,
                        matching_page_count=count, observed_page_id=str(browser.page_id),
                        snapshot_page_id=str(browser.page_id), observed_tab_owner_token=browser.owner)
                    claim = promoted["claim"]
                attempt = record_submission(repo, thread, attempt["attempt_id"], conversation_url=browser.url,
                    owner=browser.owner, prompt_sha256=digest(prompt), boundary=attempt["pre_submit_boundary"],
                    remote_turn_id=user_id, after_boundary="yes", submitted_at=now_iso())
            job.update(attempt_id=attempt["attempt_id"], stage="waiting", state="waiting",
                conversation_url=attempt["conversation_url"], remote_turn_id=attempt["remote_turn_id"],
                timings={"prepare_to_wait_seconds": round(time.monotonic() - started, 3)})
            waiting_started = time.monotonic()
            release_browser_lease(repo, token=claim["token"])
            job.update(claim_token="")
            cleanup(job)
            while True:
                event = browser.evaluate(browser_wait_script(attempt, browser.url)["function"])
                if event["status"] == "pending":
                    continue
                if event["status"] != "ready-for-capture":
                    raise BridgeError(f"Pinned-turn wait needs recovery: {event}")
                break
            claim = acquire_browser_lease(repo, holder=holder, thread_id=thread,
                expected_conversation_id=attempt["conversation_id"], expected_remote_project_id=project_id,
                browser_profile=profile)
            job.update(claim_token=claim["token"], state="running", stage="capturing")
            resolved, pages, owners = resolve(browser, repo, h, claim, allow_prepare=False)
            receipt = job.data.get("capture")
            if not receipt:
                receipt = capture(browser, attempt, output / "answer.md", config)
                job.update(capture=receipt)
            if file_sha256(Path(receipt["answer_path"])) != receipt["answer_sha256"]:
                raise BridgeError("Captured answer digest drift")
            browser.snapshot()
            from bridge_store import atomic_write_text
            captured_prompt = output/"captured-prompt.md"
            if captured_prompt.exists() and captured_prompt.read_bytes() != attempt["prompt"].encode("utf-8"):
                raise BridgeError("Immutable captured prompt drift")
            if not captured_prompt.exists():
                atomic_write_text(captured_prompt,attempt["prompt"])
            args = ["--repo", repo, "--bridge-thread-id", thread, "--attempt-id", attempt["attempt_id"],
                "--round-key", job.data["job_id"],
                "--snapshot-request-sha256", h["request_sha256"],
                "--capture-route", receipt.get("capture_route", "browser-fallback"),
                "--answer-format", receipt.get("answer_format", "copied-markdown"),
                "--expected-conversation-id", attempt["conversation_id"], "--web-url", browser.url,
                "--remote-turn-id", attempt["remote_turn_id"], "--prompt-file", captured_prompt,
                "--answer-file", receipt["answer_path"], "--response-completed-at", receipt["completed_at"],
                "--submitted-at", attempt["submitted_at"], *identity_args(browser, claim, resolved, pages, owners)]
            if receipt.get("capture_proof_path"):
                args += ["--capture-proof-file", receipt["capture_proof_path"]]
            if receipt.get("source_link_proof_path"):
                args += ["--source-link-proof-file", receipt["source_link_proof_path"]]
            args += ["--source-link-status", receipt.get("source_link_status", "unsupported")]
            if job.data.get("snapshot_input_receipt"):
                args += ["--snapshot-input-receipt", job.data["snapshot_input_receipt"]["path"]]
            preflight = attempt["preflight"]
            for key in ("requested_model", "selected_ui_label", "model_selection_kind", "requested_thinking_intensity",
                        "selected_thinking_intensity", "attachment_name", "upload_control"):
                if preflight.get(key):
                    args += ["--" + key.replace("_", "-"), preflight[key]]
            if prep.get("bundle"):
                args += ["--bundle", prep["bundle"]]
            if project_id:
                labels = browser.read_labels(("workspace", "account"))
                if labels["workspace"] != binding["workspace"] or labels["account"] != binding["account_label"]:
                    raise BridgeError("Capture account/workspace differs from the binding")
                args += ["--bridge-project-id", h["bridge_project_id"], "--remote-project-id", project_id,
                         "--observed-workspace", labels["workspace"], "--observed-account-label", labels["account"]]
            else:
                args += ["--standalone"]
            run_helper(SCRIPTS / "save_bridge_turn.py", args, repo, json_output=False)
            job.update(timings={**job.data.get("timings", {}),
                       "wait_capture_save_seconds": round(time.monotonic() - waiting_started, 3),
                       "worker_elapsed_seconds": round(time.monotonic() - started, 3)},
                       browser_tool_metrics=browser.tool_metrics)
            finish(job, repo, read_attempt(repo, thread, attempt["attempt_id"]))
        finally:
            # Preserve pre-Send bootstrap/uncertain owner state for recovery.
            # Once promoted, releasing a live claim does not erase its binding.
            if not claim.get("bootstrap"):
                release_browser_lease(repo, token=claim["token"])
                job.update(claim_token="")
            if client is not None:
                job.update(browser_tool_metrics=browser.tool_metrics)
                client.close()


def finish(job, repo, attempt):
    from .completion import validated_completion
    job.update(state="complete", stage="captured", error="", result=validated_completion(job.data))
