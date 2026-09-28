# 独立 operations store 原型

日期：2026-09-28。状态：离线原型已实现；尚未接入正式工具或迁移数据。

## 基线与隔离

- 开发基线：`b877f4dfb073ae596d4536304f4f10b8443d6c85`；开始前主工作区干净。
- 分支：`codex/operations-store`。
- 受管理 worktree：`/Users/vickers/.codex/worktrees/operations-store/MCP4ChatGPT`。
- 基线可由 Git 提交恢复；复用原项目 `.venv/bin/python` 运行独立工作区的测试。
- 本轮未重启服务、切换 exposure、修改权限或写入正式 data。

## SQLite 版本门槛

实际 Python 绑定 SQLite 为 **3.50.4**。官方披露 WAL-reset 数据损坏缺陷影响
3.7.0–3.51.2；修复版本包括 3.51.3，以及回补版本 3.44.6、3.50.7。
参见 [SQLite WAL 文档](https://www.sqlite.org/wal.html) 和
[3.51.3 发布说明](https://www.sqlite.org/releaselog/3_51_3.html)。

本原型固定 DELETE rollback journal，写连接使用 `synchronous=FULL`、
`fullfsync=ON` 和 5 秒锁等待；拒绝非 DELETE 库，不升级本机依赖。
此次验证限于本地文件系统和进程退出，不代表已证明断电或硬件故障安全。

## 实现范围

代码：`src/mcp4chatgpt/operations/store.py`。

- `(installation_id, principal_id, workspace_id, operation_id)` 构成唯一身份。
- 请求指纹包含 kind、规范化参数、契约版本、目标身份、预期版本、配置版本。
  只存指纹，不保存原始请求参数；规范化拒绝 NaN、非字符串键及非 JSON 对象。
- 同身份同指纹返回当前记录并设置 `replayed=true`；不同 kind 或指纹冲突。
- `BEGIN IMMEDIATE` 串行化登记与 revision CAS；操作记录与对应事件一起提交。
- 实现文档 29 的状态转换图和基础结果字段。成功、补偿完成要求效果类型及证据引用。
- `get`、`events` 使用只读连接，不创建库或推动业务状态。
- `create` 只创建新库，拒绝覆盖；schema version 不匹配则拒绝访问。
- `recover_database` 显式触发 SQLite 自身的 hot-journal 恢复并检查完整性。
  它不核对业务效果、不运行工具、不重新执行操作。崩溃后如存在 hot journal，
  只读访问可能报错，需在启动维护阶段先显式恢复数据库。

## 保证边界

这是操作记录层；state 的 CAS **不等于资源锁或旧 worker 的写入隔离**。
执行者必须自行验证进程和资源身份、前后条件及证据真实性。
记录层不校验证据所指向的工件是否存在，调用者需对结果与证据做脱敏。

尚未实现 attempts、资源 generation、工件生命周期、业务恢复执行器、备份接口、
旧 JSON store 迁移或工具适配。补偿状态仅表示状态图支持；实际补偿仍需独立操作身份。
首次建库中断可能留下未初始化文件，应通过明确的维护流程处理；不会自动覆盖。
不得把此模块直接接入正式文件/Job 调用后声称具备端到端事务保证。

## 验证

`tests/test_operations_store.py` 验证：

- 幂等与作用域隔离、指纹覆盖、非法转换、过期 revision、终态重放。
- 线程并发登记/CAS、8 个独立进程竞争同一操作身份。
- 事件写入异常导致整个登记/状态事务回滚。
- 子进程在登记未提交、登记后、状态未提交、prepared、running、文件替换后、
  结果提交后直接退出。未提交场景强制缓存 spill，以覆盖 hot-journal 恢复。
- 文件替换使用临时目录的真实 `atomic_replace_bytes`，结果记录之前退出时保留
  `running/unknown`。这是协议测试 fixture，尚不是生产文件执行器集成。
- 崩溃恢复后数据库完整性、事件与状态一致、重复提交不重放文件写入。

第一次联合回归：原型 14 项 + 文档 29 的原有回归 41 项，**55 passed**，
12.58 秒，5 条既有 SWIG 弃用警告。随后增加独立进程竞争测试，单独重跑原型测试：**15 passed in 1.02s**。

## 下一步

1. 为文件执行器保存稳定目标身份、before/after、备份和执行 attempt。
2. 实现显式核对：before、after、第三种内容和身份变化；观察接口保持只读。
3. 通过崩溃与外部修改测试后，设计文件工具的可选接入，再接 Jobs。
4. ChatGPT 18 次 full/compact 手工 pilot 仍待执行；本轮测试不计入模型评测。
