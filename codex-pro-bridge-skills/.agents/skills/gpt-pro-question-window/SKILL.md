---
name: gpt-pro-question-window
description: Bridge Codex to the correct signed-in ChatGPT/GPT Pro conversation, automatically choosing standalone or a bound ChatGPT Project, with scoped upload, conversation reuse, raw answer capture, and later Codex verification. Use for normal GPT Pro questions and as the browser/persistence foundation for other Codex Pro Bridge skills; do not use for local tasks that need no external reasoning.
---

# GPT Pro Question Window

Use this skill as the browser and persistence adapter for Codex Pro Bridge.

Before creating or resuming bridge state, read [references/bridge_protocol.md](references/bridge_protocol.md). It is the single source of truth for IDs, events, storage, invariants, and CLI commands.

## Required flow

1. Identify the exact question and desired output. Decide whether outside reasoning is useful, then preview the route with `../gpt-pro-project-workspace/scripts/resolve_bridge_route.py`, passing `--external-reasoning` or `--local-only`.
2. If the route is `local_only`, stop using this skill and complete the work locally. If the route requires confirmation, resolve the binding or ambiguity before continuing. Apply a ready Project route once.
3. Use the returned `bridge-thread-id`. Reuse the GPT Pro session only when its local metadata points to the intended web conversation, Bridge Thread, and ChatGPT Project. Otherwise create a new task-scoped conversation.
4. For Project mode, open the exact saved Project URL and visibly verify the Project ID, account/workspace, active binding, and current source inventory before creating or reusing the conversation. Reconcile the observed inventory locally. Never choose by title alone.
5. Open ChatGPT in the user's signed-in Chrome session. Ask the user to handle login, passwords, 2FA, CAPTCHA, rate limits, or account-security prompts.
6. Upload a focused Task Bundle when evidence is needed. A Task Bundle is not a Project Source. Never replace a failed upload with a full repository paste unless the user explicitly approves that fallback.
7. Read the exact selected model label, visible attachment name, and Project identity when applicable. Acquire the host-local browser lease; before Send, record the stable conversation ID, existing turn IDs or cursor, and prompt digest as the pre-submit boundary. Run `scripts/check_browser_preflight.py`. If the requested model is `Pro`, labels such as `极高` or an account name containing “Pro” do not satisfy the gate.
8. Click Send once. After ChatGPT visibly accepts the prompt, record the submission time and target turn ID when available, then release the browser lease immediately. Do not hold it while the remote model generates.
9. Prefer Codex's native `read_thread` on that exact ChatGPT conversation. Match the new user turn after the saved boundary, pin its remote turn ID, and accept only its completed, untruncated assistant reply. Never capture whichever turn merely happens to be latest.
10. If the native read is unavailable, ambiguous, or marked `truncated`, reacquire the browser lease only for a full browser capture of the already pinned target turn, pass the same remote turn ID to persistence, and release the lease afterward. A scheduled heartbeat may poll `read_thread`; it must stay quiet on no change and delete itself after capture, explicit failure, or timeout.
11. Capture the prompt, bundle digest, full raw answer, target remote turn ID, capture route, Project identity, model labels, attachment name, upload route, and observed timing with `scripts/save_bridge_turn.py`.
12. Re-open local evidence, verify the answer, and record the result separately with `scripts/record_codex_verdict.py`.
13. Run `scripts/verify_bridge_thread.py --require-complete-rounds` and, for
    Project mode, run
    `../gpt-pro-project-workspace/scripts/verify_bridge_project.py` with
    `--require-active-binding` before a follow-up round or final handoff.
14. Report the chosen route, saved turn and verdict paths, useful conclusions, rejected claims, and next action.

Completion criterion: the raw exchange and Codex verdict are separate immutable artifacts on the same thread, and every acted-on GPT Pro claim has a local verdict.

## Normal-question prompt

For a normal question, read and use [references/question_window_prompt.md](references/question_window_prompt.md). Specialized review skills provide their own prompt.

## Chrome upload

Use Chrome's file chooser before Computer Use:

1. Before the first browser round, verify that the Codex Chrome extension is installed and enabled. In this environment, use a US-region network node while downloading it from the Chrome Web Store.
2. Open `chrome://extensions/`, open the extension's **Details**, and verify **Allow access to file URLs** is enabled.
3. Build the zip locally and keep its output path absolute for the browser call.
4. Start `waitForEvent("filechooser")` before clicking ChatGPT's visible attachment button and visible **Upload from computer** menu item.
5. Call `chooser.setFiles([absolute_path])`.
6. Verify the filename or attachment chip before submitting.
7. Remove the attachment and confirm an empty composer after a dry run.

Never directly click a hidden input such as `#upload-files`. Use a semantic visible control first; use programmatic file assignment only as a verified fallback.

Before submission, run:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/check_browser_preflight.py \
  --repo . \
  --bridge-thread-id '<bridge-thread-id>' \
  --browser-lease-token '<lease-token>' \
  --requested-model Pro \
  --selected-ui-label '<exact visible label>' \
  --bundle /absolute/path/to/bundle.zip \
  --attachment-name '<visible filename>' \
  --upload-control visible-menu \
  --expected-conversation-id '<reserved chat id>' \
  --observed-conversation-id '<visible chat id>'
```

For a Project-bound round, also pass the exact values returned by routing and
observed in the browser:

```bash
  --expected-project-id '<bound g-p-id>' \
  --observed-project-id '<visible g-p-id>' \
  --expected-workspace '<routed workspace>' \
  --observed-workspace '<visible workspace>' \
  --expected-account-label '<routed account label>' \
  --observed-account-label '<visible account label>' \
  --binding-status active
```

If the extension is missing or the permission is disabled, stop and fix that prerequisite before retrying. Use Computer Use only when Chrome still cannot control the native or graphical UI boundary after the extension and permission checks pass.

Treat attachment preprocessing that stalls before submission as a bundle-shape problem: regenerate a smaller package or at most two or three focused attachments. Do not interrupt a response that remains visibly active merely because a Pro run is slow.

## Browser pacing

- Prefer one conversation per Bridge Thread. Independent deliverables receive independent Threads and conversations; never use the same Bridge Thread concurrently.
- Serialize only browser-mutating critical sections: attach/upload/preflight/Send and any browser fallback capture. Release the browser lease after each section; remote generations may overlap.
- Use bounded native reads for an immediate wait. If a later wake-up is useful, create one heartbeat watcher for the current task, retain its automation ID, and poll only the exact ChatGPT conversation. Delete the watcher on captured success, explicit failure, or timeout; do not resubmit.
- Treat any `truncated: true`, incomplete status, missing target turn, or multiple plausible new turns as non-capturable. Reacquire the browser only when the full raw answer cannot be obtained natively.
- Distinguish `submitted`, `generation observed`, `response complete`, `captured`, and `failed`. Record observed timestamps; do not invent missing ones.
- If progress disappears, capture diagnostics and mark the attempt failed. Do not automatically resubmit the prompt.
- Inspect after failure; avoid rapid retries, scraping, or burst submission.
- Stop for service or account protections rather than attempting to bypass them.

## Non-negotiable checks

- Keep uploaded evidence inside the user-approved scope.
- Save the answer before using it.
- Verify locally before editing code or trusting a result.
- Record model state as `verified`, `mismatch`, or `unverified`; never upgrade a mismatch to a Pro claim.
- Verify ledger parents, artifact hashes, and bundle hashes before continuing the thread.
- Never move an existing local session to another thread or web URL.
- Never overwrite a saved bundle, turn, snapshot, or verdict.
