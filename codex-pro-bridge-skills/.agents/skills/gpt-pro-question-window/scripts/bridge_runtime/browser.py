"""Bounded semantic browser actions, using a private, versioned UI profile."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from bridge_store import BridgeError, TAB_OWNER_STORAGE_KEY
from browser_observations import normalize_pages_observation
from .rpc import evaluated, text_content


def message_dom_source():
    return (Path(__file__).resolve().parents[1] / 'wait_for_reply.js').read_text(encoding='utf-8')


def unique_uid(snapshot, roles, labels=None, *, allow_missing=False):
    matches = []
    for line in snapshot.splitlines():
        found = re.search(r'uid=(\S+)\s+(\w+)(?:\s+"((?:[^"\\]|\\.)*)")?', line)
        if not found or found[2] not in roles or re.search(r"(?:\[(?:disabled|hidden)\]|\bdisabled\b|\bhidden\b)", line[found.end():]):
            continue
        label = found[3] or ""
        if labels is None or label in labels:
            matches.append(found[1])
    if not matches and allow_missing:
        return None
    if len(matches) != 1:
        raise BridgeError(f"UI profile ambiguity: expected one {roles} {labels}, found {len(matches)}")
    return matches[0]


def send_button_uid(browser, labels):
    snapshot = browser.snapshot()
    uid = unique_uid(snapshot, ("button",), labels, allow_missing=True)
    if uid is not None:
        return uid
    # September UI shortened the Chinese label. Only accept that known alias
    # on a unique enabled composer submit button, not an arbitrary page button.
    if set(labels) & {"发送提示词", "发送提示"}:
        current = browser.owned_evaluate(r'''() => {
            const form = document.querySelector('form:has([role="textbox"][contenteditable="true"])');
            const nodes = [...(form?.querySelectorAll('button[type="submit"][aria-label="发送"]') || [])]
              .filter(n => n.getClientRects().length && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
            return nodes.length === 1;
        }''')
        if current:
            return unique_uid(snapshot, ("button",), ["发送"])
    raise BridgeError("Send button does not match the configured or verified UI adapter")


class Browser:
    def __init__(self, client, ui):
        self.client, self.ui = client, ui
        self.page_id = None
        self.owner = ""
        self.url = ""
        self.tool_metrics = {}

    def call(self, name, **arguments):
        started = time.monotonic()
        try:
            return self.client.call(name, **arguments)
        finally:
            entry = self.tool_metrics.setdefault(name, {"calls": 0, "seconds": 0.0})
            entry["calls"] += 1
            entry["seconds"] = round(entry["seconds"] + time.monotonic() - started, 6)

    def evaluate(self, function, page_id=None):
        return evaluated(self.call("evaluate_script", pageId=int(page_id or self.page_id),
                                         function=function, waitForStableDom=False))

    def pages(self):
        return normalize_pages_observation(self.call("list_pages"))

    def owned_evaluate(self, function):
        """Read identity and content in one page execution, not two RPCs."""
        guard = ("if (location.href !== " + json.dumps(self.url) +
                 " || sessionStorage.getItem(" + json.dumps(TAB_OWNER_STORAGE_KEY) + ") !== " +
                 json.dumps(self.owner) + ") throw Error('Live owner/URL observation conflict; no navigation authorized');")
        return self.evaluate("() => {" + guard + " return (" + function + ")(); }")

    def owners(self, pages):
        observations = []
        for page in pages:
            if not page["url"].startswith("https://chatgpt.com/"):
                continue
            value = self.evaluate("() => ({url: location.href, owner: sessionStorage.getItem(" +
                                  json.dumps(TAB_OWNER_STORAGE_KEY) + ") || ''})", page["page_id"])
            if value["url"] != page["url"]:
                raise BridgeError("Page list and live URL disagree; recollect observations")
            observations.append({"page_id": page["page_id"], "owner_token": value["owner"]})
        return observations

    def assert_identity(self):
        value = self.evaluate("() => ({url: location.href, owner: sessionStorage.getItem(" +
                              json.dumps(TAB_OWNER_STORAGE_KEY) + ") || ''})")
        if value != {"url": self.url, "owner": self.owner}:
            raise BridgeError("Live owner/URL observation conflict; no navigation authorized; "
                              f"page_id={self.page_id!r}, expected_url={self.url!r}, "
                              f"observed_url={value.get('url')!r}, owner_matches={value.get('owner') == self.owner}")

    def snapshot(self):
        self.assert_identity()
        return text_content(self.call("take_snapshot", pageId=int(self.page_id)))

    def focus_for_copy(self):
        """Copy requires document focus, unlike snapshots or DOM observation.

        Call only while holding the host clipboard lock. Never navigate to gain
        focus, and recheck ownership after bringing the exact claimed tab forward.
        """
        self.assert_identity()
        self.call("select_page", pageId=int(self.page_id), bringToFront=True)
        if self.owned_evaluate("() => document.hasFocus()") is not True:
            raise BridgeError("Claimed page did not receive clipboard focus; no copy attempted")

    def copy_with_proof(self, message_id, role):
        self.focus_for_copy()
        source = (Path(__file__).resolve().parents[1] / "copy_with_proof.js").read_text(encoding="utf-8")
        args = json.dumps([message_id, role, self.url, self.owner, TAB_OWNER_STORAGE_KEY])
        result = self.evaluate("async () => {" + message_dom_source() + source +
                               "return await bridgeCopyWithProof(..." + args + ");}")
        if (result.get("message_id") != message_id or result.get("role") != role
                or result.get("provenance") != "visible-copy-write/v1" or result.get("writes") != 1):
            raise BridgeError("Copy provenance does not match pinned message")
        return result

    def page_serialization(self, attempt, assistant_id):
        source = (Path(__file__).resolve().parents[1] / "page_serialization.js").read_text(encoding="utf-8")
        args = json.dumps([attempt["remote_turn_id"], assistant_id, self.url, self.owner, TAB_OWNER_STORAGE_KEY])
        return self.evaluate("() => {" + source + "return bridgePageSerialized(..." + args + ");}")

    def copied_link_source(self, attempt, assistant_id):
        """Read the pinned new-UI source/reference inventory without side effects.

        Legacy message DOMs intentionally return ``unsupported`` so their
        existing DOM-link equality gate remains unchanged.  New search-unit
        pages must provide the complete fixed user/assistant source shape.
        """
        source = (Path(__file__).resolve().parents[1] / "copied_link_source.js").read_text(encoding="utf-8")
        args = json.dumps([attempt["remote_turn_id"], assistant_id, self.url, self.owner,
                           TAB_OWNER_STORAGE_KEY, str(self.page_id)])
        return self.evaluate("() => {" + message_dom_source() + source +
                             "return bridgeCopiedLinkSource(..." + args + ");}")

    def click(self, labels, roles=("button", "menuitem", "menuitemradio", "option")):
        uid = self.control_uid(labels, roles)
        self.assert_identity()
        self.call("click", pageId=int(self.page_id), uid=uid)
        return uid

    def control_uid(self, labels, roles):
        deadline = time.monotonic() + 5
        while True:
            uid = unique_uid(self.snapshot(), roles, labels, allow_missing=True)
            if uid is not None:
                return uid
            if time.monotonic() >= deadline:
                raise BridgeError(f"Control did not become ready: {roles} {labels}")
            time.sleep(0.1)

    def read_labels(self, keys):
        # The redesigned sidebar only exposes the account label inside its
        # menu. Opening it is observation, never an account/workspace switch.
        menu = self.ui.get("account_menu_labels")
        if menu and set(keys) <= {"workspace", "account"}:
            self.click(menu, roles=("button",))
            try:
                return self._read_labels(keys)
            finally:
                self.press("Escape")
        return self._read_labels(keys)

    def _read_labels(self, keys):
        selectors = {key: self.ui[key] for key in keys}
        function = "() => { const selectors = " + json.dumps(selectors) + "; const out = {}; " + r'''
          for (const [key, selector] of Object.entries(selectors)) {
            const nodes = [...document.querySelectorAll(selector)].filter(n => n.getClientRects().length && !n.closest('[inert], [aria-hidden="true"]'));
            if (nodes.length !== 1) return {__pending__: key, count: nodes.length};
            const n = nodes[0];
            out[key] = (n.getAttribute('aria-valuetext') || n.innerText || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
          } return out;
        }'''
        deadline = time.monotonic() + 5
        while True:
            result = self.owned_evaluate(function)
            if result.get("count", 0) > 1:
                raise BridgeError("UI profile produced multiple visible label nodes")
            placeholder = result.get("thinking") in self.ui.get("thinking_placeholder_labels", [])
            if "__pending__" not in result and not placeholder:
                break
            if time.monotonic() >= deadline:
                raise BridgeError("UI labels did not become ready")
            time.sleep(0.1)
        if any(not value for value in result.values()):
            raise BridgeError("UI profile produced an empty model/account label")
        return result

    def adjust_controls(self, h):
        if self.ui.get("control_layout") == "nested-slider":
            return self.adjust_nested_controls(h)
        from model_controls import model_selection_matches, validate_model_control_trace
        initial = self.read_labels(("model", "thinking"))
        counts = {key: 0 for key in ("combined_initial_read", "model_menu_open", "model_selection",
                  "thinking_control_open", "thinking_adjustment", "thinking_progress_read",
                  "combined_final_confirmation", "post_preflight_recheck")}
        counts["combined_initial_read"] = 1
        if not model_selection_matches(h["requested_model"], initial["model"], h["model_selection_kind"]):
            self.click(self.ui["model_menu_labels"])
            counts["model_menu_open"] = 1
            self.click([h["requested_model"]])
            counts["model_selection"] = 1
        if initial["thinking"] != h["requested_thinking_intensity"]:
            self.click(self.ui["thinking_menu_labels"])
            counts["thinking_control_open"] = 1
            # An exact visible menu item is the supported fast path. Slider UIs
            # require a separately verified adapter, never guessed DOM values.
            self.click([h["requested_thinking_intensity"]])
            counts["thinking_adjustment"] = 1
            observed = self.read_labels(("thinking",))["thinking"]
            counts["thinking_progress_read"] = 1
            if observed != h["requested_thinking_intensity"]:
                raise BridgeError("Thinking adjustment did not take effect")
        final = self.read_labels(("model", "thinking"))
        counts["combined_final_confirmation"] = 1
        return validate_model_control_trace({
            "schema_version": "model-controls/v1", "page_id": str(self.page_id),
            "tab_owner_token": self.owner, "observed_page_url": self.url,
            "requested_model": h["requested_model"], "selected_model": final["model"],
            "model_selection_kind": h["model_selection_kind"],
            "requested_thinking_intensity": h["requested_thinking_intensity"],
            "selected_thinking_intensity": final["thinking"], "initial_model": initial["model"],
            "initial_thinking_intensity": initial["thinking"], "counts": counts})

    def press(self, key):
        self.assert_identity()
        self.call("press_key", pageId=int(self.page_id), key=key)

    def adjust_nested_controls(self, h):
        """Two real observations of the hidden model, no redundant selections.

        Opening a menu to observe its checked item is not a model change. v2
        records these opens honestly rather than inventing v1's zero opens.
        Slider positions come from the explicitly observed private UI profile;
        the rendered label remains authoritative and must match afterwards.
        """
        from model_controls import model_selection_matches, validate_model_control_trace
        counts = dict(combined_initial_read=1, model_menu_open=0, model_selection=0,
                      thinking_control_open=0, thinking_adjustment=0,
                      thinking_progress_read=0, combined_final_confirmation=1,
                      post_preflight_recheck=0)

        thinking_location = self.ui.get("thinking_label_location", "closed-trigger")
        if thinking_location not in ("closed-trigger", "outer-menu"):
            raise BridgeError("Unsupported nested thinking_label_location")

        def open_model():
            if thinking_location == "outer-menu":
                self.click(self.ui["thinking_menu_labels"])
                counts["thinking_control_open"] += 1
                thinking = self.read_labels(("thinking",))["thinking"]
            else:
                thinking = self.read_labels(("thinking",))["thinking"]
                self.click(self.ui["thinking_menu_labels"])
                counts["thinking_control_open"] += 1
            self.click(self.ui["model_menu_labels"])
            counts["model_menu_open"] += 1
            return {"thinking": thinking, **self.read_labels(("model",))}

        initial = open_model()
        if not model_selection_matches(h["requested_model"], initial["model"], h["model_selection_kind"]):
            self.click([h["requested_model"]], roles=("menuitemradio",))
            counts["model_selection"] += 1
        self.press("Escape")
        requested = h["requested_thinking_intensity"]
        if initial["thinking"] != requested:
            target = self.ui.get("thinking_positions", {}).get(requested)
            if isinstance(target, bool) or not isinstance(target, int):
                raise BridgeError("Requested intensity has no observed slider position")
            self.click(self.ui["thinking_menu_labels"])
            counts["thinking_control_open"] += 1

            def slider_state():
                return self.owned_evaluate("() => { const nodes = [...document.querySelectorAll(" +
                    json.dumps(self.ui["thinking_slider"]) + ")].filter(n => n.getClientRects().length);" + r'''
                    if (nodes.length !== 1) throw Error('Slider ambiguity');
                    const n = nodes[0];
                    for (const key of ['aria-valuemin','aria-valuemax','aria-valuenow'])
                        if (n.getAttribute(key) === null) throw Error('Missing slider value');
                    return {min:Number(n.getAttribute('aria-valuemin')),
                      max:Number(n.getAttribute('aria-valuemax')), value:Number(n.getAttribute('aria-valuenow'))};
                }''')

            state = slider_state()
            if (not all(isinstance(v, int) and not isinstance(v, bool) for v in state.values())
                    or not state["min"] <= state["value"] <= state["max"]
                    or not state["min"] <= target <= state["max"]
                    or abs(target - state["value"]) > 6):
                raise BridgeError("Slider range or action budget mismatch")
            # Focus the accessible keyboard controller, never synthesize a
            # pointer click on an unknown slider coordinate or edit React state.
            self.assert_identity()
            focused = self.evaluate("() => { const nodes = [...document.querySelectorAll(" +
                json.dumps(self.ui["thinking_keyboard_control"]) + ")].filter(n => n.getClientRects().length && !n.closest('[inert]'));" +
                "if (nodes.length !== 1) throw Error('Keyboard controller ambiguity'); nodes[0].focus(); return document.activeElement === nodes[0]; }")
            if not focused:
                raise BridgeError("Keyboard controller could not receive focus")
            while state["value"] != target:
                direction = 1 if target > state["value"] else -1
                before = state["value"]
                self.press("ArrowRight" if direction == 1 else "ArrowLeft")
                counts["thinking_adjustment"] += 1
                state = slider_state()
                counts["thinking_progress_read"] += 1
                if state["value"] != before + direction:
                    raise BridgeError("Slider did not advance exactly one step; no retry")
            if thinking_location == "outer-menu":
                observed_thinking = self.read_labels(("thinking",))["thinking"]
            else:
                self.press("Escape")
                observed_thinking = self.read_labels(("thinking",))["thinking"]
            if observed_thinking != requested:
                raise BridgeError("Slider position does not match the requested visible intensity")
            if thinking_location == "outer-menu":
                self.press("Escape")
        final = open_model()
        self.press("Escape")
        return validate_model_control_trace({
            "schema_version": "model-controls/v2", "control_layout": "nested-slider",
            "thinking_label_location": thinking_location,
            "page_id": str(self.page_id), "tab_owner_token": self.owner,
            "observed_page_url": self.url, "requested_model": h["requested_model"],
            "selected_model": final["model"], "model_selection_kind": h["model_selection_kind"],
            "requested_thinking_intensity": requested, "selected_thinking_intensity": final["thinking"],
            "initial_model": initial["model"], "initial_thinking_intensity": initial["thinking"],
            "counts": counts})

    def messages(self, *, check_identity=True):
        source = (Path(__file__).resolve().parents[1] / "composer_text.js").read_text(encoding="utf-8")
        evaluate = self.owned_evaluate if check_identity else self.evaluate
        return evaluate("() => {" + message_dom_source() + source + " return {url: location.href, owner: sessionStorage.getItem(" +
            json.dumps(TAB_OWNER_STORAGE_KEY) + "), messages: " + r'''
            bridgeMessages().map(r => {
              if (r.role !== 'user') return {id:r.id,role:r.role,text:''};
              const body = bridgeMessageBody(r);
              const roots = body?.querySelectorAll('[data-markdown-text-tone="user-message"]') || [];
              if (roots.length > 1) throw Error('User plaintext root ambiguity');
              const text = roots.length === 1 ? bridgeComposerText(roots[0]) : (body?.innerText || '');
              return {id:r.id,role:r.role,text};
            })}; }''')

    def composer_text(self):
        source = (Path(__file__).resolve().parents[1] / "composer_text.js").read_text(encoding="utf-8")
        return self.owned_evaluate("() => {" + source + " const nodes = document.querySelectorAll(" + json.dumps(self.ui["composer"]) +
                             "); if (nodes.length !== 1) throw Error('Composer ambiguity'); " +
                             "return bridgeComposerText(nodes[0]); }")

    def fill_prompt(self, prompt):
        if self.composer_text():
            raise BridgeError("Composer is not empty; refusing to overwrite a draft")
        labels = self.ui.get("composer_labels")
        if not labels:
            label = self.owned_evaluate("() => { const nodes = document.querySelectorAll(" +
                json.dumps(self.ui["composer"]) + "); if(nodes.length !== 1) throw Error('Composer ambiguity');" +
                "return nodes[0].getAttribute('aria-label') || ''; }")
            labels = [label] if label else None
        # The new answer code editor is also a textbox. Its accessible label is
        # not the label observed on the exact configured composer element.
        uid = unique_uid(self.snapshot(), ("textbox",), labels)
        self.assert_identity()
        self.call("fill", pageId=int(self.page_id), uid=uid, value=prompt)
        if self.composer_text() != prompt.strip():
            raise BridgeError("Rendered composer differs from frozen prompt")

    def attachments(self):
        """Observe composer file tiles, including the current accessible group UI."""
        return self.owned_evaluate("() => { const legacy = " + json.dumps(self.ui["attachment_chip"]) + ";" + r'''
            const nodes = [...document.querySelectorAll(legacy),
              ...[...document.querySelectorAll('form [data-testid="library-file-icon"]')]
                .map(n => n.closest('[role="group"][aria-label]')).filter(Boolean),
              ...[...document.querySelectorAll('form button.composer-attachment-surface[aria-label]')]
                .map(n => n.closest('[class~="group/composer-attachment"]')).filter(Boolean)];
            const visible = [...new Set(nodes)].filter(n => n.getClientRects().length && !n.closest('[inert]'));
            return visible.filter(n => !visible.some(other => other !== n && other.contains(n))).map(n => ({
              name: n.getAttribute('aria-label') || n.querySelector('button.composer-attachment-surface[aria-label]')?.getAttribute('aria-label') || (n.innerText || '').split('\n')[0].trim(),
              busy: !!n.querySelector('[role="progressbar"], [aria-busy="true"]') || n.getAttribute('aria-busy') === 'true'
            }));
        }''')

    def attachment_count(self):
        return len(self.attachments())

    def attachment_ready(self, name):
        cards = self.attachments()
        return len(cards) == 1 and cards[0] == {"name": name, "busy": False}

    def recover_upload(self, staging, plan_path, result_path, save):
        from pathlib import Path
        from bridge_store import file_sha256
        from devtools_upload import validate_upload_action_plan, verify_upload_result
        self.assert_identity()
        plan = validate_upload_action_plan(json.loads(plan_path.read_text(encoding="utf-8")))
        transport = json.loads(result_path.with_suffix(".transport.json").read_text(encoding="utf-8"))
        if (transport != {"status": "accepted", "page_id": str(self.page_id),
                          "owner": self.owner, "url": self.url, "plan": plan}
                or plan["page_id"] != str(self.page_id)
                or plan["windows_path"] != staging["staged_browser_path"]
                or plan["staged_sha256"] != staging["source_sha256"]
                or plan["expected_attachment_name"] != staging["attachment_name"]):
            raise BridgeError("Upload recovery identity or transport receipt mismatch")
        for key in ("source_path", "staged_execution_path"):
            if file_sha256(Path(staging[key])) != staging["source_sha256"]:
                raise BridgeError("Upload recovery file digest drift")
        receipt = verify_upload_result(plan, {"status": "accepted",
            "attachment_chip": self.attachment_ready(staging["attachment_name"]),
            "attachment_name": staging["attachment_name"]})
        save(result_path, receipt)
        return receipt

    def upload(self, staging, plan_path, result_path, save, before_action):
        from devtools_upload import build_upload_action_plan, verify_upload_result
        before_action()  # Persist before any chooser-triggering action.
        attachment_uid = self.click(self.ui["attachment_labels"])
        # Current composer exposes the upload row's text rather than a
        # menuitem role. Pass that fresh exact-label node to upload_file;
        # never click it separately (which would leave a native chooser open).
        menu_uid = self.control_uid(self.ui["upload_labels"], ("menuitem", "button", "StaticText"))
        plan = build_upload_action_plan(page_id=str(self.page_id), attachment_button_uid=attachment_uid,
            menu_snapshot_uid="snapshot-" + menu_uid, menu_item_uid=menu_uid,
            windows_path=staging["staged_browser_path"], staged_sha256=staging["source_sha256"])
        save(plan_path, plan)
        self.assert_identity()
        self.call("upload_file", pageId=int(self.page_id), uid=menu_uid,
                         filePaths=[staging["staged_browser_path"]])
        # A returned call and a visible ready card are separate evidence. Persist
        # the former before polling so recovery never has to replay upload_file.
        save(result_path.with_suffix(".transport.json"), {
            "status": "accepted", "page_id": str(self.page_id), "owner": self.owner,
            "url": self.url, "plan": plan})
        # Inspect a chip, not arbitrary page text. Never retry the upload.
        deadline = time.monotonic() + 60
        name = staging["attachment_name"]
        while True:
            # attachment_ready already checks identity and readiness together.
            chip = self.attachment_ready(name)
            if chip or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        receipt = verify_upload_result(plan, {"status": "accepted", "attachment_chip": bool(chip),
                                              "attachment_name": name})
        save(result_path, receipt)
        return receipt
