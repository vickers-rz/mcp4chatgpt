"""Test-suite environment isolation.

Optional production feature flags must not leak from the developer shell into
baseline contract tests. Individual tests explicitly enable a feature when they
want to exercise the enabled state.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_optional_feature_flags(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_EXT_ASYNC_JOBS_ENABLED", raising=False)
