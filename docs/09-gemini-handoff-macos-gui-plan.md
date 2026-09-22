# Gemini Handoff：MCP4ChatGPT 的 Mac GUI 自动化补齐计划

日期：2026-09-21。状态：首版已实施；本文件保留原实施任务与验收要求。实际使用说明与验证记录见 `docs/macos-computer-use.md`。

## 1. 任务与交付边界

你是接手此任务的 Gemini。请在 `/Users/vickers/Documents/MCP_Creator/MCP4ChatGPT` 实现 macOS 原生 GUI 自动化，让调用方能够完成“观察应用窗口 → 定位控件 → 执行操作 → 再观察验证”的闭环。

用户已决定两个项目分别发展：本项目负责 GUI、个人应用和浏览器能力；`rebel0789-codexpro-local` 负责 coding 能力。本任务不引入 WebCodex 的项目、Runner、LSP 或 coding 工作流，不把两个项目串成多层代理。

本计划建议采用 Python MCP 层 + 本机 Swift Accessibility/ScreenCaptureKit helper。WebCodex 和 CodexPro PR #132 作为实现参考，不要求复制其运行时。首版交付是可验证的有限应用支持，不宣称覆盖全部 macOS 应用。

最终交付必须包括：实现、自动化测试、原生验证记录、安装与权限说明、工具契约、已知限制和回退办法。不能只提交工具 schema 或占位 handler。

## 2. 已核对事实与开工检查

复核基线：MCP4ChatGPT `cae48df04c556b8ef0f3bfccd01e690ae2943f7b`。这不是要求回退到该版本；开工时重新读取实际 HEAD、工作区差异及适用的 AGENTS.md。

已知源码：

- `src/mcp4chatgpt/tools.py`：Tool、build_tools、ToolRegistry、工具注解与分发。
- `src/mcp4chatgpt/config.py`：冻结 Config 与环境配置；扩展字段时检查所有构造点和测试 fixture。
- `src/mcp4chatgpt/terminal_ops.py`：动态调用 co-te 的应用上下文和文本写入。
- `src/mcp4chatgpt/mcp_types.py`：RawMCPToolResult，可保留原生 MCP image/structuredContent。
- `src/mcp4chatgpt/audit.py`、`server.py`：审计与服务生命周期。
- `tests/test_core.py`、`tests/test_server.py`、`tests/conftest.py`：现有回归入口。

外部默认 co-te 路径是 `/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py`，实际配置可覆盖。已检查的通用写入通过激活应用和剪贴板粘贴完成，并非 AX 属性直写。不要据此承诺通用、高可靠文本输入；本任务不需要修改这个外部项目。

交接时已存在用户修改：`docs/07-browser-bridge-reference-and-roadmap.md` 和未跟踪的 `docs/08-chatgpt-share-link-reading.md`。保留所有实际工作区中的无关变更。

参考：

- https://github.com/rebel0789/codexpro/pull/132
- https://github.com/Nonex111/codexpro/blob/feat/macos-computer-use/native/macos/CodexProComputerUse.swift
- https://github.com/yyjeqhc/webcodex/blob/main/docs/COMPUTER_USE.md

参考内容可能变化。记录实际使用的提交与许可证，复制源码前确认许可证和署名要求。PR #132 的索引路径 ID 不能直接当作可靠的过期元素防护；UI 重排后同一路径可能指向另一个控件。

## 3. 首版范围与明确不做的内容

必须支持：应用发现、窗口与有限 AX 树、单窗口截图、AXPress、受限按键、向已定位文本控件输入、操作后重新观察。

建议首批验证对象：专用测试应用，以及 TextEdit 的测试文档。WPS、Notes 或其他目标应用只有实际验证通过才列入支持表；遇到 AX 信息不足应报告能力缺口。

首版不做：全屏坐标点击、拖拽、OCR 自动兜底、多显示器坐标转换、远程桌面、自动批量工作流、自动修改系统权限、关闭用户现有文档、浏览器 DOM 替代方案。Chrome 网页操作继续走现有扩展/CDP。

## 4. 架构与代码落点

建议新增以下文件；若现有结构更适合，可调整命名并在交付说明中说明：

| 路径 | 职责 |
| --- | --- |
| `src/mcp4chatgpt/computer_ops.py` | 参数校验、模式与 allowlist 检查、工具返回值 |
| `src/mcp4chatgpt/computer_backend.py` | helper 启停、编译定位、超时、请求关联 |
| `native/macos/ComputerUseHelper.swift` | AX 对象注册、观察、动作、窗口截图 |
| `scripts/build_computer_helper.sh` | 可重复的本地编译流程 |
| `tests/test_computer_ops.py` | Python 契约及错误路径 |
| `tests/test_computer_backend.py` | 假 helper、退出、超时、并发与协议测试 |
| `docs/macos-computer-use.md` | 安装、权限、使用、支持矩阵和限制 |

推荐 helper 为服务拥有的长驻子进程：stdin/stdout 使用有长度上限的 JSON Lines，stderr 仅输出不含 UI 内容的诊断；首版串行执行桌面动作。维护 native AX 引用和进程内 snapshot registry，避免每次点击重新按树路径找元素。

请求含 request_id、operation 和验证后的参数；响应含相同 request_id、结果或结构化错误。helper 必须独立校验操作和目标，不接受任意命令、任意输出路径或原始脚本。限制消息大小、树节点数、树深度、字符串长度和截图字节数。

helper 崩溃或重启使所有窗口/元素 ID 失效；只允许恢复连接和观察，不自动重放已发送的动作。服务关闭时回收拥有的子进程。打包必须包含 Swift 源文件或经过验证的 helper，验证 wheel/sdist 安装后的路径，不只验证源码目录运行。

## 5. 工具契约草案

工具名为本计划建议，不是已有能力。注册与 handler 的权限判断必须一致。

| 工具 | 关键输入 | 关键输出 |
| --- | --- | --- |
| `computer_list_apps` | 可选有界 limit | allowlist 内运行应用、app_id、能力和权限状态 |
| `computer_get_state` | app_id，可选 window_id，输出上限 | snapshot_id、window_id、elements、truncated、能力 |
| `computer_screenshot` | app_id、window_id，可选 snapshot_id | MCP image、捕获尺寸、窗口身份、捕获时间 |
| `computer_click` | app_id、snapshot_id、element_id | AXPress 执行结果、effect 状态、需重新观察 |
| `computer_press_key` | app_id、snapshot_id、window_id、key、modifiers | 输入派发状态、需重新观察 |
| `computer_type_text` | app_id、snapshot_id、element_id、text、明确 mode | 实际使用的输入策略、effect 状态、需重新观察 |

`app_id` 可使用 allowlist bundle ID；窗口和元素使用不透明进程内 ID。应用重启后旧 ID 不得指向新进程。`computer_get_state` 返回窗口候选供选择；若目标不唯一，不静默选择另一个窗口。截图必须对应调用者选择的窗口，不采用“总取最大窗口”的隐含替换策略。

首版文本 mode 应限制为确实实现的语义，例如 replace_value；只有实现并验证选区/插入点语义后才开放 insert 或 replace_selection。不要把全值覆盖伪装成插入。

按键使用有限枚举与 modifier 组合，拒绝未知映射。动作默认不额外附送 Return。独立诊断入口可复用 server_info，报告平台、编译器/helper、权限、模式和能力，不增加无实际需要的工具。

## 6. 元素生命周期与效果语义

必须落实以下不变量：

1. 每次观察创建 snapshot generation，将 element_id 绑定到进程身份、窗口身份和实际 AX 引用；TTL 有界。
2. 同一应用重新观察或任何动作后，使旧 generation 失效。helper 重启、应用重启、窗口销毁也失效。
3. 动作前验证目标仍属于同一进程/窗口，仍存在、可用且支持所需 AX action；禁止退化为按旧索引路径命中另一元素。
4. 对无法稳定验证的目标返回 stale/unsupported，要求重新观察。AX 引用和校验并不能消除所有并发 UI 变化；文档必须承认检查与执行间的竞态。
5. 串行化本服务内的动作；不能宣称能够隔离用户同时点击或其他程序操作。

效果字段至少区分：`not_started`（已确认未派发）、`completed`（后端确认动作请求执行完成）、`outcome_unknown`（可能已派发但不能确认）。completed 不等于业务目标达成，调用方仍须重新读取 UI。

权限不足、旧 ID、参数错误属于可明确说明的失败；动作派发后的超时/断连不能伪装成未执行。不要自动重试 click、key、type。错误必须保留 effect 状态和下一步观察提示，并确定 MCP isError/structuredContent 的一致契约。

## 7. 输入、截图与权限

建议配置 `MCP_COMPUTER_MODE=off|observe|interact`，默认 off；`MCP_COMPUTER_ALLOWED_APPS` 为显式 bundle ID 集合。非 off 且集合为空时返回配置错误。模式只约束新 computer 工具，不声称限制现有 shell、co-te 或扩展。

- off：不暴露可执行 computer 能力；直接调用也拒绝。
- observe：只允许发现、状态和截图。
- interact：允许已启用的观察与动作。
- 非 macOS：诊断说明不支持，不能影响原有服务器启动。

macOS Accessibility、Screen Recording 权限由系统授权；诊断缺失权限，不能反复弹窗或自行操作系统设置。先查询真实 OS/API 可用性，给出准确最低版本；不能仅凭 Python 可运行就声称截图可用。

文本策略：优先使用目标控件明确支持的 AX 写入；不支持时首版可直接返回 unsupported。剪贴板粘贴应作为后续显式策略：需要焦点确认、内容恢复及格式丢失限制说明，不能默认静默降级。不得把新的 computer_type_text 直接映射到按应用名粘贴而丢掉 element 身份校验。

截图通过原生 MCP image content 返回，并保留结构化元数据；仅返回本机 PNG 路径不能满足远端模型看图需求。设置尺寸和字节上限；截图与 AX 树不是原子快照，分别标注时间，不声称完全同步。默认不落盘；需要诊断持久化时限定服务目录和生命周期。

审计只记录操作类型、目标的必要标识、结果、耗时及 effect 状态；不记录 text 参数、剪贴板、AX value、截图或完整 native 错误。MCP annotations 是描述，不是授权；实际限制必须落在代码。

## 8. 分阶段实施与验收门槛

### G0：基线与契约

检查 HEAD、依赖、现有测试入口、配置 fixture、服务生命周期。确定 helper 协议和错误模型，写最小设计说明。运行适用的现有测试建立基线，记录预先存在的失败。不要启动或重启用户正式服务。

完成标准：工具输入/输出、模式、ID 生命周期和 effect 语义已有可执行测试约束。

### G1：只读后端

实现 helper 编译/启停、allowlist、权限诊断、应用和窗口发现、有限 AX 树、单窗口截图。验证权限拒绝、无窗口、应用退出、截图 API 不可用和超大输出。

完成标准：测试应用中能读取指定窗口树并让 MCP 客户端看到该窗口图片；没有交互工具也能正常使用观察模式。

### G2：元素动作与失效检测

实现 snapshot registry、AXPress、动作串行化、失效检查和 effect 状态。测试应用必须能够在观察后改变子节点顺序，验证旧 ID 不会点击新控件。

完成标准：允许目标按钮可操作；重排、关闭、重启、过期、另一应用 ID 等负例均不会操作错误目标。

### G3：按键与文本

实现可支持的文本 mode、焦点/窗口验证和有限按键映射。测试中文、emoji、多行、空文本、长文本边界。文本控件不支持写入时明确失败。

完成标准：专用文档中输入后可通过重新观察核对内容；不会隐式提交、保存、关闭文档或触发未知键组合。

### G4：MCP 集成、打包与文档

完善工具注册、注解、结果 framing、配置、审计、退出清理和安装包。原有 co-te/ext/downstream 行为保持兼容。完成开发实例的端到端验收与支持矩阵。

完成标准：从安装包而非源码目录启动后同样可用；默认关闭时已有服务不受影响。

## 9. 测试矩阵与证据

自动化测试覆盖：三种模式、allowlist、参数边界、generation、跨目标 ID、helper 超时/退出、action 不重试、输出截断、敏感日志、MCP image 和 structuredContent。native 测试不能全部用 Python mock 替代。

真实 Mac 验收至少记录：OS/架构、helper 构建方式、权限状态、目标应用版本、实际工具调用及脱敏结果。使用测试应用/测试文档，按“读取 → 点击 → 重读 → 输入 → 重读 → 截图”完成闭环。没有原生验证条件时明确标记未验证，不称为完成。

优先执行新增测试及受影响的 core/server 测试，再运行项目已有适用回归。具体命令依据现有 Python 环境，例如 `python -m pytest tests/test_computer_ops.py tests/test_computer_backend.py tests/test_core.py tests/test_server.py`；文件名可随实现调整。

## 10. 交付与后续

最终报告列出修改文件、实际命令与结果、支持的应用/控件、未支持功能、剩余竞态和恢复办法。模式切回 off 应能禁用新功能，不删除用户数据。

本任务授权范围是实现及本地验证；发布、推送、安装到正式实例或重启现有服务按用户后续明确指令执行。需要系统权限时完成可独立进行的代码和测试，并准确说明唯一待办。

后续坐标/OCR、拖拽、滚动和更多应用支持应依据失败案例单独立项，不作为首版完成前提。
