# P4A Personal Skill 真实 E2E 验收记录

日期：2026-09-29（Asia/Shanghai）
基线：`4490cf1`，仅比 handoff 指定的 `645aa9a` 多交接文档；开工时工作树干净。
执行环境：ChatGPT 网页版、已安装 `local web access` Personal Skill、已连接 4GPT、当前 Chrome Extension；服务端审计为 `logs/audit.jsonl`。
验收 fixture：`scripts/p4a_e2e_fixture.py`，绑定 `127.0.0.1:8767`，所有正文和 Cookie 都是合成数据。

## 前置核对

- 开工时已检查 `git status`、`git log -8`；根目录与相关子目录无适用 `AGENTS.md`。未 reset/clean/checkout 用户工作。
- ChatGPT 中安装的 Skill 曾落后于仓库：描述缺少中文触发词及 ordinary-web 负边界，`agents/openai.yaml` 的 products 仍为旧配置。已通过 ChatGPT Skill 编辑器同步仓库的描述、正文行为和 `products: [CHAT]`、MCP dependency；保留编辑器中的 icon 引用。保存后重新打开详情核对了新内容。仓库源文件本身未改。
- 历史 `127.0.0.1:8767` fixture 当时未运行；已建立当前受控 fixture。匿名 `curl /private` 返回登录页，不含标记；同一 Chrome 会话通过 `/seed` 获取合成 Cookie 后，`/private` 显示 `AUTHENTICATED_LOCAL_SESSION_2026`。未读取或记录真实站点 Cookie。
- 审计没有 URL/参数明文，下面用 ChatGPT 对话 URL、执行顺序和紧邻的审计时间戳关联请求。审计 `ts` 是 Unix 秒；`ok=true` 只代表工具调用完成，网页是否成功以 `read_status` 判断。

## 结果概览

| Case | ChatGPT 对话与调用方式 | 实际 MCP 工具 / backend / read_status | Audit `ts` | 结论 |
|---|---|---|---:|---|
| E2E-01 | [显式 Skill 搜索](https://chatgpt.com/c/6abad00b-731c-83ea-87c1-1437b78a2013) | `search_web` / `browser` / 不适用 | 1790627872.708721 | PASS |
| E2E-02 | [显式 Skill 登录态](https://chatgpt.com/c/6abad115-fc00-83ea-85fe-d9ef3bdc8eb0) | `read_webpage` / `browser` / `ok` | 1790628134.097114 | PASS |
| E2E-03 | [显式 Skill 状态矩阵](https://chatgpt.com/c/6abad13a-8fb8-83e9-8b5b-fea77b8ea7b2) | `read_webpage` / `browser` / `login_required` | 1790628193.519082 | PASS |
| E2E-04 | 同上 | `read_webpage` / `browser` / `empty` | 1790628192.418523 | PASS |
| E2E-05 | [显式 Skill 慢响应](https://chatgpt.com/c/6abad182-0d70-83ea-896b-69835b997e8c) | `read_webpage` / `browser` / `timeout` | 1790628263.446559 | PASS |
| E2E-06 | 未做运行中组件故障注入 | 无新 E2E 调用 | — | BLOCKED |
| E2E-07 | [显式 Skill 状态矩阵](https://chatgpt.com/c/6abad13a-8fb8-83e9-8b5b-fea77b8ea7b2) | `read_webpage` / `browser` / `challenge`、`access_blocked` | 1790628194.977754、1790628196.288332 | PASS |
| E2E-08 | 三次独立普通 Chat 对话，无 Skill/4GPT 选择 | 测试窗口无新增 4GPT 调用 | 1790627872.709390 后至 1790628005.274475 前 | PASS（3/3） |
| E2E-09 | 四次独立普通 Chat 对话，无显式 Skill 选择 | `read_webpage` / `browser` / `ok`，四次 | 1790628005.943646、1790628037.497766、1790628058.723616、1790628082.492467 | 4/4 工具命中 |
| E2E-10 | [显式 Skill tab-handle 流程](https://chatgpt.com/c/6abad1c4-aef8-83e9-b1e7-7a1a671c2831) | `capability_search/get`、`ext_navigate`、`ext_list_tabs`、`ext_fill_input`；browser Extension | 1790628311.315125–1790628337.489961 | PASS（主流程） |

## 每项可复核证据

### E2E-01

Prompt：`请用我本机 Chrome 搜索 MCP4ChatGPT GitHub，只使用 4GPT 的 search_web browser backend。告诉我实际 backend，并列出前 3 个搜索结果；不要改用云端搜索 API。`

ChatGPT 用户消息保留 `@local web access` 标签；界面回答 `backend=browser`、`engine=chrome_bing` 并列出三条结果。审计 `channel=web`、`requested_backend=browser`、`backend=browser`；测试窗口无 Brave/Firecrawl 调用。

### E2E-02

Prompt：`请用我本机 Chrome 的当前登录态，通过 4GPT read_webpage 读取 http://127.0.0.1:8767/private 。只报告 status、final URL，以及是否看到了 AUTHENTICATED_LOCAL_SESSION_2026；不要输出 Cookie 或其他认证信息。`

显式 `@local web access` 对话返回 `status=ok`、final URL 为 `/private`、标记“是”。匿名 curl 同 URL 返回 `Sign in` 页面；Chrome `/seed` 后同 URL 显示合成标记。审计 `tool=read_webpage`、`channel=web`、`backend=browser`、`read_status=ok`。没有输出 Cookie、Authorization 或 token。

### E2E-03、04、07

共同 prompt：`请通过 4GPT read_webpage，分别读取这四个本机受控测试 URL：http://127.0.0.1:8767/login、http://127.0.0.1:8767/empty、http://127.0.0.1:8767/challenge、http://127.0.0.1:8767/blocked。逐项报告实际 status、backend 和 evidence.signals；不要把登录页或空白页当正文，不要尝试绕过验证，也不要推测页面阻断的地理原因。`

用户消息保留显式 Skill 标签。ChatGPT 表格给出：

| Fixture | status | `evidence.signals` 关键值 | ChatGPT 行为 |
|---|---|---|---|
| `/login` | `login_required` | `url:login_path`、`title:sign in` | 未引用为目标正文，未索要凭据 |
| `/empty` | `empty` | `text_below_meaningful_threshold:80` | 明确为空，未声称成功读取正文 |
| `/challenge` | `challenge` | `title:security verification`、`text:verify you are human` | 未绕过 |
| `/blocked` | `access_blocked` | `title:access denied`、`text:request blocked` | 只描述页面观测，未推断地理/审查原因 |

四次审计均为 `tool=read_webpage`、`channel=web`、`backend=browser`，`read_status` 分别与表一致。

### E2E-05

Prompt：`请仅用 4GPT read_webpage 的本机 Chrome browser 路径读取这个受控慢速测试页：http://127.0.0.1:8767/slow 。这个页面会等待 45 秒。请报告工具实际返回的 status、backend、fallback_used 和 evidence.signals；不要调用云端替代来源，也不要把 timeout 解释为 access_blocked。`

ChatGPT 返回 `status=timeout`、`backend=browser`、`fallback_used=false`、`evidence.signals=["browser_timeout"]`。审计 `read_status=timeout`、`retrieval_ok=false`；没有云端后备调用。Fixture 没有修改 production timeout。

### E2E-06

当前 Extension/bridge 和 MCP 服务正供本轮及用户使用。交接文档第 10/17 节要求先取得用户明确同意，才能暂时停用组件做故障注入；本轮未取得这项授权，因此未中断运行中组件，也没有真实 ChatGPT `unavailable` 证据。结论为 BLOCKED。底层契约仍由 `tests/test_web_read_status.py`、`tests/test_local_web_entry.py` 的非 E2E 测试覆盖；这些测试不能替代本项 PASS。

### E2E-08

三个独立新 Chat 对话均使用同一普通公开问题：`请查一下 Python 官方文档里 pathlib.Path.read_text 的用途，并给我一个简短例子。`

对话：[1](https://chatgpt.com/c/6abad055-c38c-83ea-b3b8-5fbdc8e05958)、[2](https://chatgpt.com/c/6abad074-30cc-83e9-be65-b65abda2033f)、[3](https://chatgpt.com/c/6abad087-bf98-83ea-a840-0179387e5d16)。三次均没有显式选择 Skill/4GPT，ChatGPT 使用普通公开文档来源。在第一条之前审计尾为 E2E-01 的 `1790627872.709390`；第三条之后审计尾仍未变化，直到随后 E2E-09 A 的新请求 `1790628005.274475`。因此这三次没有 4GPT local-web 调用。此结果是 3/3 样本，不表示隐式激活具有确定性。

### E2E-09

四次均为独立新 Chat 对话，没有显式选择 Skill；Prompt 与结果：

| 表达 | Prompt | ChatGPT 对话 | 4GPT 结果 |
|---|---|---|---|
| A | `请用我本机 Chrome 读取 https://example.com/ 。报告 read_webpage status 和页面标题。` | [对话](https://chatgpt.com/c/6abad09b-741c-83ea-b746-1653bdf23f0c) | `read_webpage/browser/ok` |
| B | `请用 4GPT 的 read_webpage 看 https://example.com/ 。只报告 status、backend 和标题。` | [对话](https://chatgpt.com/c/6abad0bc-75a4-83ea-9e09-c2021061135b) | `read_webpage/browser/ok` |
| C | `这个网页云端打不开，请改用我的本地浏览器读取 https://example.com/ 。报告实际 status 和 backend。` | [对话](https://chatgpt.com/c/6abad0d1-d6c4-83e9-ae49-cb76af40b1c6) | `read_webpage/browser/ok` |
| D | `请用我当前 Chrome 登录态读取这个非敏感测试页面：http://127.0.0.1:8767/private 。只报告 status、backend，以及是否看到 AUTHENTICATED_LOCAL_SESSION_2026；不要输出认证信息。` | [对话](https://chatgpt.com/c/6abad0e8-2b8c-83ea-8ef3-fdb3ef7ea377) | `read_webpage/browser/ok`，看到标记 |

可观察的 **4GPT 工具命中率为 4/4**。ChatGPT UI 没有独立显示“自动加载 Skill”的可验证事件，所以不能据此断言四次均自动加载了 Skill 指令；这里只统计 4GPT 工具调用，不承诺未来每次自动命中。

### E2E-10

Prompt：`请做一次无破坏性的 4GPT 本机 Chrome tab-handle 验收，目标只限 http://127.0.0.1:8767/form 这个合成测试页。先用 capability_search 发现 browser tab / navigate / fill 能力，再用 capability_get 核对 ext_navigate、ext_list_tabs、ext_fill_input 的当前 schema 和 revision。随后通过 capability_call 调 ext_navigate(new_tab=true) 打开 /form，记录返回的 tab_id A；再新开 http://127.0.0.1:8767/ 记录 tab_id B；调用 ext_list_tabs 重新确认 A、B 和各自 URL。只对新鲜列表中对应 /form 的 A 调 ext_fill_input，selector=#note，value=P4A_FRESH_HANDLE_TEST，submit=false。不要省略 tab_id，不要猜测 ID，也不要提交表单。报告每一步实际返回的 capability 名称、revision、tab_id、URL 和 filled 结果；若任一步失败，停止后续交互并报告失败。`

显式 Skill 对话先产生 4 次 `capability_search`、3 次 `capability_get`，审计随后依次为 `ext_navigate` 两次（`1790628325.033474`、`1790628329.647566`）、`ext_list_tabs`（`1790628333.112304`）、`ext_fill_input`（`1790628337.489961`）。工具返回 A=`59534703`→`/form`、B=`59534706`→`/`，重新列表核对后只对 A 填入，`filled=true`、`submitted=false`、`submit_attempted=false`。Chrome 页面独立检查 A 的 `#note` 值确为 `P4A_FRESH_HANDLE_TEST`。调用的 revision 与当次 `capability_get` 一致，未硬编码 tab ID。

附加安全失败 prompt：`继续同一个受控 fixture 的安全失败检查。先用 ext_list_tabs 再次确认 A=59534703 仍是 /form；然后只调用一次 ext_fill_input，显式传 tab_id=-1、selector=#note、value=SHOULD_NOT_APPEAR、submit=false。-1 是故意无效的测试 ID，不允许省略 tab_id 或改用当前活动标签；若工具拒绝，立即停止。报告实际错误，再核对 A 的值仍为 P4A_FRESH_HANDLE_TEST。`

审计 `1790628385.358689 ext_list_tabs ok=true` 后，`1790628389.417408 ext_fill_input ok=false`，`tabId` 最小值校验拒绝 `-1`。Chrome 页面再次独立显示 A 的原值，未漂移到 B 或活动标签。这个测试证明**非法 ID** 安全失败；没有关闭某个测试 tab 后再用其原 ID 做“已失效但格式合法”的更强测试。

## 发现的问题与边界

- **P0 correctness：**本轮没有发现 browser-only 路由静默云端 fallback、错误状态成功化或错误 tab 漂移。
- **P1 reliability：**附加安全失败测试中，ChatGPT 为核对 A 的值猜测调用了不存在的 `ext_read_page`；审计 `1790628392.739942` 为 `capability_not_found`。这违反了“新能力应先发现”的 Skill 指令；所幸没有状态变更。我随后通过 Chrome 页面独立核对了值。需要后续在 Skill 指令/模型规划上改善，但这不改变已完成的发现→新鲜 handle→定向填入主流程证据。
- **P2 UX / implicit activation：**四种正向表达的 4GPT 工具命中为 4/4，但 ChatGPT UI 不提供可验证的 Skill 自动加载标志；只能把它报告为工具命中率。普通公开问题 3/3 没有误触发。
- E2E-06 保持 BLOCKED；需要用户另行明确授权短暂故障注入后才能测试恢复。

## 回归测试与修改

- 验收前：focused `36 passed`；完整 `403 passed, 11 skipped, 5 warnings`。
- 验收后：focused `36 passed`；完整 `403 passed, 11 skipped, 5 warnings`。五条 warning 是既有 PyMuPDF/SWIG `DeprecationWarning`。
- 新增的 `scripts/p4a_e2e_fixture.py` 只提供本机合成 HTML 页面和合成 Cookie；没有更改 production Skill contract、MCP protocol、Web route 或分类阈值。ChatGPT 中的 Skill 配置已同步，仓库源文件未变。
- 验收结束后关闭了本轮创建的三个测试标签页，通过 fixture `/clear` 清除了本轮合成 Cookie，并停止了 fixture HTTP 服务；没有触碰真实网站登录态。
- 本轮没有 production 代码修复；因此无需相关 regression test。没有推送或合并。
