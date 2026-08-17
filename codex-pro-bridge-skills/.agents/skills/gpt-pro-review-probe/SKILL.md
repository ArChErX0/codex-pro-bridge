---
name: gpt-pro-review-probe
description: Fast lane for high-frequency, fine-grained GPT Pro review. Prepare many independent standalone probes in parallel while serializing formal Pro generations through the recoverable Codex Pro Bridge, so unrelated Project sources never block them and degraded or interrupted rounds can resume safely. Use for one mature idea, research proposal, or atomic sub-task; use gpt-pro-project-workspace when a round needs shared durable Project context.
---

# GPT Pro Review Probe

Use this skill for the high-frequency review loop: repeatedly send a specific
idea, proposal, or atomic sub-task to GPT Pro as an adversarial reviewer, across
several research lines running in parallel.

A **Review Probe** is a `standalone`, single-round Bridge Thread. It never
attaches to a Bridge Project, so it never consults Project source-sync and is
**never blocked when unrelated shared Project sources are stale or missing**.
Each probe is its own thread with its own ledger. Evidence preparation and local
verification run in parallel; formal Pro generations default to one at a time
per ChatGPT account/workspace. Browser mutations use a separate short lease.

This skill builds on [gpt-pro-question-window](../gpt-pro-question-window/SKILL.md)
for the browser and persistence seam. Read
[its bridge protocol](../gpt-pro-question-window/references/bridge_protocol.md)
for IDs, events, and invariants.
Read its
[round-controller reference](../gpt-pro-question-window/references/round_controller.md)
before submission or watcher creation.

## When to use it

- Reviewing one mature idea, proposal, or atomic sub-task from one line.
- Running several such reviews across research lines at the same time.
- Any round where GPT Pro only needs the scoped bundle, not shared Project context.

Do NOT use it when the round genuinely needs shared durable Project sources
(a project brief, glossary, standing PRD). Use `gpt-pro-project-workspace` then.
Never let a probe author research judgment: its result is external pressure only,
recorded in the owning research system, never a decision.

## Probe identity

```text
bridge_thread_id = <track>-<candidate>-<probe-slug>-<yyyymmdd>
  e.g.  m-ai-c14-reward-leakage-20260809
```

`track` and `candidate` are opaque passthrough labels; Bridge never parses them.
One probe = one thread = one `codex-snapshot → gpt-exchange → codex-verdict`
round = disposable.

## Required flow

1. Open the probe — writes the immutable Codex snapshot and the standalone bundle:

   ```bash
   python3 .agents/skills/gpt-pro-review-probe/scripts/open_review_probe.py \
     --repo . \
     --track m-ai --candidate c14 --probe-slug reward-leakage \
     --goal "<what GPT Pro should review>" \
     --question "<focused question>" \
     --summary-file /tmp/probe-notes.md \
     --mode algorithm_review --repo-context auto
   ```

   It prints `probe_thread_id`, `codex_notes`, and `bundle`. It never runs
   source-sync and never asks for Project confirmation.

2. Use the printed `request_id` to create an operational round with the exact
   prompt digest, then acquire the account/workspace generation slot. Leave the
   probe queued when another formal Pro generation holds it. An expired slot
   requires recovery inspection and is never stolen.

3. Use the DevTools-first route in the Question Window's
   [browser adapter reference](../gpt-pro-question-window/references/browser_adapters.md)
   only from the task holding the host-global dispatcher claim. A
   non-dispatcher task hands the prepared request to that exact task instead of
   invoking DevTools. Then acquire the browser lease:

   ```bash
   python3 .agents/skills/gpt-pro-question-window/scripts/manage_browser_lease.py \
     --repo . acquire --holder <worker-id> \
     --bridge-thread-id <probe_thread_id> \
     --expected-conversation-id <reserved-chat-id>
   ```

   Chrome DevTools MCP is the primary route. The connector is permitted only
   for the documented pre-submit fallback. The lease serializes browser
   mutations across local repositories and worktrees that share the user's
   ordinary signed-in Chrome profile.

4. Stage the bundle with the Question Window's `stage_bridge_attachment.py`
   (`local` for a Mac bundle, `remote` for an SSH-hosted bundle), then upload
   its returned OS-temp path through DevTools MCP and a visible semantic control.
   Record `devtools-mcp-upload-file`; record `codex-chrome-visible-menu` only
   when the compatibility fallback was actually required. The staging command
   verifies the source and browser-host digests. Then gate the submission with
   `check_browser_preflight.py`, passing `--repo`, `--bridge-thread-id`,
   `--dispatcher-thread-id`, `--dispatcher-token`, `--browser-lease-token`, the
   canonical `--bundle`, exact `--staged-file`, `--expected-conversation-id`,
   and the `--observed-conversation-id` read from the browser. The gate fails closed on
   an expired/wrong lease, the wrong model family/effort, the wrong attachment,
   a service warning, or the wrong chat. Before Send, save the existing turn
   IDs or cursor and prompt digest as the pre-submit boundary.

5. Send once. After ChatGPT visibly accepts the prompt, record the millisecond
   submission time, transition the round to `submitted`, and release the lease:

   ```bash
   python3 .agents/skills/gpt-pro-question-window/scripts/manage_browser_lease.py \
     --repo . release --token <token>
   ```

6. Use Codex's native `read_thread` on the exact reserved ChatGPT conversation.
   Match the new user turn after the saved boundary, pin its remote turn ID, and
   accept only its `completed`, untruncated assistant reply. Then capture with
   `save_bridge_turn.py --request-id <request-id> --standalone --single-round --capture-route
   native-read-thread --remote-turn-id <turn-id>`. Do not pass the released
   browser token. Record the Codex verdict separately.

7. If the native result is unavailable, ambiguous, or `truncated`, acquire a
   new browser lease, locate the already pinned turn by ID and prompt
   fingerprint, save its exact rendered HTML through
   `build_browser_capture_spec.py` plus `evaluate_script.filePath`, convert it
   with `capture_browser_markdown.py`, and capture with `--capture-route
   browser-fallback --remote-turn-id <same-turn-id> --browser-lease-token
   <new-token>`. Never use the page's latest response by position. Release the
   new lease in all terminal paths.

8. When an immediate bounded wait is not appropriate, generate one current-task
   heartbeat with `manage_bridge_round.py watcher-spec`. Keep unchanged checks
   silent and retain the automation ID. On terminal capture, classify execution
   time, release the generation slot, delete the heartbeat, and notify once.
   `<60000 ms` is `degraded_fast`; `>=60000 ms` is only `not_fast_degraded`.
   Never let the watcher resubmit.

`open_review_probe.py` prints a handoff outline for steps 2–13 with the resolved
thread id and bundle path; scheduled-task creation and browser actions remain
host-tool operations rather than shell commands.

## Parallel operation

- **Runs in parallel across lines:** `open_review_probe.py` (snapshot + bundle),
  and the local Codex verdict. Each probe is a distinct thread with its own lock
  and ledger, so concurrent probes never fork or block each other.
- **Serialized on two resources:** browser-mutating windows use the short browser
  lease; accepted formal Pro generations use the account/workspace generation
  slot. The browser is released during generation, but the generation slot is
  held until the exact remote turn is terminal.
- **SSH boundary:** remote repository or compute work may run in parallel, but
  the browser-host dispatcher owns staging, upload, preflight, and Send. A
  remote Codex process does not inherit access to the operator's local Chrome.
- **Contamination guard:** the conversation-id gate makes "right Project, wrong
  chat" fail closed, so a probe's result can never be recorded against the wrong
  chat. Record `observed_conversation_id` on each turn for later reconciliation.

## Non-negotiable checks

- A probe is standalone: it must not attach to a Bridge Project. `--standalone`
  fails closed if the thread is attached.
- One round per probe: `--single-round` refuses a different request and permits
  idempotent recovery of the same request ID.
- Verify locally before trusting a result; keep GPT Pro output as pressure only.
- Never send without both the account generation slot and browser lease; never
  bypass the conversation-id gate.
- Never capture "the latest turn" by position alone; bind the response to the
  post-submit remote turn ID. A truncated native item is not a raw answer.
