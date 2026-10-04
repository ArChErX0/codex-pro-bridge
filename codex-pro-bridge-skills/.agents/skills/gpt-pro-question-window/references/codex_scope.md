# Codex 根线程与负责人绑定

用于用户已明确启用网页 Pro 协作的主代理/组长工作。维护 Bridge 本身不构成咨询授权。

## 身份与首次绑定

- 派发者提供用户可见根线程的真实 ID `codex_root_thread_id`；子代理继承它，不把自己的线程 ID 当根。根代理可用 Codex 宿主的 `exec_command` 读取 `CODEX_THREAD_ID`；FastCtx 等独立 MCP 进程的环境不代表调用线程。无法确定时补齐身份，不按仓库名或任务标题猜测。
- `owner_agent_id` 使用负责判断的原生代理稳定身份（如 `/root`、`/root/5_6sm_numerics`），不是临时传输执行器名。压缩或恢复沿用身份。
- 首次准备若返回 `create-and-bind-project: <id>`，使用 Project skill 为该根线程创建新的网页 Project，再对返回的本地 ID 绑定、核验身份和 Sources。用户指定已有 Project/会话时遵从其目标；不能沿用同仓库其他线程的默认绑定。
- `prepare_bridge_execution.py --target-project-url` 也可在该根范围内采用明确目标。多个根显式采用同一远端 Project 时保持各自本地身份；底层 `manage_bridge_project.py bind` 需明确 `--allow-shared-remote`。绑定后按 Project skill 完成核验，才向执行 MCP 提交。

## 准备与执行

在既有准备命令中增加两个参数，不手写 request/handoff：

```text
prepare_bridge_execution.py --repo <真实仓库> \
  --codex-root-thread-id <根线程ID> --owner-agent-id <负责人稳定ID> \
  --goal <本轮目标> --question-file <问题文件> --notes <仓库内notes> \
  --context-policy explicit --file <必要原文文件> \
  --requested-model <实际页面模型标签> --model-selection-kind exact \
  --requested-thinking-intensity <实际强度标签> --allow-send
```

模型/强度以当前 UI 能确认的值为准；“最新”或孤立的“6 Pro”菜单文字不能替代模型核验。无附件轮使用 `--context-policy none`，保留问题与必要对话上下文。用户指定既有会话时，先按 Bridge 会话绑定协议采用它，仅归属于指定负责人。

本地 Project 按根线程选择，Bridge Thread 按根线程、负责人和远端 Project 确定；任务标题变化不换会话，不同负责人不因同题合并。根/owner 写入 request/handoff 并与 Project/task 交叉核对。显式切换 Project 时创建新的负责人 Thread，旧历史保留。

同负责人新问题生成新的不可变 preparation 目录，沿用原 Thread/会话。重复同一准备命令返回同一回执，不重发；确需再次问完全相同的问题时显式给 `--round-id`。同负责人各轮串行，不在上一轮未解决时提交下一轮。

已配置 MCP 时按 [程序化运行时](mcp_runtime.md) 使用 `bridge_submit → bridge_wait → bridge_result`，保存 job_id 后继续独立工作。等待可在会让出控制权的宿主 tool cell 内执行；不使用 `wait_agent` 等待程序任务，也不让模型每隔几十秒重新查询。只有实际派发原生 Executor 时才用其原生等待接口。续作沿用 job/attempt，不因等待超时切换执行路线。

没有 MCP 时按 Question Window 的既有 Executor 路线执行，同一份 handoff 只交给一个执行者。原始答复与本地判断分开保存，负责人完成核验与整合。

## 兼容边界

无 Codex scope 参数的旧调用保留 legacy 仓库绑定；不会默认选择 scoped Project。旧账本、会话、Sources 不迁移。新策略调用必须显式传根和负责人；新增参数不改变浏览器身份、单次发送及原文回收合同。
