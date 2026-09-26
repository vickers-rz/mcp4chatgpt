# 2026-09-26 代码、架构与文档审核

## 范围与结论

基线为 `000b7f3`（Harden local defaults and add safe automation controls），并包含当前未提交的 PDF 改动：README、pyproject、uv.lock、tools.py 与新增 pdf_ops.py。此前 GUI、持久任务和文件事务已提交，不能再将整个项目描述为“全部尚未提交”。

本次重点检查 HTTP/OAuth、工具路由、CUA/native、持久任务、文件事务、启动脚本及 PDF 增量；对浏览器、下游 MCP、知识库和归档模块核对结构与既有测试。不是对所有平台、所有第三方运行时和所有 UI 场景的穷尽证明。

结论：模块拆分方向合理，但若干关键故障语义与文档承诺不一致。暂不建议把“全量测试通过”等同于发布验收通过，尤其不应直接发布 PDF 编辑能力。

## 验证结果

- 完整测试：228 passed，4 skipped；默认跳过原生 GUI 实测，本轮没有操作用户桌面。
- `git diff --check` 通过。
- 使用临时目录、合成 PDF、受控子进程和故障注入复现以下问题；未修改业务代码、未重启正式服务。
- PyMuPDF 复现使用项目环境中的 1.28.2。
- 自建测试进程已清理。未访问真实用户 PDF 或 OAuth 存储。

## 可复现问题

### R1 / P1 — PDF 大小写参数不能兑现契约

位置：`src/mcp4chatgpt/pdf_ops.py:116`。

`case_sensitive=False` 引用当前 PyMuPDF 不存在的 `TEXT_IGNORECASE`，直接抛 AttributeError；`True` 分支的 `flags=0` 也不启用大小写敏感搜索。在包含 `Secret SECRET secret` 的合成 PDF 中，用默认参数查询 `Secret` 实际匹配并删除三处。

影响：默认操作永久移除超出调用者指定范围的文字，另一分支则完全不可用。

建议：显式实现与验证大小写匹配规则；测试混合大小写、多行、连字符及零匹配，并对生成 PDF 重新提取文本核验。

### R2 / P2 — PDF replacement 装不下时静默丢失

位置：`src/mcp4chatgpt/pdf_ops.py:125`。

原文先被 redact，随后忽略 `insert_textbox` 的返回值。用较长 replacement 替换短词，函数正常返回成功，输出 PDF 中既没有原词，也没有 replacement。当前测试集没有 PDF 专项测试。

建议：删除前预检布局，检查插入结果；不能容纳时明确失败，不发布半完成输出。还应覆盖中文字体和页面边界。

### R3 / P1 — CUA 超时不能约束阻塞 I/O

位置：`src/mcp4chatgpt/computer_cua_backend.py:164-196`。

select 只证明有字节可读，接下来的文本 `readline()` 仍会等待换行。受控 helper 只输出 `{` 然后停顿，`_read_message(0.1)` 在 0.5 秒后仍阻塞，直到终止测试 helper 才返回。写入端同样使用阻塞 write/flush。CUA 外层持有全局 RLock，因此一次卡住可以阻塞所有 CUA 请求。文本缓冲与 select 混用还可能漏掉已预读的下一条消息。

建议：像 native transport 一样改为非阻塞字节 I/O、持久读缓冲、统一单调时钟 deadline，并限制锁等待和消息大小；覆盖半条消息、合并消息、堵塞 stdin 和断连测试。

### R4 / P1 — CUA 自动审批未落实文档中的风险限制

位置：`src/mcp4chatgpt/computer_cua_backend.py:202-215`；文档 `docs/macos-computer-use.md:48`。

实现只看 `_meta.tool_name` 与外层 app allowlist，不检查 riskLevel 或 connector 身份。对预期工具名 `paste`，即使 metadata 标注 `riskLevel=high`、非 Computer Use connector，谓词仍返回 True。随后 `_wait_response` 将该判断映射为 accept。文档“仅低风险内部 Computer Use 请求自动批准”目前不成立。

建议：按真实 upstream schema 校验风险、connector、操作与目标；未知结构不能依据工具名自动接受。若实际产品意图是由本服务承担所有审批，应明确调整策略与文档，不能保留不实保证。

### R5 / P1 — 任务取消可留下继续运行的后代进程

位置：`src/mcp4chatgpt/jobs/runner.py:62-84`，以及 manager.py 的终态存活判断。

`_terminate_child` 只等待直接子进程。组长收到 SIGTERM 退出后便返回，不再确认整组已退出。复现中组长退出、忽略 SIGTERM 的孙进程仍存活。manager 对终态直接把 child_alive/process_alive 设为 False，掩盖实际存活进程；后续 cancel 又因 already_terminal 直接返回。

建议：组长 reap 与进程组清理分开，宽限期后仍对存活组发 SIGKILL；终态不能代替存活核查。持久化 PID/PGID 还需要进程出生身份，避免服务重启后 PID 复用误杀其他进程。

### R6 / P1 — rename 后目录 fsync 失败造成“报失败但内容已改变”

位置：`src/mcp4chatgpt/workspace/transactions.py:101-104`；底层 `workspace/recovery.py:69-96`。

`replaced=True` 在整个 atomic_replace_bytes 返回后才设置，而该函数先 os.replace，再 fsync_directory。仅对目标目录 fsync 注入异常，操作抛错、journal 记 aborted/rolled_back=false，但磁盘文件已由 before 变成 after；不会进入回滚分支。

影响：违背 docs/13 的失败前回滚承诺，调用方可能重试已经发生的变更。

建议：显式区分替换前失败、替换后耐久性未知、真正回滚完成；让上层获知 rename 是否发生，不应仅依赖函数正常返回设置 replaced。

### R7 / P2 — 并发 OAuth 注册会丢失已返回成功的客户端

位置：`src/mcp4chatgpt/oauth.py:119-127` 与 `_save_clients`。

注册对共享 JSON 执行未加锁的 read-modify-write，HTTP 服务允许并发。受控两个线程同时读取原状态后注册，两个调用都成功，但最终只保留一个客户端。直接 write_text 还允许读取方遇到中间状态并隔离该存储文件。

建议：用统一锁覆盖读改写，并使用临时文件原子替换；测试并发注册与同时授权读取。

## 架构判断

1. `ToolRegistry -> ops -> backend/store` 分层清楚；CUA 窗口身份由 native 供给、动作由 CUA 执行的设计有实际验收支撑。无需整体重写。
2. 同样的可靠性要求在各模块中实现不一致：native transport 有有界字节 I/O，而 CUA 使用阻塞文本 I/O；文件事务有恢复机制，而 PDF 直接 doc.save；OAuth JSON 没有采用现成的锁/原子写入模式。优先统一基础原语与故障契约。
3. PDF 输出路径先 exists 检查、再直接保存，没有原子独占发布。并发请求可能选中同名目的文件；“拒绝覆盖”目前不是并发保证，应补实际竞态测试。
4. `workspace/worktrees.py`、`worktree_store.py` 已存在，但没有公开 tools/local_ops 接入，也没有专门测试。应标记为内部实验模块；不要纳入对外完成声明。
5. 启动参数跨执行器传递尚不一致：tmux 分支不显式传入 MCP_* 环境，已有 tmux server 未必继承本次启动 profile；launchd 使用固定 plist，不继承调用者导出的 full-access 参数。应分别对 tmux/nohup/launchd 验证实际 server_info，而不只检查 health。这里为代码审查风险，本轮未重启执行器复现。
6. 当前是可信单用户本机服务。allowed_roots 约束文件工具及命令 cwd，不是 shell 文件系统沙箱；浏览器研究的内网访问默认开放也是 README 明确的产品选择，不应误写成多租户隔离保证。

## 文档审核

- README Quick Start 仍推荐 unittest discovery；大量 pytest 函数测试不会被它执行。应统一使用 `scripts/test.sh` / 项目解释器 pytest，并说明原生测试额外开关。
- `docs/05-architecture-and-logic.md` 被 README 指为全架构入口，但拓扑缺少 extension、downstream、computer、jobs、workspace 和新增 PDF，MCP 方法列表也未反映 resources/read 等现有功能。应重写为当前总览，旧设计标为历史。
- macos-computer-use.md 的低风险自动审批保证与实现不符（R4）；“currently 207 tests”也已过时。验收数字应绑定日期/commit，避免长期使用 currently。
- docs/14 的“未提交基线”是 9 月 23 日历史现场，现在 HEAD 已为 000b7f3；保留历史可，但须明显标注时间，避免当成当前状态。
- README 缺少清晰的 local/off 与显式 full-access 操作对照，且入口脚本会在 dotenv 读取前预设环境值；需要说明优先级和各执行器传参方式。
- docs/09、10 为两个项目的 handoff，docs/11 是历史 fault review；应增加文档索引，区分计划、已实现设计、验收记录与未解决问题。

## 修复顺序与验收门槛

1. 暂缓发布 PDF 写操作，修复 R1/R2 并增加真实合成 PDF 测试。
2. 修复 R3/R4 的传输与审批契约。
3. 修复 R5/R6 的任务取消与文件提交状态，补故障注入回归。
4. 修复 R7，再核验各启动执行器的实际配置。
5. 更新架构入口和验收矩阵，按模块整理提交。

既有 228 项通过是正常路径与已有回归覆盖的证据，不能覆盖本次新增复现。修复后应把以上复现纳入自动化，再执行必要的真实 connector/GUI 验收。

> 本文为修复前审核现场。R1–R7 的最终状态、后续验证和限制以 [2026-09-26 修复记录](16-reliability-fix-record-2026-09-26.md) 为准。
