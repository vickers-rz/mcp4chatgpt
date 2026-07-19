"""Terminal.app、iTerm2 与 Termius 的可见交互适配层。

与 ``local_ops`` 直接创建子进程不同，本模块把命令发送到用户当前可见的终端窗口，
适合需要人工观察、输入密码或保持交互会话的场景。底层能力由 co-te 模块提供，本层
负责动态加载、参数规范化和统一返回结构。

安全上应区分“后台执行”和“前台键入”：后者会改变用户正在使用的终端状态，因此
工具描述和审批注解必须准确标记副作用，敏感文本也不应进入普通审计日志。
"""

from __future__ import annotations

import importlib.util
from types import ModuleType
from typing import Any

from .config import Config

# Module-level cache: path -> loaded module.
# Keyed by the resolved string path so a config change picks up the new file.
_CO_TE_CACHE: dict[str, ModuleType] = {}


def _load_co_te(config: Config) -> ModuleType:
    key = str(config.co_te_path)
    if key in _CO_TE_CACHE:
        return _CO_TE_CACHE[key]
    if not config.co_te_path.exists():
        raise FileNotFoundError(f"co-te.py not found: {config.co_te_path}")
    spec = importlib.util.spec_from_file_location("co_te_bridge", config.co_te_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load co-te.py from {config.co_te_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CO_TE_CACHE[key] = module
    return module


def list_supported_apps(config: Config) -> dict[str, Any]:
    module = _load_co_te(config)
    return module.call_tool("list_supported_apps", {})


def get_app_context(
    config: Config,
    app: str,
    max_chars: int = 12000,
    redact_secrets: bool = True,
    label: str | None = None,
) -> dict[str, Any]:
    module = _load_co_te(config)
    return module.call_tool(
        "get_app_context",
        {"app": app, "max_chars": max_chars, "redact_secrets": redact_secrets, "label": label},
    )


def run_command(config: Config, command: str, app: str = "terminal", label: str | None = None) -> dict[str, Any]:
    module = _load_co_te(config)
    return module.call_tool("run_terminal_command", {"command": command, "app": app, "label": label})


def send_input(
    config: Config,
    text: str,
    press_return: bool = True,
    sensitive: bool = False,
    app: str = "terminal",
    label: str | None = None,
) -> dict[str, Any]:
    module = _load_co_te(config)
    return module.call_tool(
        "send_terminal_input",
        {
            "text": text,
            "press_return": press_return,
            "sensitive": sensitive,
            "app": app,
            "label": label,
        },
    )

