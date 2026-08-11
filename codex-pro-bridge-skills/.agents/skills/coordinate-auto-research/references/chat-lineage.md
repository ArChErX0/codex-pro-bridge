# Local and Web Chat Lineage

## Identity model

Use scientific identity rather than Chat count:

```text
Student
  research_line_id
  one active local_thread_id
  pro_lineage_id
    active primary conversation: 0..1
    independent probes: 0..N
    superseded continuations: 0..N
    fork explorations: 0..N
```

A new Web conversation does not create a Student. A new Idea Variant does not create a local Chat. A new Student requires an independently viable Research Line and user authorization.

## Conversation kinds

### Primary

Preserve the continuous discussion for one Program, Direction, or Student Research Line. Reuse it for the same scientific object when a new evidence delta changes a decision.

### Probe

Use a fresh standalone single-round conversation for an independent Cold, adversarial, reviewer, evidence-delta, or atomic review. Keep it outside the primary conversation's anchoring context. Return its result to the owning local Chat as pressure.

### Continuation

Create a successor physical conversation when the primary becomes unusably long, polluted, inaccessible, or product-limited while the scientific identity remains stable. Mark the old conversation `superseded`; promote the successor to the sole active primary. Preserve an immutable handoff digest rather than copying the entire transcript.

### Fork exploration

Explore whether a Variant has become an independent Research Line. Keep it owned by the parent Student until the Mentor accepts an independent problem, Claim, kill rule, evidence regime, and writer boundary. Then create a new Student and Pro lineage with explicit authorization.

## Ownership rules

- Give each Web conversation exactly one owning local binding.
- Let other local Chats hold read-only references.
- Keep at most one active primary conversation per lineage.
- Count a live `primary` or successor `continuation` (`active` or `waiting`) as occupying that single primary-path slot.
- Keep at most one active submission per conversation.
- Bind every submission to an exact conversation ID and remote turn.
- Treat titles as display metadata.
- Treat an `@`-referenced Chat as a locator, not as ownership or a completion trigger.
- Import a manual Web-originated turn explicitly. Label it non-strict when no pre-submit Codex snapshot exists.

## Mapping snapshot

Use this JSON shape only as a coordination snapshot and validation surface. The owning repository's registry and canonical research store remain authoritative.

```json
{
  "schema_version": 1,
  "sop_revision": "sha256:...",
  "bindings": [
    {
      "binding_id": "student-s-mai-001",
      "role_type": "student",
      "local_thread_id": "local-thread-id",
      "research_scope_id": "RL-MAI-001",
      "student_id": "S-MAI-001",
      "track_id": "M-AI",
      "writer_scopes": ["research-line:RL-MAI-001"],
      "status": "active"
    }
  ],
  "pro_lineages": [
    {
      "lineage_id": "PROL-MAI-001",
      "owner_binding_id": "student-s-mai-001",
      "research_scope_id": "RL-MAI-001",
      "status": "active",
      "conversations": [
        {
          "conversation_id": "web-primary-id",
          "kind": "primary",
          "status": "active",
          "bridge_thread_id": "m-ai-rl-001-primary",
          "active_submission_id": null
        },
        {
          "conversation_id": "web-probe-id",
          "kind": "probe",
          "status": "completed",
          "bridge_thread_id": "m-ai-rl-001-probe-001",
          "active_submission_id": null
        }
      ]
    }
  ]
}
```

The validator checks identity uniqueness, owner/scope alignment, one active Student writer per Research Line, one Web owner, one live primary path, unique Bridge Thread/conversation mappings, and unique active submissions. It rejects active submissions on terminal conversations and warns when active Student WIP exceeds three for one Track.
