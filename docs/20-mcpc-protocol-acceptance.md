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

为人工 OAuth 验收启动一个隔离 loopback 实例，避免正式配置、下游 MCP、工作区和日志：

```sh
TEST_ROOT=$(mktemp -d)
printf 'Temporary test directory: %s\n' "$TEST_ROOT"
mkdir -p "$TEST_ROOT/root" "$TEST_ROOT/data"
export MCP_BIND_HOST=127.0.0.1 MCP_BIND_PORT=18766
export MCP_PUBLIC_BASE_URL=http://127.0.0.1:18766
export MCP_DATA_DIR="$TEST_ROOT/data" MCP_ALLOWED_ROOTS="$TEST_ROOT/root"
export KNOWLEDGE_ROOTS="$TEST_ROOT/root" MCP_AUDIT_LOG="$TEST_ROOT/audit.jsonl"
export MCP_COMPUTER_MODE=off MCP_TOOL_EXPOSURE=compact
export MCP_AUTH_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf 'Temporary approval secret: %s\n' "$MCP_AUTH_SECRET"
PYTHONPATH=src .venv/bin/python -c 'from mcp4chatgpt.config import load_config; from mcp4chatgpt.server import create_server; s=create_server(load_config()); s.serve_forever()'
```

在另一个终端把上面显示的临时路径设为 `TEST_ROOT)，再为这次验收生成唯一且未使用过的 profile 名称。mcpc 0.7.0 把 OAuth 凭据写入 OS Keychain；使用明确唯一的 profile 可避免覆盖现有条目，完成后必须 logout 删除本次新增的 Keychain 凭据：

```sh
export MCPC_HOME_DIR="$TEST_ROOT/mcpc-home"
mcpc login http://127.0.0.1:18766/mcp --profile mcp4chatgpt-acceptance-20260926
mcpc connect http://127.0.0.1:18766/mcp @mcp4chatgpt-oauth --profile mcp4chatgpt-acceptance-20260926
mcpc @mcp4chatgpt-oauth tools-call server_info
mcpc close @mcp4chatgpt-oauth
mcpc logout http://127.0.0.1:18766/mcp --profile mcp4chatgpt-acceptance-20260926
```

在本机 OAuth 页面输入上一个终端展示的临时 approval secret。确认授权码使用 PKCE、token exchange 成功、`server_info` 可调用，logout 后测试 profile 的 Keychain 项目已删除。结束服务后删除临时目录。若 Keychain 交互或删除无法确认，则停止并记录为未验收。不要使用正式 OAuth profile 或正式服务。当前只记录可执行的人工流程，本轮没有进行浏览器 OAuth 实测。现有项目 HTTP OAuth 回归另外覆盖 DCR、授权码及 PKCE。

## 限制

此工具用于服务外部协议测试，不在 MCP4ChatGPT 内部通过 `local_run_command` 回调自身。mcpc 的 CLI 会验证 JSON 参数；畸形 JSON-RPC 与缺参数测试仍由原始 HTTP 回归覆盖。当前服务 `listChanged=false`、prompts 为空且没有独立 SSE 流，因此不是本批必验能力。`local_job_*` 是业务工具，不代表 MCP Tasks。
