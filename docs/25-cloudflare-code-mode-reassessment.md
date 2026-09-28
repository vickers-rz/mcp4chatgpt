# Cloudflare Code Mode 对本项目的借鉴复核

日期：2026-09-27。

## 结论

值得借鉴的是按需读取能力定义、在隔离执行器内组合调用、在结果进入模型前完成筛选与聚合。默认工具数量只是其中一个指标。现有 discovery 是可复用的基础，但 `capability_call` 每次只执行一个目标，尚未实现 Code Mode。

现在不应仅依据 Cloudflare 的 99.9% 数字就切换默认 exposure，或把浏览器 backend 合并成一个自动路由入口。应先补齐目录契约，再用项目工作流验证编排的收益。保留现有 `full/compact` 作为比较基线。

完整个人权限改造见 [26-personal-full-access-plan.md](26-personal-full-access-plan.md)。2026-09-28 用户明确恢复该计划，现已作为独立 personal full-access profile 实施；它与本文件的 P0–P3 / Code Mode 决策保持解耦，不作为建设通用 executor 的依据。

## P0–P3 当前进度

| 阶段 | 本轮起点 | 2026-09-27 本轮结果 |
|---|---|---|
| P0 | 固定 benchmark 已完成第一版 | **完成收尾**：新增 v2 指标契约和部分失败、downstream `isError`、缺少结构化结果、分页循环、目标版本变化场景，固定 workload 通过 |
| P1 | source、capability revision、backend instance 绑定已部分完成 | **仍为部分完成**：补齐 category、evidence-based availability、alias/deprecated/replacement、examples；资源/session identity 和真实模型 telemetry 仍未完成 |
| P2 | 未完成 | **完成本轮定义范围**：确定性字段加权检索、source/backend/category 过滤、固定查询集、`capability_list` 与版本/过滤绑定 cursor 已实现并回归 |
| P3 | 测量入口已就绪，仍未满足 executor 进入条件 | full/compact 真实 mcpc 协议往返已通过；真实模型 E2E task manifest、JSONL 记录校验与聚合入口已具备，但尚无真实模型运行记录，因此维持现有 exposure，不建设任意代码执行器，不合并浏览器 backend |


## 一手来源及数字口径

- [2026-02-20 发布文章](https://blog.cloudflare.com/code-mode-mcp/)：2,500+ endpoints，传统完整工具定义约 117 万 tokens，两个核心入口约 1,000 tokens。
- [当前官方仓库 README](https://github.com/cloudflare/mcp/blob/main/README.md)：完整 schema 为 1,170,523 tokens，仅必填参数为 244,047；加入 `docs` 后为三个工具、约 1,100 tokens。仓库还允许客户端已有 Code Mode 时关闭服务端 Code Mode。
- [当前 SDK API](https://developers.cloudflare.com/agents/tools/codemode/api-reference/)：提供 executor、RPC dispatcher、按需 TypeScript 定义及执行记录等接口。当前 SDK 能力不能全部追溯为二月首发能力。

### 两个官方仓库的关系

用户补充的 [cloudflare/mcp-server-cloudflare](https://github.com/cloudflare/mcp-server-cloudflare#which-cloudflare-mcp-server-should-you-use) 是另一个官方仓库。其 README 明确区分：本仓库维护面向具体产品领域的 MCP servers；Code Mode server 在 `cloudflare/mcp` 维护。前者包含 Observability、Workers Bindings、Browser Run 等专用工具服务，后者提供跨产品 API 的代码执行入口。

因此，117 万与约 1,000 tokens 的比较仍应引用 `cloudflare/mcp` 和发布文章；讨论 Cloudflare 的整体 MCP 架构时应同时引用两个仓库。Cloudflare 同时保留经过设计的领域工具和通用 Code Mode。这支持本项目按工作流保留直达工具、对批量数据任务试验编排的方向，不能据此主张所有能力最终都必须藏在 execute 后面。

这些数字衡量工具定义的上下文占用，不代表整项任务只消耗 1,000 tokens，也不证明总延迟、费用或错误率降低 99.9%。搜索返回、局部 schema、生成的程序、执行结果和错误修复仍有成本。不同模型的 tokenizer、消息封装与缓存也影响比较。

## 对照当前实现

| 层次 | 项目现状 | 需要借鉴的部分 |
|---|---|---|
| 定义发现 | search 已采用确定性字段加权并支持 source/backend/category 过滤；get 返回完整定义、source/backend/revision、category、availability、alias/deprecated/replacement/examples；list 提供紧凑分页目录 | 继续扩大真实查询集与客户端端到端证据，不把 inventory list 放入普通任务热路径 |
| 单次调用 | call 校验参数并调用原 handler | 继续作为 bridge 的统一入口，避免绕过校验和审计 |
| 程序编排 | 只有设计文档，没有隔离 executor | 循环、依赖调用、有限并发和局部聚合 |
| 结果 | 普通结果同时包装文本和 structuredContent，下游 MCP 内容保真 | executor 内部保留结构化数据，仅向模型返回摘要或资源引用 |
| 工具暴露 | full 或 compact，由进程配置选择 | 根据目标客户端验证 profile；工具数不是独立验收目标 |

前轮基线使用测试配置、无下游服务、无 OAuth 描述字段测量序列化 JSON（早于新增 expected_revision 参数）：

| computer mode | full 工具数 / UTF-8 bytes | compact 工具数 / UTF-8 bytes |
|---|---:|---:|
| off | 76 / 49,568 | 4 / 2,100 |
| observe | 83 / 54,803 | 11 / 7,335 |
| interact | 93 / 63,337 | 21 / 15,869 |

off 模式已有约 95.8% 的描述字节降幅。此环境未安装 tiktoken，以上不换算为 tokens。引用对话中的 142 个 capability 属于另一份运行期目录，本轮未重新连接该服务，不能当作本次测量样本。

上表保留的是改造前历史基线，其中 observe/interact 曾把 `computer_*` 作为 compact 顶层例外。2026-09-28 已移除该例外：当前正式实例在 Computer Use `interact` 下为 159 个 capability、4 个顶层工具，GUI 能力统一通过 discovery/call 按需进入上下文。

## 调整原先的优先级

### 1. 目录改进仍然有价值，但不要求模型先枚举全部能力

增加可靠的分页 list 主要服务于 inventory、测试与运维；普通任务继续按需 search/get。get 应返回 source；目录还应明确 backend、别名目标、deprecated 状态与可用性。分类使用显式元数据，不能把工具名的第一个前缀直接当作语义 category。

分页 cursor 应绑定目录版本和过滤条件；目录变化后明确要求重新开始，避免悄悄漏项或重复。list 不应成为每次对话必走的一步，否则又把完整目录搬回上下文。

### 2. 首个 Code Mode 试点聚焦只读批处理

沿用文档 21 的隔离 bridge 设计。选择一批 PDF 的元数据检查：普通调用逐个返回结果；编排调用在执行器内检查、归类并仅返回异常文件和统计。另用一个有分页与字段筛选的受控下游查询测试中间数据成本。

未来 executor 至少需要调用次数、并发、总时限、内存和结果大小限制，以及取消和部分失败报告。准入工具由服务端配置，不能只信任 downstream 的 readOnlyHint。凭据留在宿主，执行器通过 bridge 调用现有 registry。原始 MCP 的 isError、图片和资源链接需要明确适配，不能将所有返回值假定成普通 JSON。

项目现有 shell/job 或页面内 JavaScript 执行不等同于这种隔离编排运行时。Cloudflare Tunnel 仅解决连接路径，不能代替执行隔离。先比较本地与 Cloudflare 运行环境的适配成本，只对选中的方案做完整原型验收，无须把两个方案都实现完。

### 3. 暴露策略和 backend 合并由任务证据决定

对于主要依赖服务端能力的客户端，可以试验 discovery 加 execute 的小工具集；客户端已经具备编排能力时，应允许复用原生工具，避免层层套执行器。具体客户端是否受益须实际验证。

DevTools 与 Headless 名称和 schema 相似，并不表示同一 session、登录态、页面或副作用。先保留 backend 身份和显式选择；后续统一入口应绑定稳定的 session/target，不能在执行中静默切换浏览器。它们退出默认工具列表即可减少描述成本，合并实现可以单独评估。

GUI 的观察—操作循环和批量数据任务分别评估。短任务可能因发现和编排多出一次往返；高频直达工具是否应进入 bootstrap，应由真实任务频率和表现决定。

## 实施契约复核（2026-09-27）

以下约束补充并优先于后文的目标架构示意。各 plane 首先是同一进程内的模块职责，不要求拆成独立服务。ToolRegistry 保留统一门面，目录、策略、调度和结果适配应逐步拆为可独立测试的内部模块。

### 执行入口和身份

所有入口先解析 canonical tool，再校验输入、检查调用约束、执行 handler、审计结果。解析失败、参数失败和策略拒绝也必须记录；审计不得回显参数校验错误中的敏感实例值。HTTP 身份认证和各 handler 的路径/命令等权限检查继续生效。

宿主构建调用上下文，工具参数不能覆盖它。当前内部 CallContext 支持 run_id、entrypoint、canonical allowlist、expected_catalog_version、逐能力 capability_bindings 和调用前 monotonic deadline。client_id 继续由 HTTP 认证层传入。此上下文不是完整授权系统：动态撤销、资源级授权、共享调用预算、运行中取消和硬超时仍是正式 bridge 的准入条件。

目录版本检查采用显式失败语义：版本不匹配时不执行，不自动重新绑定。下游 manager 在选择 transport 前复核已校验的工具定义和 original/backend identity，拒绝定义变化。当前已进一步绑定 stdio backend 实例代次：每次 start 生成独立 instance ID，manager 快照与 client 发送前均检查；即使同名同 schema，重启也使旧绑定失效。浏览器 session/target 和上游服务内部资源变化尚未绑定，不能把 transport 实例身份当成页面或登录态身份。

当前 `bind_capabilities()` 为选中的 canonical tools 生成版本快照，放入 CallContext 后，未绑定目标或版本变化均拒绝调用；无关目录变更不影响这些绑定。它是身份约束，不赋予权限，也不保留旧 handler 执行能力。全目录 expected_catalog_version 仍可作为更严格的可选约束。

`capability_get` 返回 canonical_name、source、backend_id、backend_instance_id、capability_revision，并补充显式 category、availability、aliases、deprecated/replacement 和经过输入 schema 校验但不执行的 examples；原 tool 定义（包括 outputSchema）保持原样。availability 只报告已有证据：真实 downstream manager 的当前 state 可作为 manager evidence，本地或未经检查的依赖返回 unknown，不把“已注册”解释为“可正常执行”。客户端可把 capability_revision 放入 `capability_call.expected_revision`。省略该参数仍采用当前定义，兼容旧客户端。版本涵盖描述/schema/annotations、语义目录元数据、下游 original_name/backend/instance；它不是凭据、授权票据或远端数据快照。本地工具的 backend 字段为 null，不推断浏览器资源身份。

未来完整 run binding 还应包含资源身份；每次调用重新检查当前权限，撤销权限优先于旧快照。绑定不承诺固定服务器内部状态，也不阻止已开始调用的远端资源变化。

### 结果契约

目录按需定义应保留 outputSchema；缺少 schema 的能力必须有经验证的结果适配器才能进入编排。处理顺序为：backend 完整结果 → 保真适配 → 编排读取/聚合 → 模型展示适配。完整数据受预算约束，超过预算明确失败或返回可读取分页/资源引用；不得静默截断后继续统计。

结果契约须保留 isError、内容块、来源、完整性和部分失败状态。展示摘要和完整数据分别管理，resource reference 需要独立的权限、过期和读取预算。当前尚未引入统一结果平面，既有 MCP 内容保真行为保持有效。

### 客户端 profile

先使用显式、经过验收的 full/compact 配置；未来增加 profile 时逐项记录客户端支持能力。不得从客户端名称推断它支持原生 Tool Search、程序编排或特定授权流程。原生程序对 direct tool 的循环调用不必经过服务端 orchestration bridge，因此 bridge allowlist 不能替代 direct 调用的权限和配额。

### Benchmark 判定边界

第一阶段比较四个对照：逐项 direct、compact discovery、专用固定 batch、固定逻辑 orchestration。后两者可以共用 reducer，以证明收益究竟来自前置聚合还是通用执行器。固定 workload 不调用模型，不能测得真实模型往返数、代码生成成功率和修复成本。

原 `benchmarks/read_only.py` 保留为前轮基线；本轮新增 `benchmarks/read_only_v2.py` 作为 P0 收尾测量入口。它继续使用临时生成 PDF 和受控分页数据，无网络调用、不注册新业务工具、不接受代码，所有叶子调用经过 registry；同时显式检测部分 PDF 失败、downstream `isError`、缺少 `structuredContent`、分页循环和目标 capability revision 变化。统一记录 call_count、successful/failed calls、完整 registry MCP result bytes、projected model-visible bytes、discovery cost 与 setup/discovery/execution/projection/total 分阶段耗时。未调用模型，因此 model_round_trips、model_input_tokens、model_output_tokens 均保持 null。专用 batch 和固定 orchestration 仍共享同一宿主 reducer，只能证明前置聚合收益，不能证明通用 Code Mode 更优。

第二阶段才使用相同模型、任务和权限做端到端评测，记录实际轮次、tokens、失败修复、总耗时和成本，区分冷/热缓存并轮换执行顺序。成功条件先要求摘要正确且不隐藏部分失败，再判断收益。若专用 batch 已覆盖主要工作流，应停在该方案；不因固定聚合有效就自动进入通用 Code Mode。

## P0–P3 实施路线：Tool Search 之后补齐程序化编排

Claude 的 Tool Search / deferred loading 主要解决工具定义进入上下文过多的问题；本项目现有 `compact + capability_search/get/call` 已经覆盖了这类按需发现的核心目标，而且保持跨 MCP 客户端可用。下一阶段不以复制某一客户端的原生 Tool Search 为主要目标，而是补齐其后一层能力：批量调用、循环、分页、过滤、聚合，以及中间结果在进入模型前完成收敛。

### P0：执行契约与只读 benchmark

先实现 benchmark-only 的只读编排原型，不注册通用 `execute(code)` 工具，也不接受模型生成的任意程序，不宣称已经具备安全 Code Mode。原型使用受信任、固定逻辑的 workload，通过现有 `ToolRegistry.call_tool()` 路径调用能力，继续复用 catalog refresh、schema 校验、原 handler 和 audit。

基础字节指标和结果等价校验在 P0 同步实现，不延后到 P1；先统一 direct/discovery 的校验与失败审计。首批至少包含两个 workload：

1. **PDF batch**：对一批 PDF 调用 `pdf_inspect`，在编排侧检查页数、页面尺寸、标题等元数据，只返回异常文件和总体统计，不把每个 PDF 的完整结果逐项送回模型。
2. **Paginated downstream**：使用受控的只读 fake/downstream 工具模拟多页数据，执行分页、字段过滤和聚合，只返回最终统计、Top-N 或异常记录。

P0 的目标不是证明 sandbox，而是回答一个更基础的问题：当 backend 产生大量中间数据时，组合执行和前置聚合能否显著减少模型往返与 model-visible data。若这一收益不成立，就不应为通用 Code Mode 提前承担执行器和隔离成本。即使聚合收益成立，也必须比较专用 batch，并经第二阶段端到端评测后才决定是否建设通用执行器。

### P1：量化数据路径，并增强按需定义质量

P1 首先把 benchmark 的数据流指标做完整，至少区分：

- `backend_result_bytes`：底层工具实际产生并由编排层处理的结果总量；
- `model_visible_result_bytes`：最终真正暴露给模型的结果量；
- backend call count、模型往返数、elapsed time、失败与部分成功；
- discovery/schema 成本与最终回答成本。

核心观察值不是“总共传了多少数据”，而是中间数据在进入模型前被压缩了多少。例如 backend 可以处理数 MB 数据，而最终只向模型返回数 KB 聚合结果。

同时完善 `capability_get` 的按需定义质量。除 schema 外，优先考虑加入稳定的 source/backend、availability、canonical/deprecated/alias 元数据，以及少量经过维护的 tool-use examples。JSON Schema 负责描述参数是否合法，examples 用于表达真实调用中参数应如何组合。该增强比要求普通任务先枚举完整目录更有直接价值。

### P2：改进 discovery 检索与运维目录

在 P0/P1 有真实 workload 数据后，再优化 discovery 本身：

- 改善 `capability_search` ranking，可评估 BM25、字段加权或显式 structured metadata；
- category/backend/source 使用明确元数据，不从工具名前缀猜测语义；
- 增加可靠的 `capability_list` 与 cursor pagination，cursor 绑定 catalog version 和过滤条件；
- catalog 变化导致 cursor 失效时明确要求重新开始，避免静默漏项或重复。

`capability_list` 主要服务 inventory、调试、catalog diff、测试和管理界面，不应成为普通模型任务的前置步骤。LLM hot path 仍然应以 `capability_search → capability_get → capability_call` 为主。

### P3：根据实测决定正式 isolated Code Mode 与 exposure

只有 P0/P1 的端到端评测证明通用编排相对专用 batch 仍有实际价值后，才进入正式执行器设计与实现。正式版本再评估本地 sandbox/container/VM 与 Cloudflare isolated Workers，并要求满足文档 21 已定义的安全边界：无凭据暴露、服务端 allowlist、调用次数/并发/时限/内存/结果大小限制、取消、部分失败、审计以及未知结果处理。

正式执行器仍不得仅凭 downstream `readOnlyHint` 判断准入；GUI、native 操作、shell、写文件和其他副作用能力不进入第一版任意程序编排。

默认 exposure 也放到这一阶段决定。根据目标客户端和 benchmark 结果比较：

- direct/full：适合短、明确、交互式任务；
- compact + discovery：适合长尾能力按需发现；
- orchestration / Code Mode：适合批量、分页、过滤、聚合和中间数据量大的任务。

若目标客户端本身已经提供 Tool Search 或 Code Mode，应优先复用客户端原生机制，避免服务端再套一层执行器。浏览器 DevTools 与 Headless 继续保留明确 backend/session 身份，不因 schema 相似而提前合并。

因此，本项目的目标不是在 Tool Search、Progressive Discovery 和 Code Mode 之间三选一，而是形成分层能力。

## 最终架构构想

目标形态不是“一个越来越大的 MCP Server”，而是把 **能力发现、直接调用、程序化编排、backend 选择、授权与结果收敛** 分成相互独立但共用同一注册表和审计路径的层次。

```text
                        MCP Clients
        ChatGPT / Claude / OpenWebUI / other MCP clients
                             │
              ┌──────────────┴──────────────┐
              │                             │
      Client-native features          Generic MCP path
   Tool Search / Code Mode              full / compact
              │                             │
              └──────────────┬──────────────┘
                             ▼
                 Client Adaptation Layer
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
         ▼                   ▼                   ▼
   Direct Tool Path   Progressive Discovery   Orchestration Path
   高频、明确、短操作    search → get → call     批量/分页/过滤/聚合
         │                   │                   │
         └───────────────────┴─────────┬─────────┘
                                       ▼
                              Capability Control Plane
                       catalog / metadata / examples / version
                    source / backend / availability / aliases
                                       │
                                       ▼
                                  ToolRegistry
                         canonical identity + schema validation
                              policy gate + audit routing
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
              ▼                        ▼                        ▼
          Local handlers          Downstream MCPs          Browser/Desktop
       PDF / files / Git /       DevTools / WPS /        Extension / CUA /
       knowledge / web ...       future services ...      native adapters ...
              │                        │                        │
              └────────────────────────┼────────────────────────┘
                                       ▼
                              Structured Result Plane
                         normalize / bound / reference / aggregate
                                       │
                         ┌─────────────┴─────────────┐
                         │                           │
                         ▼                           ▼
                 direct result to model      orchestration-local data
                                             filter / join / aggregate
                                                    │
                                                    ▼
                                           bounded final result
                                                    │
                                                    ▼
                                                  Model
```

### 1. Client Adaptation Layer：适配客户端，而不是强迫所有客户端走同一路径

MCP4ChatGPT 不假定所有客户端能力相同。Claude 等客户端若原生支持 Tool Search / deferred loading，应允许直接利用客户端能力；客户端若已经有安全 Code Mode，则服务端不再强制套第二层代码执行器。普通 MCP 客户端则继续使用 `full/compact + capability_search/get/call`。

因此“默认暴露多少工具”不是全局常量，而应逐步演化为 **client/profile policy**。同一个 ToolRegistry 可以针对不同客户端产生不同 exposure profile，但 canonical capability identity 保持一致。

### 2. Capability Control Plane：目录是控制面，不是业务执行面

Capability catalog 负责回答“有什么、从哪里来、何时可用、怎么调用”，而不直接承载业务逻辑。目标 metadata 至少包括：

- canonical name、description、input schema；
- source、backend、availability、outputSchema 或明确结果适配契约；
- alias / deprecated / replacement；
- category、风险/只读策略标识；
- 少量维护过的 tool-use examples；
- catalog version 与稳定的变更语义。

`capability_search` 是模型热路径；`capability_get` 提供按需完整定义；`capability_list` 主要服务 inventory、测试、管理和 catalog diff。

目录变化不得改变正在执行任务所绑定的 capability identity。长任务和未来 orchestration run 应记录 catalog version，并对运行中能力消失或 schema 变化给出显式结果。

### 3. ToolRegistry：整个系统唯一的能力执行闸门

无论调用来自：

- 直接 MCP tool；
- `capability_call`；
- 未来 orchestration bridge；
- 客户端原生 Code Mode 的适配层；

最终都应经过同一个 ToolRegistry 路径。

ToolRegistry 负责 canonical tool resolution、schema validation、服务端策略检查、backend/channel 选择记录和 audit。不得因为进入 Code Mode 就绕过现有 handler、权限和审计逻辑。

未来 orchestration bridge 调用的本质仍应是：

```text
bounded worker request
        ↓
bridge authorization
        ↓
ToolRegistry.call_tool(...)
        ↓
existing handler
```

而不是给 worker 直接暴露 shell、凭据、downstream transport 或内部对象引用。

### 4. Backend identity 与 Resource identity 分开建模

相同 schema 不代表相同资源。特别是浏览器：

- DevTools 可能代表用户正在使用、已有登录态的 Chrome；
- Headless 可能代表隔离 profile 或后台浏览器；
- Extension 代表另一套 tab/session/channel；
- GUI/Computer Use 又有窗口和应用资源身份。

因此未来可以提供统一的逻辑 capability，但不能静默切换 backend。任何统一入口都应绑定稳定的 `backend_id / session_id / target_id`，并在任务生命周期内保持资源身份。

目标是减少模型面对的无意义重复 schema，同时保留可审计、可解释的执行目标；不是用 `backend=auto` 掩盖不同登录态和副作用边界。

### 5. Direct / Discovery / Orchestration 三条路径长期并存

三者不是升级替换关系，而是 workload routing：

```text
Direct tools
    → 高频、短、明确、交互式操作

Progressive discovery
    → 长尾 capability
    → search → get → call

Programmatic orchestration
    → 批量、分页、依赖调用、过滤、聚合
    → 大量中间数据不进入模型
```

简单任务应尽量避免为 discovery 或 executor 多付一轮延迟；复杂数据任务则应尽量避免把大量中间结果逐次送回模型。

未来可以在客户端/服务端积累真实 telemetry 后再形成 routing policy，但第一阶段保持显式选择，避免隐藏式自动路由带来不可解释行为。

### 6. Structured Result Plane：结果控制与工具定义压缩同等重要

Code Mode 的价值不只在减少 tool definition tokens，而在于把中间结果留在执行环境。系统应把两个指标分开：

```text
backend_result_bytes
        ≠
model_visible_result_bytes
```

直接调用可以把 bounded structured result 返回模型；编排模式则允许在 worker 内完成 filter、map、join、group、Top-N 和统计，只输出最终摘要和必要 source/resource references。

大文件、图片和超大结构化结果优先转换为稳定、只读、可过期的 resource reference，而不是 base64 或超长 JSON 进入模型上下文。

### 7. Policy、授权与执行能力分离

“发现得到”“能够调用”“允许在程序里批量调用”是三个不同权限层级：

```text
discoverable
    ≠ directly callable
    ≠ orchestration-allowlisted
```

服务端维护 orchestration allowlist，不依赖第三方 `readOnlyHint` 自动放行。第一版只允许经过人工确认的纯读取能力；写操作、shell、GUI、账号状态变更以及结果不确定的副作用调用继续走直接调用和明确授权路径。

将来若支持写操作编排，需要额外设计逐调用授权、事务边界、幂等键、unknown outcome 和补偿/恢复策略，而不能直接沿用只读 runner。

### 8. Execution Plane：先证明编排价值，再选择 sandbox

P0 benchmark harness 与正式 executor 必须明确分开：

```text
Phase 0 benchmark harness
    trusted fixed workloads
    no arbitrary generated code
    no security claim

        ↓ 只有收益成立才进入

Production orchestration runtime
    isolated worker
    no credentials
    narrow bridge
    resource limits
    cancellation
    bounded output
```

正式 runtime 可以比较 local sandbox/container/VM 与 Cloudflare isolated Workers，但运行位置不是架构核心。核心契约是 worker 无法直接获得宿主凭据、任意文件系统、浏览器 profile 或任意网络能力，只能通过短期 bridge handle 调用服务端明确允许的 capability。

### 9. Telemetry 驱动最终 exposure 和 routing 决策

最终不以“工具数量越少越好”作为优化目标，而记录真实 workload：

- tool-definition / discovery / schema 成本；
- backend call 数与结果 bytes；
- model-visible bytes/tokens；
- 模型往返数；
- elapsed time；
- 成功率、恢复率与错误类型；
- cold/warm cache；
- direct、compact、orchestration 三条路径的差异。

只有这些指标证明某类 workload 稳定受益后，才调整默认 profile、bootstrap 集合或 routing policy。

### 10. 最终目标

最终希望 MCP4ChatGPT 成为一个 **跨客户端 Capability Runtime / 能力运行时**，而不只是“把很多工具集中暴露给模型”的 MCP 聚合器：

```text
MCP aggregation
      ↓
Capability registry
      ↓
Progressive discovery
      ↓
Policy-controlled execution
      ↓
Programmatic orchestration
      ↓
Bounded model-visible results
```

它既保留 MCP 的通用互操作性，又能利用不同客户端已有的 Tool Search / Code Mode；既支持用户当前真实浏览器、桌面和本地开发环境，也能对批量数据任务提供隔离的程序化执行路径。不同层共享同一 capability identity、validation、policy 和 audit，因此新增 backend 或客户端不会再线性扩大模型默认上下文或产生另一套旁路执行体系。

## 验收方式

比较 full 直接调用、compact 渐进发现、实验性 Code Mode 三条路径；使用相同任务、数据、模型与权限。至少记录：

- 任务成功率、错误操作及恢复结果；
- 描述 tokens、搜索/schema tokens、中间结果 tokens、程序和最终结果 tokens；
- 模型往返数、实际 backend 调用数、总耗时与运行成本；
- 简单任务与批处理任务分别统计，区分冷缓存与热缓存；
- 取消、超时、目录变更和部分失败是否产生可解释结果。

先在只读试点证明收益，再决定默认 profile。写操作进入编排前另行设计逐调用授权、可审计副作用和未知结果处理。发现、暴露与授权各自承担自己的职责。

## 前轮工作区处理（历史记录）

撤回本对话刚加入且尚未完成验证的 bootstrap 默认、capability_list 草稿和 get/source 草稿；保留此前 discovery/mcpc 修复。没有重启正式服务。

复核后定向测试：`tests/test_capabilities.py tests/test_server.py tests/test_discovery_regressions.py`，40 passed（9.75 秒）；`git diff --check` 通过。该结果验证恢复后的代码基线，不代表尚未实现的 Code Mode 已通过测试。

## 逐能力绑定实现验收（2026-09-27）

- `capability_get` 元数据和 `capability_call.expected_revision` 已实现；旧客户端省略版本仍可调用。
- 内部 `CallContext.capability_bindings` 按目标绑定；benchmark 已使用该约束。
- stdio 服务重启即使名称/schema 不变，也生成新的 backend_instance_id；实例/目录快照同时读取，发送前再次检查实例。
- 回归覆盖无关目录变化、目标定义变化、校验后实例变化、绑定不授予权限、别名 canonical resolution、真实测试子进程重启及同一 client 对象重新启动。
- 全量测试：300 passed、11 skipped（26.83 秒）；PyMuPDF/SWIG 发出 5 条弃用警告。8 组固定 workload 重跑通过结果等价校验；结果字节投影与前轮一致。`git diff --check` 通过。
- 本轮未重启正式服务。未实现 browser session/target 绑定、远端状态快照、运行中取消或生产隔离执行器。

## P3 客户端端到端验收任务与记录格式

P3 的下一层证据必须来自**真实客户端 + 真实模型任务**，不能用协议测试或固定程序 benchmark 代替。建议固定三类任务并在相同模型、数据、权限下轮换执行顺序：

1. **短直达任务**：已知单个能力，比较 full direct 与 compact discovery 的额外发现成本。
2. **PDF 批处理任务**：复用 P0 的多 PDF 元数据检查，比较逐项 direct、progressive discovery，以及可用的专用 batch/fixed orchestration 路径。
3. **分页聚合任务**：复用受控多页数据，要求过滤、Top-N 与完整性判断；观察中间结果是否导致额外模型往返或修复。

每次运行至少记录 task_id、task_type、trial、run_order、client/version、exact model、path、exposure、dataset_version、permission_profile、catalog_version、correct、complete、discovery_failures、repair_rounds、model_round_trips、model_input_tokens、model_output_tokens、target_call_count、model_visible_bytes、elapsed_seconds、cold_or_warm 和 errors。benchmarks/model_e2e_tasks.json 固定三类任务身份；benchmarks/model_e2e.py 只验证/聚合真实客户端输出的 JSONL，不调用模型、不生成这些观测值，也拒绝把 fixed/protocol 结果冒充 model_e2e。

只有真实模型运行才能填写 model_round_trips、token 与 repair_rounds；固定 benchmark 中这些字段必须保持 null。若专用 batch 已满足主要 workload，或通用编排未显示额外价值，则 P3 的正确结果是**记录证据缺口并维持现有 exposure**，而不是继续建设 executor。

## P0–P2 实施与 P3 准备验收（2026-09-27）

- capability_get 已补齐 category、availability、aliases、deprecated/replacement、examples；examples 覆盖 PDF 读取/编辑、文件读写、后台任务和 discovery，注册时只按输入 schema 校验，不执行。
- capability metadata、分类、关键词、examples、检索排序和 cursor 编解码已集中到 src/mcp4chatgpt/capability_catalog.py；ToolRegistry 保留调度与校验职责。
- capability_search 保留既有 query/limit 和返回 envelope，新增 source/backend/category 可选过滤；固定查询集的旧检索 Top-1/Top-5 均为 **2/5 (40%)**，本轮字段加权检索均为 **5/5 (100%)**。该集合覆盖中文 PDF 编辑、文件写入、后台任务、Chrome DevTools 与 Headless backend。
- capability_list 返回紧凑目录项并支持 source/backend/category、limit、cursor；cursor 绑定 catalog version、规范化过滤条件和下一位置。过滤不匹配返回 capability_cursor_mismatch，目录版本变化返回 capability_catalog_changed。它是显式 inventory/debug 接口，不加入默认 full/compact tools/list；默认模型热路径继续只暴露 capability_search / capability_get / capability_call。
- P0 v2 固定 workload 通过：30 PDF 路径每种模式 30 次目标调用；分页路径每种模式 10 次目标调用。PDF 完整 MCP result bytes 为 40,650，分页为 233,913；专用 batch/fixed orchestration 的 projected model-visible bytes 分别为 **327** 和 **486**。direct/compact 的 projected bytes 等于完整叶子结果。未调用模型，round-trip/token 字段均为 null。
- 五个异常契约场景均被显式检测：部分 PDF 失败、downstream isError、缺少 structuredContent、分页循环、目标 capability revision 变化。
- 真实 pinned mcpc 协议往返直接复用现有 acceptance helper，对 full/compact 两个 exposure 验收：**2 passed in 14.10s**。这是协议层证据，不是模型 E2E。
- focused discovery/capability 回归先得到 **27 passed**；benchmark v2 + discovery query set 得到 **2 passed**。最终将 capability、discovery regression、benchmark v2 与 downstream suite 合并复核：**70 passed、5 warnings**。
- 首次全量复核曾得到 **303 passed、11 skipped、3 failed**。重新对照“capability_list 仅用于 inventory/debug、默认 full/compact 暴露不变”的既定约束后，确认这 3 个失败不是陈旧测试，而是实现把 capability_list 错误加入默认 tools/list 所暴露出的契约回归。已通过区分“已注册 capability tools”和“默认列出的 capability tools”修正；未修改受 secret guard 保护的 tests/test_server.py。
- 修正后 tests/test_capabilities.py + tests/test_server.py 为 **33 passed**；加入 P3 model-E2E 记录校验/manifest 测试后，最新全量 uv run pytest -q 为 **310 passed、11 skipped、0 failed**。pinned mcpc full/compact 协议往返仍为 **2 passed**；benchmark v2 + discovery query set 仍为 **2 passed**，核心 workload 与检索命中结果不变。当前 fixed benchmark 的 full/compact definition bytes 为 **50,335 / 2,405**。
- 本轮没有修改权限、认证、rm 放行、co-te 权限或脱敏/截断行为，没有实现任意代码执行器，也没有合并浏览器 backend。用户已在此前手动重启正式服务；本次“隐藏 capability_list”源代码修正发生在该次重启之后，因此仍需下一次人工重启后再做 live tools/list 验收。
