# 文件执行器与显式恢复核对

日期：2026-09-28。状态：独立文件执行器及故障测试已实现，尚未接入 MCP 工具或正式数据。

## 基线与范围

沿用 `codex/operations-store` 独立工作区，起始提交 `71cf310`。
本轮实现 `operations/files.py`，复用文档 30 的 SQLite ledger、现有候选校验、
完整读取 token、文件锁及恢复工件的原子保存函数。

支持现有普通文本文件的整文件替换和首个精确锚点 patch。两者必须提供
`expected_sha256`；整文件替换还要求完整读取 token。保留 UTF-8、Python AST、
CUA 结构及异常缩减校验。候选与原文件上限各 16 MiB。

拒绝符号链接路径、硬链接、非普通文件、非当前用户所有的文件和特殊权限位。
目标必须位于声明的 workspace 内，且不能位于 state_dir 内；ledger 必须位于
同一 state_dir，确保执行者共用锁。创建、删除、重命名、二进制及 ACL/xattr
保留尚不支持；当前只保留 Unix mode、uid、gid，不适用于依赖扩展元数据的文件。

## 执行顺序与持久化证据

1. 获得作用域操作锁，登记参数指纹。同 ID 重复提交只返回既有记录；运行期间也可查询。
   若执行者已取得锁但尚未登记，则返回忙，不代替执行者预留 ID。
2. 非阻塞获取既有 workspace 文件锁，读取并校验完整 before 内容及身份。
3. 保存 before、after 镜像；在目标目录准备候选文件并 fsync。
4. 保存 `prepared.json`，包含操作身份、attempt UUID、worker PID、路径、父目录
   dev/inode、前后镜像摘要、文件身份及校验结果。数据库保存清单哈希和 attempt。
5. 持久化 prepared、running；替换前再次核验父目录、目标和候选身份。
6. 使用持有的目录描述符执行同目录 `os.replace`，随后目录 fsync。
7. 重新读取目标，保存 `committed.json`，绑定操作、attempt、父目录和替换后的
   文件签名。最后经 verifying 和证据核对进入 succeeded。

文件签名包括 dev、inode、size、mtime_ns、ctime_ns、mode、uid、gid、nlink、SHA-256。
数据库与文件替换依然是两个提交点，不能宣称共同原子事务。

工件位于 `state_dir/operation-files/<作用域身份摘要>/`，内容镜像权限为 0600。
不记录原始请求参数。PID 用于诊断；操作 UUID 和持续持有的 flock 才是本轮
执行协调机制，不根据 PID 或心跳到期判断旧执行者已退出。

## 显式恢复核对

观察仍由 ledger 的 `get`、`events` 完成；它们不会调用文件执行器。
`FileExecutor.reconcile(key)` 是独立的、显式的业务核对动作。
SQLite hot-journal 恢复仍通过 `recover_database()` 在维护阶段显式执行。

| 核对结果 | 状态与动作 |
|---|---|
| accepted 且执行者锁已释放 | failed/none：准备中断，未进入文件替换阶段 |
| before 内容及完整身份未变，且无提交凭据 | failed/none：未应用，不自动重试 |
| after 内容、身份和提交凭据匹配 | succeeded/committed：补全元数据，不重复写文件 |
| after 匹配但没有提交凭据 | needs_reconciliation/unknown：保留不确定性 |
| 第三种内容、身份变化、清单/镜像损坏、凭据不符或缺失 | needs_reconciliation/unknown：保留工件供核查 |
| 执行者或文件资源仍被锁定 | 报锁冲突，保持原操作状态 |
| 已处于终态 | 返回原结果，不将后来的文件修改误认为旧操作失败 |

恢复核对不写目标、不自动回滚、不重新执行 patch。正常执行中，替换后的凭据或
数据库写入失败也不会返回“未发生效果”。若数据库本身不可用，调用可以报错；
调用者应查询同一 operation_id，而不能创建新 ID 盲目重试。

## 并发和路径边界

- 操作锁持有到最终结果；执行与恢复共用原文件事务锁，使用同一 state_dir 和规范路径。
- 给既有 `file_transaction_lock` 增加可选 `blocking=False`；原调用默认行为保持兼容。
- 父目录逐级以 no-follow 打开，文件读前后比对签名，rename 使用目录 fd。
- 替换前及结果核验时重查路径对应的父目录，发现父目录换位则保留不确定结果。
- flock 仅协调遵守协议的执行者；外部程序仍可能在最后校验与 rename 之间修改目标。
  此原型不提供对非协作写入者的内核级条件替换，也不保证 hostile 同用户对状态目录的隔离。
- 进程崩溃可能遗留 `.mcp4-operation-*` 候选文件。恢复保留它们和镜像，尚未实现
  自动清理、保留期限、备份接口或存储配额；不得直接在正式工作目录批量使用。

## 验证

执行：

```sh
/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT/.venv/bin/python -m pytest -q \
  tests/test_operation_files.py tests/test_operations_store.py \
  tests/test_local_file_transactions.py tests/test_local_jobs.py \
  tests/test_local_job_tools.py tests/test_benchmarks_v2.py \
  tests/test_discovery_regressions.py tests/test_model_e2e_records.py
```

结果：**90 passed in 13.95s**，5 条既有 SWIG 弃用警告。

覆盖正常 write/patch、去重与指纹冲突、读取证明、版本冲突、类型限制、真实子进程
在 accepted/prepared/running/replace/receipt/verifying 阶段退出；以及同内容换 inode、
原地修改后恢复相同内容、镜像与清单损坏、父目录换位、活跃 worker、旧文件锁竞争、
替换后的凭据和数据库状态写入故障。使用临时目录；不是断电或实际生产部署验收。

## 下一步

- 在生产接入前补齐工件保留/清理及扩展元数据支持边界，审查路径竞争窗口。
- 设计文件工具的可选严格模式、操作状态与显式恢复接口、错误契约和能力声明。
- Jobs 接入及 ChatGPT full/compact 手工 pilot 仍待开展。

本轮未注册新 MCP 工具、迁移正式数据、重启服务或切换 exposure。
