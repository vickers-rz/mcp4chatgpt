# Gemini Handoff：CodexPro 接合 WebCodex 的 Coding 能力计划

日期：2026-09-21。状态：待实施计划，不代表已有可用集成。

## 1. 任务、目标仓库与职责

你是接手此任务的 Gemini。实施目标为 `/Users/vickers/Documents/MCP_Creator/rebel0789-codexpro-local`，不是存放本文的 MCP4ChatGPT。用户决定：CodexPro 补齐 coding 能力，MCP4ChatGPT 单独补齐 Mac GUI。

目标是保留 CodexPro 的现有文件/Git/仓库能力，通过专用 MCP 适配器复用 WebCodex 的项目工作流、LSP、长任务与执行能力。先验证真实接合，再判断是否需要自研同类功能。不要先重写一套 Jobs、LSP、worktree 后才测试 WebCodex。

WebCodex 独立运行，首版连接已配置的本地 HTTP MCP；不把 Rust workspace 合并进 TypeScript 项目，不在本任务增加 Browser/Computer/桌面控制，不把 MCP4ChatGPT 放进调用链。

## 2. 已核对基线与上下文

- 主仓库基线：`2097e7ba55f9a33877d3a980d03bb808fa81f406`，package 版本 0.30.2。
- 已有设计 worktree：`/Users/vickers/Documents/MCP_Creator/codexpro-webcodex-integration`。
- 设计分支：`design/webcodex-integration`；此前读取提交 `b377b31`。相对主分支仅修改 ROADMAP.md 和 589 行设计文档，没有适配器实现。
- 设计文档：该 worktree 的 `docs/WEBCODEX_INTEGRATION.md`。
- WebCodex 本地参考：`/Users/vickers/Documents/MCP_Creator/webcodex-audit`，已核对 HEAD `0fd784b05564aeba834800ea36354242df874b82`。
- 相关 Codex 任务：`codex://threads/01a0c320-203f-77b3-9e35-7df8b4089888`，标题“复核项目无法运行原因”。

这些是历史观测值。开工重新确认各目录 HEAD、分支、工作区差异和 AGENTS.md，不强制 checkout、reset 或覆盖现有设计。实施建议使用独立 `codex/` 分支/worktree，避免影响运行中的主目录。

已知代码落点：`src/server.ts` 的工具注册、toolMode 与 supertool；`src/workspaceOps.ts` 的 workspace；`src/config.ts`、`profileStore.ts`；`src/http.ts`/`stdio.ts` 的服务；`src/bashOps.ts`、`gitOps.ts`、`fsOps.ts`；`scripts/*smoke.mjs`。

当前依赖含 `@modelcontextprotocol/sdk`；可复用 client，但必须核对安装版本的具体 API。已有 HTTP 代码是服务端，并非下游客户端。supertool 只包装已注册本地 handler，不等于已支持外部运行时。

相关前次复核：full bash 并非 OS 沙箱；workspace 路径校验不限制任意子进程访问其他目录。不要把这一点在新 Jobs 或适配器说明中说错。

## 3. 相对旧设计的调整

保留：可选 sidecar、默认关闭、本地 endpoint、凭证由本地配置提供、按能力逐步开放、不平铺全部工具。

调整：

1. 把只读接合 PoC 提前，不以原生 Jobs/LSP/worktree 为前置条件。
2. 首版重点从 Browser/Computer/plugin 改成 coding 工作流。
3. 不能只依据 tools/list 做名称转发；识别 direct/gateway/不可用及目标 schema。
4. 不能把 `share --json` 不输出 token 推成所有部署方式都不可接入。首版使用已配置的常规 Server + Runner 或另经验证的本地部署。
5. 旧文档“单公网入口”解释为 CodexPro 此接合路径只暴露 CodexPro；用户仍可独立使用 MCP4ChatGPT，不强制全系统只有一个入口。

参考文档：

- https://github.com/yyjeqhc/webcodex/blob/main/docs/MCP.zh-CN.md
- https://github.com/yyjeqhc/webcodex/blob/main/docs/PERSONAL_SETUP.zh-CN.md
- https://github.com/yyjeqhc/webcodex/blob/main/docs/AUTH_MODEL.zh-CN.md
- 本地 `webcodex-audit/src/model_surface.rs` 与 canonical tool definitions。

记录实际连接的 WebCodex 版本/提交，按该版本的 manifest 与源码建立支持清单，不能假设 main 永久兼容。

## 4. 最终架构与能力归属

```text
ChatGPT → CodexPro MCP
          ├─ 原生文件/Git/仓库分析
          └─ 受控 coding runtime 适配器
              → 本地 WebCodex HTTP MCP
                  → WebCodex Server / Runner
                      → Project / Workflow Session / Job / LSP
```

一个 coding 任务确定执行归属：进入 WebCodex 工作流后，由 WebCodex 管理 Session、Job、执行状态和恢复；CodexPro 保存关联身份、展示结果并转发显式操作。禁止同时创建另一套 CodexPro Job 状态机来“猜测”WebCodex 状态。

原生工具继续保留。原生文件修改可能影响 WebCodex 中的会话/工作区，不能宣称自动协调；在任务说明中明确工具选择，涉及切换执行路径时重新观察仓库和会话。worktree 一旦创建，所有后续项目、cwd 与资源绑定必须指向它，不能继续对源目录执行。

## 5. 模块与配置设计

建议模块：

| 模块 | 职责 |
| --- | --- |
| `src/externalRuntimeOps.ts` | MCP client、连接/会话、超时、关闭、无重放重连 |
| `src/webcodexAdapter.ts` | manifest 路由、目标白名单、参数/结果、能力协商 |
| `src/webcodexBindings.ts` | workspace → Server/Runner/Project 绑定与有效性 |
| `src/webcodexSessions.ts` | 最小 Session/Job 关联信息及恢复，不自建执行器 |
| `scripts/webcodex-adapter-smoke.mjs` | 假 MCP server 的可重复集成测试 |
| `docs/WEBCODEX_CODING.md` | 部署、支持矩阵、操作和故障处理 |

可先将少量逻辑合并，达到可维护规模再拆分；不为此全面重构 server.ts。

配置草案：

```text
CODEXPRO_WEBCODEX_MODE=off|external
CODEXPRO_WEBCODEX_URL=http://127.0.0.1:PORT/mcp
CODEXPRO_WEBCODEX_TOKEN_FILE=/private/path/token
```

项目绑定与 coding 能力开关通过本地受控配置提供，不能由模型传任意 URL/token/Runner/root。非 loopback 首版拒绝；拒绝 credential-bearing URL，避免 redirect 把认证带到其他主机。不要在日志输出认证 header 或 token 文件内容。

独立 WebCodex 凭证必须限制为实际需要的身份/项目权限，不能默认使用 bootstrap admin 凭证。CodexPro 的现有 token 不自动等同 WebCodex 身份；首版限定单个配置好的本地身份并如实说明，不宣称多用户身份透传。

模式 off 时无需 WebCodex 即可启动；external 不可用时原生工具继续可用，状态显示 degraded。runtime-owned 工具默认关闭，禁止降级为本地 full bash 执行同一请求。

## 6. 协议、路由与结果契约

### 6.1 发现与调用

完成 initialize、协议版本协商和 tools/list（含分页），再通过该版本提供的 manifest/discovery 获取目标 schema 和调用方式。`runtime_status`、`tool_manifest` 是已设计的候选名称，开工必须验证真实可达方式，不能硬编码不存在的接口。

维护内部调用表：业务 capability → 精确 canonical tool → direct 或 gateway route → 输入 schema → effect 分类 → 所需绑定。网关与直接工具的授权不因 presentation 改变。

若使用 `call_runtime_tool`，白名单必须检查它内部的最终目标。拒绝递归 gateway、未知工具、模型提供的任意嵌套转发及 `mcp_tool`/plugin/Browser/Computer 能力。网关内的 action 参数也必须验证，不能只允许一个名称就开放全部子动作。

### 6.2 返回值

以 WebCodex `structuredContent` 为主要机器结果，保留 content、image/resource、isError 与必要元数据。不要把对象简化成摘要字符串；也不要假设 HTTP 200 或 isError=false 就是业务成功。WebCodex 某些客户端兼容路径可能返回 structuredContent.success=false，应按已协商/已验证契约解读。

保留原始业务结果并加独立 CodexPro 元信息，避免与 supertool 的现有 structuredContent 包装冲突。明确并测试包装格式。对客户端无法显示结构化字段的问题，只在有证据时加入有界文本兼容，不默认复制大体积结果。

### 6.3 重连、超时和取消

工具请求超时、HTTP 会话断开不代表远端 Job 停止。区分 MCP request cancellation 与 Job stop；后者必须调用具体已授权能力。

动作派发后结果丢失返回 outcome_unknown，并保留可用 Job/Session ID。禁止自动重放修改、启动 Job、建 worktree、提交等动作；先用精确身份查询恢复。不要假造 idempotency key 的服务端支持。

首版只接入标准 MCP 能表达的已验证工具；依赖隐藏 host capability、交互 elicitation 或未支持 server-to-client 请求的功能明确 unavailable，不能伪装为完整透明代理。

## 7. 绑定与授权模型

绑定至少区分 CodexPro workspace、本地规范化根目录、WebCodex server 身份、Project ID、Runner 身份、已协商能力/版本。只比较根路径字符串不够，特别是在远程机器上。

首版限制为已确认本机 Runner 和显式映射项目。身份变更、服务重建、Project 指向变化或凭证更换时重新验证；不要把旧任务自动挂到同名新项目。外部 project/worktree 路径未经校验不得当作 CodexPro 已允许路径。

原有 toolMode、writeMode、bashMode 与 WebCodex 权限是不同概念。新增 coding adapter 必须有显式效果策略：读、文件修改、进程启动、终止、worktree 管理分别授权。现有写入禁用状态不得被适配器绕过；无法等价映射 safe bash 时，保留禁用或要求单独配置，不能把 WebCodex 任意进程启动当成 safe bash。

权限校验发生在：注册/发现、适配器入口、最终目标、项目绑定和远端 WebCodex 自身。annotations 不是执行边界。不把 workspace allowlist 描述为 OS 级沙箱。

## 8. 分阶段路线与完成标准

### C0：确认基线与开发实例

检查仓库及设计分支，读取适用说明和真实 WebCodex 契约。确认服务配置方式，但不读取或打印正式凭证。记录已有测试结果；历史任务的通过记录不能冒充本次运行。

整理目标能力表：只读仓库/符号、工作流入口、编辑、执行、Job 查询/终止、收尾、worktree；逐项记录实际工具名、route、schema 和依赖。

完成标准：选定可重复的测试 Server/Runner 配置和临时仓库，明确哪些能力本版本支持，哪些需要 Host 特殊支持。

### C1：只读接合 PoC

实现外部 client、认证文件读取、连接状态、manifest 发现和最终目标过滤。先打通只读状态与一项真实 project-bound 查询；验证结果能经 CodexPro MCP 返回给调用者。

完成标准：正确凭证可用，错误凭证/错误项目明确失败；未知 gateway target 被拒绝；WebCodex 停机不影响 CodexPro 原生工具。未过此关不得开始大规模原生 LSP/Job 自研。

### C2：Coding 只读能力

接入已支持的 LSP symbols/definition/references/diagnostics，保留相对路径、位置、截断和 stale/unavailable 信息。工具名依 manifest，不凭概念名构造。

使用受支持语言的测试仓库；语言服务器缺失时返回原因，不自动安装依赖。Git/read/search 与原生能力重复时保留清楚入口，不向模型平铺两整套同义工具。

完成标准：至少一种语言的定义或引用查询端到端有效，路径映射正确，越界项目被拒绝，缺失语言服务不是空成功结果。

### C3：工作流、执行和长任务

接入精确的 workflow start/resume、受控编辑、进程/Job 能力、状态/结果/显式终止和任务收尾。优先复用 WebCodex 管理器，不创建第二个执行器。选择少量有清晰 schema 的 model-facing 工具，长尾走受控 gateway。

持久化最小关联：任务归属、server/project/runner/session/job 身份、最后明确状态与更新时间。凭证不进入记录。重启后通过远端查询对账，不把本地最后状态当事实。

完成标准：在临时仓库完成“打开项目 → 查询代码 → 修改一处 → 启动验证 → 获取结果 → 查看 diff → 收尾”；另验证请求超时后远端 Job 继续可查询，CodexPro 重启后不重复启动 Job。

### C4：可选隔离 worktree

只有实际需要隔离任务时实现 WebCodex 原生 worktree 路径。保留 exact base ref/commit、新 Project 身份、源仓库关系。存在脏文件、失败创建、半完成绑定时明确报告，不删除或清理用户内容。

完成标准：任务修改只发生在新 worktree，查询/验证/收尾都绑定正确项目；显式清理前检查未提交变更。若前阶段已满足用户核心任务，此阶段可作为明确后续，不阻塞可用接合交付。

### C5：文档、配置与兼容回归

把验证过的模式接入配置、inventory、状态和必要的诊断；保持 minimal/standard/full 语义明确，关闭接合时原工具契约不变。更新旧设计文档中被本方案替代的 Browser/Computer-first 与 native-first 排期，不删除历史事实。

完成标准：新用户能按文档配置开发实例，默认 off 无额外依赖，配置错误可诊断，回退只需禁用接合。正式服务切换是后续部署动作。

## 9. 测试计划

假 HTTP MCP server 至少覆盖：initialize、分页、direct/gateway、schema 变化、未知工具、嵌套越权、认证失败、断连、超时、结构化业务失败、image/resource 保留和客户端关闭。fake 服务能记录调用次数，断言动作没有因重连重复执行。

绑定测试覆盖：同路径不同 Runner、同名不同 Project、服务重建、worktree 新 root、凭证切换、写入关闭、执行关闭和外部能力关闭。

真实端到端测试覆盖：只读项目查询、至少一种 LSP 查询、受控代码修改与测试、长任务状态恢复、显式停止。保存脱敏证据；若缺少真实 WebCodex 实例只能交付 PoC/未验证状态，不宣称整体集成完成。

按当前项目实际脚本先执行 `npm run build`、新增 adapter smoke，以及受影响的现有 smoke；集成到默认行为后执行 `npm run smoke`。stress、完整发布检查仅在相关改动或发布要求需要时执行，不把旧任务里通过的检查当作本次结果。

不能用“本地 handler 被 mock 成成功”代替 CodexPro → HTTP MCP → WebCodex → Runner 的端到端证据。

## 10. 首版明确不做

不接 GUI、Browser、SSH、插件任意调用、多用户 IAM、WebCodex Desktop/Console；不自动下载运行时、不从剪贴板或人类输出抓 token；不自动托管 `webcodex share`，不复制整个 Rust 项目。

不自动 commit/push 用户仓库。测试使用临时仓库和明确的测试数据。源码实现、测试、文档属于本任务；正式部署、重启现有服务、推送和发布按用户后续指令执行。

若 WebCodex 某项能力在实际版本缺失，先记录缺口及可选路径，不偷偷切换成无约束 shell，也不扩大为自研平台。

## 11. Gemini 最终交付报告模板

报告必须包括：

1. 实际仓库、分支、HEAD、修改文件及无关变更保留情况。
2. 实际 WebCodex 版本、部署方式和支持能力矩阵。
3. C0–C5 各阶段完成/未完成与证据，不用单一“已接入”掩盖缺失。
4. 实际测试命令、通过/失败、未执行的验证及原因。
5. 认证、Project/Runner/Session/Job 归属、outcome_unknown 恢复方式。
6. 哪些原生能力保留、哪些交给 WebCodex，以及用户应如何选择入口。
7. 禁用/回退步骤、剩余限制，以及是否发生服务操作或发布。

交付目标是可靠地完成一个实际 coding 闭环，而不是增加尽可能多的工具名。
