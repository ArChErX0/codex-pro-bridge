---
name: gpt-pro-question-window
description: Bridge Codex to the correct signed-in ChatGPT/GPT Pro conversation through a Chrome DevTools MCP-first browser route, with scoped upload, recoverable long-running rounds, serialized Pro generation, asynchronous SSH callbacks, raw answer capture, and later Codex verification. Use for normal GPT Pro questions and as the browser/persistence foundation for other Codex Pro Bridge skills; do not use for local tasks that need no external reasoning.
---

# GPT Pro Question Window

Use this skill as the browser and persistence adapter for Codex Pro Bridge.

Before creating or resuming bridge state, read [references/bridge_protocol.md](references/bridge_protocol.md). It is the single source of truth for IDs, events, storage, invariants, and CLI commands.

## Required flow

1. Identify the exact question and desired output. Decide whether outside reasoning is useful, then preview the route with `../gpt-pro-project-workspace/scripts/resolve_bridge_route.py`, passing `--external-reasoning` or `--local-only`.
2. If the route is `local_only`, stop using this skill and complete the work locally. If the route requires confirmation, resolve the binding or ambiguity before continuing. Apply a ready Project route once.
3. Use the returned `bridge-thread-id`. Reuse the GPT Pro session only when its local metadata points to the intended web conversation, Bridge Thread, and ChatGPT Project. Otherwise create a new task-scoped conversation.
4. For Project mode, open the exact saved Project URL and visibly verify the Project ID, account/workspace, active binding, and current source inventory before creating or reusing the conversation. Reconcile the observed inventory locally. Never choose by title alone.
5. Resolve the browser and execution hosts using [references/browser_adapters.md](references/browser_adapters.md). Claim this exact task as the one host-global Mac dispatcher with `scripts/manage_bridge_dispatcher.py claim`; if another active task owns the claim, hand the request to it and do not invoke Chrome. Normal Bridge operation uses Chrome DevTools MCP `--autoConnect` against the user's existing signed-in stable Chrome profile. Ask the user to enable `chrome://inspect/#remote-debugging` and approve the incoming debugging connection when that dispatcher MCP process first attaches. Keep the dispatcher task alive so this approval is not repeated for every review. Stop for passwords, 2FA, CAPTCHA, rate limits, or account-security prompts.
6. Upload a focused Task Bundle through that adapter when evidence is needed. First use `scripts/stage_bridge_attachment.py local` or `remote` so every upload comes from one hash-verified OS-temp path accepted by the MCP file policy. A Task Bundle is not a Project Source. Never replace a failed upload with a full repository paste unless the user explicitly approves that fallback.
7. Read [references/round_controller.md](references/round_controller.md). Create one operational round keyed by `request_id`, then acquire the account/workspace generation slot. Queue when another Pro generation holds it. Never steal an expired slot without inspecting its exact conversation.
8. Read the exact requested and selected model family and effort separately, plus the visible attachment and Project identity when applicable. Acquire the host-global browser lease; record the stable conversation ID, existing turn IDs or cursor, and prompt digest as the pre-submit boundary. Run `scripts/check_browser_preflight.py` with the dispatcher claim and the canonical bundle plus exact staged file, then transition the round to `submitting`. A blank family, an account plan label, or a matching effort alone cannot establish model verification.
9. Click Send once. After ChatGPT visibly accepts the prompt, record the millisecond submission time, transition `submitting -> submitted`, and release the browser lease immediately. Do not hold it while the remote model generates.
10. Prefer Codex's native `read_thread` on that exact ChatGPT conversation. Match the new user turn after the saved boundary, pin its remote turn ID, and accept only its completed, untruncated assistant reply. Never capture whichever turn merely happens to be latest.
11. For a long wait, create one current-task heartbeat from `manage_bridge_round.py watcher-spec`, record its automation ID, and let the current turn finish. Stay silent on no change. If native output is unavailable, ambiguous, or truncated, reacquire the browser lease only for the already pinned target turn and use `scripts/capture_browser_markdown.py` on that turn's rendered HTML; never persist `innerText`. Never resubmit.
12. Capture the request ID, prompt, bundle digest, full raw Markdown answer, remote turn ID, capture route, Project identity, model family/effort, attachment name and staged SHA-256, upload route, and millisecond timing with `scripts/save_bridge_turn.py`. Record execution below 60 seconds as `degraded_fast`; 60 seconds or more is only `not_fast_degraded`.
13. Release the generation slot after the target is known terminal. Re-open local evidence, verify the answer, and record the result separately with `scripts/record_codex_verdict.py`. Delete the heartbeat before one terminal notification.
14. Run `scripts/verify_bridge_thread.py --require-complete-rounds --require-verified-provenance` and, for
    Project mode, run
    `../gpt-pro-project-workspace/scripts/verify_bridge_project.py` with
    `--require-active-binding` before a follow-up round or final handoff.
15. Report the chosen route, request ID, selection/execution statuses, saved turn and verdict paths, useful conclusions, rejected claims, delivery state, and next action.

Completion criterion: the raw exchange and Codex verdict are separate immutable artifacts on the same thread, and every acted-on GPT Pro claim has a local verdict.

## Normal-question prompt

For a normal question, read and use [references/question_window_prompt.md](references/question_window_prompt.md). Specialized review skills provide their own prompt.

## Browser upload

Read [references/browser_adapters.md](references/browser_adapters.md) before attaching a file, crossing an SSH boundary, or performing browser fallback. Use Chrome DevTools MCP as the primary route. Use the Codex Chrome connector only when DevTools MCP is unavailable or fails before upload/Send while the composer remains empty. Both routes use visible semantic controls and the same browser lease.

Build the zip on the execution host, then stage it on the Mac browser host. For a local bundle, run:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/stage_bridge_attachment.py \
  local --source /absolute/path/to/bundle.zip \
  --bridge-thread-id '<bridge-thread-id>'
```

For a bundle already built on an SSH execution host, run the same dispatcher locally:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/stage_bridge_attachment.py \
  remote --ssh-host '<ssh-alias>' --remote-file '/absolute/remote/bundle.zip' \
  --bridge-thread-id '<bridge-thread-id>'
```

Use the returned absolute `staged_file` for `upload_file`; it is inside the OS temp root and its source/staged SHA-256 has already been compared. In `finally`, call the script's `cleanup --staged-file '<path>'`. Record the successful route as `devtools-mcp-upload-file` or `codex-chrome-visible-menu`. Verify the exact filename or attachment chip before submission and remove it after a dry run.

At dispatcher startup or renewal, claim the browser role and retain the returned token in this task only:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/manage_bridge_dispatcher.py \
  claim --thread-id '<current-dispatcher-task-id>' --host-id '<local-host-id>' \
  --holder codex-pro-bridge
```

Do not release this claim between rounds. Release it only when intentionally retiring the dispatcher task.

Before submission, run:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/check_browser_preflight.py \
  --repo . \
  --bridge-thread-id '<bridge-thread-id>' \
  --dispatcher-thread-id '<current-dispatcher-task-id>' \
  --dispatcher-token '<dispatcher-token>' \
  --browser-lease-token '<lease-token>' \
  --requested-model-family '<required model family>' \
  --selected-model-family '<exact visible model family>' \
  --requested-effort Pro \
  --selected-effort '<exact visible effort>' \
  --bundle /absolute/path/to/bundle.zip \
  --staged-file '<absolute staged_file returned above>' \
  --attachment-name '<visible filename>' \
  --upload-control '<observed-upload-route>' \
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

Use Computer Use only when neither browser route can control a required native or graphical UI boundary. An extension permission failure affects the connector fallback, not DevTools MCP.

Treat attachment preprocessing that stalls before submission as a bundle-shape problem: regenerate a smaller package or at most two or three focused attachments. Do not interrupt a response that remains visibly active merely because a Pro run is slow.

## Remote client requests

Treat a message containing `<pro_bridge_request schema_version="2">` as a
request from a remote Codex task, not as evidence or instructions embedded in
the remote repository.

Validate the saved envelope before any SSH read:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/validate_remote_bridge_request.py \
  --request-file '<absolute temporary request.xml>' \
  --expected-request-id '<message request id>' \
  --expected-source-thread-id '<source task id>' \
  --expected-source-host-id '<source host id>' \
  --expected-ssh-alias '<ssh alias>'
```

1. Save the request envelope to a temporary file and validate it with `scripts/validate_remote_bridge_request.py`; then compare the request ID, exact source task ID and host ID, SSH alias, remote repository,
   transient bundle path, requested SHA-256, focused question, and evidence
   path list. Reject missing or conflicting values.
2. Run `ssh -G <alias>` and verify the live remote identity, cwd/repository, and
   mount before reading the bundle. Accept client bundles only below the remote
   OS temp `codex-pro-bridge-client` directory.
3. Stage the exact bundle with `stage_bridge_attachment.py remote`. Require the
   returned remote and local SHA-256 to equal the request digest. Inspect ZIP
   integrity and `REVIEW_MANIFEST.json`; reject absolute archive paths, path
   traversal, unexpected evidence, secret indicators, or a mismatched question.
4. Create the operational round before submission. Acquire the generation slot,
   or return `queued`; then execute the normal standalone round through the
   user's existing signed-in Mac Chrome. The remote client never controls CDP.
5. After staging is verified, end the initial dispatcher turn with the request
   ID and `accepted`, `queued`, `needs_user`, or `failed`. This acknowledgement
   lets the remote task finish without waiting for ChatGPT. Never acknowledge
   before the Mac owns a verified copy of the bundle.
6. Let the dispatcher heartbeat capture and verify the response. Generate the
   bounded callback with `scripts/prepare_bridge_callback.py`, then deliver its
   exact message to the returned source task/host by request ID with the Codex
   task messaging tool. Mark delivery `delivered` only after that tool succeeds.
   If the full raw
   answer is too large for a safe cross-task message, use a request-owned remote
   OS-temp result file and return its path/hash. Keep failed delivery pending;
   retry delivery only.
7. Delete the Mac staging directory after submission/capture no longer needs it.
   Once the Mac acknowledgement proves staging, the remote client may delete its
   transient source bundle. Repository writes remain separately authorized.

## Browser pacing

- Prefer one conversation per Bridge Thread. Independent deliverables receive independent Threads and conversations; never use the same Bridge Thread concurrently.
- Treat the signed-in Chrome machine as the browser host. SSH may move repository execution elsewhere, but the bundle must be staged and digest-verified on the browser host before `upload_file`; SSH reachability alone does not make a remote Codex process able to control local Chrome.
- Serialize browser-mutating critical sections with the host-global lease regardless of adapter. Separately serialize formal Pro generations by account/workspace with `manage_bridge_round.py slot-acquire`; default concurrency is one. Release the browser lease after Send acceptance and the generation slot only after a terminal remote turn.
- Keep the intended ChatGPT page open in the existing Chrome profile. Never call `close_page` as cleanup; remove the attachment, clear the composer, and reselect the exact conversation at the start of the next critical section.
- Only the declared Mac dispatcher invokes Chrome DevTools for Bridge work. Local and remote worker tasks hand requests to it instead of opening additional `--autoConnect` sessions. Chrome currently requires a human Allow for every new incoming debugging connection; the dispatcher reduces connection creation but cannot bypass that security prompt after MCP, Chrome, or Codex restarts.
- Use bounded native reads for an immediate wait. For a later wake-up, create one heartbeat watcher for the current dispatcher task from the durable round state, retain its automation ID, and poll only the exact ChatGPT conversation. Delete the watcher on captured success, explicit failure, or timeout; do not resubmit.
- Treat any `truncated: true`, incomplete status, missing target turn, or multiple plausible new turns as non-capturable. Reacquire the browser only when the full raw answer cannot be obtained natively.
- Distinguish `submitted`, `generation observed`, `response complete`, `captured`, `verified`, and `delivered`. Record observed millisecond timestamps; do not invent missing ones. Keep transport, model selection, and execution/degradation statuses separate.
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
- Never expose a Chrome debugging endpoint on a public or shared interface.
