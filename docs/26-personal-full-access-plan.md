# Personal Full-Access Plan（已实施）

日期：2026-09-27；实施：2026-09-28  
状态：**IMPLEMENTED / 已实施并进入验收**  
适用场景：受信任的、单用户个人开发环境。本文不是共享/匿名公网部署建议。

> 2026-09-28 用户明确恢复本计划。实现采用独立 `MCP_PERSONAL_FULL_ACCESS=1` profile；`restart-full` 默认同时启用 `Computer Use=interact`、`allowed_apps=*`、`backend=auto` 与 `MCP_TOOL_EXPOSURE=compact`。认证、审计、文件事务、durable-job 幂等/取消语义和 capability/backend identity 校验继续保留。

## 1. 目标

后续增加一个明确选择的 personal full-access profile，使 ChatGPT/MCP 客户端在用户自己的受信任机器上可以获得完整开发代理能力，而不是继续把“发现到能力”“允许调用能力”“结果是否原样返回”混成同一个开关。

目标 profile 的基本原则：

- 权限扩大必须是**显式 opt-in**，普通/default profile 不因此改变。
- ChatGPT 可以拥有实际执行权限；读取、搜索、状态、diff 属于观察类能力，写入、patch、shell、Computer Use、终端控制属于 mutation。
- full access 不意味着放弃并发正确性、事务、幂等、恢复、审计和目标身份校验。
- review/验收必须重新读取真实文件、Git diff 和测试结果，不能只相信 mutation tool 的返回值。

## 2. 已确认的 full-access 能力范围

### 2.1 co-te

保留并纳入 personal full-access profile 的既有 co-te 集成，不复制另一套终端实现。

目标能力包括现有 terminal_*/app_*/Apple Notes 等 co-te 路径，以及其面向 Terminal.app、iTerm2、Termius 的可见终端交互能力：

- 读取终端屏幕/历史；
- 发送命令和普通输入；
- 处理交互式提示、REPL、pager 等终端状态；
- 发送 Ctrl+C / Ctrl+D 等控制输入；
- 在明确选中的个人 full-access profile 中，不再把 co-te 视为只能观察的旁路。

co-te 仍是外部集成依赖，能力可用性应由实际运行证据报告；“已注册”不能等价于“当前可执行”。

### 2.2 shell 与 rm

个人 full-access profile 中，**rm 不再因为命令名本身被本项目的 shell command policy 一刀切拒绝**。

后续实现应明确区分：

- **命令授权**：personal full-access profile 明确允许 rm；
- **执行正确性**：同步/后台任务的 timeout、operation identity、结果状态仍保留；
- **调用审计**：仍记录 canonical tool、入口、执行结果和失败状态。

这项改造的目标是取消个人受信任环境里不必要的命令类别封锁，不是删除 workspace transaction / durable job 的正确性机制。

### 2.3 全链路原文输出

personal full-access profile 的响应数据路径目标为 **raw / unredacted / untruncated by project policy**：只要底层能力实际返回了数据，MCP4ChatGPT 不应在中间层悄悄改写、脱敏或截断后再把它冒充完整结果。

需要覆盖的链路至少包括：

- local tool result；
- shell/command stdout、stderr；
- durable job logs/result；
- co-te 终端读取结果；
- downstream MCP 的 content、structuredContent、isError 与 resource/link metadata；
- browser/extension/Computer Use 的文本结果；
- discovery/capability result 中需要保真的原始定义和 output schema。

“原文输出”是 response-plane 语义：后续实现时，审计日志是否保存完整敏感正文应单独设计，不能为了日志便利反向修改正常 MCP response。

任何真实下游只返回部分数据、分页数据或资源引用时，仍必须如实标记；full access 不能把“底层本来就是 partial”伪装成完整数据。

## 3. 继承现有 workspace transaction 语义

full access **扩大可执行权限，但不回退文件写入正确性**。现有 workspace/file transaction 机制继续作为 mutation 基础：

- per-target lock；
- expected_sha256 compare-and-swap；
- whole-file replacement 的完整观察证明；
- candidate validation；
- before-image recovery blob；
- PREPARED → COMMITTED/ABORTED journal；
- same-directory temp + fsync + atomic replace；
- Git recovery ref 的 before/after 固化；
- finalize 前失败时 rollback；
- filesystem/Git 已 durable、但最终 COMMITTED journal 追加失败时，返回 commit_record_failed，不能把已提交写入误报成失败从而诱发危险 replay。

如果后续“全链路原文输出”与当前 full_replace_token 的“redacted/truncated read 不授予覆盖资格”发生冲突，应调整 read/output profile 的实现，使 full-access read 本身得到完整原文；**不能通过绕过 CAS、transaction 或 recovery 来解决**。

## 4. 继承现有 durable jobs 语义

长任务继续使用现有：

- local_start_job
- local_job_status
- local_job_logs
- local_list_jobs
- local_cancel_job

personal full-access 不新建第二套后台执行系统。必须继续保留：

- caller-supplied operation_id；
- canonical request fingerprint；
- same operation + same fingerprint replay 返回已有 job，不重复 spawn；
- same operation + different fingerprint → idempotency_conflict；
- uncertain reserved state fail-closed，不自动猜测重启；
- detached supervisor；
- stdout/stderr 持久化；
- total timeout；
- explicit cancellation；
- status/logs/list 只观察，不隐式 retry/repair/cancel；
- log cursor/offset 可重放且确定。

因此，未来允许更广 shell 命令（包括 rm）并不改变“长命令必须走 durable job”和“at-least-once delivery 不能造成重复执行”的基础契约。

## 5. 权限、认证与 profile 边界

后续实施时应把 personal full-access 做成**独立、显式 profile**，而不是把默认配置整体改成 unrestricted。

需要统一检查的控制面包括：

1. direct MCP tool；
2. capability_call；
3. co-te / terminal；
4. shell 与 durable jobs；
5. file mutations；
6. Computer Use；
7. browser/extension/downstream MCP；
8. 未来 orchestration bridge。

所有入口最终仍经过 canonical resolution、schema validation、目标身份/版本检查和 audit routing。full-access profile 可以扩大 policy allowlist，但不应创建绕过 ToolRegistry/handler 校验的“后门”执行路径。

认证、远程监听和共享部署的具体默认值在真正实施本计划时单独验收；本文不把“个人 full access”误写成“任何网络来源都应无认证访问”。

## 6. 与 doc-25 / Code Mode 的关系

本计划与 [doc-25](25-cloudflare-code-mode-reassessment.md) 的 P0–P3 分开推进：

- personal full-access 解决**用户明确授权后的能力边界**；
- doc-25 P0–P2 解决 benchmark、capability metadata、search/list discovery；
- doc-25 P3 只有在端到端证据支持后才讨论通用 orchestration executor。

本次实施遵守以下边界：

- 不为了 full access 关闭 OAuth 或变成匿名公网访问；
- `rm` 与其他 shell 命令在显式 personal full-access profile 中不再受项目级危险命令类别封锁；default/public-safe profile 维持原策略；
- 正常 MCP response plane 在 personal full-access profile 中不再做项目级 redaction/truncation；审计/command log 仍保持脱敏和有界记录；
- co-te 默认读取在 full-access profile 中改为不脱敏、不按 12k 默认截断，写入/终端交互沿用既有入口；
- 不用 full access 作为建设任意 execute(code) 的理由；
- 不合并浏览器 backend；
- full access 与 full/compact exposure 继续是独立控制面；当前 `restart-full` 仅把 compact 作为该启动入口的默认值，可用 `MCP_FULL_TOOL_EXPOSURE` 独立覆盖。
- MCP4ChatGPT 的 full-access 只扩大服务端策略，不关闭 ChatGPT/MCP Client/宿主平台自身的安全检查；宿主在工具调用到达本服务前仍可能拒绝特定请求。

## 7. 实施验收清单

真正恢复此计划时，至少应逐项验收：

- default profile 与 personal full-access profile 行为严格分离；
- co-te 读/写/交互链路；
- rm 的 direct shell 与 durable-job 路径；
- stdout/stderr/job logs/downstream content 的原文保真；
- response 中不发生项目级静默 redaction/truncation；
- workspace CAS、journal、Git recovery、rollback 不回归；
- job exactly-once/idempotency/uncertain-start 语义不回归；
- direct、capability_call、downstream、Computer Use 仍统一审计；
- 目标 capability/backend instance 变化后旧绑定仍失败关闭；
- full-access 权限语义不依赖 full/compact exposure；启动器默认 compact，但允许独立覆盖；
- 正式服务仅在用户明确要求后重启。

## 8. 当前决定与实现结果

**已恢复并实施。**

2026-09-28 已完成独立 personal full-access profile、Computer Use 完整交互 profile、response-plane 原文策略和 compact exposure 收口。default/public-safe profile 保持原有安全策略；OAuth、审计、workspace transaction、durable-job exactly-once/idempotency、capability/backend identity 校验均继续保留。

compact exposure 在 Computer Use 开启后仍只暴露 `server_info`、`capability_search`、`capability_get`、`capability_call` 四个 bootstrap 工具，其余能力通过 catalog 按需发现和调用。

正式服务验收结果：完整回归 `318 passed, 11 skipped, 0 failed`；`git diff --check` 与启动脚本 shell syntax 通过；重启后 `server_info` 报告 `personal_full_access=true`、Computer Use `interact + * + auto`、`159 capabilities / 4 listed tools`；Computer Use 权限三项均为 true，真实 capability 调用成功；正常 shell response 已验证原样返回且不发生项目级脱敏。宿主层对某些高风险调用仍可在到达 MCP4ChatGPT 前拒绝，这不属于本服务可关闭的权限层。
