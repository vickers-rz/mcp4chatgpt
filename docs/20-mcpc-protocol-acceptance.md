# mcpc 协议验收（2026-09-26）

## 目标与固定依赖

使用 Apify mcpc 0.7.0 作为 MCP 外部客户端，独立验证本项目 HTTP transport。版本锁定在 `tests/mcp_protocol/package.json` 与 lockfile。该版本要求 Node.js >=22.12.0。官方配置源码确认 `MCPC_HOME_DIR` 可隔离 sessions、bridge 和 logs；自动化使用 pytest 临时目录，并不改写用户主目录、钥匙串或现有会话。

## 自动化验收

```sh
npm ci --prefix tests/mcp_protocol
MCP_MCPC_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mcpc_acceptance.py
```

测试只启动随机端口 loopback 服务，computer off，允许文件根和数据目录均在 pytest 临时目录。配置含临时 bearer 的 mcpc JSON 文件权限为 0600；mcpc home 单独设为 0700。通过 `finally` 关闭 session 和测试服务。正常 pytest 不下载 Node 依赖或联网。

自动化验证连接发现、会话调用、工具表、schema、server_info、schema resource、无效 bearer 和 session 清理。认证使用测试代码签发的临时 bearer，因此此项只验 MCP bearer transport，不代表 mcpc OAuth browser login 验收通过。

## OAuth 人工步骤

在专用临时 mcpc home 中，针对本机测试 connector 手动运行 `mcpc login <test-server>`，完成 OAuth 浏览器授权；然后连接并调用 `server_info`，最后关闭会话并清理该临时 home。确认 authorization code 使用 PKCE、token exchange 成功且 access token 未出现在 shell history、测试产物或日志中。现有项目 HTTP OAuth 单元/集成测试另外覆盖 DCR、授权码及 PKCE。该人工流程不得对正式账号或正式服务执行，除非另有明确的发布验收安排。

## 限制

此工具用于服务外部协议测试，不在 MCP4ChatGPT 内部通过 `local_run_command` 回调自身。mcpc 的 CLI 会验证 JSON 参数；畸形 JSON-RPC 与缺参数测试仍由原始 HTTP 回归覆盖。当前服务 `listChanged=false`、prompts 为空且没有独立 SSE 流，因此不是本批必验能力。`local_job_*` 是业务工具，不代表 MCP Tasks。
