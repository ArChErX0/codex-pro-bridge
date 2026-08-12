---
name: gpt-pro-review-probe
description: Fast lane for high-frequency, fine-grained, parallel GPT Pro review. Send one mature idea, research proposal, or atomic sub-task from one research line to GPT Pro as a standalone, single-round Review Probe that never touches the Bridge Project source-sync gate, so unrelated stale shared sources never block it and many probes run in parallel. Use when reviewing per-idea/per-proposal/per-task across several research lines at once; use gpt-pro-question-window or gpt-pro-project-workspace when a round genuinely needs shared durable Project context.
---

# GPT Pro Review Probe

Use this skill for the high-frequency review loop: repeatedly send a specific
idea, proposal, or atomic sub-task to GPT Pro as an adversarial reviewer, across
several research lines running in parallel.

A **Review Probe** is a `standalone`, single-round Bridge Thread. It never
attaches to a Bridge Project, so it never consults Project source-sync and is
**never blocked when unrelated shared Project sources are stale or missing**.
Each probe is its own thread with its own ledger, so many probes run in parallel
safely; only browser-mutating critical sections are serialized by a
repository-local advisory lease. Across worktrees, use one declared dispatcher.

This skill builds on [gpt-pro-question-window](../gpt-pro-question-window/SKILL.md)
for the browser and persistence seam. Read
[its bridge protocol](../gpt-pro-question-window/references/bridge_protocol.md)
for IDs, events, and invariants.

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

2. Select an adapter using the Question Window's
   [browser adapter reference](../gpt-pro-question-window/references/browser_adapters.md),
   then acquire the browser lease before touching the signed-in Chrome profile:

   ```bash
   python3 .agents/skills/gpt-pro-question-window/scripts/manage_browser_lease.py \
     --repo . acquire --holder <worker-id> \
     --bridge-thread-id <probe_thread_id> \
     --expected-conversation-id <reserved-chat-id>
   ```

   The lease serializes browser mutations within this repository regardless of
   adapter. Across repositories or worktrees, use one declared dispatcher.

3. Upload the bundle through the selected adapter and a visible semantic
   control. Record `devtools-mcp-upload-file` or
   `codex-chrome-visible-menu`, then gate the submission with
   `check_browser_preflight.py`, passing `--repo`, `--bridge-thread-id`, the
   `--browser-lease-token`, `--expected-conversation-id`, and the
   `--observed-conversation-id` read from the browser. The gate fails closed on
   an expired/wrong lease, the wrong model, the wrong attachment, or the wrong
   chat. Before Send, also save the existing turn IDs or cursor and prompt
   digest as the pre-submit boundary.

4. Send once. After ChatGPT visibly accepts the prompt, record the submission
   time and target turn ID when available, then release the lease immediately:

   ```bash
   python3 .agents/skills/gpt-pro-question-window/scripts/manage_browser_lease.py \
     --repo . release --token <token>
   ```

5. Use Codex's native `read_thread` on the exact reserved ChatGPT conversation.
   Match the new user turn after the saved boundary, pin its remote turn ID, and
   accept only its `completed`, untruncated assistant reply. Then capture with
   `save_bridge_turn.py --standalone --single-round --capture-route
   native-read-thread --remote-turn-id <turn-id>`. Do not pass the released
   browser token. Record the Codex verdict separately.

6. If the native result is unavailable, ambiguous, or `truncated`, acquire a
   new browser lease, locate the already pinned turn by ID and prompt
   fingerprint, and capture its full direct reply with `--capture-route
   browser-fallback --remote-turn-id <same-turn-id> --browser-lease-token
   <new-token>`. Never use the page's latest response by position. Release the
   new lease in all terminal paths.

7. When an immediate bounded wait is not appropriate, use one current-task
   heartbeat to poll `read_thread`. Keep unchanged checks silent and retain the
   returned automation ID. On capture, explicit failure, or timeout, delete the
   heartbeat before the single terminal notification. If deletion fails on any
   terminal path, pause it and report that cleanup failure once. Never let it
   resubmit.

`open_review_probe.py` prints a handoff outline for steps 2–7 with the resolved
thread id and bundle path; scheduled-task creation and browser actions remain
host-tool operations rather than shell commands.

## Parallel operation

- **Runs in parallel across lines:** `open_review_probe.py` (snapshot + bundle),
  and the local Codex verdict. Each probe is a distinct thread with its own lock
  and ledger, so concurrent probes never fork or block each other.
- **Serialized on one resource:** each browser-mutating window (attach/upload/
  preflight/Send, plus a browser fallback capture) is behind the browser lease.
  The lease is released while remote generations run, so several generations
  may remain in flight without several workers driving Chrome at once.
- **Contamination guard:** the conversation-id gate makes "right Project, wrong
  chat" fail closed, so a probe's result can never be recorded against the wrong
  chat. Record `observed_conversation_id` on each turn for later reconciliation.

## Non-negotiable checks

- A probe is standalone: it must not attach to a Bridge Project. `--standalone`
  fails closed if the thread is attached.
- One round per probe: `--single-round` refuses a second exchange on a thread.
- Verify locally before trusting a result; keep GPT Pro output as pressure only.
- Never send while another probe holds the browser lease; never bypass the
  conversation-id gate.
- Never capture "the latest turn" by position alone; bind the response to the
  post-submit remote turn ID. A truncated native item is not a raw answer.
