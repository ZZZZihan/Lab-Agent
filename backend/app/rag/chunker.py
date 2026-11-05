"""Section chunker. Citations point at a heading, not a whole document."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DraftChunk:
    index: int
    heading: str
    text: str


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；\n])")


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    body = text[end + 4 :].lstrip("\n")
    return meta, body


def chunk_markdown(body: str, *, max_chars: int = 800) -> list[DraftChunk]:
    sections = _split_sections(body)
    drafts: list[DraftChunk] = []
    for heading, text in sections:
        cleaned = text.strip()
        if not cleaned:
            continue
        pieces = _split_long(cleaned, max_chars)
        for piece in pieces:
            drafts.append(DraftChunk(index=len(drafts), heading=heading, text=piece.strip()))
    return drafts


def _split_sections(body: str) -> list[tuple[str, str]]:
    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = [("概述", [])]
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
            continue
        sections[-1][1].append(line)
    rendered: list[tuple[str, str]] = []
    for heading, chunk_lines in sections:
        text = "\n".join(chunk_lines).strip()
        if text:
            rendered.append((heading, text))
    return rendered


def _split_oversized_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    raw_sentences = [s for s in _SENTENCE_SPLIT_RE.split(paragraph) if s]
    units: list[str] = []
    for sent in raw_sentences:
        if len(sent) <= max_chars:
            units.append(sent)
        else:
            for start in range(0, len(sent), max_chars):
                units.append(sent[start : start + max_chars])
    pieces: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > max_chars:
            if current.strip():
                pieces.append(current.strip())
            current = unit
        else:
            current += unit
    if current.strip():
        pieces.append(current.strip())
    return pieces or [paragraph[:max_chars]]


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    raw_paragraphs = [part for part in text.split("\n\n") if part.strip()]
    heading = raw_paragraphs[0] if raw_paragraphs and raw_paragraphs[0].startswith("## ") else ""
    para_budget = max(max_chars - len(heading) - 2, 100) if heading else max_chars
    paragraphs: list[str] = []
    for part in raw_paragraphs:
        if len(part) > para_budget:
            paragraphs.extend(_split_oversized_paragraph(part, para_budget))
        else:
            paragraphs.append(part)
    if len(paragraphs) <= 1:
        return paragraphs or [text]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        extra = len(paragraph) + (2 if current else 0)
        if current and size + extra > max_chars:
            pieces.append("\n\n".join(current))
            current = [heading, paragraph] if heading and paragraph != heading else [paragraph]
            size = len("\n\n".join(current))
            continue
        current.append(paragraph)
        size = len("\n\n".join(current))
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def _plain_windows(text: str, max_chars: int = 800, overlap: int = 80) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    units: list[str] = []
    for p_idx, para in enumerate(paragraphs):
        prefix = "\n\n" if p_idx > 0 else ""
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(para) if s]
        for s_idx, sent in enumerate(sentences):
            item = (prefix + sent) if s_idx == 0 else sent
            if len(item) <= max_chars:
                units.append(item)
            else:
                for start in range(0, len(item), max_chars):
                    units.append(item[start : start + max_chars])

    windows: list[str] = []
    current = ""
    for unit in units:
        candidate = current + unit if current else unit.lstrip("\n")
        if current and len(candidate) > max_chars:
            win = current.strip()
            if win:
                windows.append(win)
            tail = win[-overlap:].lstrip() if overlap > 0 and win else ""
            next_unit = unit.lstrip("\n")
            if tail and len(tail) + len(next_unit) <= max_chars + overlap:
                current = tail + next_unit
            else:
                current = next_unit
        else:
            current = candidate
    if current.strip():
        windows.append(current.strip())
    return windows


def chunk_plain(text: str, max_chars: int = 800, overlap: int = 80) -> list[DraftChunk]:
    windows = _plain_windows(text, max_chars=max_chars, overlap=overlap)
    return [
        DraftChunk(index=idx, heading=f"第 {idx + 1} 段", text=win)
        for idx, win in enumerate(windows)
    ]


def chunk_pdf(text: str, max_chars: int = 800, overlap: int = 80) -> list[DraftChunk]:
    pages = text.split("\f")
    drafts: list[DraftChunk] = []
    for page_num, page_text in enumerate(pages, start=1):
        windows = _plain_windows(page_text, max_chars=max_chars, overlap=overlap)
        if not windows:
            continue
        if len(windows) == 1:
            drafts.append(
                DraftChunk(index=len(drafts), heading=f"第 {page_num} 页", text=windows[0])
            )
        else:
            for k, win in enumerate(windows, start=1):
                drafts.append(
                    DraftChunk(
                        index=len(drafts),
                        heading=f"第 {page_num} 页（{k}）",
                        text=win,
                    )
                )
    return drafts


def chunk_document(text: str, source_format: str = "markdown") -> list[DraftChunk]:
    if source_format == "markdown":
        return chunk_markdown(text)
    if source_format == "text":
        return chunk_plain(text)
    if source_format == "pdf":
        return chunk_pdf(text)
    raise ValueError(f"Unsupported source_format: {source_format!r}")
