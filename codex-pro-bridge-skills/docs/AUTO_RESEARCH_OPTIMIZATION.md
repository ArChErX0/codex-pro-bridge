# Auto Research Skill Optimization

## Outcome

Add one model-invoked `$coordinate-auto-research` skill above the generic Codex Pro Bridge capabilities. Keep Auto Research organization and scientific identity in the owning repository; keep browser, Project, bundle, exchange, verdict, experiment, and consistency behavior in the existing Bridge skills.

The target model is single-user and multi-Chat:

```text
one local role Chat
  -> one owned scientific scope
  -> zero or one logical Pro lineage per scope
  -> zero or one active primary Web conversation
  -> zero to many probes and superseded continuation segments
```

Web Chat count never determines Student count. One Student owns one Research Line and one active local Codex Chat. Idea Variants remain inside that Student until an independently viable problem, Claim, kill rule, evidence regime, and writer boundary justify a user-authorized fork.

## Responsibility boundary

`$coordinate-auto-research` owns:

- local role and Research Line recovery;
- `inspect / steer / continue / correct / authorize / settle` compilation;
- Student-versus-Variant identity;
- local-to-Web conversation lineage;
- active-submission and based-on-revision guards;
- downstream capability selection;
- Handoff and Settlement routing.

Existing skills retain:

- `$gpt-pro-question-window`: browser and persistent conversation seam;
- `$gpt-pro-review-probe`: independent single-round probe;
- `$gpt-pro-project-workspace`: Project binding and durable Sources;
- `$bundle-algorithm-context`: immutable evidence bundle;
- reviewer/brainstorm/pipeline skills: external review shape;
- experiment and consistency skills: local planning and verification.

The owning Auto Research repository retains canonical Research State, resource authorization, Run/Artifact semantics, Mentor judgment, and Settlement.

## Package changes

The first implementation adds:

- `coordinate-auto-research/SKILL.md`: compact workflow and routing table;
- `references/invocation-and-authority.md`: local role, intent, authority, and trigger competition;
- `references/chat-lineage.md`: one-to-many local/Web mapping and conversation kinds;
- `references/concurrency-and-settlement.md`: active round, callback, source, browser, and close-out rules;
- `scripts/validate_auto_research_mapping.py`: read-only JSON mapping validation;
- unit tests, installer registration, one README row, one workflow section, and one usage prompt.

The validator is not a second canonical database. It accepts an exported coordination snapshot and checks identity and concurrency invariants.

## Rollout plan

### Phase 1 — Routing contract

Install the skill globally and expose it to Auto Research repositories. Use it for read-only recovery, role/scope routing, Student-versus-Variant decisions, and downstream Skill selection.

Exit criterion: every invocation names one intent, scope, owner, writer, allowed effect, stop condition, and based-on revision; missing bindings remain `unknown`.

### Phase 2 — Chat registry adapter

Implement the owning repository's coordination registry and export its local/Web mapping to the validator shape. Register Primary, Mentor, Student, Research Line, local task, Pro lineage, physical Web conversations, and binding lifecycle without copying scientific facts.

Exit criterion: one Student has one active local binding; one Web conversation has one owner; one owner/scope has at most one active Pro lineage and primary conversation.

### Phase 3 — Submission lifecycle

Persist transient `submission_id`, exact conversation and remote turn, prompt digest, pre-submit boundary, Bridge Thread, based-on revision, watcher ID, and deadline. Queue a second send to the same conversation. Keep waiting state outside the canonical three-event Bridge ledger.

Exit criterion: each submission captures or fails once, watcher cleanup is terminal and idempotent, and a stale callback cannot continue work automatically.

### Phase 4 — Handoff and Settlement adapter

Generate immutable Student Handoffs, capture Mentor accept/modify/reject, and invoke the repository's exact base-revision Settlement writer for material changes. Keep `no-material-change` in Cycle/Handoff until the canonical writer supports it.

Exit criterion: Pro pressure, local verdict, Mentor acceptance, canonical Settlement, and Dashboard projection are independently observable.

### Phase 5 — Pilot and scale

Pilot one Track, one Student, two sequential primary Web rounds, two independent probes, one stale callback, and one settled Handoff. Expand to additional Students only after Mentor settlement has no persistent backlog.

Exit criterion: the pilot passes every acceptance scenario without duplicate local ownership, Web submission, Bridge capture, canonical write, or misleading projection.

## Acceptance scenarios

| Scenario | Required result |
| --- | --- |
| One Student uses a primary Chat and three probes | One Student/local owner; four physical Web conversations in one lineage |
| A second active primary is registered | Mapping validation fails |
| A waiting primary and an active continuation coexist | Mapping validation fails; both occupy the primary path |
| Two active bindings claim the same writer scope | Mapping validation fails |
| A lineage scope differs from its owning local binding | Mapping validation fails |
| One Web conversation appears in two lineages | Mapping validation fails |
| One Bridge Thread maps to two conversations | Mapping validation fails |
| One active submission ID appears in two conversations | Mapping validation fails |
| A terminal conversation retains an active submission | Mapping validation fails |
| A callback returns against an older research revision | Result is preserved as stale pressure; no automatic continuation |
| A Web turn was created manually | Explicit non-strict import; no fabricated snapshot |
| A Project Source unrelated to a standalone/probe round drifts | Preserve Project health; do not invent semantic dependence |
| A Probe proposes a new paper-shaped object | Keep fork exploration until Mentor and user authorize a new Student |

## Compatibility and deferred enforcement

The change is additive. Existing nine skills, Bridge Threads, sessions, Project bindings, and three-event ledgers remain valid. No historical event is rewritten, and no Auto Research canonical file is migrated by the installer.

The first release documents but does not itself implement a host-global Chrome/profile lease, the owning repository's registry, scheduled watcher storage, or canonical Settlement adapter. Until those exist, use one declared browser dispatcher across worktrees, retain one active submission per conversation operationally, and treat automated binding/Handoff state as unavailable rather than inferred.
