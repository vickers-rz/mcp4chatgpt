# Chrome 浏览器工具 — 使用指南

MCP4ChatGPT 的浏览器工具组通过一个自定义 Chrome 扩展实现，让 ChatGPT 能够直接控制你**已登录、正在使用的真实 Chrome 窗口**，完整保留所有登录态和 Cookie。

---

## 架构概览

```
ChatGPT ── MCP JSON-RPC ──▶ MCP4ChatGPT Server (:8766)
                                      │
                              WebSocket Bridge (:8765)
                                      │
                        Chrome Extension (Service Worker)
                                      │
                    chrome.tabs / chrome.scripting API
                                      │
                      你正在使用的 Chrome 窗口（保留登录）
```

---

## 第一步：安装 Chrome 扩展

### 方式 A — 本地加载（推荐，无需发布）

1. 打开 Chrome，访问 `chrome://extensions/`
2. 右上角开启 **「开发者模式」**
3. 点击 **「加载已解压的扩展程序」**
4. 选择目录：`<项目根目录>/src/chrome_extension/`
5. 扩展会出现在工具栏，图标为紫色闪电⚡

### 方式 B — 打包安装

```bash
# 项目根目录下
cd src/chrome_extension
zip -r ../mcp4chatgpt_extension.zip .
```

在 `chrome://extensions/` 拖入 zip 文件即可安装。

---

## 第二步：获取 Bridge Token

启动 MCP 服务器后，终端会打印：

```
mcp4chatgpt listening on 127.0.0.1:8766
ext_bridge  listening on ws://127.0.0.1:8765  (extension token: a1b2c3d4...)
```

**完整 token** 是从 `MCP_AUTH_SECRET` 派生的 64 位十六进制字符串。

查看完整 token：
```bash
python3 -c "
import hmac, hashlib
secret = open('.env').read()  # 或直接写入 MCP_AUTH_SECRET 的值
for line in secret.splitlines():
    if 'MCP_AUTH_SECRET=' in line:
        s = line.split('=',1)[1].strip().strip('\"\'')
        token = hmac.new(s.encode(), b'ext_bridge_token_v1', hashlib.sha256).hexdigest()
        print('Token:', token)
        break
"
```

---

## 第三步：配置扩展

1. 点击 Chrome 工具栏中的 **⚡ MCP4ChatGPT 图标**
2. 在「Bridge Connection」区域：
   - **Port**: 填写 `8765`（或环境变量 `EXT_BRIDGE_PORT` 中设置的值）
   - **Token**: 粘贴上一步获取的完整 token
3. 点击 **「Connect」**
4. 状态徽章变为 **🟢 Connected** 即表示连接成功

---

## 第四步：使用浏览器工具

连接成功后，在 ChatGPT 对话中可以使用以下工具：

### 状态检查（先调用此工具）
```
ext_connection_status
```
> 返回扩展是否连接、连接时长等信息。**每次使用浏览器工具前，建议先调用此工具确认连接状态。**

### 读取工具

| 工具 | 功能 |
|---|---|
| `ext_list_tabs` | 列出所有 Chrome 窗口和标签页 |
| `ext_get_active_tab` | 读取当前标签页：标题、URL、正文文本、选中文本 |
| `ext_get_dom` | 获取页面 DOM（可指定 CSS selector） |
| `ext_get_selection` | 获取当前选中文字 |
| `ext_screenshot` | 截图，保存为 PNG 文件 |

### 操控工具

| 工具 | 功能 |
|---|---|
| `ext_navigate` | 打开 URL（当前 tab 或新 tab） |
| `ext_click_element` | 点击页面元素（by CSS selector） |
| `ext_fill_input` | 填写表单输入框 |
| `ext_run_js` | 优先通过 User Scripts 隔离环境执行 JavaScript（需在扩展 popup 和 Chrome 扩展详情页中授权） |
| `ext_listen_changes` | 监听页面导航和 DOM 变化（最长 120 秒） |

---

## 权限说明

| 权限 | 说明 |
|---|---|
| 读取页面内容 | 默认开启，只读 |
| 截图 | 默认开启，只读 |
| 导航 & 交互 | 默认开启，可点击/填表 |
| **JS 执行** | **默认关闭**，需在 popup 中开启，并在 Chrome 扩展详情页开启「Allow User Scripts」 |

`ext_run_js` 默认调用 Chrome 135+ 的 `chrome.userScripts.execute`，并在
`USER_SCRIPT` world 中执行，不受目标网页 CSP 限制。只有 User Scripts API
不可用时，才会回退到 `chrome.scripting.executeScript` 的 MAIN world。

### User Scripts 兼容性设计决策

本项目当前只需要适配本机的新版 Chrome，且本机已在扩展详情页主动开启
**Allow User Scripts**。因此，`chrome.userScripts.execute` 需要用户授权是
预期的 Chrome 安全模型，不是本项目的缺陷，也不应据此把 MAIN world 调整为
首选执行路径。

执行顺序必须保持为：

1. 优先使用 `chrome.userScripts.execute` 和 `USER_SCRIPT` world。
2. 仅当 User Scripts API 未提供或权限不可用时，回退到
   `chrome.scripting.executeScript` 和 MAIN world。

`ext_run_js` 的 MCP 结果包含 `execution_world`，正常配置下应为
`USER_SCRIPT`；该字段可用于测试和故障排查。

这样选择是因为 User Scripts API 就是 Chrome 为运行用户提供的任意脚本设计的
接口；`USER_SCRIPT` world 与网页环境隔离并免受目标网页 CSP 限制。MAIN world
会与目标网页共享 JavaScript 环境、受网页 CSP 约束，也可能被网页代码干预，因此
只适合作为兼容性降级。Chrome 138 及以上版本使用每个扩展独立的
**Allow User Scripts** 开关；较旧版本使用全局 Developer mode。

官方依据：

- [chrome.userScripts API](https://developer.chrome.com/docs/extensions/reference/api/userScripts)
- [Chrome 138 User Scripts 授权变更](https://developer.chrome.com/blog/chrome-userscript)
- [chrome.scripting API](https://developer.chrome.com/docs/extensions/reference/api/scripting)

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EXT_BRIDGE_PORT` | `8765` | WebSocket 桥接监听端口 |
| `EXT_SCREENSHOT_DIR` | `data/screenshots/` | 截图保存目录 |

---

## 常见问题

**Q: 扩展显示「Disconnected」无法连接**
- 确认 MCP 服务器已启动（`./MCP4ChatGPT.command`）
- 确认 Port 和 Token 填写正确
- 检查防火墙是否阻止了 127.0.0.1:8765

**Q: `chrome.scripting.executeScript` 报错**
- 部分特殊页面（`chrome://`、`chrome-extension://`、Chrome Web Store）不允许注入脚本，这是 Chrome 的安全限制，属正常现象

**Q: `ext_run_js` 提示 Chrome User Scripts 不可用**
- 确认 Chrome 版本为 135 或更高
- 打开 `chrome://extensions`，进入 MCP4ChatGPT 扩展详情页
- 开启 **Allow User Scripts**
- 返回扩展 popup，确认 **Allow JS execution** 也已开启

**Q: 截图保存在哪里？**
- 默认保存在 `data/screenshots/` 目录，文件名格式：`screenshot_<毫秒时间戳>.png`

**Q: 现有的 `chrome_list_tabs` / `chrome_get_active_tab_context` 还能用吗？**
- 可以，旧工具（基于 AppleScript）继续保留。新的 `ext_*` 工具功能更强，但需要安装扩展。两者可并行使用。

---

## 与 Codex 扩展的关系

Codex Chrome 扩展（[Chrome Web Store](https://chromewebstore.google.com/detail/codex/hehggadaopoacecdllhhajmbjkdcmajg)）是**闭源专有组件**，其源代码未在 `codex-main` 仓库中公开。本项目参考了相同的架构模式（本地 WebSocket 桥接），但从零实现了扩展代码，并针对 MCP4ChatGPT 的 MCP 协议进行了专项适配。
