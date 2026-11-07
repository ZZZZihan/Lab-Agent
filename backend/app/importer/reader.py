"""Safe .docx reader with magic-byte, zip-bomb, macro, merged-cell, and embedded-image extraction."""

from __future__ import annotations

import io
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any, Literal

import docx
from docx.table import Table

ZIP_MAGIC = b"PK\x03\x04"
ZERO_WIDTH_RE = re.compile(r"[​-‏﻿]")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_EMBEDDED_IMAGE_READ_BYTES = 2_097_152 + 1  # Read up to 2 MiB + 1 byte to trigger SIGNATURE_TOO_LARGE safely


@dataclass(frozen=True)
class EmbeddedImage:
    location_kind: Literal["table_cell", "paragraph"]
    table_index: int | None
    row_index: int | None
    cell_index: int | None
    block_index: int  # paragraph / table sequence index in document.xml <w:body>
    rel_id: str  # r:embed or r:id
    media_path: str  # e.g. word/media/image2.jpeg
    data: bytes


@dataclass(frozen=True)
class BodyBlock:
    block_index: int
    kind: Literal["paragraph", "table"]
    text: str
    table_index: int | None = None


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


def _iter_body_elements(container: Any) -> list[Any]:
    """Yield top-level `<w:p>` and `<w:tbl>` elements inside `<w:body>`, unwrapping `<w:sdtContent>`."""
    out: list[Any] = []
    for child in container:
        if child.tag in (f"{{{W_NS}}}p", f"{{{W_NS}}}tbl"):
            out.append(child)
        elif child.tag == f"{{{W_NS}}}sdt":
            sdt_content = child.find(f"{{{W_NS}}}sdtContent")
            if sdt_content is not None:
                out.extend(_iter_body_elements(sdt_content))
    return out


def iter_document_tables(doc: Any) -> list[Any]:
    """Return `docx.table.Table` objects in the exact `<w:body>` traversal order (including `<w:sdt>`-wrapped tables)."""
    return [
        Table(elem, doc._body)
        for elem in _iter_body_elements(doc.element.body)
        if elem.tag == f"{{{W_NS}}}tbl"
    ]


def _extract_element_lines_text(elem: Any) -> str:
    """Extract normalized text from `<w:p>` or `<w:tbl>`, preserving cell, paragraph, and `<w:br/>` boundaries."""
    paragraphs = (
        [elem]
        if elem.tag == f"{{{W_NS}}}p"
        else list(elem.iter(f"{{{W_NS}}}p"))
    )
    def _walk_p(node: Any, out: list[str]) -> None:
        local_name = node.tag.split("}")[-1] if "}" in node.tag else node.tag
        if local_name == "Fallback":
            return
        if node.tag == f"{{{W_NS}}}t":
            out.append(node.text or "")
        elif node.tag in (f"{{{W_NS}}}br", f"{{{W_NS}}}cr"):
            out.append("\n")
        for child in node:
            _walk_p(child, out)

    lines: list[str] = []
    for p in paragraphs:
        chunks: list[str] = []
        _walk_p(p, chunks)
        norm_p = normalize_cell_text("".join(chunks))
        if norm_p:
            lines.append(norm_p)
    return "\n".join(lines)


def _extract_rel_ids_from_element(elem: ET.Element) -> list[str]:
    """Extract embedded image relationship IDs (`a:blip/@r:embed` and `v:imagedata/@r:id`).

    Explicitly ignores external links (`a:blip/@r:link`) and selects at most one branch
    of `<mc:AlternateContent>` (`mc:Choice` preferred over `mc:Fallback`).
    """
    rel_ids: list[str] = []

    def _walk(node: ET.Element) -> None:
        local_name = node.tag.split("}")[-1] if "}" in node.tag else node.tag
        if local_name == "AlternateContent":
            choices = [
                ch
                for ch in node
                if (ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag) == "Choice"
            ]
            fallbacks = [
                ch
                for ch in node
                if (ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag) == "Fallback"
            ]
            for branch in (*choices, *fallbacks):
                branch_ids = _extract_rel_ids_from_element(branch)
                if branch_ids:
                    rel_ids.extend(branch_ids)
                    return
            return
        if local_name == "blip":
            embed_id = node.get(f"{{{R_NS}}}embed")
            if embed_id:
                rel_ids.append(embed_id)
        elif local_name == "imagedata":
            vml_id = node.get(f"{{{R_NS}}}id")
            if vml_id:
                rel_ids.append(vml_id)
        for child in node:
            _walk(child)

    _walk(elem)
    return rel_ids


def _parse_document_media_rels(zf: zipfile.ZipFile) -> dict[str, str]:
    """Parse `word/_rels/document.xml.rels` into `{rel_id: normalized_media_path}`.

    Ignores `TargetMode="External"` and any target outside `word/media/`.
    """
    rels_path = "word/_rels/document.xml.rels"
    if rels_path not in zf.namelist():
        return {}
    raw_rels = zf.read(rels_path)
    if b"<!entity" in raw_rels[:4096].lower() or b"<!doctype" in raw_rels[:4096].lower():
        return {}
    try:
        root = ET.fromstring(raw_rels)
    except ET.ParseError:
        return {}

    mapping: dict[str, str] = {}
    for rel in root.iter():
        local_name = rel.tag.split("}")[-1] if "}" in rel.tag else rel.tag
        if local_name != "Relationship":
            continue
        target_mode = (rel.get("TargetMode") or "").strip().lower()
        if target_mode == "external":
            continue
        rel_id = (rel.get("Id") or "").strip()
        target = (rel.get("Target") or "").strip().replace("\\", "/")
        if not rel_id or not target:
            continue
        if target.startswith("/"):
            norm_path = posixpath.normpath(target.lstrip("/"))
        elif target.startswith("word/"):
            norm_path = posixpath.normpath(target)
        else:
            norm_path = posixpath.normpath(posixpath.join("word", target))
        parts = norm_path.split("/")
        if ".." in parts or not norm_path.startswith("word/media/"):
            continue
        mapping[rel_id] = norm_path
    return mapping


def extract_body_blocks_and_images(
    raw_bytes: bytes,
) -> tuple[list[BodyBlock], list[EmbeddedImage]]:
    """Extract ordered `<w:body>` blocks and all embedded images with coordinates from `.docx` bytes."""
    blocks: list[BodyBlock] = []
    images: list[EmbeddedImage] = []

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        rels = _parse_document_media_rels(zf)
        zip_names = set(zf.namelist())
        if "word/document.xml" not in zip_names:
            return blocks, images

        media_cache: dict[str, bytes] = {}

        def _read_media(media_path: str) -> bytes | None:
            if media_path in media_cache:
                return media_cache[media_path]
            if media_path not in zip_names:
                return None
            info = zf.getinfo(media_path)
            with zf.open(info) as fp:
                # Cap read at 2 MiB + 1 byte so oversized entries trigger SIGNATURE_TOO_LARGE without OOM
                data = fp.read(MAX_EMBEDDED_IMAGE_READ_BYTES)
            media_cache[media_path] = data
            return data

        root = ET.fromstring(zf.read("word/document.xml"))
        body = root.find(f"{{{W_NS}}}body")
        if body is None:
            return blocks, images

        body_elems = _iter_body_elements(body)
        table_counter = 0

        for block_idx, elem in enumerate(body_elems):
            if elem.tag == f"{{{W_NS}}}p":
                text = _extract_element_lines_text(elem)
                blocks.append(
                    BodyBlock(
                        block_index=block_idx,
                        kind="paragraph",
                        text=text,
                        table_index=None,
                    )
                )
                for rel_id in _extract_rel_ids_from_element(elem):
                    media_path = rels.get(rel_id)
                    if not media_path:
                        continue
                    data = _read_media(media_path)
                    if data is None:
                        continue
                    images.append(
                        EmbeddedImage(
                            location_kind="paragraph",
                            table_index=None,
                            row_index=None,
                            cell_index=None,
                            block_index=block_idx,
                            rel_id=rel_id,
                            media_path=media_path,
                            data=data,
                        )
                    )
            elif elem.tag == f"{{{W_NS}}}tbl":
                t_idx = table_counter
                table_counter += 1
                text = _extract_element_lines_text(elem)
                blocks.append(
                    BodyBlock(
                        block_index=block_idx,
                        kind="table",
                        text=text,
                        table_index=t_idx,
                    )
                )
                for r_idx, tr in enumerate(elem.findall(f"{{{W_NS}}}tr")):
                    for c_idx, tc in enumerate(tr.findall(f"{{{W_NS}}}tc")):
                        # Skip vertical merge continuation cells (which have <w:vMerge/> without w:val="restart")
                        vmerge = tc.find(f"./{{{W_NS}}}tcPr/{{{W_NS}}}vMerge")
                        if vmerge is not None:
                            vmerge_val = (vmerge.get(f"{{{W_NS}}}val") or "").strip().lower()
                            if vmerge_val != "restart":
                                continue
                        for rel_id in _extract_rel_ids_from_element(tc):
                            media_path = rels.get(rel_id)
                            if not media_path:
                                continue
                            data = _read_media(media_path)
                            if data is None:
                                continue
                            images.append(
                                EmbeddedImage(
                                    location_kind="table_cell",
                                    table_index=t_idx,
                                    row_index=r_idx,
                                    cell_index=c_idx,
                                    block_index=block_idx,
                                    rel_id=rel_id,
                                    media_path=media_path,
                                    data=data,
                                )
                            )

    return blocks, images


def extract_embedded_images(raw_bytes: bytes) -> list[EmbeddedImage]:
    """Return all embedded images from `.docx` bytes with their structural locations."""
    _, images = extract_body_blocks_and_images(raw_bytes)
    return images
