---
name: coordinate-auto-research
description: Coordinate the single-user Auto Research system across Primary, Research Orchestrator, Track Mentor, Student, local Codex chats, and one-to-many GPT Pro web conversations. Use when an Auto Research request concerns Research Line identity, Idea Variants, Student or Mentor routing, Chat binding, Pro Teacher conversation lineage, WIP, Handoff, asynchronous replies, or Settlement. Delegate external review and experiments to the existing Bridge skills; use those skills directly for generic work outside an Auto Research SOP.
---

# Coordinate Auto Research

Treat Auto Research as one user's research organization, not a multi-user permission system. Keep scientific identity, local Chat ownership, Web conversation instances, external pressure, and canonical settlement separate.

## Load the live contract

1. Locate the repository root and read its live `AUTO_RESEARCH_SOP.md`.
2. Read `CONTEXT.md` for entity meanings and `OPERATIONS.md` for Evidence, Run, Decision, and Settlement rules when present.
3. Read the coordination registry or operations projection for current Chat bindings. If no registry exists, mark bindings `unknown`; do not infer a created Student or Web conversation from a Candidate, title, directory, or historical transcript.
4. Record the SOP/canonical revision that the action is based on. Treat a dirty working tree as live evidence, not as a committed revision.

Completion criterion: report Task, Research, External Review, and Resource state separately, with every unverifiable binding or live fact marked `unknown`.

## Compile the request

Classify the user's message into one primary intent:

- `inspect`: recover state only;
- `steer`: propose a priority or direction change;
- `continue`: advance the accepted Current Next Evidence without expanding scope;
- `correct`: apply a scoped protocol or instruction delta;
- `authorize`: permit the named Chat, external call, experiment, resource, or write effect;
- `settle`: close a cycle through Handoff, local review, and the canonical writer.

Resolve `scope`, `primary_owner`, `decision_owner`, `writer`, `allowed_effect`, `next_action`, and `stop_condition`. Treat a question as `inspect`. When one message contains several actions, state their order and stop before any effect that lacks authority.

Read [invocation-and-authority.md](references/invocation-and-authority.md) when the request crosses local roles, changes WIP, creates or replaces a Chat, or could alter canonical state.

Completion criterion: exactly one primary intent and one writer are named; any scope expansion is explicit.

## Preserve Research Line identity

Keep work in the same Student when the core problem, primary estimand, kill rule, and paper-shaped scientific identity still jointly survive. Keep claim narrowing, evidence updates, reproduction, ablation, confirmation, a second implementation, and ordinary Idea Variants inside that Student's active local Codex Chat.

Propose a new Student only when the new object can survive the parent's failure, has an independent Claim and kill rule, and has a distinct evidence or promotion regime. Creating a user-visible Student or fork Chat requires explicit authorization.

Completion criterion: identify the existing `research_line_id`, or label the object a Seed/fork proposal rather than inventing a Student binding.

## Resolve local-to-Web topology

Model one Student as:

```text
one Research Line
  one active local Codex Chat
  one logical Pro conversation lineage
    zero or one active primary Web conversation
    zero to many independent probes
    zero to many superseded continuation segments
    zero to many fork explorations
```

Allow one local Chat to own many Web conversations. Allow each Web conversation exactly one owning local binding; other local Chats may reference it read-only. An `@` mention selects a conversation but does not create ownership, completion subscription, or a strict Bridge round.

Read [chat-lineage.md](references/chat-lineage.md) whenever a request creates, reuses, references, imports, continues, or forks a Web conversation. If a JSON mapping snapshot exists, validate it before external effects:

```bash
python3 .agents/skills/coordinate-auto-research/scripts/validate_auto_research_mapping.py <mapping.json>
```

Completion criterion: the owning local binding, Research Line, conversation kind, conversation ID, Bridge Thread ID, and active submission state are explicit or `unknown`.

## Delegate the capability

Select the narrowest downstream capability:

| Need | Delegate to |
| --- | --- |
| Existing or persistent Web conversation | `$gpt-pro-question-window` |
| Independent Cold, adversarial, evidence-delta, or atomic probe | `$gpt-pro-review-probe` |
| ChatGPT Project binding or stable shared Sources | `$gpt-pro-project-workspace` |
| Immutable repository evidence | `$bundle-algorithm-context` |
| Paper framing or reviewer pressure | `$gpt-pro-paper-brainstormer` |
| Deep algorithm or experiment critique | `$gpt-pro-research-algorithm-reviewer` |
| Strict external-review-to-local-verdict loop | `$gpt-pro-algorithm-pipeline` |
| Experiment matrix | `$experiment-plan-generator` |
| Proposal/code/config/result consistency | `$implementation-consistency-checker` |

Retain Auto Research ownership of scope, identity, authority, Handoff, and canonical effects. Treat downstream outputs as artifacts, diagnostics, or pressure until the owning local role verifies them.

All downstream ChatGPT page effects inherit the Question Window's DevTools-first browser route. SSH may move repository and compute execution to another host, but staging, upload, preflight, and Send remain owned by the declared browser-host dispatcher.

Completion criterion: the selected capability returns to the originating local owner without changing Research Line identity or canonical state on its own.

## Guard asynchronous work

Before Send, bind one `submission_id` to the local owner, Web conversation, prompt digest, pre-submit boundary, Bridge Thread, and `based_on_revision`. Permit at most one active submission per Web conversation. Parallelize across distinct conversations, not inside one persistent conversation.

On completion, match the exact remote turn. Persist the raw answer before cleanup; then delete or pause the exact watcher. If the current research revision differs from `based_on_revision`, preserve the result as stale pressure and return it for re-adjudication instead of automatically continuing.

Read [concurrency-and-settlement.md](references/concurrency-and-settlement.md) for browser serialization, Project Source drift, manual Web turns, callback races, Handoff, and Settlement.

Completion criterion: every terminal submission is captured once or fails once, cleans up its watcher, and never resubmits implicitly.

## Close the cycle

Keep these stages distinct:

```text
planned -> submitted -> observed -> captured -> locally-verified
        -> mentor-accepted -> settled -> projected
```

Record Pro output as external pressure. Have local Codex and the scientific decision owner issue `accept`, `modify`, or `reject`. Send a bounded Handoff to the Mentor. Use the repository's unique Settlement writer for material changes; keep `no-material-change` in the cycle/Handoff when the writer does not support empty events.

Completion criterion: the cycle is `settled`, `paused`, or `closed`; the next owner, next evidence or unlock condition, and projection status are explicit.
