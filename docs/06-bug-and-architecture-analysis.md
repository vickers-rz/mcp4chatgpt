# MCP4ChatGPT 全面分析报告

## 一、项目背景与目的

### 演进路径（从文档还原）

```
docs/01  →  最早构想：IPv6 全终端桥接（仅想法，风险太大未实现）
docs/02  →  收窄方向：paste-only MCP（粘贴专用，最保守）
docs/03  →  未来规划：完整 local-ops MCP（当前项目的路线图）
docs/04  →  实现记录：当前 MCP4ChatGPT 就是"文档03"的落地
docs/05  →  架构说明：当前架构的权威参考文档
```

**项目本质**：一个跑在你 Mac 本地 `127.0.0.1:8766` 的 Python HTTP 服务，当前推荐通过 Cloudflare Tunnel 暴露到 `https://mcp.runzhe.uk/mcp` 供 ChatGPT Web 连接，让 ChatGPT 可以操作本地文件、执行命令、读写知识库、控制终端。`m6.ic2id.fun` / Caddy / IPv6 是旧 IPv6/DDNS 路径，仅作为 legacy 备用说明保留。

### 四大能力组

| 能力组 | 说明 | 外部依赖 |
|---|---|---|
| `local_*` | 文件读写/命令执行/Git/精确补丁 | 无 |
| `terminal_*` | 动态加载 `co-te.py`，控制 Terminal/iTerm2/Termius | `co-te.py`（另一个项目） |
| `web_*` | Firecrawl HTTP API 的薄层适配 | Firecrawl API Key |
| `knowledge_*` | 本地 JSON 知识库，类 NotebookLM | 无 |

---

## 二、架构优雅性评价

### ✅ 做得好的地方

1. **零外部依赖**：`pyproject.toml` 的 `dependencies = []`，整个项目只用标准库，部署极简，无 pip 地狱风险。

2. **分层清晰**：`server.py`（HTTP+OAuth路由）→ `tools.py`（工具注册中心）→ `*_ops.py`（业务逻辑）→ `safety.py`（安全底层），职责分明。

3. **工具契约稳定**：`_ok(result)` 统一包装返回格式，保留 `structuredContent`，为未来切换 embedding 后端留了口子。

4. **安全设计有意识**：HMAC 签名 token、`MCP_ALLOWED_ROOTS` 路径沙箱、危险命令黑名单、输出脱敏+截断，这些都是主动防御。

5. **co-te.py 复用**：terminal_ops 动态加载另一个项目的模块，避免复制代码，符合文档中的"reuse not copy"精神。

### ⚠️ 架构层面的不优雅之处 (已优化)

#### 问题 A：`terminal_ops.py` 每次调用都重新加载模块（性能问题）

`_load_co_te` 每次都执行 `spec.loader.exec_module(module)`，即每次工具调用都完整重新执行 `co-te.py` 的模块代码。如果 `co-te.py` 有模块级初始化逻辑（导入、全局状态），这会造成性能浪费。
**解决方案**: 引入了全局的 `_CO_TE_CACHE`，使用模块懒加载缓存提升性能。

#### 问题 B：`knowledge_ops.py` 每次操作都全量读写 JSON 文件（可扩展性差）

对于 v1 个人用途是可接受的，但未来需重构。目前所有知识数据存储在同一个巨大的 JSON 里，I/O 操作随使用时间线性退化。

#### 问题 C：`server.py` 宽泛 `except` 屏蔽了诊断信息

之前所有异常都返回 `{"error": "xxx"}` 字符串。
**解决方案**: 在 `server.py` 中增加了针对 OAuth endpoint 的规范 RFC 6749 错误处理 (`invalid_request`)，内部异常返回模糊化后的 `server_error` 避免内部栈追踪泄漏。

#### 问题 D：`AUTH_SECRET` 同时作为"用户密码"和"token 签名密钥"

一个密钥承担两个职责：用户认证 + 密码学签名。在未来的迭代中需要将两者分离。

---

## 三、Bug 清单与修复状态

| # | 模块 | 严重度 | 修复状态 | 问题描述 |
|---|---|---|---|---|
| Bug 1 | `oauth.py` | 🔴 | ✅ 已修复 | `_cleanup` 在 `pop` 前执行，导致 `issue_token` 里的过期码检查永远走不到。现已通过重排删除与清理逻辑修复。 |
| Bug 2 | `knowledge_ops.py` | 🔴 | ✅ 已修复 | `list_sources` 排序时，由于 `created_at` 可能为 `None` 时引发 `sorted()` 崩溃。现已增加兜底 `or 0.0` 处理。 |
| Bug 3 | `server.py` | 🟠 | ✅ 已修复 | `_read_json` 遇到非数字 `Content-Length` 时引发 `ValueError` 并泄露到响应中，且存在大长度请求造成的线程阻塞。现已加入整数校验与 8MB 体积硬性上限。 |
| Bug 4 | `scripts/start.sh` | 🟠 | ✅ 已修复 | 启动健康检查失败时静默错误且不清理残留进程（导致下次无法重启）。现改为等待循环轮询并确保失败时强杀僵尸进程并清空 pid 文件。 |
| Bug 5 | `.env.example` | 🟡 | ✅ 已修复 | 模板文件中包含大量硬编码个人电脑路径。现已改为泛化的 `yourname` 占位符及空白路径搭配注释。 |
| Bug 6 | `oauth.py` | 🟡 | ✅ 已修复 | `render_authorize_form` 生成授权表单时，`name` 属性未作转义，存在 XSS 风险。现已通过新增全局 HTML 转义工具函数以及属性白名单限制规避 XSS 风险。 |
| Bug 7 | `knowledge_ops.py` / `oauth.py` | 🟡 | ✅ 已修复 | 损坏或结构异常的 JSON store 之前会被静默当作空数据，后续保存可能覆盖原文件。现已在加载失败时重命名为 `.corrupt.<timestamp>` 备份后再返回空结构。 |

### 单元测试与稳定性

以上所有的安全漏洞（XSS 攻击等）和逻辑异常，均补充了相应的测试用例。目前核心单元测试覆盖 OAuth、知识库、HTTP MCP 握手、审计轮转、脱敏和损坏 JSON 隔离：
* `test_oauth_code_expiry` 确保准确返回错误语义，禁止代码重用。
* `test_list_sources_none_created_at` 验证知识库元数据兼容性。
* `test_authorize_form_xss` 验证表单参数反射的完全实体化转移与白名单校验。
* `test_corrupt_knowledge_store_is_quarantined_before_reuse` 和 `test_corrupt_oauth_clients_are_quarantined_before_reuse` 验证损坏 JSON 不会被后续保存静默覆盖。
