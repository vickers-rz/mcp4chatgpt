from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_core import make_config
from mcp4chatgpt import knowledge_ops


def test_concurrent_knowledge_additions_do_not_overwrite_each_other():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))

        def add(index: int):
            return knowledge_ops.add_source(
                config,
                title=f"Doc {index}",
                text=f"unique source body {index}",
                url=f"https://example.org/{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            added = list(pool.map(add, range(24)))

        listed = knowledge_ops.list_sources(config)["sources"]
        assert len(added) == 24
        assert len(listed) == 24
        assert {item["source_id"] for item in listed} == {item["source_id"] for item in added}


def test_chinese_knowledge_search_uses_same_lexical_terms_as_live_rag():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        added = knowledge_ops.add_source(
            config,
            title="陕西政策",
            text="陕西省传统医学师承出师考核通知已经发布。",
            url="https://example.org/policy",
        )
        results = knowledge_ops.search(config, "中医师承")
        assert results["results"]
        assert results["results"][0]["source_id"] == added["source_id"]
