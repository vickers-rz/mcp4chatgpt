"""Shared lexical retrieval helpers for Chinese and English text.

The project intentionally keeps the first retrieval layer dependency-light. English
terms are tokenized by word characters; contiguous CJK runs are expanded to
bigrams so queries can match inside unsegmented Chinese text. The same tokenization
is reused by live browser RAG, the legacy knowledge store, and the SQLite web
archive so saving a page does not make it harder to retrieve later.
"""
from __future__ import annotations

import re


_LATIN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def lexical_terms(text: str) -> list[str]:
    """Return normalized lexical terms suitable for lightweight BM25/FTS search."""
    terms = _LATIN_RE.findall(text.lower())
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def unique_terms(text: str, *, limit: int = 64) -> list[str]:
    """Return stable de-duplicated terms, bounded for database MATCH expressions."""
    seen: set[str] = set()
    result: list[str] = []
    for term in lexical_terms(text):
        if term in seen:
            continue
        seen.add(term)
        result.append(term)
        if len(result) >= limit:
            break
    return result


def fts5_query(text: str, *, limit: int = 32) -> str:
    """Build a safe OR query for an FTS5 table containing pre-tokenized text."""
    terms = unique_terms(text, limit=limit)
    if not terms:
        raise ValueError("Query cannot be empty.")
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " OR ".join(quoted)


def indexed_text(text: str) -> str:
    """Convert natural text into whitespace-separated tokens for FTS5 indexing."""
    return " ".join(lexical_terms(text))
