# MCP4ChatGPT macOS GUI 自动化加固 Handoff：实现现状、故障证据、修复设计与分阶段验收

日期：2026-09-22  
状态：**GUI 首版实现已存在于当前未提交工作区；完成一次针对调用链、权限、状态机、进程通信、截图与失败语义的专项审查；发现 3 个应在首个稳定提交前修复的高优先级问题。**  
目标仓库：`/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT`  
当前分支：`main`  
当前 HEAD：`cae48df04c556b8ef0f3bfccd01e690ae2943f7b`（`cae48df Add browser research and asynchronous extension jobs`）  
项目版本：`0.3.0`  
本次验证平台：macOS 15.8 / arm64

> 本文件是 **实施后接手文档**。早期实施计划见 `docs/09-gemini-handoff-macos-gui-plan.md`；当前使用说明见 `docs/macos-computer-use.md`。  
> 不要把 09 文档中的“建议实现”当作已经完成的事实；以本文件和实际工作区代码为准。

---

## 1. 交接目标

后续接手者的任务不是重新设计一套 GUI 自动化，而是：

1. 保留现有 **Python MCP 层 + Swift Accessibility / ScreenCaptureKit helper** 的总体架构；
2. 修复已经通过代码审查和故障注入证实的状态机缺口；
3. 补齐原生 helper / backend 的错误语义和测试覆盖；
4. 保证 GUI 操作遵循：
   - 先观察；
   - 精确绑定 application / process / window / snapshot / element；
   - 单次动作；
   - 动作后重新观察；
   - 结果不确定时绝不自动重放；
5. 在修复完成前，不把当前 GUI 改动作为“稳定完成版”提交或发布。

核心目标是让以下闭环具有可验证语义：

```text
discover app
  -> select exact process/window
  -> observe AX tree
  -> obtain snapshot_id + element_id
  -> validate target still belongs to same process/window/snapshot
  -> perform exactly one action
  -> classify effect
  -> invalidate old snapshot
  -> observe again
```

---

## 2. 当前工作区与变更保护

审查时 `git status --short --branch` 为：

```text
## main...origin/main
 M docs/07-browser-bridge-reference-and-roadmap.md
 M pyproject.toml
 M src/mcp4chatgpt/config.py
 M src/mcp4chatgpt/server.py
 M src/mcp4chatgpt/tools.py
?? docs/08-chatgpt-share-link-reading.md
?? docs/09-gemini-handoff-macos-gui-plan.md
?? docs/10-gemini-handoff-codexpro-coding-plan.md
?? docs/macos-computer-use.md
?? scripts/build_computer_helper.sh
?? src/mcp4chatgpt/computer_backend.py
?? src/mcp4chatgpt/computer_ops.py
?? src/mcp4chatgpt/native/
?? tests/native/
?? tests/test_computer_ops.py
```

### 2.1 重要约束

- **不要 reset / checkout / clean 现有工作区。**
- GUI 自动化之外还有浏览器、文档、Computer Use 等并行修改。
- 本文件只允许增量加固，不得覆盖用户已有未提交内容。
- 修复时优先修改：
  - `src/mcp4chatgpt/native/ComputerUseHelper.swift`
  - `src/mcp4chatgpt/computer_backend.py`
  - `src/mcp4chatgpt/computer_ops.py`
  - `src/mcp4chatgpt/tools.py`
  - 新增或扩展 `tests/test_computer_ops.py`
  - 建议新增 `tests/test_computer_backend.py`
  - 必要时扩展 `tests/native/ComputerFixture.swift`
  - 更新 `docs/macos-computer-use.md`

---

## 3. 当前实现概览

### 3.1 已存在模块

| 文件 | 当前职责 |
| --- | --- |
| `src/mcp4chatgpt/config.py` | `MCP_COMPUTER_MODE`、`MCP_COMPUTER_ALLOWED_APPS` 配置 |
| `src/mcp4chatgpt/tools.py` | MCP tool schema、模式过滤、ToolRegistry、审计入口 |
| `src/mcp4chatgpt/computer_ops.py` | Python 参数校验、allowlist、mode、结果 framing |
| `src/mcp4chatgpt/computer_backend.py` | Swift helper 编译、缓存、子进程、JSONL 请求、超时与 effect 分类 |
| `src/mcp4chatgpt/native/ComputerUseHelper.swift` | AX 应用/窗口/元素观察、snapshot registry、点击、按键、文本写入、ScreenCaptureKit 截图 |
| `scripts/build_computer_helper.sh` | 显式本地构建 helper |
| `tests/test_computer_ops.py` | Python 层当前基础测试 |
| `tests/native/ComputerFixture.swift` | 原生 GUI fixture |
| `docs/macos-computer-use.md` | 当前用户文档 |

### 3.2 当前工具

根据 `computer_mode` 动态暴露：

只读：

- `computer_list_apps`
- `computer_get_state`
- `computer_screenshot`

交互模式额外：

- `computer_click`
- `computer_press_key`
- `computer_type_text`

### 3.3 当前配置

```text
MCP_COMPUTER_MODE=off|observe|interact
MCP_COMPUTER_ALLOWED_APPS=com.apple.TextEdit,...
```

规则：

- `off`：不暴露 computer 工具；
- `observe`：仅发现、状态和截图；
- `interact`：允许点击、按键和文本；
- 非 off 且 allowlist 为空：启动配置失败；
- allowlist 当前按 bundle ID 控制。

默认关闭的方向是正确的，应保留。

---

## 4. 实际调用链

当前实际调用链：

```text
ChatGPT / MCP client
        |
        v
ToolRegistry.call_tool()
        |
        v
Tool.handler
        |
        v
computer_ops.py
  - mode check
  - allowlist check
  - argument validation
  - effectful classification
        |
        v
computer_backend.call()
  - compile/cache helper if needed
  - start owned helper process
  - serialize one JSON request
  - write stdin + flush
  - wait stdout line
  - validate request_id
  - classify transport/protocol error
        |
        v
ComputerUseHelper.swift
  - validate allowlist again
  - resolve NSRunningApplication
  - AXIsProcessTrusted()
  - resolve exact window
  - observe AX tree / snapshot
  - or execute action
  - optionally ScreenCaptureKit
        |
        v
JSON response
        |
        v
computer_backend -> computer_ops -> ToolRegistry
        |
        v
MCP content / structuredContent / image
```

这个双层校验结构应保留：Python 层负责 MCP 策略，Swift helper 必须继续独立校验，不应把 helper 降级成“信任 Python 的任意执行器”。

---

## 5. 权限与安全边界

### 5.1 Python 层

`computer_ops._call()` 当前检查：

- `computer_mode != off`
- effectful 操作要求 `interact`
- allowlist 非空
- `app_id` 必须满足 bundle ID 正则
- `app_id` 必须在 allowlist 中

按键另外限制：

- Return
- Tab
- Escape
- Left
- Right
- Up
- Down
- Backspace

modifier 只允许：

- shift
- option

文本只允许：

- `mode=replace_value`
- 长度不超过 20,000

这些限制应继续保留。

### 5.2 Swift helper

helper 再次检查：

- allowed_apps 必须存在且有界；
- app_id 必须在 allowlist；
- 应用必须正在运行；
- Accessibility 必须已授权；
- snapshot / window / element 必须仍然有效；
- click 仅走 AXPress；
- type 仅走可写 AXValue；
- press_key 要求 observed window 当前仍是 focus window；
- screenshot 要求 Screen Recording 权限。

这是正确的 defense-in-depth。

### 5.3 当前 macOS 权限实测

本次通过真实 helper 调用 `list_apps` 时：

```text
accessibility_trusted: True
```

即当前运行上下文已经具备 Accessibility 权限。

Screen Recording 本次专项审查没有修改权限，也没有把权限缺失当作测试失败。

---

## 6. helper 生命周期与进程通信

### 6.1 helper 构建与缓存

`computer_backend._binary()`：

1. 对 Swift 源文件 SHA-256；
2. digest 前 20 字符进入文件名；
3. 缓存在：

```text
~/Library/Caches/mcp4chatgpt/computer/
```

4. 检查 cache：
   - 不是 symlink；
   - owner 是当前 uid；
   - 权限不允许 group/other；
5. 已有 binary：
   - 不是 symlink；
   - 是 regular file；
   - owner 正确；
   - 不允许 group/other write；
6. 缺失时用 `xcrun swiftc` 编译临时文件，再原子 replace。

这套缓存校验方向正确。

### 6.2 helper ownership

Python 持有一个模块级：

```python
_process: subprocess.Popen[bytes] | None
```

所有调用通过 `RLock` 串行化。

helper：

- stdin：JSON Lines request；
- stdout：JSON Lines response；
- stderr：当前直接 DEVNULL；
- server 关闭时 `computer_backend.stop()`；
- module atexit 也 stop。

这是“服务拥有子进程”的正确模型。

### 6.3 消息约束

请求：

```json
{
  "request_id": "...",
  "operation": "...",
  "args": {...}
}
```

当前请求上限：

```text
100,000 bytes
```

response line 上限：

```text
12 MiB
```

普通 request deadline：

```text
30 s
```

截图内部另有：

```text
12 s
```

ScreenCaptureKit 等待上限。

### 6.4 不重放策略

`computer_backend.call()` 注释：

> Never replay a request after a write or read timeout.

当前没有自动 retry effectful action，这一点必须保持。

---

## 7. 当前状态模型

### 7.1 Snapshot

Swift Snapshot 当前保存：

- snapshot id
- app bundle ID
- PID
- window ID
- AX window reference
- creation time
- element_id -> AXUIElement map

当前 TTL：

```text
45 seconds
```

### 7.2 失效条件

当前代码已经检查：

- snapshot token 匹配；
- PID 匹配；
- requested window ID 匹配；
- snapshot 未超过 45 秒；
- AX window 仍属于当前 app；
- element 仍然包含在 window subtree。

完成 click/type/key 后会移除该 app snapshot。

总体思想正确：

> element_id 不是永久 selector，而是一次观察 generation 内的 opaque handle。

### 7.3 Effect 语义

当前目标语义：

- `not_started`：已确认动作没有派发；
- `completed`：底层动作请求完成；
- `outcome_unknown`：动作可能已经发生，但无法确认。

文档明确：

> completed 不是用户业务目标达成，只代表底层接受/完成了动作请求。

这一点应该保留，并进一步落实到所有协议异常和 audit 中。

---

# 8. 已确认问题总览

| ID | 严重度 | 问题 | 状态 |
| --- | --- | --- | --- |
| GUI-001 | **P0** | 多窗口时只返回 `window_id_required`，候选窗口丢失，调用方无法继续 | **真实 macOS fixture 已复现** |
| GUI-002 | **P0** | effectful 请求动作已发生，但特定 response 协议异常会错误报告 `not_started` | **fake helper 故障注入已复现** |
| GUI-003 | **P0/P1** | `success:false` 的 backend 业务失败被 ToolRegistry audit 记成 `ok:true` | **已复现** |
| GUI-004 | P1 | 同 bundle ID 多进程时无法指定 PID，`targetApp` 永远取 `.first` | 代码确定存在 |
| GUI-005 | P1 | window candidates 与实际 selected window 分多次 `allWindows()` 读取，存在 TOCTOU | 代码确定存在 |
| GUI-006 | P1 | `get_state` 选择窗口失败前就删除旧 snapshot | 代码确定存在 |
| GUI-007 | P2 | 无 AXWindowNumber 时 screenshot 依赖唯一标题，重复/空标题会失败 | 已确认是 fail-closed，不是静默截错 |
| GUI-008 | P2 | screenshot async error 被压缩成统一 `screenshot_unavailable`，诊断粒度较低 | 设计限制 |
| GUI-009 | P2 | 当前测试几乎没有 backend transport/protocol 的真实故障矩阵 | 覆盖缺口 |

---

# 9. GUI-001：多窗口选择链路断裂

## 9.1 代码根因

`ComputerUseHelper.swift` 当前：

```swift
func selectedWindow(
    _ app: AXUIElement,
    pid: pid_t,
    requested: String?
) throws -> (String, AXUIElement, [[String: Any]]) {
    let available = windowsResult(pid, app)

    guard let requested, !requested.isEmpty else {
        if available.count == 1,
           let first = allWindows(app).first {
            return (
                available[0]["window_id"] as! String,
                first,
                available
            )
        }

        throw HelperFailure(
            available.isEmpty ? "no_window" : "window_id_required"
        )
    }
    ...
}
```

问题是：

- helper **已经计算了** `available`；
- 但 `HelperFailure` 只有：
  - code
  - effect
- error response 也只发送：
  - error
  - effect

因此候选窗口信息完全丢失。

## 9.2 真实复现

本次审查创建了临时 AppKit fixture，启动两个同名窗口。

OS 侧：

```text
2
```

helper 可发现：

```json
{
  "apps": [
    {
      "name": "MCP4 Multi",
      "pid": 26824,
      "app_id": "com.vickers.MCP4Multi"
    }
  ],
  "accessibility_trusted": true
}
```

随后不带 window_id 调 `get_state`：

```json
{
  "code": "window_id_required",
  "effect": "not_started"
}
```

exception object 里没有 candidates。

所以当前文档里：

> If more than one window exists, pass the exact returned window_id.

在真实调用链上无法完成。

## 9.3 推荐修复

不要让“需要选择”退化成无上下文异常。

推荐两个可选实现，优先 A。

### A. 结构化 selection result

让 `get_state` 在目标不唯一时正常返回：

```json
{
  "status": "selection_required",
  "app_id": "com.example.App",
  "pid": 1234,
  "windows": [
    {
      "window_id": "1234:88",
      "title": "Document A",
      "focused": true
    },
    {
      "window_id": "1234:91",
      "title": "Document B",
      "focused": false
    }
  ]
}
```

优点：

- 不是异常；
- agent 可以直接选择；
- MCP structuredContent 天然适合；
- 不会混淆“系统故障”和“需要消歧”。

### B. 扩展 HelperFailure metadata

例如：

```swift
struct HelperFailure: Error {
    let code: String
    let effect: String
    let details: [String: Any]?
}
```

响应：

```json
{
  "ok": false,
  "error": "window_id_required",
  "effect": "not_started",
  "details": {
    "windows": [...]
  }
}
```

Python `ComputerBackendError` 也必须保留 details。

B 改动小，但长期 API 语义不如 A 清晰。

---

# 10. GUI-002：effectful 协议异常会误报 not_started

## 10.1 正确的现有部分

effectful request 写入 helper 后，以下情况当前都正确标成：

```text
outcome_unknown
```

包括：

- BrokenPipe / OSError；
- response timeout；
- response EOF / 无 newline；
- response 超限；
- JSON decode error；
- response 不是 dict；
- request_id mismatch。

这是正确的。

## 10.2 漏洞

当前：

```python
result = response.get("result")
if not isinstance(result, dict):
    raise ComputerBackendError("helper_protocol_error")
```

这里使用默认 effect：

```text
not_started
```

但此时已经满足：

- request 已经成功写给 helper；
- response request_id 正确；
- response `ok=true`；
- 唯一异常只是 result 结构不合法。

动作完全可能已经发生。

## 10.3 实际故障注入

本次用 fake helper：

1. 收到 effectful click；
2. 先创建 marker，代表副作用已经发生；
3. 再返回：

```json
{
  "request_id": "<correct>",
  "ok": true,
  "result": "not-an-object"
}
```

实际：

```json
{
  "case": "effect_then_invalid_result",
  "marker_exists": true,
  "code": "helper_protocol_error",
  "effect": "not_started"
}
```

即：

> **副作用已发生，但系统告诉调用方“没有开始”。**

这是 P0 状态机问题，因为调用方可能因此安全地“重试”，导致双击、双输入、双提交。

## 10.4 对照验证

fake helper 在动作后返回错误 request_id 时，当前得到：

```json
{
  "marker_exists": true,
  "code": "helper_protocol_error",
  "effect": "outcome_unknown"
}
```

这一条正确。

## 10.5 推荐原则

定义明确的“dispatch boundary”。

一旦：

```text
proc.stdin.write(payload)
proc.stdin.flush()
```

成功，对 effectful 操作来说，后续除非 helper **明确返回可信的 not_started 错误**，否则所有不确定性都应：

```text
outcome_unknown
```

至少修复：

```python
if not isinstance(result, dict):
    raise ComputerBackendError(
        "helper_protocol_error",
        "outcome_unknown" if effectful else "not_started",
    )
```

更推荐统一 helper response parser，避免以后新增分支再次漏传 effect。

---

# 11. GUI-003：业务失败被 audit 记为成功

## 11.1 根因

`computer_ops._call()`：

```python
try:
    return computer_backend.call(...)
except ComputerBackendError as exc:
    return {
        "success": False,
        "error": exc.code,
        "effect": exc.effect,
        "recovery": ...
    }
```

即 backend failure 被转换成普通 dict。

但 `ToolRegistry.call_tool()`：

```python
result = tool.handler(...)
self.audit.log(
    "tool_call",
    tool=name,
    client_id=client_id,
    ok=True,
    channel=channel,
)
```

它只根据“有没有 Python exception”判断成功。

## 11.2 实际复现

模拟：

```text
helper_timeout + outcome_unknown
```

工具返回：

```json
{
  "success": false,
  "error": "helper_timeout",
  "effect": "outcome_unknown",
  "recovery": "observe_before_retry"
}
```

audit 实际记录：

```json
{
  "event": "tool_call",
  "tool": "computer_click",
  "ok": true,
  "channel": "native"
}
```

因此操作审计和实际结果矛盾。

## 11.3 推荐修复

不要为了 audit 简单重新抛掉所有 backend error；`outcome_unknown` 需要保留为结构化业务结果。

推荐引入明确结果类型，例如：

```python
@dataclass(frozen=True)
class ToolOutcome:
    payload: dict[str, Any]
    ok: bool
    error: str | None = None
    effect: str | None = None
```

或者最小改动：

在 `ToolRegistry.call_tool()` 中识别：

```python
business_failed = (
    isinstance(result, dict)
    and result.get("success") is False
)
```

audit：

```python
self.audit.log(
    "tool_call",
    tool=name,
    client_id=client_id,
    ok=not business_failed,
    channel=channel,
    error=result.get("error") if business_failed else None,
    effect=result.get("effect") if business_failed else None,
)
```

注意：

- 绝对不要把 `text` 内容写 audit；
- 不要写 screenshot base64；
- 不要写完整 AX value；
- effect 应进入 audit；
- recovery 可选，不是必须。

如果担心全局 dict `success:false` 影响其他工具，可只对 `computer_*` 或引入内部 marker 类型处理。

---

# 12. GUI-004：同 bundle ID 多进程不可消歧

当前 `list_apps` 返回 PID：

```json
{
  "app_id": "...",
  "name": "...",
  "pid": 12345
}
```

但 `targetApp()`：

```swift
NSRunningApplication
    .runningApplications(withBundleIdentifier: id)
    .first
```

后续 API 没有 pid 参数。

因此若：

```text
bundle = com.example.App
pid = 100
pid = 200
```

调用方无法指定 pid=200。

这不是纯理论：macOS 上允许同 bundle 多实例或辅助进程形态并不罕见。

## 推荐修复

把 `pid` 作为 target identity 的一部分。

建议：

- `computer_get_state(app_id, pid?, window_id?)`
- `computer_screenshot(app_id, pid, window_id)`
- action 请求也携带 pid；
- snapshot 本身已经保存 pid；
- window token 当前本来也包含 pid。

helper：

```swift
if requestedPID != nil {
    choose exact running application with bundle + pid
} else if matches.count == 1 {
    choose it
} else {
    return process_selection_required + candidates
}
```

最终 identity：

```text
bundle_id + pid + window_id + snapshot_id + element_id
```

---

# 13. GUI-005：窗口枚举 TOCTOU

`selectedWindow()` 当前多次调用 `allWindows(app)`：

1. `windowsResult()` 内一次；
2. 单窗口 branch 再一次；
3. 指定 window_id branch 再一次。

如果 UI 在两次读取之间变化：

```text
T0: windows = [A]
T1: A closes, B opens
T2: allWindows = [B]
```

理论上可能形成：

```text
window_id = token(A)
AX window ref = B
```

对 GUI agent，这类异步变化应避免。

## 修复

一次读取：

```swift
let windows = Array(allWindows(app).prefix(32))
```

之后：

- candidates；
- single-window auto-select；
- requested matching；

都使用同一个 `windows` 数组。

不要在一次 selection transaction 里重复枚举。

---

# 14. GUI-006：失败观察提前破坏旧 snapshot

当前 `state()`：

```swift
let (id, running, app) = try targetApp(args)
snapshots.removeValue(forKey: id)
let (windowID, window, available) = try selectedWindow(...)
```

如果：

- 老 snapshot 仍合法；
- 新 get_state 忘记 window_id；
- app 恰好有多个窗口；

则：

1. 老 snapshot 先被删除；
2. selection 失败；
3. 新 snapshot 没生成。

所以一次失败 observation 会破坏已知状态。

## 推荐修复

顺序：

```swift
let (id, running, app) = try targetApp(args)
let selection = try selectedWindow(...)
snapshots.removeValue(forKey: id)
... build new snapshot
```

更严格地：

- 只有在新 snapshot **准备成功写入**时，才替换旧 generation；
- selection_required 不应触发 generation 失效；
- permission/no_window 等只读失败也不应无意义销毁旧 snapshot；
- effectful action 一旦开始执行，则旧 snapshot 应立即或确定性失效。

---

# 15. GUI-007：截图同名窗口风险的最终判断

最初怀疑：

> fallback title matching 可能在同名窗口下截错目标。

复核后，这个描述需要修正。

当前：

```swift
let matches = owned.filter {
    !title.isEmpty && $0.title == title
}
scWindow = matches.count == 1 ? matches[0] : nil
```

所以：

- 0 match -> unavailable；
- 1 match -> capture；
- 2+ match -> unavailable；
- empty title -> unavailable。

因此当前 fallback 是 **fail-closed**，不是“静默选择第一个”。

### 正常路径

若 AX window 提供 `AXWindowNumber`：

```text
window_id = pid:CGWindowID
```

ScreenCaptureKit 直接以：

```text
processID + windowID
```

匹配。

这条路径不依赖标题。

### 当前真实限制

真正限制是：

> 某些应用没有可靠 AXWindowNumber，同时窗口标题为空或重复时，截图不能完成。

当前表现：

```text
screenshot_unavailable
```

这是可接受的安全失败。

### 后续增强

可考虑给 candidates 增加：

- AX position
- AX size
- focused
- minimized

fallback 时用：

```text
pid + unique title + approximate frame
```

做更强匹配。

但首个稳定版本不应为了提高覆盖率而退化成“猜一个窗口”。

---

# 16. press_key 的 effect 语义

当前：

```swift
down.post(tap: .cghidEventTap)
up.post(tap: .cghidEventTap)
snapshots.removeValue(...)
return completed
```

CGEvent `post` 本身没有一个可用于确认目标应用处理成功的返回结果。

所以：

```text
completed
```

只能表示：

> helper 已完成事件投递代码路径。

不能表示：

> 应用一定处理了该按键。

当前文档已有这个区分，继续保留。

还应注意：

- key down 已投递；
- helper 若在 key up 或 response 前崩溃；
- Python transport 应返回 `outcome_unknown`；
- 绝不能 retry。

建议 backend 故障测试覆盖该模型，而不要求 Swift 为 `post()` 伪造应用处理确认。

---

# 17. 当前测试状态

本次实际执行：

```text
.venv/bin/python -m pytest -q   tests/test_computer_ops.py   tests/test_server.py   tests/test_core.py
```

结果：

```text
81 passed in 11.21s
```

这说明：

- 现有回归没有立即破坏；
- 但 **不代表 GUI failure semantics 已覆盖**。

---

# 18. 当前测试覆盖与缺口

`tests/test_computer_ops.py` 现有主要覆盖：

1. default off hides tools；
2. allowlist；
3. observe 禁止 action；
4. screenshot MCP image framing；
5. mock 一个 `outcome_unknown`，验证不 retry；
6. illegal modifier；
7. typed text 不进入 audit。

实际上搜索整个 tests 后：

```text
window_id_required
stale_window
screenshot_unavailable
helper_protocol_error
```

当前没有对应测试。

唯一 `outcome_unknown` 测试，本质是：

> 假设 backend 已经正确给出 outcome_unknown，上层是否保留。

它没有验证：

> backend 在真实 transport/protocol failure 下是否会正确分类 effect。

这就是 GUI-002 能存在而测试仍全绿的原因。

---

# 19. 建议新增测试文件

建议新增：

```text
tests/test_computer_backend.py
```

职责专门是：

- fake helper；
- stdin/stdout JSONL；
- process death；
- timeout；
- malformed JSON；
- wrong request ID；
- malformed result；
- effectful vs read-only；
- no replay；
- response size；
- helper restart；
- serialization。

不要把所有 transport fault 都塞进 `test_computer_ops.py`。

---

# 20. 必须补齐的 backend 故障测试矩阵

至少包含：

| 场景 | read-only effect | effectful effect | 自动重试 |
| --- | --- | --- | --- |
| helper start failed | not_started | not_started | no |
| request too large before write | not_started | not_started | no |
| broken pipe during write/flush | not_started | outcome_unknown | no |
| timeout after flush | not_started | outcome_unknown | no |
| EOF before newline | not_started | outcome_unknown | no |
| response > MAX_LINE | not_started | outcome_unknown | no |
| malformed JSON | not_started | outcome_unknown | no |
| response not dict | not_started | outcome_unknown | no |
| wrong request_id | not_started | outcome_unknown | no |
| `ok=true`, result non-dict | not_started | **outcome_unknown** | no |
| helper explicit `not_started` | not_started | not_started | no |
| helper explicit `outcome_unknown` | outcome_unknown | outcome_unknown | no |
| valid result | n/a | completed/result semantics | no |

关键断言：

> 一个 effectful operation 在 dispatch boundary 之后，任何不确定失败都不能回到 not_started。

---

# 21. 必须补齐的 native / state 测试矩阵

至少覆盖：

### Process

- 单个允许应用；
- app not running；
- 同 bundle 多进程；
- pid 精确选择；
- pid stale；
- bundle/pid mismatch。

### Window

- 0 window；
- 1 window 自动选择；
- 2+ window 返回 candidates；
- 显式 window_id；
- stale window_id；
- 同名窗口；
- 无标题窗口；
- 窗口关闭后 snapshot stale；
- window enumeration 一次性 transaction。

### Snapshot

- fresh；
- >45 s stale；
- 新 observation 替换旧 snapshot；
- failed selection 不删除旧 snapshot；
- action 后失效；
- helper restart 后失效；
- process restart 后失效。

### Element

- AXPress supported；
- AXPress unsupported；
- disabled；
- stale element；
- element 从 window tree 移除；
- tree reorder 不应让旧 ID 指向新控件。

### Text

- writable text；
- unsupported role；
- non-settable AXValue；
- empty text；
- Chinese；
- emoji；
- multiline；
- 20,000 boundary；
- >20,000 reject before helper。

### Key

- allowed key；
- disallowed key；
- allowed modifier；
- duplicate modifier；
- window not focused；
- stale snapshot；
- action 后旧 snapshot 失效。

### Screenshot

- permission missing；
- numeric window ID path；
- AX hash + unique title；
- AX hash + duplicate title -> unavailable；
- empty title -> unavailable；
- offscreen/closed window；
- zero-size；
- too-large PNG；
- ScreenCaptureKit timeout；
- returned base64 invalid。

---

# 22. 建议目标 API 契约

## 22.1 list_apps

建议：

```json
{
  "apps": [
    {
      "app_id": "com.apple.TextEdit",
      "pid": 1234,
      "name": "TextEdit"
    }
  ],
  "accessibility_trusted": true,
  "truncated": false
}
```

保留 PID。

## 22.2 get_state

### 唯一 process/window

```json
{
  "status": "ok",
  "app_id": "...",
  "pid": 1234,
  "window_id": "1234:88",
  "snapshot_id": "...",
  "windows": [...],
  "elements": [...],
  "truncated": false
}
```

### process 需要选择

```json
{
  "status": "process_selection_required",
  "app_id": "...",
  "processes": [
    {"pid": 100, "name": "..."},
    {"pid": 200, "name": "..."}
  ]
}
```

### window 需要选择

```json
{
  "status": "window_selection_required",
  "app_id": "...",
  "pid": 1234,
  "windows": [
    {
      "window_id": "...",
      "title": "...",
      "focused": true
    }
  ]
}
```

这些不是 backend crash，不应标成 `success:false`。

---

# 23. Error envelope 建议

对于真正失败：

```json
{
  "success": false,
  "error": "helper_timeout",
  "effect": "outcome_unknown",
  "recovery": "observe_before_retry"
}
```

可选：

```json
{
  "details": {
    "reason": "..."
  }
}
```

但 details 中：

- 不允许 text 内容；
- 不允许 screenshot；
- 不允许完整 AX value；
- 不允许敏感窗口正文。

---

# 24. 分阶段修复路线

下面建议按 H0–H7 实施。

---

## H0：冻结证据与建立 backend 测试基线

### 任务

- 不改行为；
- 新建 `tests/test_computer_backend.py`；
- 把本次 fake-helper 两个 fault probe 正式转成测试；
- 记录当前至少一个测试应失败，证明测试能捕获 GUI-002。

### 必须包含

1. effect happens + invalid result -> 当前错误地 not_started；
2. effect happens + wrong request id -> outcome_unknown；
3. timeout after dispatch -> outcome_unknown；
4. read-only 同类故障 -> not_started。

### 完成标准

测试在旧代码上至少能稳定暴露 GUI-002，而不是全绿。

---

## H1：修复 effect classification

### 修改

`computer_backend.py`

建立统一：

```python
def _protocol_failure(code, *, effectful, dispatched):
    ...
```

或等价抽象。

### 规则

effectful：

- dispatch 前错误 -> not_started；
- dispatch 后不确定错误 -> outcome_unknown；
- helper 明确可信地返回 not_started -> not_started。

### 验收

H0 全部通过。

---

## H2：修复 selection contract

### 修改

`ComputerUseHelper.swift`

- 一次枚举 windows；
- 不唯一时返回 candidates；
- 不要丢 candidates；
- failed selection 不删除旧 snapshot。

Python：

- 保留 structured selection result；
- ToolRegistry 不把 selection_required 当 error。

### 验收

真实多窗口 fixture：

```text
get_state(app_id)
 -> two candidate windows
 -> choose window_id
 -> get state
```

能够闭环。

---

## H3：引入 PID binding

### 修改

tool schema：

- get_state 增加 pid；
- screenshot 增加 pid；
- action 请求带 pid 或由 snapshot 强绑定后仍显式验证。

Swift：

`targetApp(args)`：

- bundle + pid 精确选择；
- 无 pid 且多 process -> process_selection_required；
- 不允许静默 `.first`。

### 验收

同 bundle 两个测试进程：

- 可以精确选任意一个；
- snapshot/action 不能跨 pid。

---

## H4：修复 audit 语义

### 目标

业务失败不能记录 `ok:true`。

### 建议

优先引入内部 outcome marker，而不是全局靠任意 dict 字段猜。

例如：

```python
@dataclass(frozen=True)
class ToolExecutionResult:
    payload: Any
    audit_ok: bool
    audit_error: str | None
    effect: str | None
```

如果不想扩大重构，可局部只处理 computer tools。

### audit 最少字段

```json
{
  "event": "tool_call",
  "tool": "computer_click",
  "ok": false,
  "channel": "native",
  "error": "helper_timeout",
  "effect": "outcome_unknown"
}
```

绝不记录 text/image/UI content。

---

## H5：截图路径加固

### 首版必须

- 保留 numeric window ID 优先；
- 重名 fallback 继续 fail-closed；
- candidates 增加足够窗口 metadata；
- screenshot failure 不要变成错误目标。

### 可选增强

- title + frame 唯一匹配；
- focused/minimized metadata；
- 截图内部错误做低敏感分类，例如：
  - window_not_found
  - capture_timeout
  - capture_failed
  - screenshot_too_large

不要把底层完整错误 message 直接回传。

---

## H6：原生 E2E fixture 扩展

把 `tests/native/ComputerFixture.swift` 扩展或新增 fixture 支持：

- 2 个窗口；
- duplicate titles；
- 动态增加/删除控件；
- 文本框；
- button counter；
- optional second process。

测试流程：

```text
list
 -> process select
 -> window select
 -> observe
 -> click
 -> observe
 -> type
 -> observe
 -> key
 -> observe
 -> screenshot
```

再做：

```text
old snapshot
 -> mutate tree/window/process
 -> action
 -> must reject
```

---

## H7：回归、文档与提交

### 回归

至少：

```bash
.venv/bin/python -m pytest -q   tests/test_computer_backend.py   tests/test_computer_ops.py   tests/test_core.py   tests/test_server.py
```

然后运行项目适用完整 pytest。

### 文档

更新：

- `docs/macos-computer-use.md`
- 本 handoff 的状态段；
- 如 09 文档有已失效描述，只加指针，不删除历史计划。

### 提交前

确认：

- 没有 secret；
- 没有截图；
- 没有 fixture 临时 app；
- 没有 /tmp artifact；
- 没有用户文档内容被 audit；
- `git diff` 只包含预期变更。

---

# 25. 推荐文件级修改方案

## `src/mcp4chatgpt/native/ComputerUseHelper.swift`

优先修改：

1. `HelperFailure` 支持 details，或 selection 改正常 structured result；
2. `targetApp` 支持 pid；
3. 同 bundle 多 process 消歧；
4. `selectedWindow` 单次 windows snapshot；
5. 返回 process/window candidates；
6. `state` 延后 old snapshot invalidation；
7. snapshot identity 继续绑定 pid/window；
8. screenshot 保留 fail-closed。

## `src/mcp4chatgpt/computer_backend.py`

优先修改：

1. 明确 dispatched 状态；
2. 统一 protocol failure effect；
3. `ok=true + malformed result` effectful -> outcome_unknown；
4. error details 有界透传；
5. 建议将 parser 拆成可单测函数；
6. 继续禁止自动 retry。

## `src/mcp4chatgpt/computer_ops.py`

优先修改：

1. pid schema/validation；
2. selection result framing；
3. backend error details；
4. 不丢 effect；
5. screenshot error structured result 保留；
6. 不引入自动 fallback。

## `src/mcp4chatgpt/tools.py`

优先修改：

1. tool input schema 增加 pid；
2. audit 能区分 business failure；
3. 记录 effect；
4. 保持 sensitive text 不进 audit；
5. annotation 仍只是描述，不代替实际权限。

## Tests

新增：

```text
tests/test_computer_backend.py
```

扩展：

```text
tests/test_computer_ops.py
tests/native/ComputerFixture.swift
```

必要时新建：

```text
tests/native/MultiWindowFixture.swift
```

---

# 26. 建议 commit 切分

不要把所有修复压成一个巨型 commit。

建议：

### Commit 1

```text
test(computer): cover backend effect and protocol failures
```

只加测试，允许暴露旧 bug。

### Commit 2

```text
fix(computer): preserve outcome_unknown after action dispatch
```

修 backend effect。

### Commit 3

```text
fix(computer): return process and window selection candidates
```

修多窗口/PID/TOCTOU/snapshot selection。

### Commit 4

```text
fix(computer): align audit status with structured failures
```

修 audit。

### Commit 5

```text
test(computer): add native multi-window and screenshot coverage
```

原生 fixture。

### Commit 6

```text
docs(computer): document hardened GUI state machine
```

文档。

如果当前 GUI 首版本身尚未提交，则接手者也可以先形成一个干净的 feature commit，但仍建议在 commit message 或 PR 中清楚区分：

- initial implementation；
- hardening fixes；
- tests。

---

# 27. 稳定版本验收标准

只有同时满足以下条件，才可称 GUI 首版“稳定可提交”。

### Identity

- [ ] bundle ID allowlist
- [ ] PID 可消歧
- [ ] window ID 可消歧
- [ ] snapshot 绑定 PID/window
- [ ] element 绑定 snapshot/window

### Observation

- [ ] 0/1/N window 行为明确
- [ ] N window 能返回 candidates
- [ ] failed selection 不破坏旧 snapshot
- [ ] stale process/window/snapshot/element 均被拒绝

### Effect

- [ ] dispatch 前错误 = not_started
- [ ] dispatch 后不确定 = outcome_unknown
- [ ] explicit helper not_started 保留
- [ ] completed 不等于业务成功
- [ ] effectful 不自动 retry

### Audit

- [ ] success=false 不记录 ok=true
- [ ] outcome_unknown 进入 audit effect
- [ ] text 不进入 audit
- [ ] screenshot 不进入 audit
- [ ] AX value 不进入 audit

### Screenshot

- [ ] numeric window ID 精确匹配
- [ ] duplicate title 不截错
- [ ] fallback 不确定时失败
- [ ] MCP image framing 有测试
- [ ] permission failure 明确

### Tests

- [ ] backend fake helper failure matrix
- [ ] Python ops
- [ ] native multi-window
- [ ] native stale snapshot
- [ ] screenshot duplicate-title
- [ ] core/server regression
- [ ] 完整适用 pytest

---

# 28. 回滚策略

任何阶段出问题，最简单运行时回滚：

```text
MCP_COMPUTER_MODE=off
```

重启 MCP service。

这样：

- computer tools 不暴露；
- 不删除数据；
- 不需要 schema migration；
- browser/ext/downstream/co-te 等其他能力不应受影响。

helper 为服务拥有子进程，server stop 时应被终止。

---

# 29. 本阶段明确不做

为避免加固任务失控，以下不属于本次 P0/P1 修复：

- 坐标点击；
- OCR；
- vision element grounding；
- drag；
- scroll；
- mouse move；
- clipboard paste fallback；
- 通用 menu traversal；
- 自动解锁 Screen Recording / Accessibility；
- 自动保存/关闭文档；
- 浏览器 DOM 自动化替代；
- WebCodex/CodexPro coding workflow；
- 远程桌面；
- 跨用户 session。

这些可以后续独立立项。

---

# 30. 与 Chrome / browser 自动化的边界

当前 MCP4ChatGPT 还有：

- Chrome extension；
- AppleScript fallback；
- chrome-devtools-mcp downstream；
- CDP；
- browser research；
- async extension jobs。

GUI Computer Use 不应吞并它们。

网页优先：

```text
Extension / CDP / DOM
```

本机非网页应用：

```text
Computer Use / Accessibility / ScreenCaptureKit
```

只有 DOM/CDP 不适合解决的本机应用 UI 才走 native computer 工具。

---

# 31. 与当前 chrome-devtools downstream 的关系

审查期间还确认：

`downstream_mcp.toml` 当前已启用：

```text
chrome-devtools-mcp@latest --autoConnect
```

而且真实运行中已经可以看到用户当前已登录 Chrome 标签页。

这与本 GUI 加固是两个不同通道：

```text
chrome_devtools__*  -> downstream MCP / CDP
computer_*         -> native Swift helper
ext_*              -> Chrome extension bridge
```

不要在本 GUI 修复中增加隐式跨通道 fallback。

---

# 32. 当前可保留的实现优点

本次审查不是推翻现有实现。以下设计应保留：

1. **默认 off**；
2. **explicit app allowlist**；
3. Python + Swift 双层校验；
4. helper owned process；
5. helper 不接受任意脚本；
6. JSONL request_id；
7. bounded message；
8. 单线程/串行动作；
9. opaque snapshot/element ID；
10. snapshot TTL；
11. action 后要求 re-observe；
12. click 使用 AXPress；
13. type 使用 writable AXValue；
14. key 枚举受限；
15. screenshot 使用 ScreenCaptureKit；
16. image 走 MCP image，而不是只返回本机路径；
17. effectful request 不自动重放；
18. helper source hash cache；
19. cache/binary 权限检查；
20. server shutdown 回收 helper。

这些构成了当前方案的正确基础。

---

# 33. 本次审查的实际证据

### 代码检查

已读取并核对：

- `computer_ops.py`
- `computer_backend.py`
- `ComputerUseHelper.swift`
- `tools.py`
- `config.py`
- `server.py`
- `audit.py`
- `mcp_types.py`
- `tests/test_computer_ops.py`
- `tests/native/ComputerFixture.swift`
- `docs/macos-computer-use.md`

### 测试

实际：

```text
81 passed
```

### 原生 helper

实际调用确认：

```text
accessibility_trusted = true
```

### 多窗口

临时 AppKit fixture：

```text
OS window count = 2
```

实际 `get_state`：

```text
window_id_required
```

且没有 candidates。

### Effect fault injection

真实 fake helper 子进程：

```text
effect marker created
+ valid request_id
+ ok=true
+ invalid result type
=> effect incorrectly reported not_started
```

### Audit

真实 ToolRegistry 调用：

```text
structuredContent.success = false
effect = outcome_unknown
```

但 audit：

```text
ok = true
```

---

# 34. 接手者第一轮操作清单

接手后按以下顺序执行，不要先大改架构：

1. `git status --short --branch`
2. `git rev-parse HEAD`
3. 确认本文件仍与工作区匹配；
4. 不 reset 用户未提交改动；
5. 新建 `tests/test_computer_backend.py`；
6. 把 GUI-002 probe 转成测试；
7. 先看测试红；
8. 修 backend effect；
9. 修多窗口 selection result；
10. 修 PID identity；
11. 修 audit；
12. 扩 native fixture；
13. 跑 targeted tests；
14. 跑 full applicable tests；
15. 做真实 Mac 多窗口闭环；
16. 更新 `docs/macos-computer-use.md`；
17. review git diff；
18. 再决定 commit。

---

# 35. 最终交付报告必须回答的问题

最终实施者提交报告时必须明确回答：

1. 实际 HEAD/branch 是什么？
2. 哪些修改属于原 GUI 首版，哪些属于本次 hardening？
3. GUI-001 是否真实修复？
4. GUI-002 的 effectful malformed-result 是否测试为 outcome_unknown？
5. audit 是否能正确记录 structured failure？
6. 同 bundle 多 process 如何选择？
7. window candidates 如何返回？
8. selection failure 是否保留旧 snapshot？
9. duplicate-title screenshot 是否仍 fail-closed？
10. action 是否存在任何自动 retry？
11. 哪些错误属于 not_started？
12. 哪些错误属于 outcome_unknown？
13. 跑了哪些 tests，结果是什么？
14. 跑了哪些真实 macOS fixture？
15. 是否写入/修改任何用户真实文档？
16. 是否重启正式 MCP service？
17. 是否 commit / push？
18. 当前剩余限制是什么？

不能用一句“GUI 自动化已完成”代替以上证据。

---

# 36. 最终设计原则

这套 GUI 自动化最重要的不是“能点击多少应用”，而是**不能在状态不确定时自信地做第二次动作**。

因此最终实现应坚持：

```text
observe exact target
    ↓
bind exact identity
    ↓
validate freshness
    ↓
act once
    ↓
if result is certain:
    re-observe
if result is uncertain:
    re-observe before any retry
```

任何无法证明：

```text
same process
same window
same observation generation
same target element
```

的情况，都应拒绝动作或要求重新观察。

任何已经跨过 dispatch boundary、但无法证明动作没有发生的情况，都必须：

```text
effect = outcome_unknown
```

而不能：

```text
effect = not_started
```

这应作为后续所有 macOS GUI / Computer Use 能力扩展的核心不变量。
