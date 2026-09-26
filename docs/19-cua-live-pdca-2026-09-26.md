# CUA 真实窗口验收与修复（2026-09-26）

基线提交：f2663d8。本文修复提交可用 `git log -- tests/test_computer_cua_live.py` 定位。

## Plan

使用临时编译的专用 ComputerFixture App、仅该应用的 allowlist、强制 cua 后端，验证真实 transport 的观察、中文 setValue、点击、后台粘贴和截图。不允许 native 回退掩盖失败；native 仅承担现有的窗口身份核验。

## Do

首次实测复现 no_window：native get_state 先创建无前缀窗口 token，list_windows 复用该 token，CUA 的 native-window 前缀检查将其过滤。统一 selectedWindow 创建的 token 为 native-window 前缀；补充 native 先观察、再列窗口时身份一致的断言，CUA 实测也保留此操作顺序。

第二次实测中，当前 CUA AX 文本将字段内容包含在 role 描述里，测试改为按真实返回结构读取。测试 App 缺少 Edit/Paste 菜单，导致上游报 `-10005: Timed out waiting for the application to read the clipboard`。此时适配器返回 outcome_unknown，测试只观察、没有重放；回读没有发现粘贴标记。为测试 App 增加标准 Paste 菜单后，后台粘贴成功。没有改写生产错误语义或把未知结果视为成功。

## Check

真实 CUA 测试：1 passed，4.34 秒。观察、中文写入回读、点击、CUABackgroundPaste 回读、真实截图内容均通过。finally 关闭 CUA transport，fixture teardown 关闭 native helper 并终止本次创建的 App 进程。

完整回归：255 passed、5 skipped，25.21 秒；5 个跳过项为需显式开启的桌面实测。native 实测独立串行运行：4 passed，4.72 秒。git diff --check 通过。第三方 PyMuPDF SWIG 弃用警告仍存在。

复现命令（在仓库根目录执行；GUI 测试应串行）：

```sh
MCP_CUA_GUI_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_computer_cua_live.py
MCP_NATIVE_GUI_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_computer_native.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

## Act 与验收边界

新增实测入口默认跳过，需 macOS Accessibility、截图授权以及可用的已配置 CUA transport。它覆盖本地代码到真实 CUA/native runtime 的路径，不覆盖 ChatGPT connector、正式服务部署或后台操作不抢占前台的独立验证。正式服务未重启，不能据此宣布正式链路发布验收通过。
