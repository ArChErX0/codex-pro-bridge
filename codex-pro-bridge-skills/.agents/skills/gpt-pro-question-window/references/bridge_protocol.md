# Bridge Protocol

Read this reference when creating, resuming, persisting, or debugging a Codex Pro Bridge task.

## Execution scopes

Every request resolves to one scope before bridge state is written:

1. `local_only`: Codex completes the task without ChatGPT.
2. `standalone`: one Bridge Thread uses one task-scoped GPT conversation.
3. `project`: one Bridge Thread belongs to the repository's Bridge Project and
   uses a conversation inside its bound ChatGPT Project.

`standalone` is a permanent first-class mode, not an incomplete Project setup.
An existing standalone thread can later be attached to a Project without
rewriting its earlier events.

## Canonical identity

Expose one required task identifier:

```text
bridge_thread_id = <repo>-<date>-<short-task>
request_id       = <immutable-end-to-end-round-id>
```

Project mode adds:

```text
bridge_project_id = <stable-local-project-id>
remote_project_id = <observed-chatgpt-g-p-id>
```

Derive endpoint session IDs unless an existing compatible session is explicitly named:

```text
codex_session_id   = <bridge_thread_id>-codex
gpt_pro_session_id = <bridge_thread_id>-gpt-pro
```

One local repository has at most one Bridge Project, and one Bridge Project has
at most one current ChatGPT Project binding. A Bridge Thread is the Project's
task identity; do not add a parallel Workstream ID.

Once created, a Codex or GPT Pro session cannot move to another bridge thread.
A GPT Pro session also cannot move to another web conversation URL or remote
Project. A mismatch is an error, not a rename.

## Canonical events

The task history contains three normal event types:

1. `codex-snapshot`: immutable Codex notes before an external review round.
2. `gpt-exchange`: the bundle actually sent, prompt, and captured GPT Pro answer.
3. `codex-verdict`: Codex verification, decision, implementation result, tests, and next question.

Bundle construction attempts are local intermediates. Do not add them to the task timeline. The sent bundle is bound to `gpt-exchange` by relative path and SHA-256.

## Storage

```text
.codex/codex-pro-bridge/
  projects/
    index.md
    <bridge-project-id>/
      project.json             # local Project identity
      remote-binding.json      # observed ChatGPT Project binding
      activity.jsonl           # append-only Project audit ledger
      overview.md              # derived Project and task view
      PROJECT_BRIEF.md         # default stable shared source
      sources/
        manifest.json          # observed Project Source state
        plans/                 # immutable upload/removal plans
  threads/
    index.md
    <bridge-thread-id>.jsonl   # canonical append-only ledger
    <bridge-thread-id>.md      # derived sequence/timeline view
  codex-sessions/
    index.md
    <codex-session-id>/
      session.md
      notes.md                 # current mutable notes
      snapshots/               # immutable historical notes
  gpt-pro-sessions/
    index.md
    <gpt-pro-session-id>/
      session.md
      001-<slug>.md            # immutable raw exchange
      verdicts/                # immutable Codex verdicts
  bundles/                     # immutable evidence artifacts
```

Each thread JSONL ledger is the source of truth for its task history. Project
identity, binding, source state, and task membership live in the Project files
and activity ledger. Markdown timelines, overviews, sequence diagrams, and
indexes are projections and may be regenerated. Older Markdown-only threads
are imported into JSONL on their next write.

Repository-local installation adds `.agents/` and `.codex/` to that repository's local `.git/info/exclude`. Keep bridge state local unless the user explicitly chooses to publish selected artifacts.

All timestamps include a timezone. Event IDs are unique, and each event points to its parent event. Artifact records contain repository-relative paths and SHA-256 digests.

Mutable wait/queue state is host-local operational state, not a canonical event:

```text
~/.codex/state/codex-pro-bridge/
  rounds/<request-id>.json
  locks/pro-generation-<account-hash>.lease.json
```

Read [round_controller.md](round_controller.md) for safe transitions, generation
serialization, heartbeat creation, SSH callback, and restart recovery. Keep the
three-event repository ledger unchanged.

## Round lifecycle

### 0. Resolve the route

Preview a decision before creating notes or bundles:

```bash
python3 .agents/skills/gpt-pro-project-workspace/scripts/resolve_bridge_route.py \
  --repo . \
  --task "<decision or deliverable>" \
  --external-reasoning
```

Codex substitutes `--local-only` when no external round is useful. For
`local_only`, stop the external workflow. For a ready Project decision,
rerun with `--apply` once to attach or reuse its Bridge Thread. If the decision
requires confirmation, create, bind, or verify the Project first. Do not
silently fall back to standalone when this repository already has a Bridge
Project with a stale or unverified binding.

Exit codes: `0` = ready (attached or clean), `3` = needs human confirmation
(the printed JSON lists `requires_confirmation`), `2` = bad invocation.
Treat `3`, not `2`, as the confirmation signal.

### High-frequency Review Probes

For repeated, fine-grained, parallel review of one idea, proposal, or atomic
sub-task, use standalone Review Probes instead of Project rounds. A probe never
attaches to a Bridge Project, so it never consults Project source-sync and is
never blocked when unrelated shared sources are stale. See
[../../gpt-pro-review-probe/SKILL.md](../../gpt-pro-review-probe/SKILL.md).
Evidence preparation and local verification may run in parallel because each
probe has its own ledger. Formal Pro generations use the account/workspace slot;
browser-mutating windows use the shorter browser lease.

### 1. Snapshot

Write current notes and an immutable snapshot:

```bash
python3 .agents/skills/bundle-algorithm-context/scripts/prepare_codex_session_notes.py \
  --repo . \
  --bridge-thread-id <thread-id> \
  --goal "<goal>" \
  --gpt-pro-question "<question>" \
  --summary-file /tmp/codex-summary.md
```

In Project mode, add `--bridge-project-id <project-id>` to snapshot, bundle,
exchange-capture, and verdict commands.

Completion criterion: `notes.md`, an immutable snapshot, one `codex-snapshot` event, and the Codex session index all exist and agree on the same thread ID.

### 2. Bundle

Build a new artifact. Existing output files are never overwritten:

```bash
python3 .agents/skills/bundle-algorithm-context/scripts/build_algorithm_bundle.py \
  --repo . \
  --bridge-thread-id <thread-id> \
  --goal "<goal>" \
  --question "<question>" \
  --mode algorithm_review \
  --format zip \
  --repo-context auto
```

By default the builder uses the latest immutable Codex notes snapshot recorded in `session.md`, not the mutable `notes.md` pointer.

Completion criterion: the manifest names the correct mode, lists every supplied file, contains no absolute local repository path, passes zip integrity checks, and is small enough to upload. Bundle creation alone does not add a task event.

For `auto`, use any `--include` paths as required focus seeds, close their definitely-local source dependencies before adding breadth, and fail if the required closure exceeds `--max-files`. For `explicit`, require at least one include and fail when any requested file is filtered or omitted; `--allow-incomplete-includes` is an audited escape hatch, not a default. Modern Node evidence includes `.mjs`, `.cjs`, `.mts`, and `.cts`. For `none`, `--max-files 0` is valid.

### 2.5 Browser preflight

After the visible attachment chip, model family, and effort are observable,
create the operational round, acquire its account-level generation slot, then
gate submission:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/manage_browser_lease.py \
  --repo . acquire \
  --holder '<worker-id>' \
  --bridge-thread-id '<thread-id>' \
  --expected-conversation-id '<reserved chat id>'

python3 .agents/skills/gpt-pro-question-window/scripts/check_browser_preflight.py \
  --repo . \
  --bridge-thread-id '<thread-id>' \
  --dispatcher-thread-id '<current-dispatcher-task-id>' \
  --dispatcher-token '<token from manage_bridge_dispatcher.py claim>' \
  --browser-lease-token '<token returned above>' \
  --requested-model-family '<required family>' \
  --selected-model-family '<exact visible family>' \
  --requested-effort Pro \
  --selected-effort '<exact visible effort>' \
  --bundle /absolute/path/to/bundle.zip \
  --staged-file '<absolute OS-temp file passed to upload_file>' \
  --attachment-name '<visible filename>' \
  --upload-control '<observed-upload-route>' \
  --expected-conversation-id '<reserved chat id>' \
  --observed-conversation-id '<visible chat id>'
```

Only click Send when this command succeeds. A subscription/account label does
not establish the selected family or effort. A visible downgrade, rate-limit,
service, or account-protection warning fails the gate.

For Project mode, also supply `--expected-project-id`,
`--observed-project-id`, `--expected-workspace`, `--observed-workspace`,
`--expected-account-label`, `--observed-account-label`, and
`--binding-status active`. Expected values come from routing; observed values
come from the visible destination, not from a same-titled sidebar item.

### 2.6 Submission handoff and response watcher

Before Send, use the stable ChatGPT conversation ID exposed by Codex's native
chat reference or the verified conversation URL. An `@`-mentioned chat supplies
this identity; it is not a completion subscription. Titles are display
metadata, not identity. When native reads are available, record the existing
turn IDs or cursor as the pre-submit boundary and keep a digest of the exact
prompt.

Acquire the account-level generation slot before entering the browser critical
section. Then acquire the host-global browser lease only to open the exact chat,
upload, preflight, and click Send once. Treat the submission as accepted only
after the user message or generating state is visible. Immediately before Send,
transition the operational round to `submitting` with the exact conversation,
boundary, and selected model family/effort. Record the observed
millisecond submission time and transition the operational round to `submitted`,
then release the browser lease in all paths. Keep the generation slot until the
target remote turn is terminal; do not hold the browser lease while it generates.

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/manage_browser_lease.py \
  --repo . release --token '<token>'
```

Prefer the Codex-native `read_thread` tool for response retrieval:

1. Read the exact ChatGPT conversation ID, never a same-titled chat.
2. Identify the new user turn after the saved boundary and verify it against the
   submitted prompt or its unique fingerprint. Pin that remote turn ID—the ID
   of the turn containing the target user message and its direct assistant
   reply; never use "the latest turn" as the sole selector.
3. Accept only that turn's direct assistant reply when the turn status is
   `completed`, no error is present, and the returned assistant item is not
   marked `truncated`.
4. If the native tool is unavailable, the target is ambiguous, or any required
   item is truncated, reacquire a new browser lease, locate that already pinned
   turn by ID and prompt fingerprint, and copy its full response from the
   verified conversation. Pass the same remote turn ID to persistence and
   release the new lease after capture or failure. Never substitute the page's
   bottom-most response.

`wait_threads` waits for Codex tasks, not ChatGPT chats. For a short wait, poll
`read_thread` with bounded intervals. For a later wake-up, generate the watcher
payload from the operational round and create one heartbeat attached to the
current dispatcher task—not a standalone cron or worktree task. Retain its
automation ID. Its durable prompt contains the conversation ID, pre-submit
boundary, prompt digest, deadline, and these terminal rules:

- On no change, do not notify, resubmit, or modify Bridge state.
- On a complete, untruncated target reply, capture it once, then delete the
  automation before the single terminal notification.
- On a complete but truncated target reply, the same watcher attempts the
  browser fallback under a new lease. If the lease is temporarily busy before
  the deadline, stay silent and retry later; do not create another watcher. On
  fallback capture, delete the watcher. On deadline or a non-retryable browser
  failure, record diagnostics, delete the watcher, and report once.
- On an explicit remote failure or deadline expiry, delete the automation and
  report the failure once; never create a duplicate watcher.

Measure completion from accepted Send to the complete target response with
millisecond timestamps. Record `<60000 ms` as `degraded_fast`. Record a visible
downgrade/service signal as `degraded_explicit`. Record `>=60000 ms` only as
`not_fast_degraded`; it does not prove full Pro execution. Keep transport,
model-selection, and execution status separate, and leave retry to the user.

On every terminal path, persist the capture or diagnostics before cleanup, then
delete the watcher by its exact automation ID. If deletion is rejected, pause
it immediately and report the cleanup failure only once. This fallback applies
equally to native success, browser-fallback success, explicit failure, and
timeout.

Release the generation slot after the exact remote target is known terminal.
An expired slot is a recovery gate, not permission to submit another round.

The wait is transient state, not a fourth canonical event. Until the full raw
answer is captured, do not append `gpt-exchange`. A failed or timed-out attempt
therefore leaves an honestly incomplete round, and `--require-complete-rounds`
must continue to fail.

### 3. Exchange capture

After the full answer is available, immediately capture the raw exchange:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/save_bridge_turn.py \
  --repo . \
  --bridge-thread-id <thread-id> \
  --request-id <request-id> \
  --web-url https://chatgpt.com/c/... \
  --web-title "<observed title>" \
  --purpose "<task purpose>" \
  --bundle .codex/codex-pro-bridge/bundles/<bundle>.zip \
  --requested-model-family '<required family>' \
  --selected-model-family '<exact visible family>' \
  --requested-effort Pro \
  --selected-effort '<exact visible effort>' \
  --attachment-name '<visible filename>' \
  --attachment-sha256 '<staged SHA-256 recorded before cleanup>' \
  --upload-control visible-menu \
  --submitted-at '<ISO-8601 with timezone>' \
  --generation-observed-at '<ISO-8601 with timezone>' \
  --response-completed-at '<ISO-8601 with timezone>' \
  --capture-route native-read-thread \
  --remote-turn-id '<matched completed turn id>' \
  --prompt-file /tmp/gpt-pro-prompt.md \
  --answer-file /tmp/gpt-pro-answer.md
```

For native capture, omit the already released browser lease token. Also pass
the staged file digest as `--attachment-sha256`; this proves an aliased visible
filename represents the canonical bundle even after OS-temp cleanup. For browser
fallback, export `innerHTML` only from the exact pinned assistant turn into a
schema-v1 payload using `evaluate_script.filePath` below the OS temp root, then
run `scripts/capture_browser_markdown.py`; pass its output
to `save_bridge_turn.py` with `--capture-route browser-fallback`, the newly
acquired `--browser-lease-token`, and the same `--remote-turn-id`. Release that
lease after capture. Never save `innerText` or a truncated native item as the
raw answer.

Capture the raw answer even when the observed model is mismatched or unverified, but preserve that status and do not claim the answer came from Pro.

For Project mode, also pass `--bridge-project-id <project-id>`,
`--remote-project-id <g-p-id>`, `--observed-workspace <workspace>`, and
`--observed-account-label <account-label>`.

Completion criterion: a numbered immutable turn exists; its bundle digest matches the file sent; its capture route and target remote turn are recorded; model and attachment provenance are recorded truthfully; the GPT Pro session remains bound to one thread and one ChatGPT URL; and one `gpt-exchange` event points to the turn.

### 4. Codex verdict

Verify the answer against local files, then record a separate verdict:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/record_codex_verdict.py \
  --repo . \
  --bridge-thread-id <thread-id> \
  --turn .codex/codex-pro-bridge/gpt-pro-sessions/<session>/001-<slug>.md \
  --summary-file /tmp/codex-summary.md \
  --verification-file /tmp/codex-verification.md \
  --decision-trail-file /tmp/decision-trail.md \
  --tests-file /tmp/tests.md \
  --next-question-file /tmp/next-question.md
```

Completion criterion: an immutable verdict artifact and one `codex-verdict` event point to the captured GPT Pro turn. Never edit the raw GPT Pro answer to add later conclusions.

### 5. Verify the thread

Before another round and before final handoff, verify the append-only chain and every referenced artifact:

```bash
python3 .agents/skills/gpt-pro-question-window/scripts/verify_bridge_thread.py \
  --repo . \
  --bridge-thread-id <thread-id> \
  --require-complete-rounds \
  --require-verified-provenance
```

The strict verifier also fails when a modern request lacks its exact remote turn,
capture route, complete model-family/effort pair, or hash-backed attachment
provenance. Without `--require-verified-provenance`, it reports those items as
warnings so immutable historical captures remain readable without being
misrepresented as verified.

Project mode also requires:

```bash
python3 .agents/skills/gpt-pro-project-workspace/scripts/verify_bridge_project.py \
  --repo . \
  --bridge-project-id <project-id> \
  --require-active-binding
```

## Evidence scope

- Default to repository-contained paths.
- Reject missing includes and paths that resolve outside the repository.
- Use `--allow-external-include` only after inspecting and confirming each external file. External archive names are anonymized.
- Include tracked and untracked non-ignored files in auto-selection.
- Fail on high-confidence secret patterns unless a human has reviewed the exact files and explicitly allows them.
- Never rewrite ordinary source contents. Include a file as evidence or omit it.
- Use a safe repository label in uploaded manifests; never expose the absolute local repository path.

## Context policy

- `auto`: first round or implementation-heavy review. Rank focus, then keep its definitely-local dependency closure whole before adding breadth.
- `explicit`: follow-up round; include only named changed or newly relevant files. Every requested safe file must be included unless the operator deliberately uses the incomplete-evidence override.
- `none`: reasoning-only follow-up with notes and compact event context.

Keep the full ledger local. Bundles use the latest 24 events and 20,000 characters by default. Increase either limit only when an older event is directly relevant.

Project Sources are durable context shared across Project conversations. Task
Bundles are immutable, round-scoped evidence. Do not upload volatile diffs and
logs as Project Sources, and do not use Project Sources as a substitute for
recording exactly what one review round saw.

## Browser route

Read [browser_adapters.md](browser_adapters.md) and use Chrome DevTools MCP for each browser-mutating critical section. The Codex Chrome connector is a compatibility fallback only when DevTools MCP is unavailable or fails before upload/Send while the composer remains empty. The connector alone requires its extension and **Allow access to file URLs** permission.

1. Use the user's existing signed-in stable Chrome profile through `--autoConnect`. Ensure `chrome://inspect/#remote-debugging` is enabled and let the user approve a newly attached dispatcher MCP process. Route repeated local and remote reviews through that one long-lived Mac dispatcher; approval may recur after MCP, Chrome, or Codex restarts.
2. Acquire the browser lease before destination selection, upload, preflight, or Send. The lease applies to every adapter.
3. Select the exact conversation by stable URL/ID, then use a visible semantic upload control.
4. Stage the bundle on the browser host with `stage_bridge_attachment.py local` or `remote`. Upload its returned OS-temp path and verify the exact attachment chip. Record `devtools-mcp-upload-file`, or `codex-chrome-visible-menu` only for an observed connector fallback, as the upload control.
5. Read the exact selected model family and effort and run `check_browser_preflight.py`; do not send on mismatch or a visible service/account warning.
6. In Project mode, open the saved Project URL and verify its visible ID,
   account/workspace, and active local binding before creating or reusing a
   conversation.
7. Use Computer Use only when neither browser route can control a native or graphical UI boundary.
8. For a dry run, remove the attachment and verify the composer is empty. Never close the selected ChatGPT page as cleanup. Delete only the run-owned staging directory in `finally`.

Release the browser lease after Send is visibly accepted. Observe the remote
generation through the native response handoff above; do not resubmit. Reacquire
the browser only for a required full-answer fallback, and record only timestamps
actually observed. On a stalled or failed state, capture diagnostics and stop
instead of duplicating the request.

If DevTools MCP fails before upload/Send and the composer remains empty, the connector may be tried once. A ChatGPT service rejection is not a browser-route failure. Stop for CAPTCHA, rate limits, abuse warnings, unusual login, passwords, 2FA, remote-debugging permission, or account-security prompts.

The browser profile and advisory lease are host-local and shared across repositories and worktrees. Page IDs and independent MCP processes do not provide mutual exclusion; the host-global lease does. Keep MCP and Chrome on the same host. When repository execution is remote, stage and digest-verify the approved bundle on the browser host before upload. A remote Codex process cannot discover or drive the operator's local Chrome; use the local dispatcher.
