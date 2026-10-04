# 安装与首次就绪验收

## 给 AI agent 的安装指令

把下面这段交给能执行本机命令的 agent，并给出要授权的工作仓库路径：

> 从 https://github.com/ArChErX0/codex-pro-bridge 安装 Codex Pro Bridge。
> 先读取 README.zh-CN.md 和 codex-pro-bridge-skills/docs/INSTALL.md。
> 判断 Codex 与 Chrome 所在主机，运行统一 setup，不手写 selectors 或覆盖无关配置。
> 只授权我给出的工作仓库。备份已有安装，执行 doctor --connect。
> 如需登录或 Chrome 授权，通知我完成后沿原安装继续。
> 只有 doctor 返回 ready 才报告就绪；不要上传、提问或发送验收消息。

## 前置条件

- Codex、Git、Python 3.11+ 和浏览器主机上的 Node.js。推荐 Node 22.12+ 或 24 LTS。
  Bridge worker 仍支持 Python 3.10+；统一安装器用 3.11+ 的标准库 TOML 解析器保护已有配置。
- 已安装支持 `chrome://inspect/#remote-debugging` 的 Chrome。用户开启远程调试，
  在该 profile 登录 ChatGPT，并允许实际连接提示；安装器不绕过这些安全交互。
- 首次安装需要访问 npm。安装器在私有目录安装 `chrome-devtools-mcp@1.8.0`，
  禁用安装脚本和用量统计，不修改全局 npm 包，也不依赖 npx 的临时缓存路径。
- 非 Codex 的 agent 也可以执行安装，但目标配置仍是 Codex。无需 API key 或 Luna 模型。

缺 Python/Node/Git 时，agent 先说明缺项，使用用户批准的系统包管理器安装，之后重跑；
安装器不自行升级系统软件或绕过组织策略。

## 一条安装命令

先 clone 上述仓库，进入仓库根目录。Windows 原生应 clone 到 Windows 驱动器，不能从
WSL UNC share 启动 Windows 安装器；WSL 路线则使用 Linux Python。以下 PATH 是用户指定的
现有工作仓库，而非 Bridge 源码目录。

Windows PowerShell：

```powershell
.\codex-pro-bridge-skills\install.ps1 -Setup -Repo "C:\work\my-project"
```

WSL（Chrome 与 Node 在 Windows）：

```bash
./codex-pro-bridge-skills/install.sh --setup --repo /path/to/my-project --topology wsl-windows
```

同主机 Linux/macOS：

```bash
./codex-pro-bridge-skills/install.sh --setup --repo /path/to/my-project --topology native
```

跨平台直接入口是 `python setup_bridge.py install --repo PATH`，脚本位于包根目录。
`--codex-home PATH` 或 `CODEX_HOME` 可指定目标 Codex；默认是当前运行主机的 `~/.codex`。
WSL 默认安装到 WSL Codex，不会顺便修改 Windows Codex。`--topology auto` 会按运行环境判断。

Windows 可用 `-Python` 选择解释器；bash 可设置 `BRIDGE_PYTHON`。路径有空格时保留引号。

## 安装器自动完成什么

1. 校验明确仓库、Python/Node 版本和 Codex 配置冲突；拒绝 symlink 目标。
2. 在浏览器主机安装固定版本 MCP，生成绝对 argv，避免每轮下载或解析 npx。
3. 安装全局 skills（包含 `.shared`），归档旧同名目录而不是删除。
4. 生成私有 runtime、授权仓库列表、状态目录，以及 Windows/WSL 的暂存路径映射。
   为有效 Git 仓库添加 `.codex/codex-pro-bridge/` 本地 exclude，避免误提交业务运行记录；
   不隐藏用户自己的其他 `.codex` 配置，不改仓库的公共 `.gitignore`。
5. 向 `config.toml` 添加唯一的 managed MCP block，注册 Bridge 与首次 Project 身份核验所需的
   Chrome DevTools MCP。已有用户管理的 Chrome 配置保留不动；完整 TOML 解析确保其他设置不变。
6. 运行非发送 doctor，核对实际工具 schemas、显式 pageId、登录和已支持 UI 控件。
   英文/中文内置 profile 只是候选；实际观察通过后才启用。滑块至多跨 6 格，逐格核对真实位置
   与可见标签，然后恢复原位置和标签；不猜测强度名称或后端模型。同名但位置不同的强度不生成
   模糊映射，回执明确列出。失焦、位置不生效或身份变化均阻断，不宣称恢复成功。
7. 保存安装及就绪回执，提示重启/重载 Codex，使新 MCP 工具进入当前会话。

默认路线是无额外模型的持久 worker；不必注册专用子代理。可选 agent 模板继承父代理模型，
仅在确认账号可用型号后指定模型；模板不会随安装自动注册。

## 状态、恢复和高级选项

安装回执位于 `$CODEX_HOME/codex-pro-bridge/install-receipt.json`。它包含一个 `doctor` argv，
复制该数组对应的命令即可从已安装副本复验，不依赖原 clone 目录。
`setup_docs` 指向一同保留的安装说明副本。MCP 注册字段遵循
[Codex 配置参考](https://developers.openai.com/codex/config-reference)。

| 返回状态 | 含义 |
| --- | --- |
| `installed-not-verified` | 文件/配置已安装，但尚未完成真实浏览器观察；不能据此提交问题 |
| `configured` | doctor 已验证工具 schema，未连接 Chrome |
| `ready` | doctor 已观察登录、支持的 UI 与空闲标签；未上传或 Send，不是完整业务链路测试 |
| `blocked`（退出码 2） | 具体依赖、配置、授权或 UI 问题；保留安装产物，处理后复验，不重新发送任何问题 |

先打开一个空白、无草稿/附件、未被其他 Bridge job 占用的 ChatGPT 标签页，再复验。
doctor 打开/关闭资料和模型观察菜单，不选择其他账号或模型菜单项，不写 ownership；对支持的强度滑块
暂时逐格观察并恢复原值（不是只读检查），全过程不生成问题或回答。没有空闲标签时，
最多新建一个专用 ChatGPT 首页标签，不操作其他 job 的页面或覆盖用户草稿。
不采用已有 Project，不上传、不填写、不 Send。未知 UI 返回 blocked，由 agent 实测适配后传
`--ui-profile PATH`，禁止把猜出的 selector 当成已验证配置。

`--no-connect` 只安装配置，适用于离线或稍后授权；之后必须 `doctor --connect` 才启用提交。
`doctor` 不带 `--connect` 只进行 schema 检查，不触发 Chrome 连接授权。
`--browser-command-json '["node", "ABSOLUTE-MCP-ENTRY", "--autoConnect", "--no-usage-statistics"]'`
可复用管理员已有 MCP；仍需通过 schema 检查。`--skip-browser-install` 要求固定版本已存在。
`--browser-host-root PATH` 指定私有依赖与暂存位置；WSL 传 Windows 驱动器的 Linux 挂载路径。
默认持久模式的 doctor 复用同一连接服务，授权后保留给 worker，而不是验完即断开。
`--browser-transport stdio` 是不保留连接的兼容模式，后续连接可能再次要求 Chrome 授权。

已有非本安装器管理的同名 MCP 时立即停止，不删除或覆盖。agent 先说明差异，请用户明确选择
保留旧配置或将其独立 section 归档，再进行迁移。重复运行安装器可更新 managed block、添加一个
明确授权仓库，保留已观察 UI 和原 job 状态。更新代码前先确认没有在途操作，重载规则仍遵循 runtime 文档。

备份位于 `$CODEX_HOME/_archive/bridge-setup-*`，`restore-map.json` 记录原路径和副本。
恢复前关闭正在使用该部署的 MCP，对照映射恢复所需配置或 skills；不要恢复旧状态覆盖新 job。
原安装目录与备份均不自动清理。安装中断时检查回执、映射和精确 setup lock，再决定恢复。

开发者运行测试前使用 `python -m pip install -r requirements-test.txt` 安装测试用 jsonschema；
它不是安装器或运行时依赖。Python 3.10 仍覆盖 worker 与 skills-only；完整 setup 测试在 3.11+ 执行。

旧 `install.sh --global/--repo` 与 `install.ps1 -Global/-Repo` 保留为 **skills-only** 路线，
也会备份同名 skills，但不配置 MCP。只安装 skills 不等于安装即可使用。
