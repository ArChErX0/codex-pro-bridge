---
name: gpt-pro-question-window
description: Route authorized Codex Pro questions to the correct root-scoped Project and owner conversation, using the persistent Bridge MCP or its agent fallback for execution and raw capture; retain local verification in the requesting agent.
---

# GPT Pro Question Window

Use this skill only when outside reasoning is useful. It is the main-agent routing and
verification adapter for Codex Pro Bridge; it is not the executor's step-by-step
playbook.

## Host runtime

Invoke every helper with one explicit Python 3.10+ interpreter appropriate to
the host (`python` on Windows or `python3` on POSIX). Keep that interpreter
consistent throughout one Bridge round instead of relying on script shebangs.

## Canonical contracts

按实际执行路线读取说明，不把手工浏览器流程再跑在持久 worker 外面：

- 普通 MCP 调用：读取下述根线程规则和 [mcp_runtime.md](references/mcp_runtime.md)。
- 委派 Agent 后备执行：完整读取 [executor_handoff.md](references/executor_handoff.md)，
  把冻结 handoff 交给 `bridge_executor`；该角色按合同读取其执行参考。
- 直接操作浏览器或诊断 UI：读取 [browser_adapters.md](references/browser_adapters.md)；
  中断恢复另读 [attempt_recovery.md](references/attempt_recovery.md)。
- 直接维护状态、账本、快照或排查身份合同：读取 [bridge_protocol.md](references/bridge_protocol.md)。

参考文档和现有 helper 是身份、预检、attempt、回收及账本的权威实现；不在提示词中重写算法。

## Main-agent responsibilities

在 Codex 主代理/组长协作中，仅在用户明确启用的任务范围内咨询。准备前读取
[根线程与负责人绑定](references/codex_scope.md)，显式携带根线程及负责人身份；
不要用仓库默认绑定或任务标题代替会话归属。

当当前环境已配置并验收 `codex-pro-bridge` 执行 MCP 时，优先使用
[mcp_runtime.md](references/mcp_runtime.md) 的 `bridge_submit → bridge_wait → bridge_result`。
父代理仍使用下述确定性准备与最终核验；机械步骤由持久 worker 执行，不再分派 Executor。
运行时缺失或尚未验收时使用下面的 Agent 路线。任务一旦提交，沿用其 job/attempt，
不能因等待超时或阻塞切换另一条路线重发。

1. Define the exact question, desired output, and whether external reasoning is useful.
   For Codex-scoped collaboration, follow the root/owner entry above; an unscoped
   route preview is not its Project decision. Legacy callers may use
   `resolve_bridge_route.py` with `--external-reasoning` or `--local-only`. A
   `local_only` result ends this route without spawning an Executor.
2. Freeze the evidence decision and any explicit target Project URL. For a new round,
   run `scripts/prepare_bridge_execution.py` with the question file, notes, frozen
   context policy (`--context-policy explicit|auto|none`), model label/kind, thinking
   intensity, and `--allow-send`. `explicit` requires repeated `--file`; `none` must
   pass no `--file` and freezes `max_files = 0`; `auto` may use an empty focus list and
   freezes a positive `--max-files` limit. Contradictory policy/file combinations fail
   before any Project rebind.
   The command reuses the Project store/router owners, performs an explicit target
   rebind when requested, derives the canonical Thread ID, and atomically publishes
   request plus `executor_handoff/v2`. Do not hand-write JSON, guess a Thread ID, or
   create an isolated repository merely because the target differs from the current
   binding.
3. Use the receipt's handoff path and digest as the only Executor input. The request
   contains the frozen goal/question/notes/files; model controls and Project target
   live in v2 handoff. A business deadline may be supplied only when the parent/user
   explicitly defines one; otherwise omit it or set it to null.
4. 只选一条路线：已验收 MCP 用 `bridge_submit`；Agent 后备用 `bridge_executor` 的
   `prepare-and-run`，恢复既有 attempt 用 `recover-only`。不能同时交给两条路线。
5. MCP 用同一 job 的 `bridge_wait/status/result`，必要时 `bridge_resume`；Agent 用同一
   handle 的 `wait_agent` 和恢复回执。等待窗口结束、页面未变化或子代理执行期限不等于
   任务完成，也不授权新建 attempt、重传或重发。未指定 business deadline 就没有业务截止时间。
6. After a `complete` receipt, reopen the immutable answer, bundle, ledger, and
   checkpoint; verify hashes and the exact pinned turn. Record the separate Codex
   verdict with `record_codex_verdict.py`, then run the thread/Project verifiers before
   reporting conclusions. Never modify the raw Pro answer to add the verdict.

## Executor boundary

执行者负责冻结合同内的打包、暂存、上传、一次 Send、固定 turn 等待、原文保存及清理；
父代理不重复这些机械步骤。Project 首次核验和 Sources 修复按 Project skill，不能从空清单
推断已核验。`context_policy=none` 不打包或上传，但仍执行授权问题和回收。

模型控件按已观察布局使用 v1（独立菜单）或 v2（嵌套菜单/滑块），仅不匹配时调整，
把回执交给预检；不在预检后重复确认，也不在恢复时重放。上传顺序和 chooser 风险仅按
browser adapter 合同执行，不能混用不同后备路线。DevTools 对新快照中的
`Upload from computer` 控件直接调用 `upload_file`，不点击该项打开 native chooser。
身份 HOLD 不授权导航、重载或换会话。

父代理保留目标、材料范围、问题、Project 决策、外部动作授权及最终核验；执行者不能
自行选材料、改问题或采纳答案，只返回路径、摘要、身份、状态和具体阻断，不转述长回答。

## Completion and failure semantics

An external round is complete only when the raw exchange is saved as an immutable turn
artifact on the intended Bridge Thread and the receipt includes the exact answer path,
digest, remote turn ID, URL, capture route, and observed model metadata. A successful
Send alone is not a completed review. A host wait window or runtime yield is not a web
failure; preserve the attempt and continue with the same identity. A declared business
deadline is handled as an actual deadline, not relabeled as a send failure. Unknown Send
outcomes always remain non-resendable.

Stop and report a concrete blocker for login, 2FA, CAPTCHA, remote-debugging permission,
account-security prompts, rate limits, unavailable host/MCP capability, owner/URL/
Project ambiguity, hash drift, stale observations, missing target turn, or incomplete
or truncated answers. Do not switch to a new conversation or silently weaken a gate.

## Normal question

For ordinary questions, read [question_window_prompt.md](references/question_window_prompt.md)
and place the final prompt in the frozen request's `question` field. Specialized review
skills provide their own prompt; they still use this skill for route selection, the
Executor handoff, capture, and final verification.

## Scope boundary

Task Bundles are immutable, round-scoped evidence and are not Project Sources. Keep
Project Source changes in `gpt-pro-project-workspace`. Keep bundle selection logic in
`bundle-algorithm-context`; this skill only consumes the already-frozen request. Keep
the full raw exchange and local verdict separate. Use the Chrome DevTools route and the
existing browser adapter's fallback only as its canonical contract permits; never open,
close, or repurpose a browser tab outside that contract.
