"""Safe .docx reader with magic-byte, zip-bomb, macro, and merged-cell checks."""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from typing import Any

import docx

ZIP_MAGIC = b"PK\x03\x04"
ZERO_WIDTH_RE = re.compile(r"[​-‏﻿]")


class DocxValidationError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def normalize_cell_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    text = ZERO_WIDTH_RE.sub("", text)
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def normalize_label(raw: str) -> str:
    text = normalize_cell_text(raw)
    return re.sub(r"[\s:：()（）\n]+", "", text)


def validate_and_load_docx(
    filename: str,
    raw_bytes: bytes,
    *,
    max_file_mb: int = 20,
    max_uncompressed_mb: int = 50,
) -> Any:
    lower_name = (filename or "").strip().lower()
    if not lower_name.endswith(".docx"):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "仅支持上传 .docx 格式的申报书文件（请将 .doc 或 .docm 另存为 .docx）",
        )
    if len(raw_bytes) > max_file_mb * 1024 * 1024:
        raise DocxValidationError(
            413,
            "import_file_too_large",
            f"文件大小超过 {max_file_mb}MB 上限",
        )
    if len(raw_bytes) < 4 or raw_bytes[:4] != ZIP_MAGIC:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "不是有效的 .docx (ZIP) 文件",
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                raise DocxValidationError(
                    415,
                    "import_unsupported_format",
                    "缺少 word/document.xml，不是有效的 Word 文档",
                )
            if "word/vbaProject.bin" in names:
                raise DocxValidationError(
                    415,
                    "import_unsupported_format",
                    "不支持含宏的 Word 文档 (.docm)，请另存为普通 .docx",
                )
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > max_uncompressed_mb * 1024 * 1024:
                raise DocxValidationError(
                    413,
                    "import_uncompressed_too_large",
                    f"文档解压后大小超过 {max_uncompressed_mb}MB 安全上限",
                )
            doc_xml_head = zf.read("word/document.xml")[:4096].lower()
            if b"<!entity" in doc_xml_head or b"<!doctype" in doc_xml_head:
                raise DocxValidationError(
                    415,
                    "import_unsupported_format",
                    "文档包含不安全的 XML 实体声明",
                )
    except DocxValidationError:
        raise
    except zipfile.BadZipFile as exc:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .docx 压缩包",
        ) from exc

    try:
        return docx.Document(io.BytesIO(raw_bytes))
    except Exception as exc:
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "无法解析 .docx 文档结构",
        ) from exc


def extract_table_grid(table: Any) -> list[list[str]]:
    """Extract 2D cell text grid from a python-docx Table, deduplicating merged cells by `_tc`."""
    rows: list[list[str]] = []
    for row in table.rows:
        seen_tc: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            tc_id = id(cell._tc)
            if tc_id in seen_tc:
                continue
            seen_tc.add(tc_id)
            cells.append(normalize_cell_text(cell.text))
        rows.append(cells)
    return rows
