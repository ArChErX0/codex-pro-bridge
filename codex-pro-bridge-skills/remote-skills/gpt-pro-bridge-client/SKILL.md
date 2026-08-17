---
name: gpt-pro-bridge-client
description: Submit a focused review asynchronously from a remote Codex task to the user's Mac-hosted GPT Pro Bridge and receive the verified result in the same source task. Use when a remote SSH Codex session needs GPT Pro to review scoped code, data, experiment evidence, or a consequential proposal. Do not use for ordinary local reasoning or to expose/control the Mac browser directly.
---

# GPT Pro Bridge Client

Use exactly one deployment-local Mac Codex task as the browser dispatcher.
Before first use, require a configured client:

```bash
python3 ~/.codex/skills/gpt-pro-bridge-client/scripts/prepare_review_bundle.py \
  config-status
```

If it is not configured, stop and ask the user or Mac dispatcher owner to run
the same script once with `configure --dispatcher-thread-id <exact-task-id>
--dispatcher-host-id <exact-host-id> --ssh-alias <Mac-to-remote-alias>`. Never
hardcode or guess these deployment identities in a distributable Skill.
Configuration is idempotent for the same values and rejects rebinding; use
`--replace` only after explicitly verifying that the previous dispatcher has
been retired.

The remote task owns evidence selection and local verification. The dispatcher
owns SSH staging, the signed-in Mac Chrome profile, GPT Pro submission, capture,
and Bridge persistence. Never create an SSH/CDP tunnel or start Chrome remotely.

## Submit one review

1. Define one focused question and choose only the files needed to answer it.
   Inspect every selected path. Exclude secrets, credentials, cookies, user data,
   large raw datasets, and unrelated repository content.
2. Resolve the exact current remote Codex `hostId`; do not derive it from the SSH
   alias. Build a transient explicit bundle without modifying the repository:

   ```bash
   python3 ~/.codex/skills/gpt-pro-bridge-client/scripts/prepare_review_bundle.py \
     prepare --repo . --source-thread-id '<current-codex-task-id>' \
     --source-host-id '<exact-current-codex-host-id>' \
     --question '<focused review question>' \
     --include '<repo-relative-file-or-small-directory>' \
     --include '<another-path>'
   ```

   Use only the returned request, dispatcher, bundle, digest, and manifest facts.
   Do not paste file contents into the cross-task message.
3. Call `codex_app__send_message_to_thread` exactly once with
   `threadId=<returned dispatcher_thread_id>`,
   `hostId=<returned dispatcher_host_id>`, and a short instruction to use
   `$gpt-pro-question-window` followed by the exact returned `request_xml`.
   Do not reconstruct, rename, or add XML fields. Its shape is:

   ```text
   Use $gpt-pro-question-window to execute this remote review request.

   <pro_bridge_request schema_version="2">
     <request_id>...</request_id>
     <source_thread_id>...</source_thread_id>
     <source_host_id>...</source_host_id>
     <ssh_alias>...</ssh_alias>
     <remote_repository>...</remote_repository>
     <transient_bundle_path>...</transient_bundle_path>
     <bundle_sha256>...</bundle_sha256>
     <focused_question>...</focused_question>
     <evidence_paths>
       <path>one repository-relative path</path>
       <path>another repository-relative path</path>
     </evidence_paths>
   </pro_bridge_request>
   ```

4. Wait only for the initial dispatcher acknowledgement. Accept only the same
   `request_id` with status `accepted`, `queued`, `needs_user`, or `failed` and
   `staging_verified=true` for every non-staging failure. A timeout means inspect
   that dispatcher/request once; never resubmit. After `accepted` or `queued`,
   end the current turn instead of waiting for ChatGPT. The Mac heartbeat owns
   the long wait and later calls this exact source task/host.
5. After a staging-verified acknowledgement or terminal staging failure, clean
   only this request's transient source bundle:

   ```bash
   python3 ~/.codex/skills/gpt-pro-bridge-client/scripts/prepare_review_bundle.py \
     cleanup --bundle '<exact bundle path returned by prepare>'
   ```

6. On a later `<pro_bridge_result schema_version="2">` callback, accept only the
   matching request ID, source task/host, and recorded bundle digest. Keep transport status, model
   family/effort verification, and execution status separate. Treat
   `degraded_fast`, `degraded_explicit`, or model mismatch as an unsuccessful
   formal Pro review; never retry automatically.
7. If the callback contains a remote result path, verify it with:

   ```bash
   python3 ~/.codex/skills/gpt-pro-bridge-client/scripts/prepare_review_bundle.py \
     result-status --request-id '<request-id>'
   ```

   Require the returned path/hash to match the callback. Read it, then call
   `result-cleanup --request-id '<request-id>'` in `finally`. A missing callback
   remains delivery-pending; it is not permission to submit again.
8. Re-open the remote evidence, evaluate the Pro response, and answer the user
   with accepted claims, rejected claims, unknowns, and next actions. GPT Pro is
   external review pressure, not ground truth.

## Failure rules

- Send one request ID once. After timeout, ambiguous completion, possible
  submission, or missing callback, inspect/resume that ID; retry delivery only.
- Do not use the Mac debugging endpoint, Chrome profile, passwords, or cookies
  from the remote host.
- Do not broaden the evidence set after bundling unless the user approves a new
  round.
- Always report whether the transient source bundle and any returned result file
  were cleaned.
