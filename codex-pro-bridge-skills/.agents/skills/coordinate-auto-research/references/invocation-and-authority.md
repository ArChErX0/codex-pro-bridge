# Invocation and Authority

## Local role routing

Auto Research has one human user and several local context containers:

| Current local Chat | Default scope | May own |
| --- | --- | --- |
| Primary Portfolio | program intent, global WIP, cross-scope agenda | routing and portfolio proposals |
| Research Orchestrator | cross-Track synthesis, Peer Diagnostic, Teacher Packet | Program-level Pro lineage |
| Track Mentor | one Track, Student roster, direction judgment | Direction-level Pro lineage and Mentor verdict |
| Student | one paper-shaped Research Line | implementation, experiment interpretation, Student-level Pro lineage, Handoff |
| Execution control task | accepted Run/Job/resource facts | queue and monitoring state only |

The user can override any level. When a user instruction arrives in a narrower local Chat but affects a parent scope, preserve the instruction locally and emit an `instruction_delta` to the owning parent instead of silently rewriting unrelated scopes.

## Intent semantics

### Inspect

Recover live state without sending, creating, running, or settling. Separate:

- Task: local Chat/task lifecycle;
- Research: Judgment, Evidence, Decision, Next Evidence, falsifier;
- External Review: conversation, answer, local verdict, strict chain;
- Resource: Spec, Run, Artifact, Watch/Job, actual infrastructure.

### Steer

Produce a route or priority proposal. Preserve the current canonical decision until the owning role accepts and settles the change.

### Continue

Advance the accepted Current Next Evidence within its existing evidence and resource boundary. Treat new data, model, paid service, Web conversation, GPU scale, external write, or scientific object as scope expansion.

### Correct

Apply the correction only to the named scope and dimension. Propagate a durable `instruction_delta` to affected owners. Preserve evidence, safety, and provenance gates.

### Authorize

Permit only the named effect. Creating a Student/fork Chat, activating a Paper Project, using paid/remote resources, changing canonical ownership, or writing/removing remote content requires explicit authority.

### Settle

Collect the bounded Student Handoff, Mentor review, reusable-knowledge check, and material canonical transition. A finished task, Pro answer, code change, or completed Job is not itself Settlement.

## Conflict order

Resolve conflicting instructions in this order:

1. the latest explicit user instruction for the same scope and dimension;
2. active hard evidence, security, provenance, and resource boundaries;
3. the accepted parent Brief and canonical revision;
4. local role defaults;
5. external Pro pressure and unaccepted proposals.

Do not convert missing bindings into authority. Use `unknown`, `unregistered`, `proposed`, or `create-ready` until the corresponding identity is observed.

## Skill ownership

`$coordinate-auto-research` owns Auto Research intent, scope, role binding, and close-out routing. Downstream Bridge and experiment skills own their technical seams and return results to the caller. An explicitly user-named downstream skill still runs, but its output does not bypass Auto Research Handoff or Settlement.
