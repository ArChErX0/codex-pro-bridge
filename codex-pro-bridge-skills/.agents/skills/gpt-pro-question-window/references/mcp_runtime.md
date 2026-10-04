# 程序化执行与持久任务 MCP

本运行时把固定步骤移入 Python worker，通过现有 Chrome DevTools MCP 操作浏览器。
身份解析、模型和上传回执、attempt 与交换账本仍由原有 helper 校验。
worker 不调用语言模型；主代理仅接收状态和结果文件路径。

## 可用范围

- 接受父代理通过 `prepare_bridge_execution.py` 发布的 `executor_handoff/v2`。
- 支持 `explicit`、`auto`、`none`，复用材料准备、暂存和 SHA-256 校验。
- 支持已核验的 Project 和 standalone、新会话 bootstrap，以及已有绑定会话续聊。
- 初次 Project 身份核验、Sources inventory 修复仍使用 Project skill，完成后再提交任务。
- UI profile 必须来自当前浏览器实际观察。支持独立模型/强度菜单，以及显式配置的
  `control_layout=nested-slider`。后者提供 `thinking_slider`、`thinking_keyboard_control`
  和 `thinking_positions`（可见强度名称到已观察位置的映射）。通过键盘逐格调整并回读位置、
  最终可见文字；不猜坐标，不编辑网页内部状态，不把“6 Pro”之类强度标签解释成模型。
- `latest-alias` 的可见模型比较唯一由 `.shared/model_controls.py` 负责：仅把请求 `最新`
  与观察到的 `Latest` 视作同一动态 alias；`exact` 和其他未知 kind 保持字面严格比较，
  不据此推断后端型号。nested UI 若声明 `thinking_label_location=outer-menu`，必须先打开
  outer `Select ChatGPT model` 菜单并读取可见 `Select model` 项的完整文字（例如 `6 Pro`），
  再打开 model 子菜单读取 checked `Latest`；关闭 trigger 的 `Pro` 或 DOM 内部 effort 属性
  不能替代该观察。
- 嵌套菜单使用 `model-controls/v2`：模型子菜单初始与最终各观察一次（总计两次展开），
  模型已匹配时零次选择；外层菜单为两次观察，加至多一次强度调整。仍禁止预检后重复确认。
  独立菜单保留 v1 合同。位置映射失效、焦点失败、单步不生效或标签不符均停止，不重复按键。
- Windows 原生和 WSL→Windows 使用已有 host staging 配置；持久 worker 通过可见 Copy
  操作的 `visible-copy-write/v1` 回收原文，避免每次轮询启动 PowerShell 读取全局剪贴板。
- 当前自动执行适配器依赖各浏览器工具显式支持 `pageId`，不以全局选中页判定身份。
  复制前在剪贴板锁内将已核验 owner + URL 的精确标签页置前，再次核验身份与文档焦点；
  不导航、不更换会话。无法取得焦点时明确停止，不读取其他窗口的剪贴板内容作为答案。

### 2026-09-30 中文界面

`config/ui-chatgpt-20260930.zh-CN.json.example` 提供新界面的 `ui` 对象示例；保留私有运行配置的
路径、允许仓库与浏览器命令，只替换现场核实过的 UI 项。输入框按 form 内可编辑 textbox
识别；模型与强度仍独立核验，不依据“最新”推断后端型号。可选 `account_menu_labels`
仅打开个人资料菜单读取账号，再按 Escape 关闭，不选择账号或工作区。示例的 account/workspace
同指个人账号标签，不适用于据此证明独立组织工作区身份。

新文件卡片与旧 chip 均按精确名称、唯一性和忙碌状态核对。消息适配由 `wait_for_reply.js`
统一供提交、等待和复制回收使用：旧版 message-id 与新版 search-message-ids 分别读取，
新版仅接受唯一稳定 ID；同一属性内重复同一 ID 可归一，多 ID 或重复消息节点均拒绝。
复制按钮不得跨越另一条同角色消息的容器，也不得取正文中的代码复制按钮代替回答复制。
首次 Send 后可能短暂出现 `/c/local-chatgpt%3A…`（或未转义的 `local-chatgpt:`）地址。
它不是服务器 conversation ID，统一身份解析拒绝它；发送后观察继续等待同一 owner 页
出现正式地址，不能提前 promotion，也不导航或重发。等待未取得稳定身份时保留原 attempt。
生成文件的新资源卡片可能包含独立预览与下载按钮：结构校验按一个资源行计一个链接，
不把两个按钮算成两条，也不与外层锚点或旧文件图标重复计数。保留 Copy 原文中的
`sandbox:` 链接与引用标记；捕获链接不等于文件二进制内容已下载或验证。
更新私有配置后需重载已加载的 Bridge MCP；已有 job 的冻结配置保持不变。

同 job 的 `ui-profile-overlay/v1` 只是一份不可变、repository-local 的 UI receipt。它必须
绑定原 job/Thread/Project URL、原始 profile readiness 错误、handoff/request/base-config、
bundle/staging 与观察 artifact SHA；只允许覆盖已观察的 UI selectors/accessible labels，
不得覆盖 browser route、host、network、request 或 payload。新的 worker 必须重新取得
canonical claim、传完整 pages/owners resolver、确认空 composer/零附件和实际账号，再进入
原有 model→upload→single-Send 流程。connected 阶段不能伪造回退 checkpoint 或直接走普通
`bridge_resume`；任何 owner/URL/digest/receipt 漂移都保持 HOLD。

### 浏览器响应格式

`evaluate_script` 先解码一个完整 JSON 值，再核对外层结束标记；正文中的代码围栏不能
作为封装边界。Chrome MCP 可能追加 `Page navigated to <URL>.`，仅当返回对象的 `url`
与该 URL 完全一致时接受。其他附加文本、重连提示、缺失或冲突的 URL 仍失败；该提示
不授权导航、改绑或重发，后续 owner、Project 和 conversation 校验保持不变。

`list_pages` 同时接受有标题的 `id: title (URL)` 和无标题的 `id: URL`，均可带
`[selected]`。保留无关页面的精确 ID/URL，不因空白页没有标题而阻断，也不静默丢弃
未知格式。被选中状态不替代 owner + URL 身份证明。

## 安装与配置

推荐先按包内 `docs/INSTALL.md` 运行 `install.sh --setup --repo PATH` 或 Windows
`install.ps1 -Setup -Repo PATH`。统一 setup 注册 MCP、生成私有主机配置并备份旧文件；
`doctor --connect` 通过前，setup 创建的 runtime 禁止提交。检查只观察，不上传或 Send。
setup 用 Python 3.11+，下述 worker 仍兼容 3.10+。手动部署路线仍然可用。

入口：`scripts/bridge_mcp.py --config <private-runtime.json>`，Python 3.10+，运行时只依赖标准库。
将包内 `config/bridge-runtime.json.example` 复制到私有位置，填写绝对状态目录、授权仓库根目录、
浏览器 MCP argv 和基于实际观察的 UI profile。不把这些主机值提交到仓库。

`browser_command` 指向 Chrome 所在宿主的 MCP；worker 在一次任务内复用同一进程。
推荐显式设置 `browser_transport=persistent`：在私有 `state_dir/browser-connections/` 中
按浏览器命令、环境、profile 和超时配置识别一个共享连接服务，不按 worker 新建浏览器 MCP。
服务仅转发调用，不选择页面、不改写问题、不做 Send 或上传重试；身份与 attempt 门仍由 worker 负责。
不同 worker 退出不关闭已授权连接；Chrome 退出、服务故障或显式断开后仍可能需要重新授权。
省略该配置或设置 `stdio` 保留每个 worker 自建连接的兼容路径。
可直接使用已安装的同版本 Node 与 MCP 入口组成 argv，避免每轮 `npx` 的包解析及更新检查；
入口路径必须显式配置，不扫描缓存选择任意版本，不自动升级 MCP。直接启动只减少启动耗时，
不绕过 Chrome 的远程调试授权。退出时先关闭 stdin，让 MCP 按 EOF 正常断开浏览器；
只有未正常退出才逐级终止其启动进程，不关闭用户标签页。
stdio 路径的 `browser_connect_timeout_seconds` 默认 300 秒，仅用于首次只读 `list_pages` 触发的连接；
`browser_tool_timeout_seconds` 默认 90 秒，用于其他调用和连接后的读页。persistent 服务保留首次
只读授权等待，不因调用方的等待超时或进程退出取消这个请求；之后的工具调用仍有超时，未知结果
阻止继续调用，不自动重连或重放。初始化成功不等于
浏览器连接完成；job 的 `browser_phase` 区分 `connecting-browser` 与 `connected`。
两项超时均不会重试上传或 Send，也不是网页回答的业务 deadline。Chrome 的授权仍须用户完成。

共享服务只监听本机 loopback，使用私有随机凭据和 JSON，不反序列化 pickle。
`state_dir` 必须只有当前用户可读写；Linux 服务叶目录要求 0700，Windows 应使用当前用户私有 ACL
的目录。勿把该目录放入共享盘、提交仓库或输出其中的 `endpoint.json`。维护代码可通过
`SharedClient.status()` 查看状态；只有明确要求断开时调用 `shutdown()`，它不关闭 Chrome 页面，
且拒绝打断正在执行的普通工具动作。运行中的服务不会热加载源码修改。
更新后按代码实际驻留进程判断是否重载：仅 worker 使用的代码由下一次新 worker 加载，
已运行 worker 不受影响；MCP 的状态/结果逻辑或启动配置变化须重载父 MCP。连接服务代码
变化须在确认无在途操作后显式重启该服务，单独重载父 MCP 不保证共享服务更新。
重载不更换 job/attempt，也不自动重放 Send。源码、WSL 安装、Windows 正式安装及隔离
测试副本是不同部署位置，不能从其中一个已更新推断其他位置已同步。
`browser_env` 只作用于浏览器进程；staging 的配置变量须由 Bridge MCP 启动环境提供。
多个 Codex MCP 实例必须共用同一个 `state_dir` 和 browser ownership registry，
才能共享任务去重和剪贴板互斥；不同主机不能通过这个目录获得跨主机互斥。
`pageId` 只属于当前浏览器 MCP 连接；重连后必须重新列页并通过 owner + URL 定位，
不能把另一连接的数字 ID 直接传给新连接。

可选 Codex 配置：

```toml
[mcp_servers.codex-pro-bridge]
command = "<absolute-python-executable>"
args = ["<absolute-skill-path>/scripts/bridge_mcp.py", "--config", "<absolute-private-runtime.json>"]
tool_timeout_sec = 90
```

## 调用顺序

1. 冻结证据、目标、模型、强度和 Send 授权，运行现有准备 helper。
2. `bridge_submit(handoff_path, handoff_sha256)`，保留返回的 `job_id`。
3. `bridge_wait(job_id, seconds=55)` 可长轮询，其他任务同时可调用 status。
   单次 wait 结束只返回当时状态，worker 继续运行；不得因此重建任务。
4. 完成后调用 `bridge_result(job_id)`，它重新校验原始 answer 与 canonical turn 的摘要。
   主代理读取文件，核验答案并独立记录 Codex verdict。
5. MCP 重连或 worker 中断时用 `bridge_status` 检查；`bridge_resume` 沿用原任务恢复。

同一个 handoff 摘要重复 submit 返回同一个任务；重复 resume 不会产生第二个执行者。
回答正文保存在工件中，不在 MCP 状态消息里返回或转述。

`bridge_resume` 的 `continuation` 区分 `pending`（已接受、同一可信 child 尚未握手）、`started`
（取得原 worker lock 并确认本 request token）、`already-running`（锁已由执行者持有）及具体
启动错误。token 随 child 启动冻结，迟到旧 child 不认领新请求；PID 同启动标识一起验证。
job 更新在原 `.job.lock` 中读取合并；退出观察只条件更新同 token 且仍 accepted 的记录，不能
覆盖 racing started。可信 started 清除当前旧 blocker 并显示 running，原 checkpoint 诊断单独保留；
status/wait 对活的 accepted child 显示等待，新 worker 失败仍明确 blocked，握手不代表 capture 完成。

`capture_route` 配置默认 `browser-fallback`；显式 `browser-page-serialized` 使用
[页面来源合同](bridge_protocol.md#页面原始序列化)，无原始来源时失败，不自动降级为伪来源。

## 中断语义

任务 envelope 只记录调度、材料产物和浏览器阶段；Send 是否发生只信任 canonical attempt。
`finish`、`status` 和 `result` 共用完成校验：核对原始答案、canonical turn、attempt 的问题和
捕获指纹、唯一 exchange 的身份与摘要。已捕获而 envelope 仍阻塞时，status 只读返回核验后
的结果，不重复派发；摘要、身份或缓存结果冲突时明确返回阻塞，不能仅凭 `state=complete` 采纳。

完成核验同时核完整 ledger 和每种 route 的原始来源。缺 envelope capture/result 时，只能从
canonical exchange 已存 raw/proof 和 capture fingerprint 只读重建 receipt，不能解析包装后的 turn
猜原文或连接浏览器。`local_verdict=pending/recorded` 与 `ledger_round_complete` 只报告该 job 的
精确 captured turn 是否已有经完整账本核验的 Codex verdict；`ledger_thread_complete` 报告整个
Thread 是否还存在未闭环的轮次。后续轮次未完成不会把先前已完成的轮次重新标为未完成。
Pro 原文 captured 并不完成 Codex verdict；`verify_bridge_thread --require-complete-rounds`
仍要求整条 Thread 的所有轮次完整。

host/path/secret 在 snapshot、打包与 staging 副作用前只读准入，`.codex` 不整体豁免。
`prepare_review --packaging-receipt` 保存 `verified-packaging/v1`，冻结 request/input/code 摘要、
prompt/material sidecar、ZIP 与原 source→packed 字节；staging 独立保存目标/源路径、映射、名称与摘要。
stage 失败后，同 job 用 `--reuse-packaging` 只复核前段并 stage，禁止重打包或重放 upload/draft/Send。
staging 收据按实际 verify_staged_file 返回证据逐项核对，不因文件存在即采纳。
所有合法 prepared/connection/identity/uploaded 复用路径在创建浏览器/模型或上传动作前复核所需
packaging/staging；旧准备按原 prompt/bundle/map/sidecar/staging 摘要核验，不因缺新字段一律拒绝。
无论新旧，都按冻结 request 的共同 delivery_prompt/material_map 规则核实际问题和 source→ZIP。
已有不可变新 packaging receipt 时，schema/marker 缺失或 envelope 改写明确失败，不能降到旧兼容分支。
新 prompt/material 与 canonical raw/proof 显式写 UTF-8 原始字节，保留 CRLF。仅无新 version/marker
且无新 packaging anchor 的合法旧准备可使用 `legacy-windows-text/v1`：磁盘字节必须精确等于
冻结 request 生成问题的逐 LF→CRLF 转码，旧 prompt_sha256 必须仍等于原 LF 文本的 UTF-8 SHA。
该兼容来自旧 Windows text writer；错正文、混合换行、额外 CR、摘要漂移均拒绝。runtime 使用已核
原问题创建 canonical attempt 与 captured-prompt，不改旧 prompt 文件或重做已消费的准备步骤。
公开 completion 只输出经类型与语义校验的标量元字段；Copy provenance 若存在，只允许
browser-fallback 的 `visible-copy-write/v1`，不能通过该字段嵌套私有 proof 或 owner token。
`completed_at` 来自 canonical exchange 的已知响应时间或已核 page proof；无 canonical proof 的
非 page 路线省略 proof 路径/摘要，无可核来源则省略可选 assistant ID。envelope 字符串不能
添加这些身份或覆盖已知时间；冲突缓存明确拒绝或剔除，正常 Copy marker 保持原已知语义。

只读身份与内容观察可在同一次页面执行中合并；owner/URL 必须先通过才读取内容，不跨页面缓存。
job 保存当前 worker 段的阶段耗时和浏览器工具计数；含模型生成等待的总耗时不是纯 Bridge 开销。
没有显式业务 deadline 时，worker 不设置整轮 15/30 分钟截止时间。

- `send-started` 后恢复只读取原 owner 页面并验证保存边界之后的唯一 prompt/turn，不再次点击 Send。
- 用户消息的可见文字可能因行内代码等 Markdown 渲染丢失标点。此时仅对边界后唯一候选消息
  使用可见 Copy message 按钮，在剪贴板互斥锁内读取原文、核对内容与冻结 prompt 精确一致，
  再固定远端 turn；保留复制原文与摘要回执。新 UI 字面用户消息的 Copy 若输出转义 Markdown，
  仅在已观察到该渲染布局时使用 `literal-user-markdown/v1` 还原转义、硬换行及文字与目标完全
  一致的自动网址链接；还原结果仍须与冻结问题逐字一致，并在回执记录 codec。禁止删除任意标点、
  合并空白、忽略链接目标或用模糊匹配放宽身份检查。
  复制控件缺失、无法取得本次可见 Copy 的唯一成功写入凭证、失焦、消息或 owner/URL
  变化均停止，不重发；系统剪贴板内容是否变化不是持久 worker 的成功判据，详见
  `browser_adapters.md` 的 `visible-copy-write/v1`。
- 在上传、填写草稿或 `prepared` 后中断时，自动恢复不重放副作用，返回具体待检查阶段。
- 上传调用返回后立即保存 `upload-result.transport.json`，再检查附件卡片。若停在
  `upload-started` 且已有该回执，可只读恢复：核对同一 page/owner/URL、上传计划、原包与
  暂存摘要、唯一同名且非忙碌的附件、空草稿，再重新预检。不会再次调用上传；任何缺失或冲突均停止。
  旧版未留下上传调用回执的任务不适用，不从页面文件名或异常文字补造回执。
- 浏览器身份变化、原生 chooser 未知、非唯一控件或捕获结构不匹配均返回 `blocked`。
- 已提交的短浏览器 claim 在等待前释放，捕获时重新领取。页面不关闭、不重载。
- worker 可跨父 MCP 的 stdin 断开继续运行；宿主关机/进程被杀后需要同一 job 的 resume。
  `worker_recently_alive` 是最近心跳，不等于网页有进展。

只有任务尚未开始（queued）、已经有 canonical attempt，或停在
`prepared-materials` 且尚未开始浏览器动作（未记录 browser phase、`acquiring-claim` 或
`connecting-browser`），或满足上述完整上传回执恢复条件时才支持自动 continuation。
claim 冲突必须先由其原 owner 正常结束；恢复不抢占、不清除其他任务身份。
连接阶段恢复复用原始包与摘要，再经 canonical resolver 定位页面，不重新打包。
一旦进入 `browser_phase=connected` 后的模型、上传或草稿阶段，仍禁止无凭据重放。
发生 pre-Send 阻塞时保留草稿、暂存路径与 claim 供检查，不能宣称 UI 已清理。
不得在一个未解决 job 上同时派旧 Executor 再跑一轮。

## 验证边界

`test_bridge_runtime.py` 通过浏览器替身驱动真实准备、预检、bootstrap promotion、attempt 和账本，
并测试同一 handoff 并发提交、Send 后断连禁重发和 immutable result 校验。
这证明程序与协议的衔接；不替代当前 ChatGPT UI 的真实上传和模型控件验收。
运行时未通过当前宿主的真实验收前，不把它设为已有生产线程的默认执行路径。

验收报告分别记录功能、无人值守、性能和部署证据：真实上传与无附件轮次、首次 Project
发送与后续快照、富文本原文回收、原 attempt 恢复及账本闭环不能互相替代。修复后人工
resume 成功不等于最终版本全程零干预；下载文件链接、多线程或网络故障组合未测时明确
列出。比较耗时须使用同等场景的完整新轮次，不能用恢复片段对比整轮，或将模型生成时间
算作 Bridge 自身开销。实现了优化不等于已经证明整体足够快。

Windows 冻结工件使用 UTF-8 原字节写入，不进行 LF→CRLF 自动转换。准备回执中的 request
和 handoff 摘要必须等于实际落盘文件的 SHA-256，不能仅验证文本解码后的内容相同。
