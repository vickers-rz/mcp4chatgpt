# 工具发现与 mcpc 修复记录

日期：2026-09-27。代码基线为 `084ccec` 加当前未提交改动。本轮修复报告 23 中的 R1–R8，并保留工作区原有的交接记录。没有重启正式服务或更改 Lucky。

## 修复与证据

| 问题 | 修复 | 回归证据 |
|---|---|---|
| R1 运行期只读旧缓存 | stdio 客户端处理真实 list_changed 通知，分页重新发现；管理器重新过滤并替换目录 | 独立假 stdio 服务在 full/compact 下连续新增、删除工具和改变字段类型；检验 search/get/call/resources 及拒绝旧名称 |
| R2 schema 与哈希不同步 | 复制输入/输出 schema 和注解；输出定义也复制，避免外部修改共享对象 | 原地改变下游 schema 后两个版本变化；修改 get 返回值不会污染目录 |
| R3 tools/list 审计不同步 | 同一次快照产生响应、数量及哈希 | HTTP 受控变更测试，审计数量/哈希与实际返回一致 |
| R4 搜索契约回归 | 恢复按空白拆词、Unicode casefold、全部词项匹配名称/描述/source | 原有搜索测试与不完整词项、schema 字段不匹配测试通过 |
| R5 未知方言放行 | 显式声明但不支持的方言拒绝；仅未声明时默认 Draft 2020-12 | 未知方言失败且 handler 未执行 |
| R6 普通 $ref 字段误拒绝 | 只遍历 schema 位置，引用解析器禁止外部读取 | 合法 $ref 属性和 examples 数据通过；真实外部引用继续拒绝 |
| R7 错误结果记成功 | 原始 isError 影响目标审计；保留返回内容 | 下游 isError=true 原样返回并记 ok=false，身份仍正确 |
| R8 mcpc 清理 | 从服务启动前注册清理；跟踪全部 session/home/service；随机密钥、0600 创建、精确清理本轮 Keychain 凭据、删除配置及验证进程退出 | 正常 full/compact，以及 setup/after_connect/after_restart/CLI timeout 共 6 项 |

六条基础回归在修复前全部失败。真实 stdio 测试还暴露原发现逻辑只读取第一页，现已支持分页；限制为最多 64 页、10,000 个工具，并为一次发现设置超时。重复名称、坏游标或无效元数据不发布到目录。

复核最新代码时另修复了三处边界：

- 刷新失败期间收到更新通知，后续更新不再被丢弃。
- handler 及其 audit channel 在同一个目录快照中选择，避免工具被移除后误记 native。
- 写入阻塞期间取消发现请求，也清理 pending future；关闭客户端会取消并等待刷新任务结束。

`referencing` 已列为显式运行依赖，lockfile 离线更新。第三阶段仍只保留设计，没有新增执行器。

## 验证

使用项目 `.venv/bin/python`，不是缺少依赖的系统 Python。

- 范围内测试曾完成 80 passed（新增写入取消回归前）。
- 最后的发现与下游定向测试：52 passed，2.91 秒。
- mcpc 外部验收：6 passed，24.80 秒；包含 4 个清理故障路径。
- 完整 `PYTHONPATH=src .venv/bin/python -m pytest -q`：278 passed、11 skipped，25.61 秒；默认跳过 5 个 GUI 和 6 个 mcpc 测试，mcpc 已另行开启验证。另有 5 条 SWIG 类型弃用警告。
- `git diff --check` 和 `uv lock --check --offline` 均通过。

## 当前边界

运行期更新依赖下游发出 `notifications/tools/list_changed`。不发送通知的下游需要重启后重新发现；配置文件的 allow/deny 等变更也仍通过重启加载。发现失败保留上一个完整目录，状态中提供 catalog_error，后续通知可再次触发更新。

向上游仍声明 `listChanged=false`，没有增加 SSE/通知流。新的 search/get/list 请求可以看到更新，但已经缓存工具定义的 ChatGPT 客户端不会自动收到推送。

正式服务尚未加载这些源码改动，OAuth 浏览器登录与正式 ChatGPT connector 验收尚未执行。以上自动化通过不代表正式发布已完成。
