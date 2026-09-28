# 基线固定、手工模型对照与事务基础设计

日期：2026-09-28。状态：基线和本地测量完成；ChatGPT 手工配对评测待执行；事务契约已制定、尚未实现。

## 1. 本轮已完成

- 保存 HEAD、工作区状态、169 个源文件内容哈希、Python/SQLite 版本：`benchmarks/runs/2026-09-28-foundation/baseline.json`。
- 这是可核对指纹，不是 Git 提交或可恢复备份。原有未提交修改未提交、未覆盖。后续开发前必须创建可恢复的隔离基线；指纹不代表正式运行进程加载了相同代码。
- 本地 SQLite 为 3.50.4。尚未完成版本缺陷审查；在版本兼容性通过前，不启用新的 WAL 状态库。
- 本地只读 benchmark：8 组测量、5 个失败契约场景通过。固定检索集 weighted Top-1 为 5/5，旧策略为 2/5；样本很小，不能推广成真实模型成功率。
- 通过真实 4GPT 读取服务状态：personal_full_access=true；GUI interact/auto/*；compact；159 capabilities、4 个顶层工具。
- 本轮观察到 catalog_version：`5e0c79d927e881b7e7e30d56e30b761b88229f3e33a77af407d0794b664e4212`。后续每轮必须重新记录，不能把这个值写死为验收条件。
- 通过 4GPT capability_get → capability_call 成功读取手工测试 fixture。此为连接 smoke，不是干净上下文的 ChatGPT full/compact 对照，也不计入模型评测样本。
- 未切换正式服务 exposure、未重启、未改变权限、未实现通用 Code Mode。

本地结果：`benchmarks/runs/2026-09-28-foundation/read_only_v2.json`、`discovery_search.json`。其中模型 tokens/往返是空值或投影，不能当作真实模型数据。

本轮回归：`test_benchmarks_v2`、`test_discovery_regressions`、`test_model_e2e_records`、`test_local_file_transactions`、`test_local_jobs`、`test_local_job_tools` 合计 **41 passed in 10.72s**。出现 5 条 SWIG 类型弃用警告；无失败。JSON、文档代码块与 git diff --check 校验通过。结束时复核基线，已捕获源文件中仅 docs/README.md 被本轮更新。

## 2. 用户选择的真实评测渠道

使用 ChatGPT/4GPT 手工对照，只记录能取得的指标。当前 Codex 对话的上下文、工具包装和模型配置不能冒充 ChatGPT 新会话。

现有 `benchmarks/model_e2e.py` 要求精确模型标识与完整 token 等 telemetry。本轮保留其严格契约，不向其中填入猜测或 0；手工结果使用独立 `manual_model_observation` 记录，模板位于 `benchmarks/runs/2026-09-28-foundation/manual-record.template.json`。

### 2.1 首轮实验设计

- 三道题，每种 exposure 做三次，共 18 次新会话。
- trial 1：full → compact；trial 2：compact → full；trial 3：full → compact。
- 每个 exposure 区块依次跑三道题，但每题新建干净会话。不要把答案、报告、历史评测记录放进任务会话。
- 固定 ChatGPT UI 中选择的模型、4GPT 授权、personal_full_access profile、fixture 与能力集合；隐藏后端版本不明时注明，只声称相同可见配置。
- full/compact 指工具暴露方式；两组都保持 personal_full_access，不要将它与权限 full 混淆。
- 记录实际客户端是否自动搜索或省略工具；服务端 full 不代表完整 schema 一定全部进入模型上下文。
- 先运行已上线 compact 的试题验证流程，正式配对数据按上面的交替顺序开始。

### 2.2 切换步骤（尚未执行）

只有在其他任务没有使用正式实例的窗口中切换；切换涉及短暂重启，不能与其他对话部署同时进行。

```sh
# full exposure，保留 personal_full_access、interact 等设置
MCP_FULL_TOOL_EXPOSURE=full ./MCP4ChatGPT.command restart-full

# compact exposure；实验结束后恢复此配置
MCP_FULL_TOOL_EXPOSURE=compact ./MCP4ChatGPT.command restart-full
```

这是已核对启动脚本后的配置变量。不要使用 restart-public 来做这组比较，以免权限 profile 一并改变。每次切换后刷新客户端工具目录，在新会话核对 server_info 与实际暴露工具，并记录 catalog/工具清单身份；如果能力集合发生实质变化，这一对配对作废重跑。

## 3. 可直接复制的题目

### A：单文件短请求

> 仅使用 4GPT 的只读文件能力，读取 `/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT/benchmarks/fixtures/manual_discovery_v1/one.json`，返回 project、revision、status 三个字段的 JSON。不要使用 shell、执行代码或修改文件。

### B：两个文件聚合

> 仅使用 4GPT 的只读文件能力，读取 `/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT/benchmarks/fixtures/manual_discovery_v1/a.json` 与同目录 `b.json`。计算 items 总数、score 总和，以及 score 降序、同分按 id 升序排列的前三个 id。只返回含 count、score_total、top3 的 JSON。不要使用 shell、执行代码或修改文件。

### C：缺失文件与完整性

> 仅使用 4GPT 的只读文件能力，分别读取 `/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT/benchmarks/fixtures/manual_discovery_v1/a.json`、同目录 `b.json` 和 `missing.json`。返回 requested、completed、missing 三个字段；missing 为无法读取的文件名数组。不要创建缺失文件，不要用 shell 或执行代码。

标准答案由评测人员查看 `benchmarks/runs/2026-09-28-foundation/manual-truth.json`，不传入任务会话。该文件也保存 fixture 哈希。不要将这三题当作原 `model_e2e_tasks.json` 中 PDF 和分页任务已经执行；它们是第一轮文件能力 pilot。

## 4. 数据与决策

每次保存会话链接、可见模型名、开始/结束时间、工具调用轨迹、正确性、完整性、发现调用数、失败/修复次数。无法观察的 token、字节、后端模型版本填 null 并说明原因。不可用 schema 字节推算实际 token。

从用户发送任务到最终回答统计墙钟耗时；人工暂停、工具审批等待另记。只统计可见调用，不声称可见轨迹等于客户端全部内部调用。

先按题型报告两组成功数、耗时中位数和范围、发现与修复次数。18 次小样本仅用于发现明显回归，不做显著性或全局成本优势结论。失败和客户端拦截均保留，不删除坏样本。

进入下一阶段的门槛：正确性和完整性无可复现下降；能解释短任务的额外发现开销；实测是否值得扩大到 PDF/分页。只读 pilot 完成前，不根据本地聚合投影启动 Code Mode 实现。

## 5. 事务基础契约 v0.1

这是下一批实现的约束，不是新工具已上线。

### 5.1 操作身份

唯一键：`(installation_id, principal_id, workspace_id, operation_id)`。

指纹：规范化工具参数、工具契约版本、目标稳定身份、预期资源版本、影响执行的配置版本。相同唯一键同指纹查询既有结果；不同指纹返回 idempotency_conflict。超时不会创建新 ID。

结果字段：`operation_id, state, effect, replayed, retry_policy, evidence_refs, recovery_action, revision`。对观察不到的信息返回 unknown/null，不以 false 代替。

### 5.2 状态迁移

| 当前状态 | 允许后继 | 条件 |
|---|---|---|
| accepted | prepared / failed | 先持久化身份和指纹；准备失败无外部效果 |
| prepared | running / cancelled / needs_reconciliation | 执行者身份与资源占用已核验 |
| running | verifying / needs_reconciliation / failed / cancelled | failed/cancelled 也可能有部分效果 |
| verifying | succeeded / failed / needs_reconciliation | 结果证据支持结论 |
| needs_reconciliation | verifying / failed / compensated | 专用恢复执行器核对；不盲重放 |
| succeeded / failed / cancelled | compensating | 仅支持补偿且前置条件满足时 |
| compensating | compensated / compensation_failed / needs_reconciliation | 补偿也是有身份的独立动作 |

观察接口不推进状态。重复提交允许返回当前状态，但不触发恢复。状态变更使用 revision CAS；心跳到期不证明旧 worker 已无法写入。

### 5.3 文件执行器首批契约

- 第一批只接管现有普通文件 write/patch，继续复用验证、备份和 atomic_replace。
- patch 严格模式要求 expected_sha256；整文件替换继续要求完整读取证明。
- 持久化 before/after 哈希、路径身份、备份定位、提交阶段。
- 崩溃后目标为 before/after/第三种内容分别核对；第三种内容禁止覆盖。
- 数据库提交与文件 rename 不组成共同原子事务；用恢复协议明确这段窗口。
- 回滚前再次比对当前内容和身份，不能覆盖外部新修改。

### 5.4 命令执行器首批契约

- 短命令接入现有 Job 执行器，前台等待到预算后返回任务身份。
- 保留预留后不盲目再 spawn 的原则；建立 attempt 与 supervisor 握手证据。
- 取消只表示阻止继续运行；已有外部效果不自动撤销。
- scope 内幂等不等于任意 shell exactly-once；不确定启动状态允许牺牲自动重试。

### 5.5 存储与迁移门槛

候选 SQLite store 管理 operations、attempts、events、artifact refs；工件继续独立文件。正式编码前验证本机数据库版本和崩溃行为，不在当前 3.50.4 上直接假定 WAL 配置可用。

旧 JSON store 保留只读兼容；切换必须协调仍运行的 supervisor。第一次迭代先用临时数据库故障测试，不迁移正式 data，不改变服务。

## 6. 下一批具体工作

1. 审查 baseline 与当前 diff，建立保留未提交改动的可恢复开发基线。
2. 实现独立 operations store 原型与状态迁移测试，不接生产流量。
3. 注入登记、执行领取、文件替换、结果记账之间的崩溃；验证观察无副作用。
4. 接文件工具后再接 Jobs；GUI 去重与后置核验随后接入。
5. 在 ChatGPT 新会话完成本文件手工 pilot，汇总实际记录后决定扩大评测。

Code Mode、全局 GUI 补偿和生产迁移均不在本轮完成范围。
