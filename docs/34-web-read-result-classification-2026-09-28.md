# 本机 Web 读取结果分类（P2）

日期：2026-09-28。分支：`codex/operations-store`。基线：`7042af6`。

本记录承接 [文档 33](33-local-web-top-level-exposure-2026-09-28.md)。P1 已建立稳定的首层 `read_webpage`；本轮 P2 解决“浏览器调用成功不等于获得有效正文”的问题，为后续 Skill 提供稳定、结构化、证据驱动的读取结果契约。

## 1. 状态契约

`read_webpage` 现在返回以下七种状态之一：

```text
ok
empty
login_required
challenge
access_blocked
timeout
unavailable
```

语义如下：

- `ok`：得到达到最低有效长度的 rendered text，且没有命中强阻断信号。
- `empty`：浏览器成功返回页面，但可见正文不足以作为可靠内容证据。
- `login_required`：最终 URL、标题或短页面正文呈现明确登录态特征。
- `challenge`：标题或短页面正文呈现 CAPTCHA、人机验证、安全挑战等明确特征。
- `access_blocked`：标题或短页面正文呈现访问被拒绝、请求被拦截、地区不可用等明确特征。
- `timeout`：浏览器调用发生明确 timeout。
- `unavailable`：Extension 未连接、bridge 未运行、浏览器队列饱和或其他浏览器运行时不可用。

这些状态描述的是**本次浏览器观测结果**，不是网络根因诊断。

## 2. evidence

每个成功返回的页面分类都包含 `evidence`，其中保存：

- `classification_version`
- `signals`
- `text_length`
- `original_length`
- `title`
- `final_url`
- `canonical_url`
- `extraction_method`
- `truncated`

运行时失败则保存：

- `classification_version`
- `signals`
- `error_type`
- `error`

当前版本：

```text
web_read_status_v1
```

后续如果修改启发式规则，应升级 classifier version，而不是静默改变 Skill 所依赖的语义。

## 3. 证据边界

当前 Chrome Extension `browser_read` 原始返回包含最终 URL、canonical URL、标题、正文、提取方式、正文长度、截断信息等，但**不包含 HTTP status code**。

因此本轮明确不做以下推断：

- 不声称某页面真实返回了 HTTP 403，除非页面可见标题/正文自身出现该文字。
- 不把 `access_blocked` 等价于“反向 GFW”。
- 不从一个验证码或访问拒绝页面推断网络审查、地域封锁或站点策略的具体根因。
- 不把任意浏览器异常都归类成 `timeout`。

如果未来需要 HTTP response status、network error、redirect chain 等证据，应从 CDP/network 或 Extension 网络层显式增加原始字段，再扩展 classifier。

## 4. 保守分类原则

分类器位于：

```text
src/mcp4chatgpt/web_read_status.py
```

它是独立纯函数模块，不执行浏览器操作。

为了降低文章正文中的关键词误报：

- challenge/access-blocked 的正文关键词只在相对短的页面中作为强信号；
- 长文章即使讨论 “access denied” 或 “verify you are human”，也不因此被判成拦截页；
- login 可由明确登录 URL path、标题，或短页面中的登录词 + 账号上下文共同确认；
- 低于最低可用正文阈值且无强阻断信号时归 `empty`。

## 5. 错误契约变化

P1 时，浏览器断线/timeout 等运行时异常会向上抛出。

P2 后：

- `RuntimeError` / `TimeoutError` 类型的浏览器运行时失败转换为结构化 `timeout` 或 `unavailable`；
- 仍然保持 `fallback_used=false`；
- 不调用 Brave / Firecrawl 作为隐式 fallback；
- 输入校验、非法 URL、程序调用错误等 `ValueError` 仍继续抛出，不伪装成网络不可用。

这一区分让调用方能把“读取没有成功”作为数据处理，同时仍能发现真正的调用/契约错误。

## 6. 审计

`read_webpage` 审计继续记录：

```text
channel=web
backend=browser
```

并新增：

```text
read_status=<七种状态之一>
retrieval_ok=true|false
```

其中 `retrieval_ok=true` 只对应 `status=ok`。

对于 `timeout/unavailable`，MCP 工具调用本身完成了结构化分类，因此 audit 的 tool-call `ok` 可以为 true；业务读取状态由 `read_status/retrieval_ok` 单独表达，避免把协议执行成功与网页内容成功混在一起。

## 7. 测试

新增：

```text
tests/test_web_read_status.py
```

并更新：

```text
tests/test_local_web_entry.py
```

覆盖：

- 七类状态中的页面类和运行时类；
- challenge / login / access-blocked 证据；
- 空正文；
- timeout / Extension disconnected / bridge unavailable / queue saturated；
- 长文章讨论拦截术语时不误判；
- access-blocked 不携带 GFW/censorship 根因推断；
- 浏览器失败仍不走云端 fallback；
- 非运行时的 ValueError 仍抛出；
- 审计 read_status / retrieval_ok。

本轮实际执行：

```text
uv run pytest -q tests/test_web_read_status.py tests/test_local_web_entry.py
22 passed
```

P1/P2 相关 focused suite：

```text
130 passed
```

完整 Python suite：

```text
389 passed, 11 skipped, 5 warnings
```

5 条 warning 仍为既有 PyMuPDF/SWIG DeprecationWarning。

## 8. 下一阶段

下一阶段是 P3：MCP Skill。

Skill 不应重新解析异常字符串，而应直接依赖 `read_webpage.status`：

- `ok`：使用正文；
- `empty/login_required/challenge/access_blocked`：说明本机浏览器已经到达页面，但正文不可直接作为正常来源；
- `timeout/unavailable`：说明本机 browser path 当前不可用；
- 对“云端失败后使用本机 MCP”的策略只做路由，不把 `access_blocked` 自动解释为反向 GFW。

P3 仍需实现 MCP Skills capability/list/get/resource delivery；真实 ChatGPT 工具扫描、缓存刷新和端到端路由验收留到 P4。
