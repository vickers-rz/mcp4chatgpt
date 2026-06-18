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
| `ext_run_js` | 执行 JavaScript（需在扩展 popup 中开启） |
| `ext_listen_changes` | 监听页面导航和 DOM 变化（最长 120 秒） |

---

## 权限说明

| 权限 | 说明 |
|---|---|
| 读取页面内容 | 默认开启，只读 |
| 截图 | 默认开启，只读 |
| 导航 & 交互 | 默认开启，可点击/填表 |
| **JS 执行** | **默认关闭**，需手动在 popup 中开启，有安全风险 |

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

**Q: 截图保存在哪里？**
- 默认保存在 `data/screenshots/` 目录，文件名格式：`screenshot_<毫秒时间戳>.png`

**Q: 现有的 `chrome_list_tabs` / `chrome_get_active_tab_context` 还能用吗？**
- 可以，旧工具（基于 AppleScript）继续保留。新的 `ext_*` 工具功能更强，但需要安装扩展。两者可并行使用。

---

## 与 Codex 扩展的关系

Codex Chrome 扩展（[Chrome Web Store](https://chromewebstore.google.com/detail/codex/hehggadaopoacecdllhhajmbjkdcmajg)）是**闭源专有组件**，其源代码未在 `codex-main` 仓库中公开。本项目参考了相同的架构模式（本地 WebSocket 桥接），但从零实现了扩展代码，并针对 MCP4ChatGPT 的 MCP 协议进行了专项适配。
