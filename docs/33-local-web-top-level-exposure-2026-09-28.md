# 本机 Web 首层入口实施记录

日期：2026-09-28。分支：`codex/operations-store`。基线：`100eaf8`。

本记录承接 [文档 32](32-browser-topology-and-web-routing-review-2026-09-28.md) 的 P1 建议。本轮只实施稳定高层 Web 入口和 compact 暴露策略；网页阻断页分类、MCP Skill 协议、真实 ChatGPT 扫描/缓存验收与生产部署均未在本轮完成。

## 1. 已实施

### 1.1 稳定高层 URL 读取入口

新增 `read_webpage`，作为模型直接调用的稳定入口：

- 输入为 `url` 与 `max_chars`。
- 内部复用现有 `browser_search.read()`。
- 强制使用已连接的本机 Chrome Extension 会话。
- 返回 `requested_url`、实际页面结果、`backend="browser"` 和 `fallback_used=false`。
- 不静默回退 Brave、Firecrawl 或其他云端读取器。
- 原有 `ext_read_webpage` 保留，继续作为底层/兼容 capability。

这样后续 P2 即使增加阻断页分类、结构化状态或更多 browser backend，模型首层契约仍可保持 `read_webpage` 不变。

### 1.2 compact 顶层从 4 个入口调整为 6 个入口

compact 的直接工具现在设计为：

```text
server_info
search_web
read_webpage
capability_search
capability_get
capability_call
```

其余 Extension、CDP、Headless、Computer Use、文件和下游工具仍保留在 capability catalog，可通过 discovery/call 使用。

原先初始化与 catalog refresh 各自复制 compact 筛选逻辑。本轮收敛到 `ToolRegistry._select_listed_names()`，避免 downstream `tools/list_changed` 刷新后重新退回旧的四工具列表。

### 1.3 discovery 元数据

`search_web` 和 `read_webpage` 已明确归入 `browser` category，并增加维护关键词，包括：

- `local chrome`
- `browser session`
- `本机浏览器`
- `反向gfw`
- `云端访问失败`
- `地域限制`
- `登录态`（URL 读取）

因此 `capability_search("反向GFW")` 应能发现高层本机 Web 入口，而不需要模型先知道 `ext_*` 名称。

两者同时加入 schema example 校验。

### 1.4 server instructions

初始化 instructions 已改为优先告诉客户端：

- 搜索先考虑 `search_web`；
- 已给 URL 时先考虑 `read_webpage`；
- 二者默认面向本机 Chrome 网络/会话；
- 深层浏览器检查继续使用 `chrome_devtools__*`；
- 其他能力继续走 capability discovery。

文本长度控制在既有 512 字符约束内。

## 2. 新增/更新测试

已修改或新增以下回归：

- `tests/test_capabilities.py`
  - compact 精确工具列表改为六入口；
  - 验证 `capability_search("反向GFW")` 至少发现 `search_web` 与 `read_webpage`。
- `tests/test_discovery_regressions.py`
  - 在真实 downstream 动态目录更新前后都验证 compact 六入口不漂移。
- `tests/test_local_web_entry.py`
  - 验证 `read_webpage` 只调用本机 browser reader；
  - 验证返回 backend/fallback 标记；
  - 验证断线、超时和读取异常时不调用 Firecrawl/Brave 等云端 fallback；
  - 验证 `search_web(backend=browser)` 失败时不自动切 API；
  - 验证高层 `search_web/read_webpage` 与底层 Extension 入口保持相同的保守副作用 annotations；
  - 验证审计 `channel=web`，并在有证据时记录实际 `backend` / `engine`。

## 3. 本轮验证状态

### 3.1 已完成的静态核对

- Git 基线和 worktree 已核对；修改发生在 `codex/operations-store`，不是 `main`。
- repository search 未发现其他测试把 compact 精确固定为旧四工具列表。
- mcpc acceptance 的 compact 断言只要求隐藏低层工具，与六入口设计不冲突。
- server instructions 的既有顺序约束仍满足：`ext_connection_status` 出现在 `ext_run_js` 之前，并保留 `least-privileged`。
- instructions 长度为 506，低于既有 512 字符测试上限。

### 3.2 动态回归状态

P1 收尾后的回归已在本 worktree 真正执行完成：

```text
uv run pytest -q tests/test_local_web_entry.py tests/test_capabilities.py tests/test_discovery_regressions.py tests/test_core.py tests/test_server.py
111 passed in 13.68s
```

随后执行完整 Python 测试集：

```text
uv run pytest -q
375 passed, 11 skipped, 5 warnings in 36.08s
```

5 条 warning 均为 PyMuPDF/SWIG 类型的 `DeprecationWarning`，不是本轮 Web 路由改动引入的测试失败。

由于当前 CodexPro 为 `CODEXPRO_BASH_MODE=safe`，测试前临时把 lock 中已经存在的 pytest 从 dev extra 提升为主依赖，使允许执行的 `uv run pytest` 使用 Python 3.13 项目环境；测试完成后已把 `pyproject.toml` 和 `uv.lock` 精确恢复，二者最终内容/hash 与修改前一致。

此前的 162 项 P1 相关回归记录仍可作为前一阶段证据；本记录以本次 **111 focused passed + 375 full passed / 11 skipped** 作为 P1 收尾后的最新代码级验收结果。

## 4. 尚未实施

下一阶段保持文档 32 的顺序：

1. **P2：读取结果分类**
   - 区分正文成功、空正文、登录墙、验证码/挑战、拒绝访问、超时等；
   - 返回结构化 status + evidence；
   - 不把任意 HTTP 403 简化成“反向 GFW”。

2. **P3：MCP Skill**
   - 实现 `io.modelcontextprotocol/skills` capability；
   - `skills/list` / `skills/get`；
   - 资源读取与摘要校验；
   - 定义“云端访问失败 / 本机登录态 / 地域限制”触发条件。

3. **P4：真实 ChatGPT 验收**
   - 重扫工具/插件版本；
   - 确认 compact 顶层实际看到六入口；
   - 验证共享 URL、受限网页、登录态网页的真实路由。

4. **P5：下游 MCP 暴露**
   - 在需要时让其他 MCP 复用当前单实例 `/mcp`；
   - 保留 backend identity、page handle、revision 和错误语义。

## 5. 当前边界

本轮没有：

- 合并到 `main`；
- 重启或部署 MCP4ChatGPT；
- 修改 Chrome Extension；
- 改变 CDP / Headless sidecar；
- 实现自动跨浏览器 backend fallback；
- 执行需要外部 mcpc 客户端的专项 acceptance；
- 做真实 ChatGPT 会话、工具缓存刷新或部署验收。
