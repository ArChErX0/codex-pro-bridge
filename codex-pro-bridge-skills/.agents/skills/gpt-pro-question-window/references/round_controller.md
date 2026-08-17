# Long-running Round Controller

Read this reference for a submitted round that may outlive the current Codex turn, for concurrent local/SSH requests, or for dispatcher restart recovery.

## State boundary

Keep canonical evidence in the repository's three-event ledger. Keep mutable operational state under `~/.codex/state/codex-pro-bridge/rounds/<request-id>.json` with `manage_bridge_round.py`. Never append `gpt-exchange` until the complete raw answer is captured.

Use one immutable `request_id` across remote bundling, Mac staging, ChatGPT submission, capture, verdict, and delivery. A retry with the same ID must resume or fail on conflicting content; it must not submit again.

## Two locks

1. Acquire the account-level generation slot before opening the composer. Default to one active Pro generation per ChatGPT account/workspace. Queue other rounds. An expired slot requires inspection of the exact conversation before release; it is never stolen automatically.
2. Hold the host-global dispatcher claim for the lifetime of the one Mac dispatcher task. Acquire the browser lease only around destination selection, upload, preflight, one Send, and browser fallback. Release it immediately after visible Send acceptance. Keep the generation slot until the remote turn is terminal.

Create the round and slot:

```bash
python3 scripts/manage_bridge_round.py create \
  --request-id '<request-id>' --bridge-thread-id '<thread-id>' \
  --source-kind local --prompt-sha256 '<sha256>' \
  --requested-model-family '<visible-family>' --requested-effort Pro \
  --account-key '<workspace/account-key>' --deadline-at '<ISO-8601>'

python3 scripts/manage_bridge_round.py slot-acquire \
  --request-id '<request-id>' --account-key '<workspace/account-key>' \
  --deadline-at '<ISO-8601>'
```

Record only safe transitions: `prepared -> queued|submitting -> submitted -> running -> capture_needed -> captured -> verdict_recorded`. Use `submit_ambiguous`, `failed`, or `timed_out` as honest exceptional states. A possible Send always becomes `submit_ambiguous`; inspect the saved boundary instead of retrying. When that exact submission is later proven accepted, transition to `submitted`, move the former failure into `recovered_diagnostics`, and clear the current failure fields.

## Heartbeat

Immediately before Send, transition `prepared|queued -> submitting` with the
exact conversation ID, pre-submit boundary, selected model family/effort, and
selection status. Then click Send once.

After visible Send acceptance:

1. Transition the round to `submitted` with the millisecond acceptance timestamp.
2. Release the browser lease, then generate a watcher payload with `watcher-spec`.
3. Create one heartbeat on the current dispatcher task and save its exact automation ID with the `watcher` command.
4. Poll only the exact ChatGPT conversation with native `read_thread`. Match the user turn after the boundary by prompt digest and pin its remote turn ID.
5. Stay silent while unchanged. On complete and untruncated, persist the target once. On truncation, reacquire the browser lease and capture that same turn. On ambiguity, explicit failure, or deadline, persist diagnostics and notify once.
6. Release the generation slot only after the target is known terminal. Delete the watcher before the terminal notification; pause it and report once if deletion fails.

Classify transport, selection, and execution separately. A complete elapsed time below `60000 ms` is `degraded_fast`. A visible downgrade/service warning is `degraded_explicit`. At least `60000 ms` is only `not_fast_degraded`, not proof of full Pro execution. Never retry a degraded round automatically.

## SSH callback

The remote client sends one schema-v2 request containing the exact remote Codex `source_thread_id` and `source_host_id`. The Mac dispatcher stages and validates the bundle, records `staging --staging-status verified` with the staged path/hash, and only then returns `accepted` or `queued`. The remote turn may then finish; it does not wait for ChatGPT.

Use this exact request shape and validate it with
`validate_remote_bridge_request.py` before invoking SSH:

```xml
<pro_bridge_request schema_version="2">
  <request_id>stable-request-id</request_id>
  <source_thread_id>exact-codex-task-id</source_thread_id>
  <source_host_id>exact-codex-host-id</source_host_id>
  <ssh_alias>devbox-review</ssh_alias>
  <remote_repository>/absolute/remote/repository</remote_repository>
  <transient_bundle_path>/tmp/codex-pro-bridge-client/stable-request-id/review.zip</transient_bundle_path>
  <bundle_sha256>lowercase-64-character-sha256</bundle_sha256>
  <focused_question>One focused review question.</focused_question>
  <evidence_paths>
    <path>repository/relative/evidence.py</path>
  </evidence_paths>
</pro_bridge_request>
```

On terminal capture, run `prepare_bridge_callback.py` for the same `request_id`.
Send its exact `message` with the Codex task messaging tool using the returned
`target_thread_id` and `target_host_id`. Only after a successful tool result run
`manage_bridge_round.py delivery --delivery-status delivered`. Send a bounded
verdict plus artifact hashes. If the full answer does not fit safely, write a
request-owned result below the remote OS temp Bridge directory by piping it over
SSH to the remote client's `prepare_review_bundle.py result-import` command;
return that command's exact path/hash. Writing into the remote repository
requires explicit authorization.
An unavailable source task remains `pending`. Retry delivery only, never
submission.

## Recovery

On dispatcher restart, run `manage_bridge_round.py list --active-only`. Resume from the recorded status:

- `submitting` or `submit_ambiguous`: inspect the exact conversation and boundary.
- `submitted` or `running`: recreate at most one watcher and poll.
- `capture_needed`: capture the pinned turn without sending.
- `captured` or `verdict_recorded`: retry verification/delivery only.
- expired generation slot: inspect the remote target before releasing it.

Keep Chrome and the dispatcher available for fallback. Local heartbeat execution pauses while the Mac sleeps or Codex is unavailable; persistent round state makes wake-up recovery safe.
