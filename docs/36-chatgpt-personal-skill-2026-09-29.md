# ChatGPT 个人 Skill 安装通路（无需开发者身份验证）

日期：2026-09-29。此通路已在真实 ChatGPT 对话中验证。

## 用途与前提

`chatgpt_skills/local-web-access/` 是可在 ChatGPT Skill 编辑器中创建的个人 Skill 文件。它教 ChatGPT 在需要本机 Chrome 会话读取网页时调用已连接的 4GPT MCP 工具。此通路不经过公开插件提交入口的 Scan Tools，也不要求该入口的开发者身份验证。

仍需登录 ChatGPT、保持 MCP4ChatGPT 服务及 Chrome 扩展运行，并在 ChatGPT 中连接和授权 4GPT。Skill 的 MCP 依赖声明指向本项目当前公网地址 `https://mcp.runzhe.uk/mcp`；使用其他部署时，应先修改 `agents/openai.yaml` 中的 `url`。

## 安装

1. 在 ChatGPT 中先确认 4GPT 插件已连接，并能在对话中调用 `read_webpage`。服务或工具有变更时，在插件页面刷新工具。
2. 打开 [ChatGPT Skill 编辑器](https://chatgpt.com/skills/editor)，新建 Skill。名称和描述分别填写本项目 [SKILL.md](../chatgpt_skills/local-web-access/SKILL.md) 顶部 frontmatter 中的 `name` 和 `description`；“说明”编辑区只粘贴第二个 `---` 之后的正文，避免把 frontmatter 重复放入正文。
3. 在编辑器的 `agents/openai.yaml` 中填入本项目 [openai.yaml](../chatgpt_skills/local-web-access/agents/openai.yaml) 的内容，然后保存。当前仓库使用官方 schema 的 `policy.products: [CHAT]`，并提供一个 `default_prompt` 用于快速验收本机读取链路。
4. 打开 [ChatGPT Skills](https://chatgpt.com/skills)，确认 `local web access` 同时出现在“已安装”和“由我创建”。
5. 在 Skill 卡片点击“在聊天中试用”，保留输入框里的 `local web access` Skill 标签。编辑器若加载了仓库提供的 `default_prompt`，可直接用该提示进行验收；否则输入：`请用我本机 Chrome 的 4GPT read_webpage 读取 https://example.com/ ，报告返回的 status 和正文。` 发送时无需手动选择插件。应看到 `status=ok` 和 Example Domain 正文，并在服务审计日志 `logs/audit.jsonl` 中看到新的 `tool=read_webpage`、`backend=browser`、`read_status=ok` 记录。

普通新对话中的隐式触发仍属于 best-effort：一次只写“本机 Chrome”的复测被 ChatGPT 改用云浏览器，服务审计日志没有对应调用。当前仓库已在 Skill description 中加入“本机 Chrome / 当前登录态 / 4GPT / read_webpage / cloud web failed”等中英文触发意图，并明确普通网页研究在云端路径正常时不应激活本 Skill，以提高召回同时减少误触发。若需要可靠地指定此路径，仍应显式选用 Skill；若仍未调用 4GPT，再在对话的插件菜单中选中 4GPT，并检查连接状态。不要只凭回答中出现页面内容判断本机 MCP 已被使用。

这份文件是可复用的安装源。修改它后，需要在个人 Skill 编辑器中同步并保存；MCP 服务端 `skills/list` 的内容更新或插件的“刷新工具”不会自动改写已经创建的个人 Skill。

## 与 MCP 导入通路的关系

现有服务端 Skill 资源位于 `src/mcp4chatgpt/skill_resources.py`，可通过 `skills/list`、`skills/get` 和 `resources/read` 供公开插件提交流程导入。公开提交入口的 Scan Tools 目前会要求开发者身份验证；个人 Skill 编辑器的创建和使用不依赖该步骤。两条通路可以并存，但各自保存一份 Skill 内容。

个人 Skill 没有替代 MCP 连接及其 OAuth 授权；它只是为已连接的工具提供触发条件和使用步骤。[OpenAI Skill 文档](https://developers.openai.com/plugins/build/skills)说明了 `agents/openai.yaml` 中的 MCP 依赖格式。

## 2026-09-29 实测

- 已在 [Skill 页面](https://chatgpt.com/skills?skill_id=6aba9a0576e88191b012fe66442a1d84) 创建并安装 `local-web-access`，保存了 MCP 依赖声明。
- [直接试用对话](https://chatgpt.com/c/6aba9a43-dde8-83e9-9d8d-4717441635d0) 在显式选择 4GPT 时返回 `read_webpage.status=ok`。
- [新对话](https://chatgpt.com/c/6aba9afc-79dc-83ea-9427-cfc620b56232) 没有手动选择 4GPT，仍调用了 `read_webpage`；响应给出 `ok`、`https://example.com/` 和页面正文。服务审计日志记录 `tool=read_webpage`、`backend=browser`、`read_status=ok`。
- 将仓库中的 `agents/openai.yaml` 保存到编辑器后，[显式 Skill 标签对话](https://chatgpt.com/c/6abac6c7-fbcc-83e9-ba28-2c875c22a82e) 在没有手动选择插件的情况下再次调用 `read_webpage`，审计日志为 `read_status=ok`。
- 同一配置下，一次没有 Skill 标签且没有提到 `read_webpage` 的普通新对话使用了云浏览器，审计日志没有 4GPT 调用。因此不能承诺隐式触发每次都会选中本机工具。

这验证了当前账号和连接的可用路径；其他 ChatGPT 账号或部署仍应按上面的步骤单独检查。


## 2026-09-29 Personal Skill 收尾

当前主线分为两个层级：

- **可靠路径**：显式选择 `local web access` Skill。Skill dependency 负责让已连接的 4GPT 工具可用，Skill 正文规定 `read_webpage` / `search_web(backend=browser)` 的使用和结果判断。
- **便利路径**：普通新对话中的 implicit invocation。仓库保持 `allow_implicit_invocation: true` 并强化中英文触发描述，但不把自动命中率当作正确性保证。

仓库新增 `tests/test_personal_skill.py`，自动检查：

- Personal Skill frontmatter 的名称和关键触发意图；
- `agents/openai.yaml` 使用官方 `CHAT` 产品枚举；
- `default_prompt`、implicit invocation 和 MCP dependency 基本契约；
- Personal Skill 与服务端 Skill 对 `read_webpage`、`search_web(backend=browser)`、challenge/access-blocked 及 ordinary-web negative boundary 的核心规则保持一致。

高层 `search_web` / `read_webpage` 继续采用保守的非只读 annotations：虽然业务语义是检索，但实现会创建并关闭临时 Chrome 标签页；`openWorldHint=true` 继续表示它们访问开放互联网。这里优先保证 metadata 与实际副作用一致，而不是通过错误的只读标记降低确认摩擦。

仍需真实会话补齐的验收矩阵：

1. Personal Skill → `search_web(backend=browser)`；
2. 只有当前 Chrome 登录态才能读取的非敏感页面；
3. `login_required` / `empty` / `timeout` / `unavailable`；
4. ordinary web research 的 should-not-activate negative case；
5. 有状态浏览器操作时重新发现 capability 与新鲜 page/tab handle。

这些未完成项不阻塞“显式 Personal Skill → 4GPT → 本机 Chrome”的现有可用路径，但决定 implicit invocation 与异常场景的最终可靠性。

本次仓库级验证：Personal Skill / Skill protocol / local-web focused suite **36 passed**；完整 Python suite **403 passed, 11 skipped, 5 warnings**。5 条 warning 仍为既有 PyMuPDF/SWIG `DeprecationWarning`。
