# Codex Pro Bridge Skills Package

This directory contains the managed skills, shared runtime, installers, tests, examples, and internal workflow references for Codex Pro Bridge.

Start with the repository-level [English README](../README.md) or [中文说明](../README.zh-CN.md).

## Install

Global installation:

```bash
./install.sh --global
```

Repository-local installation:

```bash
./install.sh --repo /path/to/repo
```

The installer replaces only the ten managed skills and `.shared`. Repository-local installation also adds `.agents/` and `.codex/` to the target repository's local `.git/info/exclude`.

## Entry points

| Need | Skill |
| --- | --- |
| Coordinate Auto Research roles, Research Lines, local Chats, Web conversation lineages, Handoff, and Settlement | `$coordinate-auto-research` |
| Bind, inspect, route, or repair a ChatGPT Project | `$gpt-pro-project-workspace` |
| Normal question or existing conversation | `$gpt-pro-question-window` |
| High-frequency probes with parallel evidence preparation and serialized Pro generation | `$gpt-pro-review-probe` |
| Deep algorithm, pipeline, or experiment review | `$gpt-pro-research-algorithm-reviewer` |
| Paper framing and reviewer pressure test | `$gpt-pro-paper-brainstormer` |
| Complete external-review loop | `$gpt-pro-algorithm-pipeline` |
| Local experiment matrix | `$experiment-plan-generator` |
| Local implementation/result consistency check | `$implementation-consistency-checker` |

`$gpt-pro-question-window` resolves `local_only`, `standalone`, and `project`
routes automatically. `$bundle-algorithm-context` prepares evidence for
source-backed external rounds. ChatGPT page mutations use Chrome DevTools MCP
by default; the Codex Chrome connector is a pre-submit compatibility fallback.
SSH may host repository or compute work, while one local browser-host dispatcher
stages verified bundles and controls the signed-in Chrome profile.

Long-running rounds use host-level recoverable state, an account/workspace
generation slot, and one current-task heartbeat. The SSH-side asynchronous
client source lives at `remote-skills/gpt-pro-bridge-client`; it is deployed
explicitly to the remote Codex host and is not part of the local installer.

On the remote host, install and bind that client once:

```bash
./remote-skills/install.sh --global
python3 ~/.codex/skills/gpt-pro-bridge-client/scripts/prepare_review_bundle.py \
  configure --dispatcher-thread-id '<Mac dispatcher task id>' \
  --dispatcher-host-id '<Mac Codex host id>' \
  --ssh-alias '<Mac-to-remote SSH alias>'
```

Deployment identities are stored under the remote user's private Codex state;
they are never committed in the distributable Skill.

See [usage_prompts.md](examples/usage_prompts.md) for invocation examples.
Operational details live in the relevant skills, the
[canonical protocol](.agents/skills/gpt-pro-question-window/references/bridge_protocol.md),
and the
[Project protocol](.agents/skills/gpt-pro-project-workspace/references/project_protocol.md).
