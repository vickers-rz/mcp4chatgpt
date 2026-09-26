"""Controlled PDF inspection and editing backed by PyMuPDF.

All paths are constrained to ``MCP_ALLOWED_ROOTS``. Mutations write a new PDF
by default and refuse to overwrite any existing file, including the source.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
from typing import Any

from .config import Config
from .safety import resolve_allowed_path, truncate_text


def _pymupdf():
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is unavailable; install the project dependencies.") from exc
    return pymupdf


def _input_pdf(config: Config, path: str) -> Path:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=True)
    if not target.is_file() or target.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file: {target}")
    return target


def _output_pdf(config: Config, source: Path, output_path: str | None) -> Path:
    if output_path:
        candidate = Path(output_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        target = resolve_allowed_path(str(candidate), config.allowed_roots)
        if target.suffix.lower() != ".pdf":
            raise ValueError("PDF output path must end in .pdf")
        if target == source:
            raise ValueError("Refusing to overwrite the source PDF; choose a different output_path")
        if target.exists():
            raise ValueError(f"Output already exists: {target}")
    else:
        parent = source.parent
        stem = f"{source.stem}_edited"
        target = parent / f"{stem}.pdf"
        suffix = 2
        while target.exists():
            target = parent / f"{stem}_{suffix}.pdf"
            suffix += 1
        target = resolve_allowed_path(str(target), config.allowed_roots)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _publish_pdf(doc, target: Path, *, automatic: bool) -> Path:
    """Save, reopen-validate and atomically publish without replacing a file."""
    while True:
        fd, name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".pdf", dir=target.parent)
        os.close(fd)
        temp = Path(name)
        try:
            temp.unlink()
            doc.save(temp, garbage=4, deflate=True)
            with _pymupdf().open(temp) as check:
                if not check.is_pdf or check.page_count < 1:
                    raise ValueError("Generated PDF failed validation")
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                if not automatic:
                    raise ValueError(f"Output already exists: {target}")
                base = target.stem
                counter = 2
                while target.exists():
                    target = target.with_name(f"{base}_{counter}.pdf")
                    counter += 1
                continue
            with _pymupdf().open(target):
                pass
            from .workspace.recovery import fsync_directory
            try:
                fsync_directory(target.parent)
            except Exception as exc:
                try:
                    target.unlink()
                    fsync_directory(target.parent)
                except Exception as rollback_exc:
                    raise RuntimeError(f"PDF output publication outcome_unknown: {target}") from rollback_exc
                raise exc
            return target
        finally:
            temp.unlink(missing_ok=True)


def _line_matches(page, query: str, *, case_sensitive: bool):
    """Return exact line-local match rectangles, retaining Unicode char mapping."""
    needle = query if case_sensitive else query.casefold()
    raw = page.get_text("rawdict")
    found = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = [char for span in line.get("spans", []) for char in span.get("chars", [])]
            original = "".join(c.get("c", "") for c in chars)
            folded_parts = [(c if case_sensitive else c.casefold()) for c in original]
            haystack = "".join(folded_parts)
            offsets = []
            for index, part in enumerate(folded_parts):
                offsets.extend([index] * len(part))
            start = 0
            while needle and (position := haystack.find(needle, start)) >= 0:
                end = position + len(needle)
                if end <= len(offsets):
                    selected = chars[offsets[position]:offsets[end - 1] + 1]
                    boxes = [c.get("bbox") for c in selected if c.get("bbox")]
                    if boxes:
                        found.append(_pymupdf().Rect(
                            min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes),
                        ))
                start = position + max(1, len(needle))
    return found


def inspect(config: Config, path: str, *, max_chars: int = 20000) -> dict[str, Any]:
    pymupdf = _pymupdf()
    target = _input_pdf(config, path)
    with pymupdf.open(target) as doc:
        if not doc.is_pdf:
            raise ValueError(f"Not a valid PDF: {target}")
        pages = []
        text_parts = []
        remaining = max(0, min(int(max_chars), 100000))
        for index, page in enumerate(doc):
            text = page.get_text("text")
            pages.append({"page": index + 1, "width": page.rect.width, "height": page.rect.height, "text_chars": len(text)})
            if remaining:
                chunk = text[:remaining]
                text_parts.append(f"--- Page {index + 1} ---\n{chunk}")
                remaining -= len(chunk)
        raw_text = "\n".join(text_parts)
        visible, truncated = truncate_text(raw_text, max_chars)
        return {
            "path": str(target), "page_count": doc.page_count,
            "metadata": doc.metadata, "toc": doc.get_toc()[:200],
            "pages": pages, "text": visible, "truncated": truncated,
        }


def search(config: Config, path: str, query: str, *, max_results: int = 100) -> dict[str, Any]:
    pymupdf = _pymupdf()
    target = _input_pdf(config, path)
    query = str(query)
    if not query.strip():
        raise ValueError("query must not be empty")
    limit = max(1, min(int(max_results), 500))
    hits = []
    with pymupdf.open(target) as doc:
        for index, page in enumerate(doc):
            for rect in page.search_for(query):
                hits.append({"page": index + 1, "rect": [round(v, 2) for v in rect], "text": query})
                if len(hits) >= limit:
                    return {"path": str(target), "query": query, "hits": hits, "truncated": True}
    return {"path": str(target), "query": query, "hits": hits, "truncated": False}


def redact_text(
    config: Config,
    path: str,
    query: str,
    *,
    replacement: str = "",
    output_path: str | None = None,
    case_sensitive: bool = True,
) -> dict[str, Any]:
    """Permanently remove matching text and optionally insert replacement."""
    pymupdf = _pymupdf()
    source = _input_pdf(config, path)
    if not str(query).strip():
        raise ValueError("query must not be empty")
    if "\n" in query or "\r" in query:
        raise ValueError("query must be contained within a single PDF text line")
    destination = _output_pdf(config, source, output_path)
    matches = 0
    with pymupdf.open(source) as doc:
        for page in doc:
            rects = _line_matches(page, query, case_sensitive=case_sensitive)
            for rect in rects:
                page.add_redact_annot(rect, fill=(1, 1, 1), cross_out=False)
                matches += 1
            if rects:
                page.apply_redactions()
                if replacement:
                    # Place the replacement in each original match rectangle.
                    for rect in rects:
                        result = page.insert_textbox(rect, replacement, fontsize=10, fontname="helv", color=(0, 0, 0))
                        if result < 0:
                            raise ValueError("Replacement text does not fit the matched area")
        if matches == 0:
            raise ValueError(f"No matches found for {query!r}; no output was written")
        destination = _publish_pdf(doc, destination, automatic=output_path is None)
    return {"source_path": str(source), "output_path": str(destination), "matches": matches, "replacement": replacement}


def insert_text(
    config: Config,
    path: str,
    page_number: int,
    x: float,
    y: float,
    text: str,
    *,
    fontsize: float = 11,
    output_path: str | None = None,
) -> dict[str, Any]:
    pymupdf = _pymupdf()
    source = _input_pdf(config, path)
    destination = _output_pdf(config, source, output_path)
    if not text:
        raise ValueError("text must not be empty")
    if not 1 <= int(page_number):
        raise ValueError("page_number is 1-based and must be positive")
    if not 1 <= float(fontsize) <= 200:
        raise ValueError("fontsize must be between 1 and 200")
    with pymupdf.open(source) as doc:
        if page_number > doc.page_count:
            raise ValueError(f"page_number exceeds page count ({doc.page_count})")
        page = doc[page_number - 1]
        if not page.rect.contains(pymupdf.Point(float(x), float(y))):
            raise ValueError("Text position must be inside the page")
        page.insert_text((float(x), float(y)), text, fontsize=float(fontsize))
        destination = _publish_pdf(doc, destination, automatic=output_path is None)
    return {"source_path": str(source), "output_path": str(destination), "page": page_number}
