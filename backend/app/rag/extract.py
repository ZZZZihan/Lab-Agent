"""Extract plain text from uploaded Markdown, TXT, PDF, DOC, or DOCX files."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from app.importer.reader import DocxValidationError, extract_word_text


class UploadRejected(Exception):
    """Raised when an upload cannot be extracted or fails validation."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Extracted:
    text: str  # PDF 的各页用 '\f' 连接
    source_format: str


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    raise UploadRejected(422, "bad_encoding", "文本编码无法识别，请使用 UTF-8 或 GB18030。")


def _extract_pdf(payload: bytes) -> str:
    if not payload.startswith(b"%PDF"):
        raise UploadRejected(422, "pdf_invalid", "不是有效的 PDF 文件。")
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise UploadRejected(422, "pdf_invalid", "PDF 文件无法解析。") from exc
    if reader.is_encrypted:
        raise UploadRejected(422, "pdf_encrypted", "不支持加密的 PDF 文件。")
    pages_text: list[str] = []
    try:
        for page in reader.pages:
            pages_text.append((page.extract_text() or "").strip())
    except Exception as exc:
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            raise UploadRejected(422, "pdf_encrypted", "不支持加密的 PDF 文件。") from exc
        raise UploadRejected(422, "pdf_invalid", "PDF 页面文本提取失败。") from exc
    non_whitespace = "".join(ch for page in pages_text for ch in page if not ch.isspace())
    if len(non_whitespace) < 20:
        raise UploadRejected(422, "pdf_no_text", "看起来是扫描版 PDF，暂不支持")
    return "\f".join(pages_text)


def extract_pdf_text(source: bytes | Path | str) -> str:
    """Extract page-joined UTF-8 text ('\\f'-separated) from PDF bytes or a file path."""
    payload = Path(source).read_bytes() if isinstance(source, (Path, str)) else source
    return _extract_pdf(payload)


def extract_upload(filename: str, payload: bytes, max_chars: int) -> Extracted:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".md", ".markdown"}:
        source_format = "markdown"
        text = _decode_text(payload)
    elif suffix == ".txt":
        source_format = "text"
        text = _decode_text(payload)
    elif suffix == ".pdf":
        source_format = "pdf"
        text = _extract_pdf(payload)
    elif suffix in {".doc", ".docx"}:
        try:
            text = extract_word_text(filename or f"upload{suffix}", payload)
        except DocxValidationError as exc:
            code = "unsupported_type" if exc.status == 415 else "doc_invalid"
            raise UploadRejected(exc.status, code, exc.message) from exc
        source_format = detect_pasted_format(text)
    else:
        raise UploadRejected(415, "unsupported_type", "只支持 .md / .txt / .pdf / .doc / .docx")

    if len(text) > max_chars:
        raise UploadRejected(413, "document_too_long", f"文档字符数超过上限（{max_chars}）。")
    if not text.strip():
        raise UploadRejected(400, "empty_document", "文档内容为空。")

    return Extracted(text=text, source_format=source_format)


def detect_pasted_format(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("## "):
            return "markdown"
    return "text"
