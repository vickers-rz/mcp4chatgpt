# 2026-09-26 可靠性修复与验收记录

> 后续独立验收发现四项未覆盖问题，整体未通过。当前验收结论以 [17 号验收报告](17-acceptance-2026-09-26.md) 为准；本文保留实施阶段的历史记录。

本记录对应 [项目审核报告](15-project-review-2026-09-26.md) 的 R1–R7，覆盖后续实现与验收。每批独立提交：

| 批次 | 提交 | 内容 |
|---|---|---|
| 1 | `a6edf63`, `683587e`, `439129a` | 文件事务 rename 后错误识别与回滚；PDF 字面大小写/Unicode 匹配、替换布局和字体检查、临时文件验证及原子不覆盖发布；补齐失败注入与重复匹配测试 |
| 2 | `4fdeb7b`, `3a39841` | CUA 非阻塞字节传输、请求截止时间、连接清理；任务进程组完整终止和 PID 启动身份校验；补充 deadline、残留后代和身份不匹配测试 |
| 3 | `26c4484` | OAuth 客户端读改写跨线程/进程加锁及 fsync+原子替换 |

## 问题状态

- **R1/R2 PDF**：已修复。跨行查询明确拒绝；replacement 仍使用原字号，不能容纳或 Helvetica 缺少字符时失败且不发布输出。匹配依赖 PDF 可提取的字符位置，扫描图片与复杂阅读顺序不支持。
- **R3 CUA**：已修复阻塞 `readline`、半消息和缓冲合并问题；发送/读取与串行锁使用一个总截止时间。部分请求字节已发送后，副作用结果保持 `outcome_unknown`，不会回放到 native。协议错误会关闭 helper 并清空缓存。
- **R4 审批策略**：已按用户选择保留对受信任 CUA transport 的授权委托。只接受预期操作工具名，并使用已校验应用 allowlist；不独立验证上游 risk level 或 connector 身份。说明已同步到 macOS 计算机使用文档。
- **R5 任务进程**：已对 SIGTERM 后仍存活的进程组发 SIGKILL；普通退出也清理其后台后代。进程身份不匹配时不会仅凭旧 PID 发信号，而返回 unknown。清理失败写明错误，并保持非成功终态。
- **R6 文件事务**：底层替换错误携带 rename 是否发生；rename 后失败尝试回滚。回滚或 aborted 日志失败返回带事务 ID 的 `outcome_unknown`。提交日志失败仍保持已提交结果并附警告。
- **R7 OAuth**：客户端文件读取/修改/保存在共享锁内完成，写入采用同目录临时文件、文件 fsync、原子替换和目录 fsync；格式与响应不变。

## 验收结果

- 完整测试：**245 passed，4 skipped**，命令 `.venv/bin/python -m pytest -q`，2026-09-26。
- 专用 macOS 原生 GUI fixture：`MCP_NATIVE_GUI_TESTS=1 .venv/bin/python -m pytest -q tests/test_computer_native.py`，**4 passed**。测试会创建并清理自己的 fixture app 窗口。
- tmux 独立 socket 验证通过：用 `new-session -e` 启动的新会话收到本次 profile 的 `MCP_COMPUTER_MODE` 和 listener 配置。脚本已显式传入 listener/tunnel/computer 相关变量。
- `git diff --check` 和 Python 编译检查通过。PyMuPDF 的 bundled SWIG 类型仍会发出第三方弃用警告。
- 默认跳过项属于 GUI 验收；本次已另外显式运行并通过。没有对正在使用的 Chrome/ChatGPT 窗口执行真实 CUA 输入；CUA 的管道故障与审批路径用受控子进程和单元测试验证。

## 仍需留意

- `ps` 启动时间和命令用于恢复后 PID 身份核验；无法读取身份时任务标记为 unknown，需人工检查，不会尝试猜测并杀进程。
- PDF 输出发布使用同目录硬链接实现“不覆盖”原子提交；若文件系统不支持硬链接，操作会失败且源文件保留。
- 部署服务未自动重启。代码在本地通过测试，正式服务切换仍需后续显式执行。
