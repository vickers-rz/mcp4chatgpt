# mcpc 与工具发现 Plan：代码复核与重新验收

> 本文保留修复前证据。后续修复及最新验证见 [修复记录](24-discovery-fixes-2026-09-27.md)。

日期：2026-09-27（Asia/Shanghai）。基线：`084ccec`，加工作区已有的 `tools.py`、`server.py` 和 `test_capabilities.py` 未提交改动。

## 结论与范围

当前工作区不能通过整份 Plan 的验收。正常 mcpc full/compact 流程通过，工具列表体积达到目标，但搜索契约、schema 校验、目录版本和审计仍有确定问题。mcpc 测试的异常清理与部分断言也不足以支持文档中的完整验收声明。

本次检查工具注册和调用、schema resource、full/compact、下游目录、HTTP 审计、mcpc 测试入口、依赖锁定和第三阶段设计。此前验收过的 PDF、文件事务、OAuth、任务、GUI 内部实现不属于本次代码复核范围。完整 pytest 作为回归执行；GUI 实测没有重新开启。

本次未修改业务源码、现有在途改动或正式服务。新增此报告和文档索引。

需要纠正上一轮结论中的一处表述：原 Plan 明确规定启动时固定目录、不增加热刷新或变更通知。`listChanged=false` 本身符合原计划，不能单独列为缺陷。当前新增的 `refresh_catalog()` 是范围扩展；若要声明支持真实下游运行期变化，仍缺少下述完整链路。目录按需刷新也不必然要求增加通知流，客户端缓存行为应单独约定。

## 已确认问题

### R1 / P2：运行期刷新目前只同步管理器缓存

位置：`tools.py:1121`、`downstream/manager.py:190`、`downstream/client.py:346` 与 `_dispatch_message()`。

Registry 的刷新读取 `manager.get_tools()`，后者只返回 `_tools`。真实 stdio 客户端只在启动时发出 `tools/list`；下游通知只记日志，没有更新客户端和管理器目录的路径。因此下游运行期间新增、删除或修改工具时，Registry 再次读取的仍是旧缓存。

现有新增测试直接替换 `FakeDownstream.tools`，证明的是管理器缓存变化后 Registry 能感知，不是完整链路。此项属于新增运行期能力未完成，并非原始静态目录计划的遗漏。

建议：先明确支持的变更来源。若支持真实下游变化，需要有界地重新发现、应用 allow/deny、原子替换管理器快照，再发布 Registry 快照。用独立假 stdio 服务在运行中改变工具表验证，不仅修改 Python 假对象。客户端通知是否实现另行约定，未实现时继续如实声明 false。

### R2 / P2：schema 已改变，版本哈希却不改变

位置：`tools.py:1082`、`tools.py:1146`。

注册工具直接持有下游 `input_schema` 和 `output_schema` 的可变对象。对同一个 schema 原地修改后，旧 Tool 与新 Tool 引用相同内容，比较认为没有变化，跳过重算版本。

临时目录复现结果：给 `docs__lookup` 的 schema 增加 `new_field` 后，get 已返回新字段，但 `refresh_catalog()` 返回 false，`catalog_version` 和 `toolset_hash` 都维持旧值。这违反“仅 schema 变化也必须改变版本”的要求。

建议：在目录边界深复制定义，以独立、不可变的快照和摘要比较变化；同时测试 input/output schema 与嵌套字段的原地变更。

### R3 / P2：tools/list 审计与实际响应不是同一快照

位置：`server.py:501`、`tools.py:1298`。

HTTP 先刷新并记录数量/哈希，随后 `list_tools()` 再刷新。如果管理器在两次读取之间变化，审计记录与返回列表不一致。前面补一次刷新不能消除这个窗口。

已用隔离 HTTP 实例和受控管理器复现：审计 `tool_count=77`，实际返回 76 个工具；记录的哈希与响应对应的新目录哈希不一致。

建议：一次获取列表及其 metadata，响应与审计共用该快照。不要靠分别加锁的多次读取拼接一次请求。

### R4 / P2：搜索行为偏离原计划并导致回归失败

位置：`tools.py:1176`、`tests/test_capabilities.py:69`。

原计划要求按空白拆词、全部词项命中名称/描述/source、按精确名称和名称命中排序。在途改动增加标点拆词、schema 字段和部分词项匹配。搜索 `PDF INSPECT`、limit=1 时，不完整匹配也进入候选，返回 `truncated=true`，现有测试失败。

工作区的 `.ai-bridge/current-plan.md` 又要求部分词项与 schema 字段发现，说明两份契约存在冲突。文件中的要求不自动替代用户指定的原 Plan；不能仅修改失败断言就宣称原计划通过。

建议：按原契约恢复搜索，或在明确采用新契约后同步工具说明、文档和测试；将此变更与运行期目录修复分开。

### R5 / P2：未知 JSON Schema 方言被静默接受

位置：`tools.py:1279`。

`validator_for(schema, default=Draft202012Validator)` 同时用于缺少 `$schema` 和无法识别 `$schema` 的情况。受控 schema 指定 `https://example.invalid/unsupported-dialect` 后，校验仍成功。无法确认其关键字语义时继续执行 handler，不能算遵循指定方言。

建议：仅在未声明方言时使用 Draft 2020-12；已声明但不支持的方言明确失败，并验证目标 handler 未执行。

### R6 / P2：引用检查误拒绝合法的普通字段

位置：`tools.py:1261`。

检查器递归扫描所有字典，把属性映射、examples/default 等数据里的 `$ref` 都当作 schema 引用。例如合法 schema `{"type":"object","properties":{"$ref":{"type":"string"}}}`，输入 `{"$ref":"literal"}`，会报 `capability_external_ref_unsupported`。

建议：只检查实际 schema 节点中的引用关键字，同时显式禁止外部资源检索。覆盖普通 `$ref` 属性、example 数据、内部引用和真实外部引用。

### R7 / P2：下游工具失败却记成功审计

位置：`tools.py` 的 `_invoke_tool()`。

下游返回的 MCP `isError=true` 被原样保留，这是正确的；但 `audit_ok` 默认 true，只有 computer 特殊分支会修改它。受控下游经 `capability_call` 返回 `isError=true` 时，最终目标审计仍为 `ok=true`。

建议：RawMCPToolResult 的 `isError` 应影响目标调用审计，同时保留原结果、真实 channel/client_id 和一次调用语义。这个问题原本也影响直接下游调用，新增通用入口继续继承了它。

### R8 / P2：mcpc 测试异常清理与凭据生命周期不完整

位置：`tests/test_mcpc_acceptance.py:63`、`:169`、`:175`。

以下为代码路径确认，尚未用故障注入逐项执行：

- 重启后创建的 `session + "-restart"` 只在正常分支关闭；连接后断言失败时 finally 只关闭原 session，可能留下新 bridge。
- 无效 bearer 使用独立 home，但 finally 没有针对该 home 的清理核验。
- bearer 配置先普通写入，再 chmod 0600；不是创建时即限制权限。
- `mcpc.json` 含 bearer，测试结束没有主动删除，pytest 临时目录会保留。`make_config()` 使用固定 `test-secret`，也没有按 Plan 为每轮生成随机服务密钥。
- finally 检查 session 文件和 socket 路径，不等于已确认所有 bridge 进程退出。

建议：从服务启动起进入统一资源清理范围，追踪每个 home/session/process；finally 清理全部已创建对象和临时凭据。凭据文件创建时设为 0600，服务密钥每轮随机生成，并补连接后、重连后和 CLI 超时的故障注入。

## 测试覆盖与文档差距

- mcpc 工具表仅断言部分名称存在及没有重复，没有与 registry 的完整定义逐项比较；单工具 schema 只检查 name；schema resources 也只抽查 server_info。正常流程 2 passed 不能证明完整一致性。
- 两个版本测试没有完整验证实际发布版本。其中 `test_catalog_hash_tracks_schema_and_ignores_object_key_order` 把原 `catalog_version` 与另一种结构的 `_definition_hash()` 比较，不能证明 schema 更新触发 catalog_version 更新。
- 缺少真实运行期下游变更、并发刷新与调用、上游缓存行为的端到端覆盖。
- 第三阶段设计符合只设计、不实现执行器的范围，包含维护者只读名单、隔离、资源限制、取消、部分结果和两个试点。未发现需要在本轮部署运行时的理由。
- OAuth 浏览器登录及正式 ChatGPT connector 验收仍未执行，不从临时 bearer 测试推导通过。

## 本次从头运行的验证

| 验证 | 命令/方法 | 实际结果 |
|---|---|---|
| 范围内回归 | `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capabilities.py tests/test_server.py tests/test_downstream.py` | 69 passed、1 failed，11.25 秒 |
| 外部 mcpc | `MCP_MCPC_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mcpc_acceptance.py` | full/compact：2 passed，10.72 秒 |
| 全项目回归 | `PYTHONPATH=src .venv/bin/python -m pytest -q` | 266 passed、1 failed、7 skipped，25.32 秒 |
| R2/R5/R6/R7 | 临时目录、现有假管理器及受控返回值 | 均复现，未触及正式服务 |
| R3 | 独立随机端口 HTTP 实例 | 77/76 数量错位及哈希不一致复现 |

所有 pytest 失败均指向 R4。7 个 skip 是 5 个显式 GUI 项和 2 个默认关闭的 mcpc 项；mcpc 已单独开启。PyMuPDF 的 5 条 deprecation warning 仍存在，未纳入本轮修复范围。

同一序列化方式（sort_keys、紧凑分隔符、ensure_ascii=False）、无下游时重新测量：

| computer | full 工具数 / UTF-8 bytes | compact 工具数 / UTF-8 bytes | 减少 |
|---|---:|---:|---:|
| off | 76 / 49,568 | 4 / 2,100 | 95.76% |
| observe | 83 / 54,803 | 11 / 7,335 | 86.62% |
| interact | 93 / 63,337 | 21 / 15,869 | 74.95% |

## 修复顺序与复验要求

1. 先确定搜索仍遵循哪份契约；按原 Plan 验收时修复 R4。补 R5/R6 的校验边界和 R7 的错误审计。
2. 用目录快照解决 R2/R3，再覆盖并发读取及调用；不在持有目录锁时执行业务 handler。
3. 若本轮正式纳入运行期同步，实现真实下游重新发现并补端到端测试；否则撤回“已支持运行期同步”的完成声明，保留启动时固定目录。
4. 修复 mcpc 资源清理，增强 registry/schema/resource 精确比对及故障路径测试；重新运行表中的验收命令。
5. 自动化通过后再单独安排 OAuth/ChatGPT 人工验收和正式服务切换。

本报告对应的源码 SHA-256：

```text
tools.py: 8ce40568ec451ad4a8b72b12c5903a4990b3287140453cc6bf1eb17d1df9c07e
server.py: d7c1f01ef9b273b4b14cd8be412eb30d148743346caea2b6c02273305e4d4c96
test_capabilities.py: 88d7620caa15b8ec0d38e4daea3fe5b562efe7c8d4b09fc31113e5b8d5ffb569
test_mcpc_acceptance.py: 44aad5a9e47996c53698f6519bc87dff4e6106d6711563426a7a0ab685c4e27b
```
