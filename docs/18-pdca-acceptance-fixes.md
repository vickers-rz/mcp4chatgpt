# A1–A4 修复：PDCA 记录（2026-09-26）

## Plan：目标与判定标准

以 17 号独立验收报告为依据，修复四项遗漏。先创建失败回归，再修改实现；判定以实际 PDF 输出、协议故障清理及任务身份核验为准。

本轮新增回归第一次运行：6 failed，3 passed。失败覆盖 Ω 编码、ß 部分匹配、非法 UTF-8、身份不匹配/不可读与成功核验。对照项为正常 Latin-1 写入与已拒绝的中文。

## Do：实现

- A1：Helvetica 替换先验证 Latin-1 编码，再验证字体字形。Ω 与中文明确拒绝，不发布文件；é 和 OK 的成功输出经重新打开、提取文本验证。
- A2：casefold 匹配起止位置必须落在完整原字符边界上。s 不命中 ß；ss 命中一次。跳过不完整命中后继续搜索。
- A3：UnicodeDecodeError 归类为 cua_protocol_error。已发送副作用请求的协议错误返回 outcome_unknown，禁止 fallback；真实 helper 管道故障验证终止进程并清空接收缓存。
- A4：非终态查询读取并比较实际 supervisor 启动身份；不匹配、缺失或不可读时返回 unknown，成功时 process_identity_verified=true。

## Check：结果与第二轮调整

身份查询增加了 ps 调用，旧幂等测试把所有 Popen 都禁止，导致定向测试 44 passed、1 failed。调整测试为允许 ps 观察、继续禁止重复启动监督进程或任务；没有削弱生产身份检查。

最终检查：

- 新增验收回归：10 passed，包含真实子进程协议故障清理。
- 完整测试：255 passed，4 skipped，24.74 秒。
- 原生 GUI fixture 单独串行运行：4 passed，4.18 秒；覆盖完整测试默认跳过项。
- git diff --check：通过。
- 第三方 PyMuPDF SWIG 弃用警告仍存在，不影响上述结果。

## Act：固化与边界

A1–A4 的修复回归验收通过。回归固定在 tests/test_acceptance_regressions.py；幂等测试仍验证不会重复启动任务。

17 号报告保留修复前证据，当前这四项的状态以本文为准。正式服务未重启；真实 CUA connector 的专用窗口端到端验收尚未执行，不能将本轮通过扩大为正式服务发布验收通过。
