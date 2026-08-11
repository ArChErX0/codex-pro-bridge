# Concurrency and Settlement

## Concurrency layers

Treat these as separate coordination domains:

| Domain | Serialized identity | Safe parallelism |
| --- | --- | --- |
| Scientific write | canonical entity / writer scope | different non-overlapping entities |
| Persistent Pro round | Web conversation ID | different conversations |
| Browser mutation | signed-in Chrome/profile | remote generation after Send |
| Project Source apply | Project binding and manifest revision | local read-only analysis |
| Settlement | canonical base revision and close-out writer | preparation against immutable snapshots |

## Active submission

Before external Send, persist or retain transiently:

- `submission_id`;
- owning local binding and Research Line;
- Web conversation and Bridge Thread IDs;
- prompt digest and pre-submit boundary;
- `based_on_revision`;
- watcher/automation ID and deadline when used.

Queue a second submission to the same conversation. Release the browser lease after visible Send acceptance, but retain the active-round state until capture and local verdict close the round. Parallel Web generation is safe only across different conversations.

## Browser coordination

Use the browser lease supplied by `$gpt-pro-question-window` for upload, preflight, Send, and browser fallback. Require one coordinator for the signed-in Chrome/profile across repository worktrees. If the environment cannot prove host-global serialization, funnel browser mutations through one declared dispatcher and report the limitation instead of assuming repository-local locks protect the browser.

## Callback and revision races

On a Pro reply, Job result, or watcher wake-up:

1. persist the observed terminal result once;
2. compare its `based_on_revision` with the current accepted revision;
3. send a current result to local verification;
4. return a stale result as bounded pressure/Handoff;
5. clean up the exact watcher and notify once.

Never discard a valid result merely because state drifted after submission. Never let a stale callback authorize the next experiment, fork, resource expansion, or Settlement.

## Manual Web activity

Human messages, edits, regenerations, and branches made directly in the Web conversation are not Bridge submissions. Import the exact turn explicitly when useful. Record the conversation and remote turn IDs, capture provenance, and local verdict; do not fabricate a pre-submit snapshot or strict round.

## Project Sources

Project Sources are durable shared context. Task Bundles are round-scoped evidence. Resolve each external call through the Bridge router:

- use a Project round only when the decision genuinely depends on verified shared Sources;
- use a standalone persistent conversation for scoped continuity without shared Project dependence;
- use a Review Probe for an independent single-round review;
- preserve source drift and route readiness as observed facts.

A sync plan is not a remote apply. A source defect unrelated to a self-contained standalone/probe round is not a semantic dependency merely because both belong to the same research repository.

## Handoff and Settlement

Keep the state vector orthogonal:

- Task: created, running, waiting, completed, not-loaded;
- Research: active, handoff-ready, settled, paused, closed;
- External review: none, submitted, captured, verdict-pending, verified, failed;
- Run: requested, queued, running, completed, failed, cancelled;
- Archive: draft, verified, published, superseded;
- Mentor settlement: unreviewed, accepted, rejected, settled.

Close a Student cycle through:

```text
Student Handoff
  -> Mentor source/code/Run/Pro review
  -> accept, modify, or reject
  -> unique canonical Settlement writer when material
  -> Dashboard/read-model projection
```

Pro output remains pressure until local verification. A completed task, Web answer, Run, Artifact, or archive does not independently change the Candidate. Use exact base-revision/CAS behavior from the owning repository's Settlement writer; rebase and re-adjudicate a rejected stale write.
