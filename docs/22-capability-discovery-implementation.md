# mcpc 与渐进式工具发现实施验收（2026-09-26）

## Plan

基线为 `ff429a2`，实现固定版 mcpc 外部协议测试、共享能力目录、full/compact 列表和第三阶段编排设计。正式 connector 配置、生产进程和线上服务均未变动。

## Do

- 第一批提交 `c264217`：固定 `@apify/mcpc@0.7.0`，Node 要求 >=22.12.0；项目测试独立 package-lock，自动测试通过 `MCPC_HOME_DIR` 指向 pytest 临时目录隔离 bridge/session/log。仅用临时签发 bearer。
- 第二批提交：registry 暴露 `capability_search/get/call`，共用本地及过滤后的下游定义；调用校验 JSON Schema（遵循 schema dialect，阻止外部普通和 dynamic/recursive 引用），只通过原 handler 执行一次并保留原 MCP 内容及审计身份。增加 `MCP_TOOL_EXPOSURE=full|compact`，默认 full。compact 仍接受隐藏旧工具直接调用，目录并非授权边界。
- 第三批设计：`21-capability-orchestration-design.md`，说明后续隔离执行器的拓扑、限制、只读试点与 runtime 评估准入；本次没有添加 execute 接口或执行器。

## Check

mcpc 命令：

```sh
npm ci --prefix tests/mcp_protocol
MCP_MCPC_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mcpc_acceptance.py
```

外部客户端自动验收分别在 full、compact 下通过。覆盖 session、服务发现、目录和 schema、server_info、普通文件读取、资源读取、下游 image/resource_link/structuredContent 保真、能力 search→get→call、无效 bearer、测试服务停止后的旧 session 失败、重启测试服务后的显式重连，以及会话清理。结果为 `2 passed`。mcpc 使用临时 bearer，不等于 OAuth 浏览器授权实测；项目现有 HTTP 测试覆盖 DCR、授权码与 PKCE。原始 HTTP 回归覆盖 mcpc CLI 不便生成的畸形和缺参请求。

在同一序列化及无下游定义环境下测量 UTF-8 JSON 工具表：

| computer mode | full 工具数 / bytes | compact 工具数 / bytes | 字节减少 |
|---|---:|---:|---:|
| off | 76 / 49,568 | 4 / 2,100 | 95.8% |
| observe | 83 / 54,803 | 11 / 7,335 | 86.6% |
| interact | 93 / 63,337 | 21 / 15,869 | 74.9% |

这些是序列化字节，不等同 token 数。验证结果：

- 定向能力与 server 协议测试：27 passed；mcpc full/compact 外部集成：2 passed。
- 完整 pytest：266 passed、7 skipped，25.05 秒。7 个跳过项为需显式开启的 5 个 GUI 测试和 2 个 mcpc 外部客户端模式。
- native GUI 实测单独串行运行：4 passed，4.80 秒；真实 CUA fixture 单独串行运行：1 passed，4.46 秒。
- `git diff --check` 与 `uv lock --check --offline` 通过。PyMuPDF 的 5 条 SWIG deprecation warning 仍存在。

## Act 与边界

配置默认 full，回退只需移除或设为 `MCP_TOOL_EXPOSURE=full`，无需迁移数据；配置生效需重新启动所测服务。自动 mcpc home 与凭据均为临时目录。OAuth 浏览器人工验收、ChatGPT connector、正式服务切换未执行；不能将本次协议验收称作生产发布通过。
