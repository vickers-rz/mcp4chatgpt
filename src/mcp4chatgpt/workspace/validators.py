"""Pre-commit validation for workspace text mutations."""

from __future__ import annotations

import ast
from pathlib import Path


class CandidateValidationError(ValueError):
    """Raised before any durable mutation when a candidate looks unsafe."""


_TEXT_SHRINK_MIN_BYTES = 4096
_TEXT_SHRINK_MIN_RATIO = 0.35

_CUA_REQUIRED_FUNCTIONS = {
    "supports",
    "call",
    "stop",
    "_parse_elements",
    "_image_metadata",
}

_CUA_SUPPORTS_BANNED_CALLS = {
    "_call_js",
    "_resolve_window",
    "_select_window_background",
    "_verify_window_binding",
    "_start_session",
    "_await_response",
    "_call_locked",
}


def _decode_utf8(target: Path, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateValidationError(
            f"candidate_not_utf8: {target}: byte={exc.start}"
        ) from exc


def _parse_python(target: Path, text: str) -> ast.Module:
    try:
        return ast.parse(text, filename=str(target))
    except SyntaxError as exc:
        location = f"line={exc.lineno}, column={exc.offset}"
        raise CandidateValidationError(
            f"candidate_python_syntax_error: {target}: {exc.msg}; {location}"
        ) from exc


def _top_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _validate_cua_backend(tree: ast.Module) -> None:
    functions = _top_level_functions(tree)
    missing = sorted(_CUA_REQUIRED_FUNCTIONS - functions.keys())
    if missing:
        raise CandidateValidationError(
            "computer_cua_backend_contract_missing: " + ", ".join(missing)
        )

    supports = functions["supports"]
    banned = sorted(
        {
            name
            for node in ast.walk(supports)
            if isinstance(node, ast.Call)
            for name in [_called_name(node)]
            if name in _CUA_SUPPORTS_BANNED_CALLS
        }
    )
    if banned:
        raise CandidateValidationError(
            "computer_cua_supports_not_pure: banned calls: " + ", ".join(banned)
        )


def validate_candidate(
    target: Path,
    *,
    before: bytes | None,
    after: bytes,
    allow_large_reduction: bool = False,
) -> dict[str, object]:
    """Validate a complete candidate before PREPARED or filesystem replacement."""

    if (
        before is not None
        and len(before) >= _TEXT_SHRINK_MIN_BYTES
        and not allow_large_reduction
        and len(after) < int(len(before) * _TEXT_SHRINK_MIN_RATIO)
    ):
        raise CandidateValidationError(
            "suspicious_file_shrink: "
            f"before_bytes={len(before)}, after_bytes={len(after)}, "
            f"minimum_ratio={_TEXT_SHRINK_MIN_RATIO}; "
            "set allow_large_reduction=true only for an intentional whole-file rewrite"
        )

    text = _decode_utf8(target, after)
    checks: list[str] = ["utf8"]

    if target.suffix == ".py":
        tree = _parse_python(target, text)
        checks.append("python_ast")
        if target.name == "computer_cua_backend.py":
            _validate_cua_backend(tree)
            checks.append("computer_cua_backend_contract")

    return {
        "validated": True,
        "checks": checks,
        "before_bytes": len(before) if before is not None else None,
        "after_bytes": len(after),
    }
