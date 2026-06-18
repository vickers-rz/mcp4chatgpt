# 终端登录模式配置

**User:** Anonymous  
**Created:** 6/18/2026 15:22:47  
**Updated:** 6/18/2026 15:54:37  
**Exported:** 6/18/2026 15:58:12  
**Link:** [https://chatgpt.com/c/6a339cc6-345c-83ea-acba-d9cde94e6883](https://chatgpt.com/c/6a339cc6-345c-83ea-acba-d9cde94e6883)  

## Response:
6/18/2026, 3:52:47 PM

> **浏览网页以获取最新信息和引用**
>
> [GitHub - devspace-sh/devspace: DevSpace - The Fastest Developer Tool for Kubernetes ⚡ Automate your deployment workflow with DevSpace and develop software directly inside Kubernetes.](https://github.com/devspace-sh/devspace)
> [Create a Workspace | DevSpace docs | DevContainers everywhere](https://devspace.netlify.app/docs/developing-in-workspaces/create-a-workspace/)
> [Custom Commands | DevSpace | Documentation](https://www.devspace.sh/docs/configuration/commands/)
> [I built a local MCP to make ChatGPT + Codex workflows practical for solo devs](https://www.reddit.com/r/codex/comments/1tyba25/i_built_a_local_mcp_to_make_chatgpt_codex/)
> [Workstream: A Local-First Developer Command Center for the AI-Augmented Engineering Workflow](https://arxiv.org/abs/2604.17055)
>
> **搜索 GitHub 上的 Waishnav/devspace 仓库**
>
> [GitHub - Waishnav/devspace: Turn ChatGPT into Codex · GitHub](https://github.com/Waishnav/devspace)
> [raw.githubusercontent.com](https://raw.githubusercontent.com/Waishnav/devspace/main/README.md)
>
> **考虑是否使用写作块来制定计划**
>
> 用户要求一个详细的借鉴计划，我应该考虑是否需要使用写作块。计划里要包括阶段、模块、任务和测试等。我可以用普通的写法展示并附上引用。尽管写作块有些限制，但如果不引入引用，可能更合适。
>
> 已思考 14s

我会把计划拆成“直接可做的工程改造”，不是泛泛对比：先确认 DevSpace 的关键设计点，再映射到你当前 Python MCP4ChatGPT 的模块、工具、测试和安全边界。

基于 DevSpace 的公开说明，我建议你的 `MCP4ChatGPT` 不要“复刻一个 TypeScript DevSpace”，而是走更适合你的路线：**Python 本地 MCP + workspace/worktree 架构 + 可见 Terminal 接管能力**。DevSpace 的核心定位是把 ChatGPT 连接到用户本机项目，让它读写文件、搜索代码、运行命令、使用 Git worktree、读取 `AGENTS.md` / `CLAUDE.md`、发现 agent skills，并通过 Owner password/OAuth/allowlist 控制访问边界。([GitHub](https://github.com/Waishnav/devspace)) 它的安全模型明确强调：这是对开发机的远程访问，应使用窄 filesystem allowlist、OAuth Owner password、Host allowlist 和显式 MCP tool call。([GitHub](https://raw.githubusercontent.com/Waishnav/devspace/main/docs/security.md))

下面是详细借鉴计划。

**document: **

# MCP4ChatGPT 借鉴 DevSpace 的详细改造计划

## 0. 总体定位

`MCP4ChatGPT` 不应简单复制 DevSpace，而应形成自己的差异化定位：

> MCP4ChatGPT = DevSpace 的 workspace / worktree / skills / 安全架构 + Python 本地工具链 + macOS Terminal 可见接管能力。

DevSpace 的优势是产品化 coding workspace；你的项目优势是：

1. Python 实现，更易快速迭代；
2. 已有本地文件、Git、shell、知识库能力；
3. 已有 `co-te.py` 桥接 Terminal.app / iTerm2 / Termius；
4. 已经实现 `local_run_command` 后台日志；
5. 更适合个人本机自动化、Mac 场景和可见终端回放。

目标不是“让 ChatGPT 直接乱改本地机器”，而是建立一个可控的本地开发工作台：

- ChatGPT：架构师、代码审查、任务拆解、状态观察；
- Codex：实现者；
- 本地 Git repo：事实源；
- MCP4ChatGPT：安全受控的本地工具层；
- Terminal 回显：人类可追溯执行轨迹。

---

# 第一阶段：Workspace 模型改造

## 1.1 当前问题

当前 `MCP4ChatGPT` 多数工具直接接收 `cwd` 或绝对路径，例如：

- `local_read_text`
- `local_write_file`
- `local_apply_patch`
- `local_run_command`
- `local_git_status`
- `local_git_diff`

这种模式能用，但有几个问题：

1. 每个工具都要重复做路径校验；
2. ChatGPT 容易传错路径；
3. 没有“当前打开的项目”这一层抽象；
4. 后续要支持 worktree、session、change summary 会比较难；
5. 安全边界分散在各个工具参数中。

## 1.2 目标

新增 workspace 抽象：

```text
workspace_id -> project root -> policy -> session state
```

后续所有代码相关工具优先基于 `workspace_id` 操作，而不是直接暴露任意路径。

## 1.3 新增模块

建议新增：

```text
src/mcp4chatgpt/workspace_ops.py
```

职责：

- open workspace
- list workspaces
- close workspace
- resolve workspace-relative path
- detect repo metadata
- read project instructions
- expose workspace session state

## 1.4 新增数据文件

建议新增：

```text
data/workspaces.json
```

结构示例：

```json
{
  "workspaces": {
    "ws_abc123": {
      "workspace_id": "ws_abc123",
      "root": "/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT",
      "name": "MCP4ChatGPT",
      "created_at": 1781760000,
      "last_used_at": 1781761000,
      "git": {
        "is_repo": true,
        "branch": "main",
        "remote": "git@github.com:vickers-rz/mcp4chatgpt.git"
      },
      "policy": {
        "allow_write": true,
        "allow_shell": true,
        "allow_terminal": true
      }
    }
  }
}
```

## 1.5 新增 MCP tools

### `workspace_open`

用途：打开一个 allowed root 下的项目目录。

输入：

```json
{
  "path": "/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT",
  "name": "MCP4ChatGPT"
}
```

返回：

```json
{
  "workspace_id": "ws_abc123",
  "root": "/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT",
  "name": "MCP4ChatGPT",
  "git": {
    "is_repo": true,
    "branch": "main",
    "status": "clean"
  },
  "instructions": {
    "AGENTS.md": "...",
    "CLAUDE.md": null
  }
}
```

### `workspace_list`

返回当前已打开 workspace。

### `workspace_close`

关闭 workspace session。

### `workspace_info`

返回 workspace 的 root、git、instructions、policy。

## 1.6 兼容策略

不要立刻删除旧工具。

保留：

```text
local_read_text
local_write_file
local_run_command
```

但新增 workspace 版本：

```text
workspace_read_file
workspace_write_file
workspace_apply_patch
workspace_search
workspace_tree
workspace_git_status
workspace_git_diff
workspace_run_command
```

后续让 ChatGPT 优先使用 workspace 工具。

---

# 第二阶段：Repo-relative 路径规范化

## 2.1 当前问题

绝对路径对模型不友好，也不利于审查。

例如模型可能传：

```text
/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT/src/mcp4chatgpt/tools.py
```

更好的方式是：

```text
workspace_id = ws_abc123
path = src/mcp4chatgpt/tools.py
```

## 2.2 改造目标

所有 workspace 工具只接受 repo-relative path：

```json
{
  "workspace_id": "ws_abc123",
  "path": "src/mcp4chatgpt/tools.py"
}
```

内部做：

```python
resolved = workspace.root / path
resolved.relative_to(workspace.root)
```

禁止：

```text
../
/absolute/path
~/
```

除非显式允许。

## 2.3 新增安全函数

在 `safety.py` 或 `workspace_ops.py` 中新增：

```python
def resolve_workspace_path(workspace_root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
    ...
```

规则：

1. `relative_path` 不能为空；
2. 不允许 absolute path；
3. 不允许逃逸 workspace root；
4. 可选 `must_exist`；
5. 对 symlink 要谨慎，最终 `resolve()` 后仍必须在 workspace root 内。

## 2.4 测试

新增测试：

- `src/file.py` 正常解析；
- `../secret.txt` 被拒绝；
- `/etc/passwd` 被拒绝；
- symlink 指向 workspace 外部时被拒绝；
- 不存在路径在 `must_exist=False` 时允许创建。

---

# 第三阶段：Worktree 隔离开发模式

## 3.1 为什么要做

你现在经常让 Codex 或 ChatGPT 改项目。如果直接在主工作区改：

- 容易污染主分支；
- 多个实验无法并行；
- 回滚依赖 Git 状态；
- 审查 Codex 改动不够清晰。

DevSpace 支持 isolated Git worktrees。MCP4ChatGPT 也应该借鉴这一点。

## 3.2 目标

新增 worktree session：

```text
main repo -> worktree -> agent session
```

例如：

```text
/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT
/Users/vickers/Documents/MCP_Creator/.mcp4chatgpt-worktrees/MCP4ChatGPT/ws_20260618_command-log
```

## 3.3 新增配置

`.env.example` 增加：

```env
MCP_WORKTREE_ROOT=~/Documents/MCP_Creator/.mcp4chatgpt-worktrees
MCP_ALLOW_WORKTREE=true
```

## 3.4 新增 tools

### `workspace_create_worktree`

输入：

```json
{
  "workspace_id": "ws_abc123",
  "branch_name": "agent/command-log-refactor",
  "base": "HEAD"
}
```

行为：

```bash
git worktree add -b agent/command-log-refactor <worktree_path> HEAD
```

返回新的 workspace：

```json
{
  "workspace_id": "ws_def456",
  "root": ".../.mcp4chatgpt-worktrees/MCP4ChatGPT/agent-command-log-refactor",
  "parent_workspace_id": "ws_abc123",
  "branch": "agent/command-log-refactor"
}
```

### `workspace_remove_worktree`

安全移除 worktree。

要求：

- 如果 worktree 有未提交变更，默认拒绝；
- 除非传 `force=true`；
- 操作前返回当前 `git status`。

### `workspace_list_worktrees`

返回当前 repo 的所有 worktree。

## 3.5 安全规则

禁止模型执行：

```bash
git worktree remove --force
git reset --hard
git clean -fdx
git checkout .
```

除非你后续设计专门的安全工具，并需要二次确认。

## 3.6 测试

新增：

- 非 Git repo 创建 worktree 报错；
- clean repo 创建 worktree 成功；
- dirty worktree remove 默认拒绝；
- force remove 能删除；
- 分支名含危险字符时拒绝。

---

# 第四阶段：AGENTS.md / CLAUDE.md / 项目指令自动发现

## 4.1 目标

打开 workspace 时自动读取项目指令文件：

```text
AGENTS.md
CLAUDE.md
.cursor/rules
.github/copilot-instructions.md
.codex/instructions.md
```

初期建议只做：

```text
AGENTS.md
CLAUDE.md
```

后续再扩展。

## 4.2 新增函数

```python
def discover_instruction_files(root: Path, max_chars: int = 12000) -> dict[str, Any]:
    ...
```

返回：

```json
{
  "files": [
    {
      "path": "AGENTS.md",
      "text": "...",
      "truncated": false
    }
  ]
}
```

## 4.3 使用场景

当 ChatGPT 调用：

```text
workspace_open
```

返回内容里直接包含 instructions。

这样后续我可以先基于项目规则做判断，而不是每次都问你“项目规范是什么”。

## 4.4 测试

- 没有指令文件时返回空；
- 有 `AGENTS.md` 时读取；
- 超长文件截断；
- symlink 到外部被拒绝。

---

# 第五阶段：Agent Skills 发现与加载

## 5.1 为什么需要

DevSpace 支持发现 local agent skills。你的项目后续如果要服务 Codex、Claude Code、ChatGPT、Antigravity，最好有统一 skill 目录。

## 5.2 建议目录

```text
~/.mcp4chatgpt/skills/
  python-refactor/
    SKILL.md
  terminal-safe-paste/
    SKILL.md
  llm-agent-dev/
    SKILL.md
```

或项目内：

```text
.mcp/skills/
```

## 5.3 新增 tools

### `skills_list`

返回可用 skills：

```json
{
  "skills": [
    {
      "name": "python-refactor",
      "path": "~/.mcp4chatgpt/skills/python-refactor/SKILL.md",
      "description": "Python refactor workflow"
    }
  ]
}
```

### `skills_read`

读取某个 skill。

### `workspace_skills`

读取当前 workspace 内 `.mcp/skills` 或 `.codex/skills`。

## 5.4 安全规则

- skills 只读；
- 不执行 skill 中的命令；
- 只返回文本；
- 限制单个 skill 最大字符数。

## 5.5 测试

- 正常发现 skill；
- 无 skill 返回空；
- 超长 skill 截断；
- skill symlink 到外部拒绝。

---

# 第六阶段：Shell 执行策略分层

## 6.1 当前已有能力

你现在已经有两类执行模式：

### 后台 shell

```text
local_run_command
```

特点：

- 后台执行；
- 不显示在当前 Terminal；
- 返回 stdout/stderr；
- 写 `logs/commands.jsonl`。

### 可见 Terminal

```text
terminal_run_command
terminal_send_input
```

特点：

- 通过 `co-te.py` 写入前台 Terminal / iTerm2 / Termius；
- 用户可以看到回显；
- `press_return=false` 时只粘贴不执行。

## 6.2 借鉴 DevSpace 后的改造方向

新增 workspace 级执行工具：

```text
workspace_run_command
workspace_terminal_run_command
workspace_terminal_send_input
```

### `workspace_run_command`

输入：

```json
{
  "workspace_id": "ws_abc123",
  "command": ".venv/bin/pytest -q",
  "timeout_sec": 120
}
```

行为：

- cwd 自动绑定 workspace root；
- 写 `commands.jsonl`；
- 返回 stdout/stderr；
- 不污染前台终端。

### `workspace_terminal_run_command`

输入：

```json
{
  "workspace_id": "ws_abc123",
  "command": ".venv/bin/pytest -q",
  "app": "terminal"
}
```

行为：

- 可选先发送 `cd <workspace root>`；
- 再发送命令；
- 按回车执行；
- 用户在终端里能看到。

### `workspace_terminal_send_input`

输入：

```json
{
  "workspace_id": "ws_abc123",
  "text": ".venv/bin/pytest -q",
  "press_return": false
}
```

行为：

- 只粘贴，不执行；
- 适合你要求“让我看一眼再回车”。

## 6.3 命令日志策略

保留当前：

```text
logs/commands.jsonl
```

但建议增加字段：

```json
{
  "workspace_id": "ws_abc123",
  "workspace_root": "...",
  "mode": "background",
  "visible_terminal": false
}
```

对于 Terminal 可见执行，也建议写一条事件：

```json
{
  "tool": "workspace_terminal_send_input",
  "workspace_id": "ws_abc123",
  "text_preview": ".venv/bin/pytest -q",
  "press_return": false,
  "sensitive": false
}
```

如果 `sensitive=true`，只记录：

```json
{
  "text_preview": "[SENSITIVE]"
}
```

## 6.4 安全规则

继续禁止：

- `sudo`
- `rm -rf`
- `dd of=/dev`
- `mkfs`
- `diskutil erase`
- `curl | sh`
- `chmod -R 777`
- `chown -R`

并新增 Git 高风险命令拦截：

```text
git reset --hard
git clean -fd
git clean -fdx
git checkout .
git rebase
git push --force
```

建议这些命令未来通过专门的 Git 安全工具实现，而不是普通 shell。

---

# 第七阶段：Change Summary / Diff Review

## 7.1 目标

每次模型改完项目后，能自动生成变更摘要，避免你靠肉眼翻 diff。

## 7.2 新增 tools

### `workspace_change_summary`

输入：

```json
{
  "workspace_id": "ws_abc123",
  "staged": false,
  "max_chars": 20000
}
```

返回：

```json
{
  "git_status": "...",
  "files_changed": [
    {
      "path": "src/mcp4chatgpt/local_ops.py",
      "change_type": "modified",
      "insertions": 42,
      "deletions": 7
    }
  ],
  "diff": "...",
  "summary": [
    "Added command log tail tool",
    "Improved terminal tool descriptions",
    "Added tests for command log truncation"
  ]
}
```

初期 `summary` 可以只是规则化摘要，不需要 LLM 生成。

## 7.3 新增 tools

### `workspace_review_checklist`

根据项目类型返回审查清单，例如 Python 项目：

```text
- 是否更新测试？
- 是否更新 README？
- 是否引入外部依赖？
- 是否改变安全边界？
- 是否影响 OAuth？
- 是否影响 path allowlist？
- 是否影响 shell command policy？
```

## 7.4 测试

- clean repo 返回空变更；
- modified 文件能列出；
- staged / unstaged 区分正确；
- diff 超长会截断。

---

# 第八阶段：Tool Surface 分层

## 8.1 当前问题

工具越多，ChatGPT 越容易选错。

你已经遇到过：

```text
明明有 terminal_*，但先用了 local_run_command
```

所以工具面要分层。

## 8.2 建议分层

### Core tools

默认暴露：

```text
server_info
workspace_open
workspace_info
workspace_tree
workspace_read_file
workspace_search
workspace_git_status
workspace_git_diff
workspace_change_summary
workspace_run_command
local_command_log_tail
```

### Write tools

需要配置开启：

```text
workspace_write_file
workspace_apply_patch
workspace_create_worktree
workspace_remove_worktree
```

### Terminal tools

默认可见，但描述要非常明确：

```text
workspace_terminal_run_command
workspace_terminal_send_input
terminal_get_app_context
```

### Admin/debug tools

调试时才暴露：

```text
local_read_text
local_write_file
local_run_command
local_apply_patch
```

## 8.3 配置项

`.env.example` 增加：

```env
MCP_TOOL_PROFILE=core
MCP_ENABLE_WRITE_TOOLS=true
MCP_ENABLE_TERMINAL_TOOLS=true
MCP_ENABLE_RAW_LOCAL_TOOLS=false
```

## 8.4 实现方式

在 `tools.py` 中：

```python
def build_tools(config: Config) -> list[Tool]:
    ...
```

目前 `build_tools()` 不接收 config。建议改成可基于 config 动态决定暴露哪些工具。

注意：这会影响 `ToolRegistry` 初始化，需要同步改测试。

---

# 第九阶段：Host Header / Public URL 安全增强

## 9.1 当前问题

如果 MCP 暴露到公网，Host header、public URL、tunnel 配置需要更严格。

DevSpace 的安全模型里明确有 Host header allowlist。你的项目也应该做。

## 9.2 新增配置

```env
MCP_ALLOWED_HOSTS=mcp.runzhe.uk,127.0.0.1,localhost
```

## 9.3 新增逻辑

在 `server.py` 的 `Handler` 请求入口加：

```python
def _validate_host(self):
    host = self.headers.get("Host", "").split(":")[0]
    if host not in config.allowed_hosts:
        reject
```

注意：

- 本地开发允许 `127.0.0.1` / `localhost`；
- public_base_url 的 hostname 自动加入 allowlist；
- Cloudflare tunnel 域名也加入。

## 9.4 测试

- `Host: 127.0.0.1` 允许；
- `Host: evil.com` 拒绝；
- 未配置时默认只允许 public_base_url host + localhost。

---

# 第十阶段：OAuth / Owner Password 加固

## 10.1 已知待修点

当前应补齐：

1. `redirect_uris` 必须是 `list[str]`；
2. `_read_json()` 必须确保 JSON body 是 object；
3. `AUTH_CODES` 需要加锁；
4. OAuth error response 更规范；
5. Owner password 失败应写 audit log，但不能记录密码。

## 10.2 修改点

### `oauth.py`

增加：

```python
AUTH_CODES_LOCK = threading.Lock()
```

包裹：

- `_cleanup_expired_codes`
- `create_auth_redirect`
- `issue_token`

### `register_client`

增加：

```python
redirect_uris = payload.get("redirect_uris", [])
if not isinstance(redirect_uris, list) or not all(isinstance(x, str) for x in redirect_uris):
    raise ValueError("redirect_uris must be a list of strings.")
```

### `server.py`

`_read_json()` 增加 object 校验：

```python
data = json.loads(raw or "{}")
if not isinstance(data, dict):
    raise ValueError("JSON body must be an object.")
return data
```

对 form-urlencoded 保持 dict。

## 10.3 测试

- `/oauth/register` 传 list body 返回 400；
- `redirect_uris: "https://x"` 返回 400；
- PKCE 仍然通过；
- expired code 仍然被消费；
- 并发 issue_token 同一个 code 只能成功一次。

---

# 第十一阶段：Doctor / Self-check 工具

## 11.1 目标

借鉴 DevSpace 的 doctor 思路，为你的项目增加本地诊断。

## 11.2 新增脚本

```text
scripts/doctor.sh
```

检查：

- Python 版本；
- `.venv` 是否存在；
- `mcp4chatgpt` 是否 editable install；
- `pytest` 是否可用；
- `co-te.py` 是否存在；
- `MCP_ALLOWED_ROOTS` 是否存在；
- `MCP_PUBLIC_BASE_URL` 是否合理；
- GitHub CLI 是否登录；
- SSH GitHub 是否可用；
- Cloudflare tunnel 是否运行；
- Terminal 辅助功能是否可能缺失。

## 11.3 新增 MCP tool

```text
local_doctor
```

返回结构化结果：

```json
{
  "checks": [
    {
      "name": "python",
      "ok": true,
      "detail": "Python 3.14.5"
    },
    {
      "name": "pytest",
      "ok": true,
      "detail": "pytest 9.1.0"
    }
  ]
}
```

## 11.4 测试

单元测试只测纯 Python 检查函数，不测真实系统依赖。

---

# 第十二阶段：UI / Tool Card 友好化

## 12.1 目标

让 ChatGPT Apps 里工具返回更容易读。

当前工具多返回 JSON，够用但不够产品化。

可以针对核心工具返回：

```text
content: Markdown summary
structuredContent: JSON
```

例如 `workspace_change_summary` 返回：

```markdown
## Change Summary

- Modified `src/mcp4chatgpt/tools.py`
- Added `local_command_log_tail`
- Tests: 25 passed

### Next action

Review diff or run `.venv/bin/pytest`.
```

同时 structuredContent 保留机器可读字段。

## 12.2 优先改造 tools

- `workspace_open`
- `workspace_git_status`
- `workspace_change_summary`
- `workspace_run_command`
- `local_command_log_tail`
- `local_doctor`

---

# 第十三阶段：项目文档体系

## 13.1 README 增加

```markdown
## Coding workflow with ChatGPT and Codex
```

说明推荐用法：

1. 打开 workspace；
2. 读取 git status；
3. 让 ChatGPT 生成 Codex prompt；
4. Codex 修改；
5. ChatGPT 通过 MCP 审查 diff；
6. 跑测试；
7. 生成 change summary；
8. 你决定 commit。

## 13.2 新增 docs

```text
docs/workspace-model.md
docs/worktree-workflow.md
docs/terminal-visible-mode.md
docs/security-model.md
docs/codex-handoff-workflow.md
```

## 13.3 新增 AGENTS.md

在你的项目根目录新增：

```text
AGENTS.md
```

内容包括：

- 项目架构；
- 测试命令；
- 安全边界；
- 禁止事项；
- 修改要求；
- Codex 工作流。

这样 Codex 和 ChatGPT 都能读。

---

# 第十四阶段：Codex 协作工作流

## 14.1 新增工具或模板

新增：

```text
workspace_create_codex_task
```

生成：

```text
docs/tasks/2026-06-18-workspace-model.md
```

内容包括：

- 目标；
- 约束；
- 涉及文件；
- 验收命令；
- 测试要求；
- 禁止事项。

## 14.2 新增 `RESULT.md` 读取约定

Codex 完成后在项目内写：

```text
.codex/RESULT.md
```

MCP4ChatGPT 新增：

```text
workspace_read_codex_result
```

我可以通过 MCP 读取 Codex 的结果，再审查实际 diff。

## 14.3 推荐流程

```text
ChatGPT 生成任务
↓
Codex 实现
↓
ChatGPT 读 git diff
↓
ChatGPT 跑 pytest
↓
ChatGPT 生成 review
↓
用户决定是否 commit
```

---

# 第十五阶段：实施路线图

## Phase 1：基础安全与测试修补

目标：修掉当前已知稳健性问题。

任务：

1. `redirect_uris` 类型校验；
2. `_read_json()` body 类型校验；
3. `AUTH_CODES` 加锁；
4. `pyproject.toml` 增加 dev optional dependency；
5. `commands.jsonl` tail 改成 `deque(maxlen=limit)`；
6. 增加对应测试。

验收：

```bash
.venv/bin/pytest
```

预期：

```text
所有测试通过
```

---

## Phase 2：Workspace MVP

目标：建立 workspace 抽象，但不破坏旧工具。

任务：

1. 新增 `workspace_ops.py`；
2. 新增 `workspace_open`；
3. 新增 `workspace_list`；
4. 新增 `workspace_info`；
5. 新增 repo-relative path resolver；
6. 新增 `workspace_read_file`；
7. 新增 `workspace_tree`；
8. 新增 `workspace_git_status`；
9. 新增测试。

验收：

```bash
.venv/bin/pytest
```

手动验证：

```text
打开 MCP4ChatGPT 项目作为 workspace
读取 README.md
查看 git status
```

---

## Phase 3：Workspace Shell + Terminal

目标：把后台执行和可见终端执行绑定到 workspace。

任务：

1. 新增 `workspace_run_command`；
2. 新增 `workspace_terminal_run_command`；
3. 新增 `workspace_terminal_send_input`；
4. 命令日志增加 `workspace_id`；
5. Terminal 执行日志增加 preview；
6. 测试 tool schema；
7. 手动测试 Terminal 可见输入。

验收：

```bash
.venv/bin/pytest
```

手动验证：

```text
让 MCP 在 Terminal 里粘贴 `.venv/bin/pytest -q`，不按回车
```

---

## Phase 4：Worktree

目标：支持隔离开发。

任务：

1. 新增 worktree root config；
2. 新增 `workspace_create_worktree`；
3. 新增 `workspace_list_worktrees`；
4. 新增 `workspace_remove_worktree`；
5. 防止 dirty worktree 被误删；
6. 测试 Git worktree 流程。

验收：

```bash
.venv/bin/pytest
```

手动验证：

```text
创建 agent 分支 worktree
在 worktree 跑测试
删除 clean worktree
```

---

## Phase 5：Instructions + Skills

目标：让模型自动理解项目规则。

任务：

1. `workspace_open` 自动读取 `AGENTS.md` / `CLAUDE.md`；
2. 新增 `skills_list`；
3. 新增 `skills_read`；
4. 支持 workspace 内 `.mcp/skills`；
5. 增加文档。

验收：

```bash
.venv/bin/pytest
```

---

## Phase 6：Change Summary / Codex Workflow

目标：形成 ChatGPT + Codex 的闭环。

任务：

1. 新增 `workspace_change_summary`；
2. 新增 `workspace_review_checklist`；
3. 新增 `workspace_create_codex_task`；
4. 新增 `workspace_read_codex_result`；
5. 新增 `docs/codex-handoff-workflow.md`；
6. README 增加推荐流程。

验收：

```bash
.venv/bin/pytest
```

手动验证：

```text
让 Codex 改一个小功能
ChatGPT 通过 MCP 审查 diff
ChatGPT 跑测试
ChatGPT 总结变更
```

---

## Phase 7：Tool Profile 与 Host 安全

目标：降低误调用和公网暴露风险。

任务：

1. `build_tools(config)` 支持动态工具注册；
2. 新增 `MCP_TOOL_PROFILE`；
3. 新增 `MCP_ENABLE_RAW_LOCAL_TOOLS`；
4. 新增 `MCP_ALLOWED_HOSTS`；
5. server 增加 Host header allowlist；
6. 测试 Host 拦截。

验收：

```bash
.venv/bin/pytest
```

---

# 最终建议的优先级

## 必做

1. Workspace 模型；
2. repo-relative path；
3. worktree；
4. instruction discovery；
5. workspace command logging；
6. Codex handoff workflow。

## 强烈建议

1. Host allowlist；
2. OAuth 加锁；
3. tool profile；
4. doctor；
5. change summary。

## 可以后做

1. fancy UI tool cards；
2. skills marketplace；
3. multi-client session；
4. web dashboard；
5. sqlite 状态库。

---

# 最小可行版本

如果只做一个最小版本，我建议只实现这 8 个工具：

```text
workspace_open
workspace_info
workspace_tree
workspace_read_file
workspace_git_status
workspace_git_diff
workspace_run_command
workspace_change_summary
```

再加两个 Terminal 工具：

```text
workspace_terminal_run_command
workspace_terminal_send_input
```

这就足以让你的 MCP4ChatGPT 从“本地工具集合”升级成“本地 coding workspace”。

---

# 对 Codex 的实施策略

不要一次性让 Codex 做完整大改。

推荐拆成 6 个 Codex 任务：

1. 修复 OAuth / pytest / command log tail 小问题；
2. 实现 workspace MVP；
3. 实现 workspace file/git tools；
4. 实现 workspace shell/terminal tools；
5. 实现 worktree；
6. 实现 AGENTS.md / skills / change summary。

每个任务都必须要求：

```bash
.venv/bin/pytest
```

通过，并输出：

1. 修改文件；
2. 新增工具；
3. 安全边界；
4. 测试结果；
5. 未完成事项。

---

# 核心判断

DevSpace 证明了一个方向：

> ChatGPT 不一定要通过 API agent 才能做本地开发。通过 MCP，它可以成为本地 repo 的架构师、审查者和操作协调器。

但你的项目应该比 DevSpace 多一个关键能力：

> 所有关键命令既可以后台执行，也可以在真实 Terminal 窗口中可见粘贴/执行。

这对你这种高频调试、本地开发、希望回溯操作的人非常重要。

所以最终形态应是：

```text
MCP4ChatGPT
├── Workspace 管理
├── Git / diff / change summary
├── 后台 shell 执行日志
├── 可见 Terminal 接管
├── Worktree 隔离开发
├── AGENTS.md / skills 规则加载
├── Codex handoff
└── Host / OAuth / allowlist 安全边界
```

这条路线比单纯复刻 DevSpace 更值得做。

---

**Sources:**

- [GitHub - Waishnav/devspace: Turn ChatGPT into Codex · GitHub](https://github.com/Waishnav/devspace)
- [raw.githubusercontent.com](https://raw.githubusercontent.com/Waishnav/devspace/main/docs/security.md)



---
Powered by [ChatGPT Exporter](https://www.chatgptexporter.com)