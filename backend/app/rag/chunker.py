"""Section chunker. Citations point at a heading, not a whole document."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DraftChunk:
    index: int
    heading: str
    text: str


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


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    if len(paragraphs) <= 1:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    heading = paragraphs[0] if paragraphs[0].startswith("## ") else ""
    for paragraph in paragraphs:
        extra = len(paragraph) + 2
        if current and size + extra > max_chars:
            pieces.append("\n\n".join(current))
            current = [heading, paragraph] if heading and paragraph != heading else [paragraph]
            size = sum(len(part) for part in current)
            continue
        current.append(paragraph)
        size += extra
    if current:
        pieces.append("\n\n".join(current))
    return pieces
