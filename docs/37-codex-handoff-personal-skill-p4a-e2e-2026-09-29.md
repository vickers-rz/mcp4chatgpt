# Codex Handoff：P4A Personal Skill 真实 E2E 验收

日期：2026-09-29  
状态：待执行验收计划。  
目标仓库：`/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT`  
基线分支：`main`  
基线提交：`645aa9af0e913ec78c47512fc9cef1215ad6e509`（`Add personal ChatGPT local web skill path`）

> 这是给后续 Codex 的直接执行 handoff。目标不是继续改 Skill 基础结构，而是把 P4A 的真实 ChatGPT Personal Skill E2E 矩阵补齐，并只在有真实证据时更新验收状态。

---

## 1. 任务目标

当前可靠主路径已经建立：

```text
ChatGPT
  ↓ explicit local web access Personal Skill
Skill dependency
  ↓
connected 4GPT MCP
  ↓
search_web(backend=browser) / read_webpage
  ↓
MCP4ChatGPT Chrome Extension
  ↓
user's current local Chrome session
```

本 handoff 要验证五类剩余目标：

1. Personal Skill 能真实触发 `search_web(backend=browser)`。
2. Personal Skill 能利用**当前 Chrome 登录态**读取匿名云端无法等价读取的非敏感页面。
3. `read_webpage` 的 `login_required / empty / timeout / unavailable` 在真实链路上符合 P2 契约。
4. ordinary web research 在不要求本机浏览器时**不应强行触发 Personal Skill / 4GPT**。
5. 有状态浏览器交互遵守 capability discovery 和 **fresh tab handle** 约束，不猜、不复用失效 tab id。

最终不是追求“implicit invocation 每次都命中”，而是建立：

```text
explicit Skill invocation = correctness path
implicit invocation       = best-effort convenience
```

---

## 2. 开工前必须重新确认

不要假设本文写入时的状态仍然成立。开工首先执行：

```text
git status
git log -5 --oneline --decorate
```

要求：

- 当前目标仍是 MCP4ChatGPT 主仓库；
- 不覆盖用户未提交改动；
- 不 reset / clean / checkout 掉现有工作；
- 如果 HEAD 已不是 `645aa9a`，记录新的基线，并确认后续提交没有改变 Personal Skill、Web route 或 browser extension 契约；
- 读取根目录和相关子目录中的 `AGENTS.md`（若存在），后续服从最新指令。

重点文件：

```text
chatgpt_skills/local-web-access/SKILL.md
chatgpt_skills/local-web-access/agents/openai.yaml
src/mcp4chatgpt/tools.py
src/mcp4chatgpt/browser_search.py
src/mcp4chatgpt/web_read_status.py
src/mcp4chatgpt/skill_resources.py
src/mcp4chatgpt/ext_ops.py
src/mcp4chatgpt/capability_catalog.py
docs/35-mcp-local-web-skill-2026-09-28.md
docs/36-chatgpt-personal-skill-2026-09-29.md
logs/audit.jsonl
tests/test_personal_skill.py
tests/test_local_web_entry.py
tests/test_web_read_status.py
tests/test_skill_protocol.py
tests/test_skill_resources.py
```

---

## 3. 已完成能力，不要重复实现

除非验收发现真实缺陷，否则不要重写以下部分。

### 3.1 P1 顶层入口

compact profile 已直接暴露：

```text
server_info
search_web
read_webpage
capability_search
capability_get
capability_call
```

`read_webpage` 是 browser-only；不得静默转 Brave / Firecrawl。

`search_web(backend=browser)` 失败时不得转 API；只有用户/调用方显式使用允许 fallback 的模式时才能走其他 backend。

### 3.2 P2 七状态契约

`read_webpage.status`：

```text
ok
empty
login_required
challenge
access_blocked
timeout
unavailable
```

关键边界：

- `backend=browser` 不等于正文成功；
- `access_blocked` 只是页面观测，不等于 GFW / 审查 / 地域原因；
- `challenge` 不绕过；
- `timeout/unavailable` 是结构化读取结果；
- 调用契约错误和输入错误不能伪装成 `unavailable`。

### 3.3 P3 MCP server Skill

服务端已有：

```text
skills/list
skills/get
resources/read(skill://...)
```

当前公共/需身份验证的 Scan Tools 路径不是本任务主线。

### 3.4 P4A Personal Skill

仓库已有：

```text
chatgpt_skills/local-web-access/SKILL.md
chatgpt_skills/local-web-access/agents/openai.yaml
```

当前配置要求至少保持：

```yaml
policy:
  products:
    - CHAT
  allow_implicit_invocation: true
```

MCP dependency 指向：

```text
https://mcp.runzhe.uk/mcp
transport: streamable_http
```

Personal Skill 已有真实 `read_webpage.status=ok`、`challenge`、`access_blocked` 审计证据。

---

## 4. 验收原则

### 4.1 真实 ChatGPT E2E 与本地测试必须分开

以下证据**不能**替代真实 Personal Skill E2E：

- pytest 通过；
- 直接调用 MCP；
- 直接调用 4GPT tool；
- curl / mcpc 调用；
- 本地 Python 调用 `_read_webpage`；
- 仅看到网页正文但没有 MCP 审计记录。

真实 Personal Skill E2E 至少要求：

1. 请求发生在 ChatGPT 对话；
2. 对该场景明确记录是否显式选择 `local web access` Skill；
3. 服务端 `logs/audit.jsonl` 出现与时间窗口匹配的新调用；
4. 审计中 `tool` / `backend` / `read_status` 与预期一致；
5. 如果声称使用了登录态，还必须有只有当前会话可见的非敏感标记。

如果当前 Codex 环境不能直接操作 ChatGPT UI：

- 不得把该 case 标成 PASS；
- 准备精确测试 prompt；
- 让用户在 ChatGPT 中执行；
- 用户执行后再读取新的审计日志、必要时读取返回结果；
- 最终状态写成 PASS / FAIL / BLOCKED，不得写“推测通过”。

### 4.2 每个 case 都要保存四类证据

建议为每个 case 记录：

```text
case_id
chatgpt_prompt
invocation_mode = explicit_skill | implicit | ordinary_web
chatgpt_visible_result
audit_event(s)
verdict = PASS | FAIL | BLOCKED
```

不要把 token、OAuth bearer、Cookie、密码、私人正文写入 docs 或测试 fixture。

---

# 5. E2E-01：Personal Skill → search_web(backend=browser)

## 5.1 目的

证明 Personal Skill 不只会调用 `read_webpage`，也会对“搜索/发现”任务选择：

```text
search_web
backend=browser
```

且不走 Brave / Firecrawl。

## 5.2 前置条件

- 4GPT 已连接；
- Chrome Extension connected；
- Personal Skill 已同步最新 `SKILL.md` / `openai.yaml`；
- 测试前记下 `logs/audit.jsonl` 最后时间戳。

## 5.3 推荐 prompt

显式选择 `local web access` Skill，然后发送：

```text
请用我本机 Chrome 搜索 MCP4ChatGPT GitHub，只使用 4GPT 的 search_web browser backend。告诉我实际 backend，并列出前 3 个搜索结果；不要改用云端搜索 API。
```

英文备用：

```text
Use my local Chrome through 4GPT to search for MCP4ChatGPT GitHub. Use search_web with backend=browser only, report the actual backend, and return the first three results. Do not fall back to a cloud search API.
```

## 5.4 PASS 条件

必须同时成立：

- ChatGPT 调用了 4GPT `search_web`；
- arguments 中 backend 为 `browser`，或服务端结果明确证明 browser route；
- 审计 `channel=web`；
- 实际 backend 为 browser；
- 没有 Brave / Firecrawl fallback；
- 返回至少一个有效搜索结果，或者 browser search 正常执行后明确返回“无结果”；不能拿 cloud result 代替。

## 5.5 FAIL

以下任一项为 FAIL：

- ChatGPT 用内建 Web search 而没有调用 4GPT；
- 调用了 `search_web(backend=auto)`；
- browser 失败后静默走 API；
- 把 `capability_search` 当互联网搜索。

---

# 6. E2E-02：真实 Chrome 登录态

## 6.1 目的

证明链路实际复用了**当前用户 Chrome session**，而不是匿名网页抓取。

优先使用受控、非敏感测试站点。若本机测试服务仍可用，优先使用：

```text
http://127.0.0.1:8767/private
```

历史测试站点约定：

- 无 Cookie：显示登录页；
- 当前本机 Chrome 已登录：正文含非敏感标记
  `AUTHENTICATED_LOCAL_SESSION_2026`。

开工必须先验证这个 fixture 仍然存在；如果不存在，不要假造结果。可以改用用户认可的其他非敏感登录态页面。

## 6.2 推荐 prompt

显式 Personal Skill：

```text
请用我本机 Chrome 的当前登录态，通过 4GPT read_webpage 读取 http://127.0.0.1:8767/private 。只报告 status、final URL，以及是否看到了 AUTHENTICATED_LOCAL_SESSION_2026；不要输出 Cookie 或其他认证信息。
```

## 6.3 PASS 条件

- `tool=read_webpage`；
- `backend=browser`；
- `read_status=ok`；
- 返回正文中确实有 `AUTHENTICATED_LOCAL_SESSION_2026`；
- 没有输出 Cookie、Authorization、session token；
- 同一 URL 的匿名/无 Cookie 行为与登录态结果可区分。

如果用户当前 session 本来未登录，则此 case 不能标 PASS；应转到 E2E-03 的 `login_required` 验收。

---

# 7. E2E-03：login_required

## 7.1 目的

验证浏览器成功到达登录表面时，不会把登录页当成正常正文。

## 7.2 推荐目标

优先使用受控 fixture：

```text
http://127.0.0.1:8767/private
```

但只有在**可安全获得无登录态页面**时使用。

不要为了测试主动删除用户真实网站 Cookie。

如果本地 fixture 支持独立无认证 endpoint，例如 `/login`，优先使用它。

## 7.3 PASS 条件

- `backend=browser`；
- `status=login_required`；
- evidence 包含明确 login signal；
- ChatGPT 不把登录页文本当目标正文引用；
- 不要求用户在聊天中发送密码；
- 不自动填写凭据。

## 7.4 安全边界

除非用户另行明确要求，本 handoff 不授权：

- 删除 Chrome Cookie；
- 登出用户真实账号；
- 输入密码；
- 更改 MFA；
- 导出 session / Cookie。

无法安全构造无登录态时，标记 BLOCKED，并保留单元/集成测试作为非 E2E 证据。

---

# 8. E2E-04：empty

## 8.1 目的

验证浏览器到达页面，但有效正文低于 P2 阈值时返回 `empty`，而不是 `ok`。

## 8.2 测试目标

优先使用**受控 fixture**，返回：

- 正常 HTML；
- 标题可有；
- body 可见文字少于当前 meaningful threshold；
- 不包含 login/challenge/block 特征。

如果当前 127.0.0.1 测试服务没有 `/empty`：

- 可以给测试 fixture 增加 `/empty`；
- 不要改 production `web_read_status` 阈值来制造 PASS；
- 不要选择不稳定的第三方空白页面。

## 8.3 PASS 条件

- `status=empty`；
- `backend=browser`；
- evidence 含 `text_below_meaningful_threshold`；
- ChatGPT 明确说“到达页面但没有可靠正文”，不能声称正文读取成功。

---

# 9. E2E-05：timeout

## 9.1 目的

验证真实 browser path 超时时返回结构化 `timeout`，不走云 fallback。

## 9.2 推荐方法

优先受控 local fixture，例如 `/slow`：

- 请求可建立；
- 页面响应/读取时间超过 browser command timeout；
- 测试结束后可恢复；
- 不修改 production timeout 只为让测试容易通过。

如果没有这种 fixture，可以新增测试 fixture endpoint。

## 9.3 PASS 条件

- `status=timeout`；
- `backend=browser`；
- `fallback_used=false`；
- audit `read_status=timeout`；
- 没有 Brave / Firecrawl / cloud scrape 调用；
- ChatGPT 不把 timeout 描述为 access_blocked。

---

# 10. E2E-06：unavailable

## 10.1 目的

验证 Extension / bridge 不可用时，返回结构化 `unavailable`。

## 10.2 安全限制

**不要未经用户明确同意停止当前正在使用的 Chrome Extension、MCP 服务或 bridge。**

如果用户允许做短暂故障注入：

1. 只关闭最小必要组件；
2. 记录关闭前状态；
3. 执行一次显式 Skill `read_webpage`；
4. 立即恢复；
5. 验证恢复后一次正常 `ok`。

如果当前环境不适合中断服务，则：

- 保留现有 unit/integration test 证据；
- E2E-06 标记 BLOCKED；
- 不伪造真实故障注入 PASS。

## 10.3 PASS 条件

- `status=unavailable`；
- evidence 是 extension not connected / bridge not running / queue unavailable 等实际运行时信号；
- 不 fallback cloud；
- 恢复组件后再次 `read_webpage` 能回到正常状态。

---

# 11. E2E-07：challenge / access_blocked 回归确认

这两类已有历史真实审计证据，但本轮至少确认 contract 没退化。

优先使用现有受控 fixture：

```text
http://127.0.0.1:8767/challenge
http://127.0.0.1:8767/blocked
```

如果 fixture 仍存在：

### challenge PASS

- `status=challenge`；
- evidence 有安全验证/人机验证 signal；
- ChatGPT 明确说这是 challenge；
- **不绕过**。

### access_blocked PASS

- `status=access_blocked`；
- evidence 有页面阻断 signal；
- 只描述页面观测到的阻断；
- **不推断 GFW、审查、国家/地区原因**，除非有独立证据。

---

# 12. E2E-08：ordinary web should-not-activate

## 12.1 目的

验证 negative boundary：普通网页研究在用户没有要求本机 Chrome、登录态或 4GPT 时，不应被 Personal Skill 抢占。

这是对 implicit invocation 精度的测试，不是“每次都必须完全相同”的确定性协议测试。

## 12.2 测试方式

新建干净 ChatGPT 对话：

- 不显式选择 `local web access` Skill；
- 不手动选择 4GPT；
- 不写 “本机 Chrome / 4GPT / read_webpage / 当前登录态”；
- 使用普通可公开检索问题。

示例：

```text
请查一下 Python 官方文档里 pathlib.Path.read_text 的用途，并给我一个简短例子。
```

测试前后比较 `logs/audit.jsonl`。

## 12.3 PASS 条件

- ChatGPT 可以用正常 Web、已有知识或其他合适来源；
- 在测试时间窗口内 **没有新增 4GPT local-web tool_call**；
- 尤其没有 `search_web/read_webpage` 调用。

## 12.4 解释边界

如果偶发触发 Personal Skill：

- 记录为 activation false positive；
- 不立刻改工具路由；
- 优先调整 Skill description / negative boundary；
- 至少重复 3 次独立新对话判断是否是持续问题。

不要承诺 implicit invocation 100% deterministic。

---

# 13. E2E-09：implicit positive trigger

## 13.1 目的

验证强化后的中英文 description 是否提高“用户没有显式点 Skill，但明确要求本机路径”时的命中率。

建议至少测试 4 种表达，每种独立新对话：

```text
A. 请用我本机 Chrome 读取 https://example.com/
B. 请用 4GPT 的 read_webpage 看 https://example.com/
C. 这个网页云端打不开，请改用我的本地浏览器读取 https://example.com/
D. 请用我当前 Chrome 登录态读取这个页面：<非敏感测试 URL>
```

## 13.2 记录方式

每条记录：

```text
prompt
是否自动加载 local-web-access
是否自动调用 4GPT
实际 tool
audit event
status
```

这是**统计/体验验收**，不是 correctness gate。

建议结论使用：

```text
4/4
3/4
...
```

不要写“以后一定自动触发”。

---

# 14. E2E-10：fresh tab handle / stateful browser capability

## 14.1 目的

验证 Skill 中关于有状态浏览器操作的规则：

> 需要交互时，重新发现当前 capability，并取得新鲜 tab/page handle；不能猜 ID，也不能盲目复用旧 handle。

## 14.2 现有相关 capability

开工必须通过 catalog 再确认，不要直接假设名称永久存在。当前代码中可见：

```text
ext_connection_status
ext_list_tabs
ext_get_active_tab
ext_navigate
ext_get_dom
ext_click_element
ext_fill_input
...
```

特别是 `ext_fill_input` 的 schema 明确要求：

```text
tab_id = exact target tab ID, normally returned by ext_navigate or ext_list_tabs
```

## 14.3 推荐测试

使用**无破坏性的本地测试页面**，不要拿用户真实账号表单做验收。

流程：

1. `capability_search` 搜索 browser tab / navigate；
2. `capability_get` 取得当前 capability schema 与 revision；
3. 调 `ext_list_tabs` 或 `ext_navigate` 获得当前真实 `tab_id`；
4. 保存 handle A；
5. 打开/导航到另一个受控测试页，获得 handle B；
6. 再次 `ext_list_tabs` / `ext_get_active_tab` 验证当前状态；
7. 后续交互使用**当前返回的 handle**；
8. 不根据数组位置、URL 猜 tab id；
9. 如果要测试 stale handle，必须只在测试 tab 上做，且预期是安全失败或明确 target mismatch，不能漂移到另一活动 tab。

## 14.4 PASS 条件

- 所有 stateful 操作前有 discovery / list / navigate 证据；
- 使用的 tab_id 来自当前工具返回值；
- 没有硬编码或猜测 ID；
- stale/错误 ID 不会操作到其他 tab；
- 后续动作返回的 backend/capability identity 与目标一致。

---

# 15. 自动化仓库回归

真实 E2E 前后都至少运行：

```text
uv run pytest -q tests/test_personal_skill.py tests/test_skill_resources.py tests/test_skill_protocol.py tests/test_local_web_entry.py tests/test_web_read_status.py
```

当前基线：

```text
36 passed
```

完整 suite：

```text
uv run pytest -q
```

当前基线：

```text
403 passed, 11 skipped, 5 warnings
```

5 条 warning 为既有 PyMuPDF/SWIG `DeprecationWarning`。

如果测试数量变化，要报告新数量，不照抄本文。

---

# 16. 何时允许改代码

默认先验收，不先重构。

只有出现以下情况才进入实现修复：

### A. Personal Skill 没调用正确高层工具

优先检查：

```text
chatgpt_skills/local-web-access/SKILL.md
agents/openai.yaml
compact tools/list
tool descriptions
```

不要先改 Extension。

### B. status 分类错误

只在有可重复 fixture 时修改：

```text
src/mcp4chatgpt/web_read_status.py
```

每个修复必须添加 regression test，避免靠真实网页关键词拍脑袋。

### C. browser-only 路由发生 cloud fallback

这是 correctness bug，优先修：

```text
src/mcp4chatgpt/tools.py
src/mcp4chatgpt/browser_search.py
```

修复后必须证明：

```text
backend=browser
fallback_used=false
```

### D. implicit activation 不理想

优先改 Skill metadata / description，不改 MCP server 路由。

### E. fresh-handle 问题

先检查 capability schema、tab identity 与 Extension 实现，不允许通过“永远操作 active tab”来掩盖 handle bug。

---

# 17. 禁止事项

本任务禁止：

1. 为了让测试通过，把 `readOnlyHint` 错标为 true；当前实现会创建/关闭临时 Chrome 标签页，保留保守 annotation。
2. 把 `access_blocked` 写成 GFW / censorship 根因判断。
3. 绕过 CAPTCHA / challenge。
4. 从聊天中索取或记录密码、Cookie、OAuth bearer、session token。
5. 为测试删除用户真实 Cookie、退出真实账号或改变 MFA。
6. 在 `read_webpage` 失败后静默调用 Brave / Firecrawl。
7. 用 `capability_search` 代替互联网搜索。
8. 猜测 tab/page handle。
9. 仅凭 HTTP 200 或 MCP tool-call `ok=true` 判定网页读取成功。
10. 没有真实 ChatGPT 调用时声称 Personal Skill E2E PASS。
11. 因公开 Scan Tools 身份验证受阻而回退去重做 P3；P4A Personal Skill 是当前主线。
12. 未经用户明确同意故障注入正在使用的 Extension/bridge/MCP 服务。

---

# 18. 建议执行顺序

按风险从低到高：

```text
Step 1  仓库 focused/full tests
Step 2  E2E-01 browser search
Step 3  E2E-08 ordinary-web negative
Step 4  E2E-09 implicit positive matrix
Step 5  E2E-02 logged-in session
Step 6  E2E-03 login_required
Step 7  E2E-04 empty
Step 8  E2E-07 challenge/access_blocked
Step 9  E2E-10 fresh handle
Step 10 E2E-05 timeout
Step 11 E2E-06 unavailable（仅用户批准故障注入时）
```

先完成不会破坏运行状态的测试，再做 timeout/unavailable。

---

# 19. 验收结果表

执行时更新本文或新建 dated acceptance record：

| Case | 场景 | 真实 ChatGPT | Audit | 结果 |
|---|---|---:|---:|---|
| E2E-01 | Personal Skill browser search | required | required | TODO |
| E2E-02 | current Chrome login state | required | required | TODO |
| E2E-03 | login_required | required if safely reproducible | required | TODO |
| E2E-04 | empty | required | required | TODO |
| E2E-05 | timeout | required if controlled fixture available | required | TODO |
| E2E-06 | unavailable | user approval required | required | TODO |
| E2E-07 | challenge/access_blocked | required | required | TODO |
| E2E-08 | ordinary web should-not-activate | required | audit absence required | TODO |
| E2E-09 | implicit positive trigger | required | required on hits | TODO |
| E2E-10 | fresh tab handle | required for stateful flow | required | TODO |

“BLOCKED” 是合法结果，只要明确缺少什么前置条件。

---

# 20. 完成定义（Definition of Done）

P4A 不要求 implicit invocation 100% 命中。

**必须完成：**

- E2E-01 PASS；
- E2E-02 PASS，或用户明确没有可用的安全登录态 fixture 并记录 BLOCKED；
- E2E-04 PASS；
- E2E-07 PASS；
- E2E-08 至少 3 次独立新对话，无持续 false-positive；
- E2E-09 给出真实命中率，不做确定性承诺；
- E2E-10 PASS；
- focused/full pytest 保持通过；
- docs 记录真实证据与未解决问题；
- 所有代码修复都有 regression test。

**条件完成：**

- E2E-03 / 05 / 06 若缺少安全 fixture 或用户未授权故障注入，可以 BLOCKED，但必须保留原因和现有非 E2E 测试证据。

---

# 21. 最终报告格式

Codex 最终回复必须包含：

## 已验证

逐项列出 PASS case，以及：

```text
ChatGPT invocation mode
actual MCP tool
actual backend
actual read_status
audit evidence
```

不要贴敏感正文。

## 未验证 / BLOCKED

明确写：

- 缺什么前置条件；
- 为什么不能安全验证；
- 哪些 unit/integration test 仍覆盖了底层契约。

## 发现的问题

按：

```text
P0 correctness
P1 reliability
P2 UX / implicit activation
```

分类。

## 修改

如果改代码：

- 文件；
- 原因；
- regression test；
- 是否改变 Skill contract / MCP protocol。

## 测试

写实际命令和实际数字。

## Git

若形成提交：

- commit hash；
- commit message；
- worktree clean/dirty；
- 不要 merge/push，除非用户另行要求。

---

# 22. 提交策略

建议把“验收记录”和“必要修复”分开：

1. 若只有文档/验收记录：
   ```text
   Record Personal Skill P4A E2E acceptance
   ```

2. 若发现代码 bug：
   - 先单独修 bug + regression tests；
   - 再单独提交 E2E acceptance record。

不要把无关 browser 重构、P5 下游 MCP 暴露或公开 plugin submission 工作混进本轮。

---

## 23. 当前明确不做

本 handoff 不处理：

- P5“再向下暴露给其他 MCP”；
- public plugin submission；
- developer/business identity verification；
- 将 MCP server Skill 自动同步到 Personal Skill Editor；
- 自动绕过 challenge；
- 改写 ChatGPT 内建 Web planner；
- 把 implicit invocation 变成服务端强制 fallback。

当前产品目标保持：

```text
explicit Personal Skill → connected 4GPT → local Chrome
```

这条路径必须可靠、可证据化；implicit invocation 只做可测量的体验优化。
