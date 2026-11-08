"""Safe .doc / .docx reader with magic-byte, zip-bomb, OLE2, macro, merged-cell, and embedded-image extraction."""

from __future__ import annotations

import bisect
import codecs
import hashlib
import io
import posixpath
import re
import struct
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal

import docx
from docx.table import Table

ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZERO_WIDTH_RE = re.compile(r"[​-‏﻿]")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1f]")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_EMBEDDED_IMAGE_READ_BYTES = 2_097_152 + 1  # Read up to 2 MiB + 1 byte to trigger SIGNATURE_TOO_LARGE safely
MAX_IMPORT_IMAGE_COUNT = 64
MAX_IMPORT_DEDUP_IMAGE_BYTES = 32 * 1024 * 1024  # 32 MiB


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
    quota_exceeded: bool = False


@dataclass(frozen=True)
class BodyBlock:
    block_index: int
    kind: Literal["paragraph", "table"]
    text: str
    table_index: int | None = None


@dataclass(frozen=True)
class ParsedLegacyDoc:
    tables: list[list[list[str]]]
    body_blocks: list[BodyBlock]
    embedded_images: list[EmbeddedImage]


class DocxValidationError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def normalize_cell_text(raw: str) -> str:
    s = raw or ""
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in s):
        s = s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    text = unicodedata.normalize("NFKC", s)
    text = text.replace("\x0b", "\n").replace("\x0c", "\n")
    text = ZERO_WIDTH_RE.sub("", text)
    text = CONTROL_CHARS_RE.sub("", text)
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def normalize_label(raw: str) -> str:
    text = normalize_cell_text(raw)
    return re.sub(r"[\s:：()（）\n]+", "", text)


_MSDOC_SPECIAL_BYTES: dict[int, str] = {
    0x82: "‚",
    0x83: "ƒ",
    0x84: "„",
    0x85: "…",
    0x86: "†",
    0x87: "‡",
    0x88: "ˆ",
    0x89: "‰",
    0x8A: "Š",
    0x8B: "‹",
    0x8C: "Œ",
    0x91: "‘",
    0x92: "’",
    0x93: "“",
    0x94: "”",
    0x95: "•",
    0x96: "–",
    0x97: "—",
    0x98: "˜",
    0x99: "™",
    0x9A: "š",
    0x9B: "›",
    0x9C: "œ",
    0x9F: "Ÿ",
}


def _decode_msdoc_compressed(chunk: bytes) -> str:
    out: list[str] = []
    for b in chunk:
        if b in _MSDOC_SPECIAL_BYTES:
            out.append(_MSDOC_SPECIAL_BYTES[b])
        else:
            out.append(chr(b))
    return "".join(out)


def _strip_msdoc_fields(
    raw: str,
    state: tuple[list[bool], list[int]] | None = None,
) -> str:
    """Strip Word field instructions (`\\x13...\\x14`), keeping visible field results (`\\x14...\\x15`)."""
    if "\x1e" in raw:
        raw = raw.replace("\x1e", "-")
    if state is None:
        if "\x13" not in raw and "\x14" not in raw and "\x15" not in raw:
            return raw
        field_stack: list[bool] = []
        active_instr = [0]
    else:
        field_stack, active_instr = state
        if "\x13" not in raw and "\x14" not in raw and "\x15" not in raw:
            return "" if active_instr[0] > 0 else raw

    out: list[str] = []
    for ch in raw:
        if ch == "\x13":
            field_stack.append(True)
            active_instr[0] += 1
        elif ch == "\x14":
            if field_stack and field_stack[-1]:
                field_stack[-1] = False
                active_instr[0] -= 1
        elif ch == "\x15":
            if field_stack:
                if field_stack.pop():
                    active_instr[0] -= 1
        elif active_instr[0] == 0:
            out.append(ch)
    return "".join(out)


def _sniff_legacy_doc_kind(
    raw_bytes: bytes,
) -> Literal["ole2", "rtf", "xml", "html"] | None:
    if len(raw_bytes) >= 8 and raw_bytes[:8] == OLE2_MAGIC:
        return "ole2"
    sample = raw_bytes[:4096]
    if sample.startswith(b"\xef\xbb\xbf"):
        head_str = sample[3:].decode("latin-1", errors="ignore")
    elif sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        head_str = sample.decode("utf-16", errors="ignore")
    elif (
        b"\x00<" in sample[:128]
        or b"<\x00" in sample[:128]
        or b"{\x00" in sample[:128]
        or b"\x00{" in sample[:128]
    ):
        enc = (
            "utf-16-le"
            if (b"<\x00" in sample[:128] or b"{\x00" in sample[:128])
            else "utf-16-be"
        )
        head_str = sample.decode(enc, errors="ignore")
    else:
        head_str = sample.decode("latin-1", errors="ignore")

    head_lower = head_str.lstrip("﻿ \t\r\n").lower()
    if head_lower.startswith("{\\rtf"):
        return "rtf"
    if (
        head_lower.startswith("<?xml")
        or head_lower.startswith("<w:worddocument")
        or head_lower.startswith("<pkg:package")
    ) and ("word" in head_lower or "<w:" in head_lower):
        return "xml"
    if head_lower.startswith(
        ("<!doctype html", "<html", "<head", "<meta", "<table", "<body")
    ):
        return "html"
    return None


def _looks_like_legacy_doc_bytes(raw_bytes: bytes) -> bool:
    return _sniff_legacy_doc_kind(raw_bytes) is not None


def _read_ole2_streams(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> tuple[dict[str, bytes], set[str]]:
    if len(raw_bytes) < 512 or raw_bytes[:8] != OLE2_MAGIC:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "不是有效的 .doc (OLE2) 文件",
        )
    sector_shift = struct.unpack_from("<H", raw_bytes, 0x1E)[0]
    mini_sector_shift = struct.unpack_from("<H", raw_bytes, 0x20)[0]
    if sector_shift not in (9, 12) or mini_sector_shift != 6:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc (OLE2) 文件头",
        )
    sector_size = 1 << sector_shift
    mini_sector_size = 1 << mini_sector_shift
    max_file_sectors = (len(raw_bytes) - 512) // sector_size
    num_fat_sectors = struct.unpack_from("<I", raw_bytes, 0x2C)[0]
    first_dir_sector = struct.unpack_from("<I", raw_bytes, 0x30)[0]
    mini_cutoff = struct.unpack_from("<I", raw_bytes, 0x38)[0] or 4096
    first_minifat_sector = struct.unpack_from("<I", raw_bytes, 0x3C)[0]
    num_minifat_sectors = struct.unpack_from("<I", raw_bytes, 0x40)[0]
    first_difat_sector = struct.unpack_from("<I", raw_bytes, 0x44)[0]
    num_difat_sectors = struct.unpack_from("<I", raw_bytes, 0x48)[0]

    if (
        num_fat_sectors <= 0
        or num_fat_sectors > max_file_sectors
        or num_difat_sectors > max_file_sectors
        or (num_minifat_sectors > max_file_sectors and first_minifat_sector < 0xFFFFFFFC)
    ):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc (OLE2) 扇区分配表",
        )
    if (
        num_fat_sectors * sector_size > max_uncompressed_bytes
        or num_minifat_sectors * sector_size > max_uncompressed_bytes
    ):
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档解压后大小超过安全上限",
        )

    def _read_sector(sec_id: int) -> bytes:
        offset = (sec_id + 1) * sector_size
        if sec_id < 0 or sec_id >= max_file_sectors or offset + sector_size > len(raw_bytes):
            raise DocxValidationError(
                415,
                "import_unsupported_format",
                "损坏的 .doc 扇区数据",
            )
        return raw_bytes[offset : offset + sector_size]

    difat: list[int] = [
        sec for sec in struct.unpack_from("<109I", raw_bytes, 0x4C) if sec < 0xFFFFFFFC
    ]
    difat_sec = first_difat_sector
    visited_difat: set[int] = set()
    entries_per_sector = sector_size // 4
    for _ in range(min(num_difat_sectors, max_file_sectors, 4096)):
        if difat_sec >= 0xFFFFFFFC:
            raise DocxValidationError(
                415,
                "import_unsupported_format",
                "损坏的 .doc (OLE2) DIFAT 扇区链",
            )
        if difat_sec in visited_difat:
            raise DocxValidationError(
                415,
                "import_unsupported_format",
                "损坏的 .doc (OLE2) DIFAT 重复扇区引用",
            )
        visited_difat.add(difat_sec)
        sec_bytes = _read_sector(difat_sec)
        vals = struct.unpack_from(f"<{entries_per_sector}I", sec_bytes, 0)
        difat.extend(v for v in vals[:-1] if v < 0xFFFFFFFC)
        difat_sec = vals[-1]
    if difat_sec < 0xFFFFFFFC:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc (OLE2) DIFAT 扇区链未正常终止",
        )

    if len(difat) != num_fat_sectors:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc (OLE2) FAT 扇区数量不一致",
        )

    fat: list[int] = []
    seen_fat_sectors: set[int] = set()
    for fat_sec in difat[:num_fat_sectors]:
        if fat_sec in seen_fat_sectors or fat_sec in visited_difat:
            raise DocxValidationError(
                415,
                "import_unsupported_format",
                "损坏的 .doc (OLE2) 重复 FAT 扇区引用",
            )
        seen_fat_sectors.add(fat_sec)
        sec_bytes = _read_sector(fat_sec)
        fat.extend(struct.unpack_from(f"<{entries_per_sector}I", sec_bytes, 0))
    fat = fat[:max_file_sectors]

    def _read_chain(start_sec: int, exact_size: int | None = None) -> bytes:
        if start_sec >= 0xFFFFFFFC or exact_size == 0:
            return b""
        chunks: list[bytes] = []
        total = 0
        sec = start_sec
        visited: set[int] = set()
        limit = exact_size if exact_size is not None else max_uncompressed_bytes
        while sec < 0xFFFFFFFC:
            if sec in visited or sec >= len(fat):
                break
            visited.add(sec)
            blk = _read_sector(sec)
            chunks.append(blk)
            total += len(blk)
            if total > max_uncompressed_bytes:
                raise DocxValidationError(
                    413,
                    "import_uncompressed_too_large",
                    "文档解压后大小超过安全上限",
                )
            if exact_size is not None and total >= limit:
                break
            sec = fat[sec]
        joined = b"".join(chunks)
        return joined[:exact_size] if exact_size is not None else joined

    dir_bytes = _read_chain(first_dir_sector)
    if len(dir_bytes) < 128:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc 目录流",
        )

    minifat: list[int] = []
    if num_minifat_sectors > 0 and first_minifat_sector < 0xFFFFFFFC:
        minifat_bytes = _read_chain(first_minifat_sector, num_minifat_sectors * sector_size)
        count = len(minifat_bytes) // 4
        if count > 0:
            minifat = list(struct.unpack_from(f"<{count}I", minifat_bytes, 0))

    root_start_sec = struct.unpack_from("<I", dir_bytes, 116)[0]
    root_size = struct.unpack_from("<Q", dir_bytes, 120)[0]
    if sector_shift == 9:
        root_size &= 0xFFFFFFFF
    if root_size > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档解压后大小超过安全上限",
        )
    mini_stream = (
        _read_chain(root_start_sec, int(root_size))
        if root_start_sec < 0xFFFFFFFC and root_size > 0
        else b""
    )

    def _read_mini_chain(start_mini_sec: int, exact_size: int) -> bytes:
        if start_mini_sec >= 0xFFFFFFFC or exact_size <= 0:
            return b""
        chunks: list[bytes] = []
        total = 0
        sec = start_mini_sec
        visited: set[int] = set()
        while sec < 0xFFFFFFFC and total < exact_size:
            if sec in visited:
                break
            visited.add(sec)
            off = sec * mini_sector_size
            if off < 0 or off + mini_sector_size > len(mini_stream):
                break
            chunks.append(mini_stream[off : off + mini_sector_size])
            total += mini_sector_size
            if sec >= len(minifat):
                break
            sec = minifat[sec]
        return b"".join(chunks)[:exact_size]

    streams: dict[str, bytes] = {}
    all_names_lower: set[str] = set()
    total_stream_bytes = 0
    num_entries = len(dir_bytes) // 128

    # Collect direct children of Root Entry (entry 0) by traversing only sibling links
    # (left_sib at offset 68, right_sib at offset 72) from Root Entry's child_id (offset 76),
    # without descending into nested storages (obj_type == 1) such as ObjectPool.
    root_direct_indices: set[int] = set()
    if num_entries > 0:
        root_child_id = struct.unpack_from("<I", dir_bytes, 76)[0]
        stack: list[int] = [root_child_id]
        while stack:
            curr = stack.pop()
            if curr == 0 or curr >= num_entries or curr in root_direct_indices:
                continue
            c_entry = dir_bytes[curr * 128 : (curr + 1) * 128]
            if c_entry[66] == 0:
                continue
            root_direct_indices.add(curr)
            left_sib, right_sib = struct.unpack_from("<II", c_entry, 68)
            if left_sib < num_entries:
                stack.append(left_sib)
            if right_sib < num_entries:
                stack.append(right_sib)

    for idx in range(num_entries):
        entry = dir_bytes[idx * 128 : (idx + 1) * 128]
        name_cb = struct.unpack_from("<H", entry, 64)[0]
        obj_type = entry[66]
        if obj_type == 0 or name_cb < 2 or name_cb > 64:
            continue
        name = entry[: name_cb - 2].decode("utf-16-le", errors="ignore").strip("\x00")
        if not name:
            continue
        all_names_lower.add(name.lower())
        if obj_type != 2:
            continue
        start_sec = struct.unpack_from("<I", entry, 116)[0]
        size = struct.unpack_from("<Q", entry, 120)[0]
        if sector_shift == 9:
            size &= 0xFFFFFFFF
        stream_size = int(size)
        total_stream_bytes += stream_size
        if total_stream_bytes > max_uncompressed_bytes:
            raise DocxValidationError(
                413,
                "import_uncompressed_too_large",
                "文档解压后大小超过安全上限",
            )
        if root_direct_indices and idx not in root_direct_indices:
            continue
        if name in streams:
            continue
        if stream_size < mini_cutoff and mini_stream:
            streams[name] = _read_mini_chain(start_sec, stream_size)
        else:
            streams[name] = _read_chain(start_sec, stream_size)

    return streams, all_names_lower


def _parse_papx_table_flags(
    word_doc: bytes,
    table_stream: bytes,
    fc_plcf_bte_papx: int,
    lcb_plcf_bte_papx: int,
) -> tuple[set[int], set[int]]:
    """Parse `PlcfBtePapx` FKP pages to return `(in_table_fc_ends, ttp_fc_ends)`."""
    in_table_fc_ends: set[int] = set()
    ttp_fc_ends: set[int] = set()
    if (
        lcb_plcf_bte_papx < 12
        or fc_plcf_bte_papx < 0
        or fc_plcf_bte_papx + lcb_plcf_bte_papx > len(table_stream)
    ):
        return in_table_fc_ends, ttp_fc_ends

    plc = table_stream[fc_plcf_bte_papx : fc_plcf_bte_papx + lcb_plcf_bte_papx]
    n = (len(plc) - 4) // 8
    if n <= 0:
        return in_table_fc_ends, ttp_fc_ends

    pn_offset = 4 * (n + 1)
    pns = struct.unpack_from(f"<{n}I", plc, pn_offset)

    seen_pns: set[int] = set()
    for pn_raw in pns:
        pn = pn_raw & 0x003FFFFF
        if pn in seen_pns:
            continue
        seen_pns.add(pn)
        page_off = pn * 512
        if page_off < 0 or page_off + 512 > len(word_doc):
            continue
        fkp = word_doc[page_off : page_off + 512]
        cpara = fkp[511]
        if cpara <= 0 or 4 * (cpara + 1) + 13 * cpara > 511:
            continue
        rgfc = struct.unpack_from(f"<{cpara + 1}I", fkp, 0)
        bx_base = 4 * (cpara + 1)
        b_offset_cache: dict[int, tuple[bool, bool]] = {}
        for k in range(cpara):
            fc_end = rgfc[k + 1]
            b_offset = fkp[bx_base + k * 13]
            if b_offset == 0:
                continue
            cached_flags = b_offset_cache.get(b_offset)
            if cached_flags is not None:
                is_in_tbl, is_ttp = cached_flags
                if is_in_tbl:
                    in_table_fc_ends.add(fc_end)
                if is_ttp:
                    ttp_fc_ends.add(fc_end)
                continue
            papx_pos = b_offset * 2
            if papx_pos >= 511:
                b_offset_cache[b_offset] = (False, False)
                continue
            cb = fkp[papx_pos]
            if cb == 0:
                if papx_pos + 2 > 511:
                    b_offset_cache[b_offset] = (False, False)
                    continue
                cb_bytes = fkp[papx_pos + 1] * 2
                grp_start = papx_pos + 4  # skip 2-byte cb + 2-byte istd
                grp_end = min(511, papx_pos + 2 + cb_bytes)
            else:
                cb_bytes = cb * 2 - 1
                grp_start = papx_pos + 3  # skip 1-byte cb + 2-byte istd
                grp_end = min(511, papx_pos + 1 + cb_bytes)
            if grp_start >= grp_end:
                b_offset_cache[b_offset] = (False, False)
                continue
            grpprl = fkp[grp_start:grp_end]
            pos = 0
            is_in_tbl = False
            is_ttp = False
            while pos + 2 <= len(grpprl):
                sprm = struct.unpack_from("<H", grpprl, pos)[0]
                pos += 2
                spra = (sprm >> 13) & 0x7
                if spra in (0, 1):
                    op_len = 1
                elif spra in (2, 4, 5):
                    op_len = 2
                elif spra == 3:
                    op_len = 4
                elif spra == 7:
                    op_len = 3
                else:  # spra == 6 (variable length)
                    if sprm == 0xD608:  # sprmTDefTable
                        if pos + 2 > len(grpprl):
                            break
                        td_cb = struct.unpack_from("<H", grpprl, pos)[0]
                        op_len = max(2, td_cb + 1)
                        is_in_tbl = True
                        is_ttp = True
                    else:
                        if pos >= len(grpprl):
                            break
                        op_len = 1 + grpprl[pos]
                if pos + op_len > len(grpprl):
                    break
                if sprm in (0x2416, 0x244B) and grpprl[pos] != 0:  # sprmPFInTable / InnerTableCell
                    is_in_tbl = True
                elif sprm == 0x6649:  # sprmPItap (table nesting depth)
                    itap = struct.unpack_from("<I", grpprl, pos)[0]
                    if itap > 0:
                        is_in_tbl = True
                elif sprm in (0x2417, 0x244C) and grpprl[pos] != 0:  # sprmPFTtp / InnerTtp
                    is_in_tbl = True
                    is_ttp = True
                pos += op_len
            b_offset_cache[b_offset] = (is_in_tbl, is_ttp)
            if is_in_tbl:
                in_table_fc_ends.add(fc_end)
            if is_ttp:
                ttp_fc_ends.add(fc_end)

    return in_table_fc_ends, ttp_fc_ends


def _finalize_legacy_blocks_and_tables(
    tables: list[list[list[str]]],
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]],
    embedded_images: list[EmbeddedImage] | None = None,
) -> ParsedLegacyDoc:
    """Normalize extracted legacy `.doc` tables/blocks and synthesize foundation table if needed."""
    cleaned_tables: list[list[list[str]]] = []
    table_remap: dict[int, int] = {}
    for old_idx, tbl in enumerate(tables):
        norm_rows = [
            [normalize_cell_text(c) for c in row]
            for row in tbl
            if any(normalize_cell_text(c) for c in row)
        ]
        if norm_rows:
            table_remap[old_idx] = len(cleaned_tables)
            cleaned_tables.append(norm_rows)

    body_blocks: list[BodyBlock] = []
    paragraph_texts: list[str] = []
    for kind, txt, t_idx in raw_blocks:
        if kind == "paragraph":
            norm_t = normalize_cell_text(txt)
            if norm_t:
                paragraph_texts.append(norm_t)
                body_blocks.append(
                    BodyBlock(
                        block_index=len(body_blocks),
                        kind="paragraph",
                        text=norm_t,
                        table_index=None,
                    )
                )
        elif kind == "table" and t_idx is not None and t_idx in table_remap:
            mapped_idx = table_remap[t_idx]
            flat_lines = [
                cell
                for row in cleaned_tables[mapped_idx]
                for cell in row
                if cell
            ]
            body_blocks.append(
                BodyBlock(
                    block_index=len(body_blocks),
                    kind="table",
                    text="\n".join(flat_lines),
                    table_index=mapped_idx,
                )
            )

    # If the foundation anchors ([学术简历]/[前期成果]/[承担项目]) are split across paragraphs or rows/cells,
    # synthesize a consolidated 1x1 table cell so `_extract_t4_foundation` can parse them identically to .docx.
    foundation_markers = (
        "[学术简历]",
        "[前期成果]",
        "[承担项目]",
        "[与本项目关系]",
        "［学术简历］",
        "［前期成果］",
        "［承担项目］",
        "［与本项目关系］",
    )
    all_lines: list[str] = [b.text for b in body_blocks if b.text]
    combined_text = "\n".join(all_lines)
    doc_marker_count = sum(1 for m in foundation_markers if m in combined_text)
    has_single_cell_with_all = doc_marker_count > 0 and any(
        sum(1 for m in foundation_markers if m in cell) >= doc_marker_count
        and len(cell.strip()) > sum(len(m) for m in foundation_markers if m in cell) + 10
        for tbl in cleaned_tables
        for row in tbl
        for cell in row
    )
    if doc_marker_count > 0 and not has_single_cell_with_all:
        first_marker_idx = next(
            (
                idx
                for idx, b in enumerate(body_blocks)
                if any(m in b.text for m in foundation_markers)
            ),
            0,
        )
        foundation_lines = [
            b.text for b in body_blocks[first_marker_idx:] if b.text
        ]
        consolidated_t4 = "\n".join(foundation_lines) if foundation_lines else combined_text
        cleaned_tables.append([[consolidated_t4]])

    return ParsedLegacyDoc(
        tables=cleaned_tables,
        body_blocks=body_blocks,
        embedded_images=embedded_images or [],
    )


def _parse_msdoc_ole2(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    streams, all_names_lower = _read_ole2_streams(
        raw_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    if any(
        macro_name in all_names_lower
        for macro_name in ("vba", "_vba_project", "_vba_project_cur", "macros")
    ):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "不支持含宏的 Word 文档，请移除宏后重试",
        )
    if "encryptedpackage" in all_names_lower:
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "不支持加密的 Word 文档",
        )

    word_doc = streams.get("WordDocument")
    if word_doc is None or len(word_doc) < 68:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "缺少 WordDocument 流，不是有效的 .doc 文档",
        )

    flags = struct.unpack_from("<H", word_doc, 0x000A)[0]
    if flags & 0x0100:  # fEncrypted
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "不支持加密的 Word (.doc) 文档",
        )

    which_tbl = "1Table" if (flags & 0x0200) else "0Table"
    table_stream = (
        streams.get(which_tbl)
        or streams.get("1Table")
        or streams.get("0Table")
    )

    # Parse FIB sections
    csw = struct.unpack_from("<H", word_doc, 0x0020)[0]
    offset_lw = 0x0022 + 2 * csw
    ccp_text = 0
    fc_clx = 0
    lcb_clx = 0
    fc_plcf_bte_papx = 0
    lcb_plcf_bte_papx = 0

    if offset_lw + 2 <= len(word_doc):
        cslw = struct.unpack_from("<H", word_doc, offset_lw)[0]
        rg_lw_start = offset_lw + 2
        if rg_lw_start + 16 <= len(word_doc) and cslw >= 4:
            ccp_text = struct.unpack_from("<I", word_doc, rg_lw_start + 12)[0]
        offset_fclcb = rg_lw_start + 4 * cslw
        if offset_fclcb + 2 <= len(word_doc):
            cb_rg_fc_lcb = struct.unpack_from("<H", word_doc, offset_fclcb)[0]
            rg_fclcb_start = offset_fclcb + 2
            if cb_rg_fc_lcb > 13 and rg_fclcb_start + 14 * 8 <= len(word_doc):
                fc_plcf_bte_papx, lcb_plcf_bte_papx = struct.unpack_from(
                    "<II", word_doc, rg_fclcb_start + 13 * 8
                )
            if cb_rg_fc_lcb > 33 and rg_fclcb_start + 34 * 8 <= len(word_doc):
                fc_clx, lcb_clx = struct.unpack_from(
                    "<II", word_doc, rg_fclcb_start + 33 * 8
                )

    pieces: list[tuple[int, int, int, int, str, bool]] = []  # (py_start, py_end, fc_byte, char_width, text, has_supp)
    py_cursor = 0
    if (
        table_stream is not None
        and lcb_clx > 5
        and 0 <= fc_clx
        and fc_clx + lcb_clx <= len(table_stream)
    ):
        clx = table_stream[fc_clx : fc_clx + lcb_clx]
        pos = 0
        while pos < len(clx) and clx[pos] == 0x01:
            if pos + 3 > len(clx):
                break
            cb_grpprl = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + cb_grpprl
        if pos + 5 <= len(clx) and clx[pos] == 0x02:
            pcdt_lcb = struct.unpack_from("<I", clx, pos + 1)[0]
            plc_pcd = clx[pos + 5 : pos + 5 + pcdt_lcb]
            if len(plc_pcd) >= 16 and (len(plc_pcd) - 4) % 12 == 0:
                n_pieces = (len(plc_pcd) - 4) // 12
                cps = struct.unpack_from(f"<{n_pieces + 1}I", plc_pcd, 0)
                pcd_base = 4 * (n_pieces + 1)
                total_piece_bytes = 0
                for i in range(n_pieces):
                    cp_s, cp_e = cps[i], cps[i + 1]
                    if ccp_text > 0:
                        if cp_s >= ccp_text:
                            break
                        if cp_e > ccp_text:
                            cp_e = ccp_text
                    if cp_e <= cp_s:
                        continue
                    char_count = cp_e - cp_s
                    fc_val = struct.unpack_from("<I", plc_pcd, pcd_base + i * 8 + 2)[0]
                    compressed = bool(fc_val & 0x40000000)
                    if compressed:
                        fc_byte = (fc_val & 0x3FFFFFFF) // 2
                        byte_len = char_count
                        if fc_byte < 0 or fc_byte + byte_len > len(word_doc):
                            continue
                        total_piece_bytes += byte_len
                        if total_piece_bytes > max_uncompressed_bytes:
                            raise DocxValidationError(
                                413,
                                "import_uncompressed_too_large",
                                "文档解压后大小超过安全上限",
                            )
                        raw_slice = word_doc[fc_byte : fc_byte + byte_len]
                        txt_slice = _decode_msdoc_compressed(raw_slice)
                        pieces.append(
                            (
                                py_cursor,
                                py_cursor + len(txt_slice),
                                fc_byte,
                                1,
                                txt_slice,
                                False,
                            )
                        )
                        py_cursor += len(txt_slice)
                    else:
                        fc_byte = fc_val & 0x3FFFFFFF
                        byte_len = char_count * 2
                        if fc_byte < 0 or fc_byte + byte_len > len(word_doc):
                            continue
                        total_piece_bytes += byte_len
                        if total_piece_bytes > max_uncompressed_bytes:
                            raise DocxValidationError(
                                413,
                                "import_uncompressed_too_large",
                                "文档解压后大小超过安全上限",
                            )
                        raw_slice = word_doc[fc_byte : fc_byte + byte_len]
                        txt_slice = raw_slice.decode("utf-16-le", errors="replace")
                        has_supp = len(txt_slice) * 2 != len(raw_slice)
                        pieces.append(
                            (
                                py_cursor,
                                py_cursor + len(txt_slice),
                                fc_byte,
                                2,
                                txt_slice,
                                has_supp,
                            )
                        )
                        py_cursor += len(txt_slice)

    if not pieces:
        fc_min, fc_mac = struct.unpack_from("<II", word_doc, 0x0018)
        if 0 < fc_min < fc_mac <= len(word_doc):
            raw_body = word_doc[fc_min:fc_mac]
            is_16bit = bool(flags & 0x0400) or (b"\x00" in raw_body[:64])
            if is_16bit:
                if ccp_text > 0:
                    raw_body = raw_body[: ccp_text * 2]
                txt = raw_body.decode("utf-16-le", errors="replace")
                pieces.append(
                    (0, len(txt), fc_min, 2, txt, len(txt) * 2 != len(raw_body))
                )
            else:
                if ccp_text > 0:
                    raw_body = raw_body[:ccp_text]
                txt = _decode_msdoc_compressed(raw_body)
                pieces.append((0, len(txt), fc_min, 1, txt, False))

    if not pieces:
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "无法解析 .doc 文档文本流",
        )

    main_text = "".join(p[4] for p in pieces)
    piece_starts = [p[0] for p in pieces]
    supp_cursor = [-1, 0, 0]

    def _cp_to_fc_end(py_idx: int) -> int:
        p_idx = bisect.bisect_right(piece_starts, py_idx) - 1
        if 0 <= p_idx < len(pieces):
            py_s, py_e, fc_b, width, txt_slice, has_supp = pieces[p_idx]
            if py_s <= py_idx < py_e:
                rel = py_idx - py_s + 1
                if width == 1:
                    return fc_b + rel
                if not has_supp:
                    return fc_b + rel * 2
                if supp_cursor[0] == p_idx and rel >= supp_cursor[1]:
                    u16_len = supp_cursor[2] + len(
                        txt_slice[supp_cursor[1] : rel].encode(
                            "utf-16-le", errors="surrogatepass"
                        )
                    )
                else:
                    u16_len = len(
                        txt_slice[:rel].encode("utf-16-le", errors="surrogatepass")
                    )
                supp_cursor[0] = p_idx
                supp_cursor[1] = rel
                supp_cursor[2] = u16_len
                return fc_b + u16_len
        return -1

    in_table_fc_ends, ttp_fc_ends = (
        _parse_papx_table_flags(word_doc, table_stream, fc_plcf_bte_papx, lcb_plcf_bte_papx)
        if table_stream is not None
        else (set(), set())
    )

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []
    current_table: list[list[str]] = []
    current_row: list[str] = []
    cell_lines: list[str] = []

    def _flush_table() -> None:
        nonlocal current_table, current_row, cell_lines
        if cell_lines:
            current_row.append("\n".join(cell_lines))
            cell_lines = []
        if current_row:
            if any(c.strip() for c in current_row):
                current_table.append(current_row)
            current_row = []
        if current_table:
            t_idx = len(tables)
            tables.append(current_table)
            raw_blocks.append(("table", "", t_idx))
            current_table = []

    # Tokenize main_text by \r and \x07 while preserving cp indices
    n_chars = len(main_text)
    idx = 0
    seg_start = 0
    field_state: tuple[list[bool], list[int]] = ([], [0])
    while idx < n_chars:
        ch = main_text[idx]
        if ch not in ("\r", "\x07"):
            idx += 1
            continue

        raw_seg = _strip_msdoc_fields(main_text[seg_start:idx], state=field_state)
        fc_end = _cp_to_fc_end(idx)

        # Check for \r\x07 pair (standard row-end or cell-end sequence)
        if ch == "\r" and idx + 1 < n_chars and main_text[idx + 1] == "\x07":
            fc_end_next = _cp_to_fc_end(idx + 1)
            is_ttp = (
                (fc_end in ttp_fc_ends or fc_end_next in ttp_fc_ends)
                if ttp_fc_ends
                else (not raw_seg.strip() and not cell_lines)
            )
            if is_ttp:
                if raw_seg.strip() or cell_lines:
                    if raw_seg.strip():
                        cell_lines.append(raw_seg)
                    current_row.append("\n".join(cell_lines))
                    cell_lines = []
                if current_row:
                    current_table.append(current_row)
                    current_row = []
            else:
                if raw_seg.strip():
                    cell_lines.append(raw_seg)
                current_row.append("\n".join(cell_lines))
                cell_lines = []
            idx += 2
            seg_start = idx
            continue

        if ch == "\x07":
            if ttp_fc_ends:
                is_ttp = fc_end in ttp_fc_ends
            else:
                expected_cols = len(current_table[0]) if current_table else 0
                next_has_cell_Quick = "\x07" in main_text[idx + 1 : idx + 200]
                is_ttp = (
                    not raw_seg.strip()
                    and not cell_lines
                    and bool(current_row)
                    and (
                        (expected_cols > 0 and len(current_row) >= expected_cols)
                        or not next_has_cell_Quick
                    )
                )
            if is_ttp:
                if raw_seg.strip() or cell_lines:
                    if raw_seg.strip():
                        cell_lines.append(raw_seg)
                    current_row.append("\n".join(cell_lines))
                    cell_lines = []
                if current_row:
                    current_table.append(current_row)
                    current_row = []
            else:
                if raw_seg.strip():
                    cell_lines.append(raw_seg)
                current_row.append("\n".join(cell_lines))
                cell_lines = []
            idx += 1
            seg_start = idx
            continue

        # ch == "\r" (not immediately followed by \x07)
        in_tbl = (
            (fc_end in in_table_fc_ends)
            if in_table_fc_ends
            else bool(current_row or cell_lines)
        )
        if in_tbl:
            if raw_seg.strip():
                cell_lines.append(raw_seg)
        else:
            _flush_table()
            if raw_seg.strip():
                raw_blocks.append(("paragraph", raw_seg, None))
        idx += 1
        seg_start = idx

    if seg_start < n_chars:
        tail = _strip_msdoc_fields(main_text[seg_start:], state=field_state).strip()
        if tail:
            if current_row or cell_lines:
                cell_lines.append(tail)
            else:
                _flush_table()
                raw_blocks.append(("paragraph", tail, None))
    _flush_table()

    return _finalize_legacy_blocks_and_tables(tables, raw_blocks)


def _parse_word2003_xml_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    if len(raw_bytes) > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档大小超过安全上限",
        )
    decoded_xml = _decode_html_doc_bytes(raw_bytes).lstrip("﻿")
    lower_xml = decoded_xml.lower()
    lower_bytes = raw_bytes.lower()
    if (
        b"<!entity" in lower_bytes
        or b"<!doctype" in lower_bytes
        or "<!entity" in lower_xml
        or "<!doctype" in lower_xml
    ):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "文档包含不安全的 XML 实体声明",
        )
    try:
        root = ET.fromstring(raw_bytes)
    except (ET.ParseError, ValueError, LookupError):
        cleaned_xml = re.sub(
            r"(<\?xml\b[^>]*?)\bencoding\s*=\s*[\"'][^\"']*[\"']",
            r"\1",
            decoded_xml,
            count=1,
            flags=re.IGNORECASE,
        )
        try:
            root = ET.fromstring(cleaned_xml.encode("utf-8"))
        except (ET.ParseError, ValueError, LookupError) as exc:
            raise DocxValidationError(
                422,
                "import_parse_failed",
                "无法解析 Word XML (.doc) 文档结构",
            ) from exc

    def _local(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    def _p_text(p_elem: ET.Element) -> str:
        parts: list[str] = []

        def _walk(node: ET.Element) -> None:
            if _is_wordml_deletion(node):
                return
            ln = _local(node.tag)
            if ln == "oMath":
                parts.append(_render_omath_node(node))
                return
            if ln == "t" and node.text:
                parts.append(node.text)
            elif ln in ("br", "cr"):
                parts.append("\n")
            elif ln in ("tab", "ptab"):
                parts.append("\t")
            elif ln == "noBreakHyphen":
                parts.append("-")
            for ch in node:
                _walk(ch)

        _walk(p_elem)
        return normalize_cell_text("".join(parts))

    def _iter_wordml_blocks(elem: ET.Element) -> list[ET.Element]:
        out: list[ET.Element] = []
        for ch in elem:
            if _is_wordml_deletion(ch):
                continue
            ln = _local(ch.tag)
            if ln in ("p", "tbl", "oMathPara", "oMath"):
                out.append(ch)
            elif ln in (
                "sect",
                "sub-section",
                "sdt",
                "sdtContent",
                "customXml",
                "body",
                "annotation",
                "ins",
            ):
                out.extend(_iter_wordml_blocks(ch))
        return out

    def _iter_wordml_trs(elem: ET.Element) -> list[ET.Element]:
        out: list[ET.Element] = []
        for ch in elem:
            if _is_wordml_deletion(ch):
                continue
            ln = _local(ch.tag)
            if ln == "tr":
                out.append(ch)
            elif ln in ("sdt", "sdtContent", "customXml", "annotation", "ins"):
                out.extend(_iter_wordml_trs(ch))
        return out

    def _iter_wordml_cell_ps(elem: ET.Element) -> list[ET.Element]:
        out: list[ET.Element] = []
        for ch in elem:
            if _is_wordml_deletion(ch):
                continue
            ln = _local(ch.tag)
            if ln == "p":
                out.append(ch)
            else:
                out.extend(_iter_wordml_cell_ps(ch))
        return out

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []

    body_nodes = [el for el in root.iter() if _local(el.tag) == "body"]
    container = body_nodes[0] if body_nodes else root

    for child in _iter_wordml_blocks(container):
        ln = _local(child.tag)
        if ln in ("p", "oMathPara", "oMath"):
            txt = _p_text(child)
            if txt:
                raw_blocks.append(("paragraph", txt, None))
        elif ln == "tbl":
            grid: list[list[str]] = []
            propagated_chars = 0
            for tr in _iter_wordml_trs(child):
                row_cells: list[str] = []
                for tc in tr:
                    if _local(tc.tag) != "tc":
                        continue
                    cell_ps = [
                        _p_text(p)
                        for p in _iter_wordml_cell_ps(tc)
                    ]
                    cell_text = "\n".join(cp for cp in cell_ps if cp)
                    tc_pr = next(
                        (ch for ch in tc if _local(ch.tag).lower() == "tcpr"),
                        None,
                    )
                    if not cell_text and grid and len(row_cells) < len(grid[-1]):
                        has_vmerge_cont = False
                        if tc_pr is not None:
                            for sub in tc_pr:
                                if _local(sub.tag).lower() == "vmerge":
                                    val = next(
                                        (
                                            v
                                            for k, v in sub.attrib.items()
                                            if _local(k).lower() == "val"
                                        ),
                                        "continue",
                                    )
                                    if val.lower() != "restart":
                                        has_vmerge_cont = True
                                    break
                        if has_vmerge_cont:
                            prev_cell = grid[-1][len(row_cells)]
                            if (
                                len(prev_cell) <= 256
                                and propagated_chars + len(prev_cell) <= 65_536
                            ):
                                cell_text = prev_cell
                                propagated_chars += len(prev_cell)
                    grid_span = 1
                    if tc_pr is not None:
                        for sub in tc_pr:
                            if _local(sub.tag).lower() == "gridspan":
                                val = next(
                                    (
                                        v
                                        for k, v in sub.attrib.items()
                                        if _local(k).lower() == "val"
                                    ),
                                    "1",
                                )
                                try:
                                    grid_span = max(1, min(int(val), 64))
                                except ValueError:
                                    grid_span = 1
                                break
                    row_cells.append(cell_text)
                    for _ in range(grid_span - 1):
                        row_cells.append("")
                if row_cells:
                    grid.append(row_cells)
            if grid:
                t_idx = len(tables)
                tables.append(grid)
                raw_blocks.append(("table", "", t_idx))

    return _finalize_legacy_blocks_and_tables(tables, raw_blocks)


class _HtmlDocParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []
        self._in_table = 0
        self._current_table: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._current_para: list[str] = []
        self._skip_depth = 0
        self._pending_spans: dict[int, tuple[int, str]] = {}
        self._propagated_chars = 0
        self._col_idx = 0
        self._cell_rowspan = 1
        self._cell_colspan = 1

    def _consume_pending_spans(self) -> None:
        while self._current_row is not None and self._col_idx in self._pending_spans:
            rem, span_text = self._pending_spans.pop(self._col_idx)
            self._current_row.append(span_text)
            if rem > 1:
                self._pending_spans[self._col_idx] = (rem - 1, span_text)
            self._col_idx += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "head", "del"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t == "table":
            if self._in_table == 0 and self._current_para:
                txt = "".join(self._current_para).strip()
                if txt:
                    self.raw_blocks.append(("paragraph", txt, None))
                self._current_para = []
            self._in_table += 1
            if self._in_table == 1:
                self._current_table = []
                self._pending_spans.clear()
                self._propagated_chars = 0
        elif t == "tr" and self._in_table == 1:
            self._current_row = []
            self._col_idx = 0
        elif t in ("td", "th") and self._in_table == 1:
            self._current_cell = []
            attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
            try:
                rs = int(attr_map.get("rowspan", "1") or "1")
            except ValueError:
                rs = 1
            try:
                cs = int(attr_map.get("colspan", "1") or "1")
            except ValueError:
                cs = 1
            self._cell_rowspan = max(1, min(rs, 256))
            self._cell_colspan = max(1, min(cs, 64))
        elif t == "br":
            if self._current_cell is not None:
                self._current_cell.append("\n")
            elif self._in_table == 0:
                self._current_para.append("\n")
        elif t in ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            if self._current_cell is not None and self._current_cell:
                self._current_cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "head", "del") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t in ("td", "th") and self._in_table == 1:
            if self._current_row is not None and self._current_cell is not None:
                self._consume_pending_spans()
                cell_text = normalize_cell_text("".join(self._current_cell))
                for c_off in range(self._cell_colspan):
                    col_val = cell_text if c_off == 0 else ""
                    self._current_row.append(col_val)
                    if self._cell_rowspan > 1:
                        rem_rows = self._cell_rowspan - 1
                        span_cost = len(col_val) * rem_rows
                        if (
                            len(col_val) <= 256
                            and self._propagated_chars + span_cost <= 65_536
                        ):
                            span_val = col_val
                            self._propagated_chars += span_cost
                        else:
                            span_val = ""
                        self._pending_spans[self._col_idx] = (
                            rem_rows,
                            span_val,
                        )
                    self._col_idx += 1
                    self._consume_pending_spans()
            self._current_cell = None
        elif t in ("td", "th") and self._in_table > 1:
            if self._current_cell is not None:
                self._current_cell.append(" ")
        elif t == "tr" and self._in_table == 1:
            if self._current_row is not None:
                self._consume_pending_spans()
                for col_k in sorted(k for k in self._pending_spans if k >= self._col_idx):
                    while self._col_idx < col_k:
                        self._current_row.append("")
                        self._col_idx += 1
                    self._consume_pending_spans()
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = None
        elif t == "tr" and self._in_table > 1:
            if self._current_cell is not None:
                self._current_cell.append("\n")
        elif t == "table" and self._in_table > 0:
            if self._in_table == 1:
                self._pending_spans.clear()
                if self._current_table:
                    t_idx = len(self.tables)
                    self.tables.append(self._current_table)
                    self.raw_blocks.append(("table", "", t_idx))
                    self._current_table = []
            self._in_table -= 1
        elif t in ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6") and self._in_table == 0:
            txt = normalize_cell_text("".join(self._current_para))
            if txt:
                if t in ("h1", "h2", "h3") and not txt.startswith("#"):
                    txt = f"## {txt}"
                self.raw_blocks.append(("paragraph", txt, None))
            self._current_para = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._current_cell is not None:
            self._current_cell.append(data)
        elif self._in_table == 0:
            self._current_para.append(data)


def _decode_html_doc_bytes(raw_bytes: bytes) -> str:
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw_bytes.decode("utf-16", errors="replace")
    if b"\x00<" in raw_bytes[:128] or b"<\x00" in raw_bytes[:128]:
        enc = "utf-16-le" if b"<\x00" in raw_bytes[:128] else "utf-16-be"
        return raw_bytes.decode(enc, errors="replace")

    head_latin1 = raw_bytes[:4096].decode("latin-1", errors="ignore")
    charset_match = re.search(
        r"<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)",
        head_latin1,
        re.IGNORECASE,
    ) or re.search(
        r"<\?xml\b[^>]*?\bencoding\s*=\s*[\"']\s*([a-zA-Z0-9._-]+)",
        head_latin1,
        re.IGNORECASE,
    )
    if charset_match is not None:
        declared = charset_match.group(1).strip().lower()
        if declared in ("gb2312", "gbk", "gb18030", "x-gbk"):
            declared = "gb18030"
        try:
            codecs.lookup(declared)
            return raw_bytes.decode(declared, errors="replace")
        except LookupError:
            pass

    for enc in ("utf-8-sig", "gb18030", "cp1252"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_html_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    if len(raw_bytes) > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档大小超过安全上限",
        )
    text = _decode_html_doc_bytes(raw_bytes)
    parser = _HtmlDocParser()
    parser.feed(text)
    if parser._current_para:
        rem = normalize_cell_text("".join(parser._current_para))
        if rem:
            parser.raw_blocks.append(("paragraph", rem, None))
    return _finalize_legacy_blocks_and_tables(parser.tables, parser.raw_blocks)


_RTF_CODEPAGE_MAP: dict[int, str] = {
    437: "cp437",
    850: "cp850",
    852: "cp852",
    866: "cp866",
    874: "cp874",
    932: "cp932",
    936: "gb18030",
    949: "cp949",
    950: "cp950",
    1250: "cp1250",
    1251: "cp1251",
    1252: "cp1252",
    1253: "cp1253",
    1254: "cp1254",
    1255: "cp1255",
    1256: "cp1256",
    1257: "cp1257",
    1258: "cp1258",
    10000: "mac_roman",
    54936: "gb18030",
    65001: "utf-8",
}

_RTF_CHARSET_MAP: dict[int, str] = {
    0: "cp1252",
    2: "cp1252",
    77: "mac_roman",
    128: "cp932",
    129: "cp949",
    130: "johab",
    134: "gb18030",
    136: "cp950",
    161: "cp1253",
    162: "cp1254",
    163: "cp1258",
    177: "cp1255",
    178: "cp1256",
    186: "cp1257",
    204: "cp1251",
    222: "cp874",
    238: "cp1250",
    254: "cp437",
    255: "cp850",
}

_RTF_SKIP_DESTINATIONS: frozenset[str] = frozenset(
    {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "info",
        "pict",
        "object",
        "header",
        "footer",
        "headerl",
        "headerr",
        "headerf",
        "footerl",
        "footerr",
        "footerf",
        "fldinst",
        "bkmkstart",
        "bkmkend",
        "datafield",
    }
)


def _strip_rtf_nonbody_groups(raw: str) -> str:
    """Remove `{\\*...}` and header/instruction destination groups (`\\fonttbl`, `\\fldinst`, `\\pict`, etc.) with nested braces and `\\binN`."""
    out: list[str] = []
    i = 0
    n = len(raw)
    skip_depth = 0
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 < n and raw[i + 1] in ("\\", "{", "}"):
                if skip_depth == 0:
                    out.append(raw[i : i + 2])
                i += 2
                continue
            m_bin = re.match(r"\\bin(-?\d+) ?", raw[i : i + 32], re.IGNORECASE)
            if m_bin is not None:
                bin_len = max(0, int(m_bin.group(1)))
                i = min(n, i + m_bin.end() + bin_len)
                continue
        if ch == "{":
            if skip_depth > 0:
                skip_depth += 1
                i += 1
                continue
            m_upr = re.match(r"\{\s*\\upr(?![a-zA-Z]) ?", raw[i : i + 32], re.IGNORECASE)
            if m_upr is not None:
                out.append("{")
                i += m_upr.end()
                upr_depth = 0
                while i < n:
                    uch = raw[i]
                    if uch == "\\":
                        if i + 1 < n and raw[i + 1] in ("\\", "{", "}"):
                            i += 2
                            continue
                        m_ubin = re.match(
                            r"\\bin(-?\d+) ?", raw[i : i + 32], re.IGNORECASE
                        )
                        if m_ubin is not None:
                            i = min(n, i + m_ubin.end() + max(0, int(m_ubin.group(1))))
                            continue
                        i += 1
                        continue
                    if uch == "{":
                        if upr_depth == 0:
                            m_ud_in = re.match(
                                r"\{\s*(?:\\\*\s*)?\\ud(?![a-zA-Z]) ?",
                                raw[i : i + 32],
                                re.IGNORECASE,
                            )
                            if m_ud_in is not None:
                                out.append("{")
                                i += m_ud_in.end()
                                break
                        upr_depth += 1
                        i += 1
                        continue
                    if uch == "}":
                        if upr_depth > 0:
                            upr_depth -= 1
                            i += 1
                            continue
                        out.append("}")
                        i += 1
                        break
                    i += 1
                continue
            m_ud = re.match(
                r"\{\s*(?:\\\*\s*)?\\ud(?![a-zA-Z]) ?", raw[i : i + 32], re.IGNORECASE
            )
            if m_ud is not None:
                out.append("{")
                i += m_ud.end()
                continue
            m_star = re.match(r"\{\s*\\\*\s*\\[a-zA-Z]+", raw[i : i + 40])
            m_dest = re.match(r"\{\s*\\([a-zA-Z]+)(?![a-zA-Z])", raw[i : i + 40])
            if m_star is not None or (
                m_dest is not None and m_dest.group(1).lower() in _RTF_SKIP_DESTINATIONS
            ):
                skip_depth = 1
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            if skip_depth > 0:
                skip_depth -= 1
                if skip_depth == 0:
                    out.append("{}")
            else:
                out.append(ch)
            i += 1
            continue
        if skip_depth == 0:
            out.append(ch)
        i += 1
    return "".join(out)


def _resolve_rtf_codepage_num(cpg: int) -> str | None:
    if cpg in _RTF_CODEPAGE_MAP:
        return _RTF_CODEPAGE_MAP[cpg]
    cand = f"cp{cpg}"
    try:
        codecs.lookup(cand)
        return cand
    except LookupError:
        return None


def _resolve_rtf_encoding(raw_str: str) -> str | None:
    cpg_match = re.search(r"\\ansicpg(\d+)(?![a-zA-Z0-9])", raw_str[:4096])
    if cpg_match is not None:
        resolved = _resolve_rtf_codepage_num(int(cpg_match.group(1)))
        if resolved is not None:
            return resolved
    if re.search(r"\\mac(?![a-zA-Z])", raw_str[:1024]):
        return "mac_roman"
    if re.search(r"\\pca(?![a-zA-Z])", raw_str[:1024]):
        return "cp850"
    if re.search(r"\\pc(?![a-zA-Z])", raw_str[:1024]):
        return "cp437"
    if re.search(r"\\ansi(?![a-zA-Z])", raw_str[:1024]):
        return "cp1252"
    return None


def _extract_rtf_font_encodings(raw_str: str) -> tuple[dict[int, str], int | None]:
    """Parse `\\deffN` and `{\\fonttbl ...}` to map font index `N` -> Python codec name."""
    deff_match = re.search(r"\\deff(-?\d+)(?![a-zA-Z0-9])", raw_str[:4096])
    default_font = int(deff_match.group(1)) if deff_match is not None else None
    font_map: dict[int, str] = {}
    m_ft = re.search(r"\{\s*\\fonttbl(?![a-zA-Z])", raw_str)
    if m_ft is None:
        return font_map, default_font

    i = m_ft.start()
    n = len(raw_str)
    depth = 0
    cur_f: int | None = None
    pending_enc: str | None = None
    while i < n:
        ch = raw_str[i]
        if ch == "\\" and i + 1 < n and raw_str[i + 1] in ("\\", "{", "}"):
            i += 2
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            if depth <= 2:
                if cur_f is not None and pending_enc is not None and cur_f not in font_map:
                    font_map[cur_f] = pending_enc
                cur_f = None
                pending_enc = None
            depth -= 1
            i += 1
            if depth <= 0:
                break
            continue
        if ch == ";":
            if cur_f is not None and pending_enc is not None and cur_f not in font_map:
                font_map[cur_f] = pending_enc
            cur_f = None
            pending_enc = None
            i += 1
            continue
        if ch == "\\":
            m_cw = re.match(r"\\([a-zA-Z]+)(-?\d*) ?", raw_str[i : i + 64])
            if m_cw is not None:
                word = m_cw.group(1).lower()
                arg_str = m_cw.group(2)
                i += m_cw.end()
                if word == "f" and arg_str:
                    cur_f = int(arg_str)
                    if pending_enc is not None:
                        font_map[cur_f] = pending_enc
                        pending_enc = None
                elif word == "fcharset" and arg_str:
                    cs_enc = _RTF_CHARSET_MAP.get(int(arg_str))
                    if cs_enc is not None:
                        if cur_f is not None:
                            font_map[cur_f] = cs_enc
                        else:
                            pending_enc = cs_enc
                elif word == "cpg" and arg_str:
                    cpg_enc = _resolve_rtf_codepage_num(int(arg_str))
                    if cpg_enc is not None:
                        if cur_f is not None:
                            font_map[cur_f] = cpg_enc
                        else:
                            pending_enc = cpg_enc
                continue
        i += 1
    return font_map, default_font


_RTF_CHAR_CONTROL_WORDS: dict[str, str] = {
    "emdash": "—",
    "endash": "–",
    "emspace": " ",
    "enspace": " ",
    "qmspace": " ",
    "bullet": "•",
    "lquote": "‘",
    "rquote": "’",
    "ldblquote": "“",
    "rdblquote": "”",
    "zwj": "‍",
    "zwnj": "‌",
    "ltrmark": "‎",
    "rtlmark": "‏",
    "zwbo": "​",
    "zwnbo": "﻿",
}


def _tokenize_rtf_body(
    s: str,
    declared_enc: str | None,
    font_encodings: dict[int, str] | None = None,
    default_font: int | None = None,
) -> str:
    """Single-pass RTF body tokenizer preserving literal braces/backslashes, `\\ucN` counts, active font charset, hidden/deleted state (`\\v`, `\\deleted`), and codepage bytes."""
    out: list[str] = []
    byte_buf = bytearray()
    uc = 1
    font_enc_map = font_encodings or {}
    active_font: int | None = default_font
    hidden = False
    deleted = False
    state_stack: list[tuple[int, int | None, bool, bool]] = []

    def _current_enc() -> str | None:
        if active_font is not None and active_font in font_enc_map:
            return font_enc_map[active_font]
        return declared_enc

    def _sanitize_literal(text: str) -> str:
        return (
            text.replace("\x07", " ")
            .replace("\r", " ")
            .replace("\x1d", " ")
            .replace("\x1e", " ")
        )

    def _flush_bytes() -> None:
        if not byte_buf:
            return
        b = bytes(byte_buf)
        byte_buf.clear()
        if hidden or deleted:
            return
        eff_enc = _current_enc()
        if eff_enc is not None:
            decoded = b.decode(eff_enc, errors="replace")
        else:
            try:
                decoded = b.decode("gb18030", errors="strict")
            except UnicodeDecodeError:
                decoded = b.decode("cp1252", errors="replace")
        out.append(_sanitize_literal(decoded))

    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch in ("\r", "\n"):
            i += 1
            continue
        if ch == "{":
            _flush_bytes()
            state_stack.append((uc, active_font, hidden, deleted))
            i += 1
            continue
        if ch == "}":
            _flush_bytes()
            if state_stack:
                uc, active_font, hidden, deleted = state_stack.pop()
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                i += 1
                continue
            nxt = s[i + 1]
            if nxt in ("\\", "{", "}"):
                if not (hidden or deleted):
                    if byte_buf:
                        byte_buf.append(ord(nxt))
                    else:
                        out.append(nxt)
                i += 2
                continue
            if nxt in ("\r", "\n"):
                _flush_bytes()
                out.append("\r")
                i += 2
                if nxt == "\r" and i < n and s[i] == "\n":
                    i += 1
                continue
            if nxt == "~":
                _flush_bytes()
                if not (hidden or deleted):
                    out.append("\u00a0")
                i += 2
                continue
            if nxt == "-":
                _flush_bytes()
                i += 2
                continue
            if nxt == "_":
                _flush_bytes()
                if not (hidden or deleted):
                    out.append("-")
                i += 2
                continue
            if (
                nxt == "'"
                and i + 3 < n
                and all(c in "0123456789abcdefABCDEF" for c in s[i + 2 : i + 4])
            ):
                if not (hidden or deleted):
                    byte_buf.append(int(s[i + 2 : i + 4], 16))
                i += 4
                continue

            m_cw = re.match(r"\\([a-zA-Z]+)(-?\d*) ?", s[i : i + 64])
            if m_cw is not None:
                _flush_bytes()
                word = m_cw.group(1).lower()
                arg_str = m_cw.group(2)
                i += m_cw.end()
                if word == "uc" and arg_str:
                    uc = max(0, int(arg_str))
                    continue
                if word == "bin" and arg_str:
                    i = min(n, i + max(0, int(arg_str)))
                    continue
                if word == "f" and arg_str:
                    active_font = int(arg_str)
                    continue
                if word == "af" and arg_str:
                    af_idx = int(arg_str)
                    if af_idx in font_enc_map and active_font is None:
                        active_font = af_idx
                    continue
                if word == "v":
                    hidden = arg_str != "0"
                    continue
                if word == "deleted":
                    deleted = arg_str != "0"
                    continue
                if word == "plain":
                    active_font = default_font
                    hidden = False
                    deleted = False
                    continue
                if word == "u" and arg_str:
                    code = int(arg_str)
                    if code < 0:
                        code += 65536
                    if not (hidden or deleted) and 0 <= code <= 0x10FFFF:
                        out.append(_sanitize_literal(chr(code)))
                    skipped = 0
                    while skipped < uc and i < n:
                        if s[i] in ("\r", "\n"):
                            i += 1
                            continue
                        if s[i] in ("{", "}"):
                            break
                        if (
                            s[i] == "\\"
                            and i + 3 < n
                            and s[i + 1] == "'"
                            and all(
                                c in "0123456789abcdefABCDEF"
                                for c in s[i + 2 : i + 4]
                            )
                        ):
                            i += 4
                            skipped += 1
                        elif s[i] == "\\":
                            m_next_cw = re.match(
                                r"\\([a-zA-Z]+)(-?\d*) ?", s[i : i + 32]
                            )
                            if m_next_cw is not None:
                                if m_next_cw.group(1).lower() in (
                                    "par",
                                    "cell",
                                    "nestcell",
                                    "row",
                                    "nestrow",
                                    "trowd",
                                    "u",
                                    "uc",
                                    "pard",
                                    "intbl",
                                    "cellx",
                                ):
                                    break
                                i += m_next_cw.end()
                                skipped += 1
                            else:
                                i += 2 if i + 1 < n else 1
                                skipped += 1
                        else:
                            i += 1
                            skipped += 1
                    continue
                if word == "trowd":
                    out.append("\x1d")
                elif word in ("cell", "nestcell"):
                    out.append("\x07")
                elif word in ("row", "nestrow"):
                    out.append("\x1e")
                elif word == "par":
                    out.append("\r")
                elif word == "line":
                    if not (hidden or deleted):
                        out.append("\n")
                elif word == "tab":
                    if not (hidden or deleted):
                        out.append("\t")
                elif word in _RTF_CHAR_CONTROL_WORDS:
                    if not (hidden or deleted):
                        out.append(_RTF_CHAR_CONTROL_WORDS[word])
                continue

            _flush_bytes()
            i += 2
            continue

        code_pt = ord(ch)
        if code_pt >= 0x80:
            if not (hidden or deleted):
                byte_buf.append(code_pt)
            cur_enc = _current_enc()
            if (
                cur_enc in (None, "gb18030")
                and 0x81 <= code_pt <= 0xFE
                and i + 3 < n
                and 0x30 <= ord(s[i + 1]) <= 0x39
                and 0x81 <= ord(s[i + 2]) <= 0xFE
                and 0x30 <= ord(s[i + 3]) <= 0x39
            ):
                if not (hidden or deleted):
                    byte_buf.append(ord(s[i + 1]))
                    byte_buf.append(ord(s[i + 2]))
                    byte_buf.append(ord(s[i + 3]))
                i += 4
                continue
            if (
                cur_enc in (None, "gb18030", "cp932", "cp949", "cp950", "johab")
                and 0x81 <= code_pt <= 0xFE
                and i + 1 < n
                and 0x40 <= ord(s[i + 1]) < 0x80
                and s[i + 1] not in ("\\", "{", "}")
            ):
                if not (hidden or deleted):
                    byte_buf.append(ord(s[i + 1]))
                i += 2
                continue
            i += 1
            continue

        if byte_buf and _current_enc() in (
            "gb18030",
            "cp932",
            "cp949",
            "cp950",
            "johab",
        ):
            if not (hidden or deleted):
                byte_buf.append(code_pt)
            i += 1
            continue

        if (
            byte_buf
            and len(byte_buf) % 2 == 1
            and 0x81 <= byte_buf[-1] <= 0xFE
            and _current_enc() is None
            and 0x30 <= code_pt <= 0x39
        ):
            if (
                len(byte_buf) >= 3
                and 0x81 <= byte_buf[-3] <= 0xFE
                and 0x30 <= byte_buf[-2] <= 0x39
            ) or (
                i + 1 < n
                and (
                    0x81 <= ord(s[i + 1]) <= 0xFE
                    or s[i + 1 : i + 3] == "\\'"
                )
            ):
                if not (hidden or deleted):
                    byte_buf.append(code_pt)
                i += 1
                continue

        _flush_bytes()
        if not (hidden or deleted):
            out.append(_sanitize_literal(ch))
        i += 1

    _flush_bytes()
    rendered = "".join(out)
    return rendered.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _parse_rtf_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    if len(raw_bytes) > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档大小超过安全上限",
        )
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]
    raw_str = raw_bytes.decode("latin-1", errors="ignore")
    declared_enc = _resolve_rtf_encoding(raw_str)
    font_encodings, default_font = _extract_rtf_font_encodings(raw_str)
    s = _strip_rtf_nonbody_groups(raw_str)
    s = _tokenize_rtf_body(s, declared_enc, font_encodings, default_font)
    # Convert in-cell paragraph breaks (\r inside a table row before \x1e) into \n in O(N) time
    row_chunks = s.split("\x1e")
    processed_chunks: list[str] = []
    for chunk in row_chunks[:-1]:
        if "\x1d" in chunk:
            before_row, in_row = chunk.split("\x1d", 1)
            processed_chunks.append(
                before_row + in_row.replace("\x1d", "").replace("\r", "\n")
            )
        elif "\x07" in chunk:
            processed_chunks.append(chunk.replace("\r", "\n"))
        else:
            processed_chunks.append(chunk)
    processed_chunks.append(row_chunks[-1].replace("\x1d", ""))
    s = "\r".join(processed_chunks)

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []
    cur_table: list[list[str]] = []
    for line in s.split("\r"):
        if "\x07" in line:
            parts = [normalize_cell_text(p) for p in line.split("\x07")]
            if parts and parts[-1] == "":
                parts = parts[:-1]
            if any(parts):
                cur_table.append(parts)
        else:
            if cur_table:
                t_idx = len(tables)
                tables.append(cur_table)
                raw_blocks.append(("table", "", t_idx))
                cur_table = []
            norm_ln = normalize_cell_text(line)
            if norm_ln:
                raw_blocks.append(("paragraph", norm_ln, None))
    if cur_table:
        t_idx = len(tables)
        tables.append(cur_table)
        raw_blocks.append(("table", "", t_idx))

    return _finalize_legacy_blocks_and_tables(tables, raw_blocks)


def _parse_legacy_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_mb: int = 50,
) -> ParsedLegacyDoc:
    max_uncompressed_bytes = max_uncompressed_mb * 1024 * 1024
    kind = _sniff_legacy_doc_kind(raw_bytes)
    if kind == "ole2":
        return _parse_msdoc_ole2(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    if kind == "rtf":
        return _parse_rtf_doc(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    if kind == "xml":
        return _parse_word2003_xml_doc(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    if kind == "html":
        return _parse_html_doc(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    raise DocxValidationError(
        415,
        "import_unsupported_format",
        "不是有效的 .doc 或 .docx Word 文档",
    )


def build_ole2_doc_bytes(
    *,
    tables: list[list[list[str]]] | None = None,
    paragraphs: list[str] | None = None,
) -> bytes:
    """Build a valid binary OLE2 Word 97–2003 (`.doc`, `[MS-CFB]` + `[MS-DOC]`) byte payload."""
    parts: list[str] = []
    for p in paragraphs or []:
        parts.append(p.replace("\n", "\x0b") + "\r")
    for tbl in tables or []:
        for row in tbl:
            cells_str = "\x07".join(str(c).replace("\n", "\x0b") for c in row)
            parts.append(cells_str + "\x07\r\x07")
    body_text = "".join(parts)
    if not body_text:
        body_text = "\r"
    text_utf16 = body_text.encode("utf-16-le")
    ccp_text = len(text_utf16) // 2

    # Build 1Table stream with a Clx Piece Table (1 uncompressed UTF-16LE piece starting at fc=1024)
    clx_bytes = (
        b"\x02"
        + struct.pack("<I", 16)
        + struct.pack("<II", 0, ccp_text)
        + struct.pack("<HIH", 0, 1024, 0)
    )
    table_stream = clx_bytes.ljust(4096, b"\x00")

    # Build WordDocument stream: FIB at 0..1024, UTF-16LE text at 1024..
    fib = bytearray(1024)
    struct.pack_into("<HH", fib, 0x0000, 0xA5EC, 0x00C1)
    struct.pack_into("<H", fib, 0x000A, 0x0200)  # fWhichTblStm = 1 (1Table)
    struct.pack_into("<II", fib, 0x0018, 1024, 1024 + len(text_utf16))
    struct.pack_into("<H", fib, 0x0020, 14)  # csw = 14
    offset_lw = 0x0022 + 28  # 0x003E
    struct.pack_into("<H", fib, offset_lw, 22)  # cslw = 22
    rg_lw_start = offset_lw + 2  # 0x0040
    struct.pack_into("<I", fib, rg_lw_start + 12, ccp_text)  # ccpText at index 3
    offset_fclcb = rg_lw_start + 88  # 0x0098
    struct.pack_into("<H", fib, offset_fclcb, 93)  # cbRgFcLcb = 93
    rg_fclcb_start = offset_fclcb + 2  # 0x009A
    struct.pack_into("<II", fib, rg_fclcb_start + 33 * 8, 0, len(clx_bytes))

    word_doc_raw = bytes(fib) + text_utf16
    if len(word_doc_raw) < 4096:
        word_doc_raw = word_doc_raw.ljust(4096, b"\x00")
    if len(word_doc_raw) % 512 != 0:
        word_doc_raw = word_doc_raw.ljust(((len(word_doc_raw) + 511) // 512) * 512, b"\x00")

    n_word_secs = len(word_doc_raw) // 512
    n_tbl_secs = len(table_stream) // 512  # 8 sectors
    # Sector layout:
    # 0 .. n_word_secs-1: WordDocument
    # n_word_secs .. n_word_secs+n_tbl_secs-1: 1Table
    # dir_sec = n_word_secs + n_tbl_secs
    # fat_sec_0 .. : FAT sectors
    dir_sec = n_word_secs + n_tbl_secs

    def _make_dir_entry(
        name: str,
        obj_type: int,
        start_sec: int,
        size: int,
        left_sib: int = 0xFFFFFFFF,
        right_sib: int = 0xFFFFFFFF,
        child_id: int = 0xFFFFFFFF,
    ) -> bytes:
        buf = bytearray(128)
        name_bytes = (name + "\x00").encode("utf-16-le")
        buf[: len(name_bytes)] = name_bytes
        struct.pack_into("<H", buf, 64, len(name_bytes))
        buf[66] = obj_type
        buf[67] = 1  # black node
        struct.pack_into("<III", buf, 68, left_sib, right_sib, child_id)
        struct.pack_into("<I", buf, 116, start_sec)
        struct.pack_into("<Q", buf, 120, size)
        return bytes(buf)

    dir_sector = (
        _make_dir_entry("Root Entry", 5, 0xFFFFFFFE, 0, child_id=1)
        + _make_dir_entry("WordDocument", 2, 0, len(word_doc_raw), right_sib=2)
        + _make_dir_entry("1Table", 2, n_word_secs, len(table_stream))
        + (b"\x00" * 128)
    )

    total_data_secs = dir_sec + 1
    num_fat_secs = max(1, (total_data_secs + 127) // 128)
    while (total_data_secs + num_fat_secs) > num_fat_secs * 128:
        num_fat_secs += 1

    fat_entries = [0xFFFFFFFF] * (num_fat_secs * 128)
    for i in range(n_word_secs - 1):
        fat_entries[i] = i + 1
    fat_entries[n_word_secs - 1] = 0xFFFFFFFE
    for i in range(n_tbl_secs - 1):
        fat_entries[n_word_secs + i] = n_word_secs + i + 1
    fat_entries[n_word_secs + n_tbl_secs - 1] = 0xFFFFFFFE
    fat_entries[dir_sec] = 0xFFFFFFFE
    fat_sec_ids = [dir_sec + 1 + k for k in range(num_fat_secs)]
    for f_sec in fat_sec_ids:
        fat_entries[f_sec] = 0xFFFFFFFD  # FATSECT

    fat_bytes = struct.pack(f"<{len(fat_entries)}I", *fat_entries)

    header = bytearray(512)
    header[:8] = OLE2_MAGIC
    struct.pack_into("<HH", header, 0x18, 0x003E, 0x0003)
    struct.pack_into("<HH", header, 0x1C, 0xFFFE, 9)  # sector_shift=9
    struct.pack_into("<H", header, 0x20, 6)  # mini_sector_shift=6
    struct.pack_into("<I", header, 0x2C, num_fat_secs)
    struct.pack_into("<I", header, 0x30, dir_sec)
    struct.pack_into("<I", header, 0x38, 4096)
    struct.pack_into("<IIII", header, 0x3C, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    for i in range(109):
        sec_val = fat_sec_ids[i] if i < len(fat_sec_ids) else 0xFFFFFFFF
        struct.pack_into("<I", header, 0x4C + i * 4, sec_val)

    return bytes(header) + word_doc_raw + table_stream + dir_sector + fat_bytes


def validate_and_load_docx(
    filename: str,
    raw_bytes: bytes,
    *,
    max_file_mb: int = 20,
    max_uncompressed_mb: int = 50,
) -> Any:
    lower_name = (filename or "").strip().lower()
    if not (lower_name.endswith(".docx") or lower_name.endswith(".doc")):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "仅支持上传 .doc 或 .docx 格式的申报书文件（不支持含宏的 .docm）",
        )
    if len(raw_bytes) > max_file_mb * 1024 * 1024:
        raise DocxValidationError(
            413,
            "import_file_too_large",
            f"文件大小超过 {max_file_mb}MB 上限",
        )
    if len(raw_bytes) >= 4 and raw_bytes[:4] == ZIP_MAGIC:
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

    if lower_name.endswith(".doc") and _looks_like_legacy_doc_bytes(raw_bytes):
        return _parse_legacy_doc(raw_bytes, max_uncompressed_mb=max_uncompressed_mb)

    raise DocxValidationError(
        415,
        "import_unsupported_format",
        "不是有效的 .doc 或 .docx Word 文档",
    )


def extract_table_grid(table: Any) -> list[list[str]]:
    """Extract 2D cell text grid from a python-docx Table or legacy `.doc` grid."""
    if isinstance(table, list):
        return [[normalize_cell_text(str(cell)) for cell in row] for row in table]
    rows: list[list[str]] = []
    tc_text_cache: dict[int, tuple[Any, str]] = {}
    propagated_chars = 0
    for row in table.rows:
        seen_tc: dict[int, Any] = {}
        cells: list[str] = []
        for cell in row.cells:
            tc_obj = cell._tc
            tc_id = id(tc_obj)
            if tc_id in seen_tc:
                continue
            seen_tc[tc_id] = tc_obj
            if tc_id not in tc_text_cache:
                norm_val = normalize_cell_text(_extract_element_lines_text(tc_obj))
                tc_text_cache[tc_id] = (tc_obj, norm_val)
                cells.append(norm_val)
            else:
                cached = tc_text_cache[tc_id][1]
                if len(cached) <= 256 and propagated_chars + len(cached) <= 65_536:
                    cells.append(cached)
                    propagated_chars += len(cached)
                else:
                    cells.append("")
        rows.append(cells)
    return rows


def _iter_body_elements(container: Any) -> list[Any]:
    """Yield top-level `<w:p>`, `<w:tbl>`, and `<m:oMathPara>`/`<m:oMath>` elements inside `<w:body>`, unwrapping `<w:sdtContent>`."""
    out: list[Any] = []
    for child in container:
        if child.tag in (
            f"{{{W_NS}}}p",
            f"{{{W_NS}}}tbl",
            f"{{{M_NS}}}oMathPara",
            f"{{{M_NS}}}oMath",
        ):
            out.append(child)
        elif child.tag == f"{{{W_NS}}}sdt":
            sdt_content = child.find(f"{{{W_NS}}}sdtContent")
            if sdt_content is not None:
                out.extend(_iter_body_elements(sdt_content))
    return out


def iter_document_tables(doc: Any) -> list[Any]:
    """Return `docx.table.Table` objects or legacy `.doc` 2D grids in `<w:body>` traversal order."""
    if isinstance(doc, ParsedLegacyDoc):
        return list(doc.tables)
    return [
        Table(elem, doc._body)
        for elem in _iter_body_elements(doc.element.body)
        if elem.tag == f"{{{W_NS}}}tbl"
    ]


_OMATH_PROP_TAGS: frozenset[str] = frozenset(
    {
        "rPr",
        "ctrlPr",
        "fPr",
        "sSubPr",
        "sSupPr",
        "sSubSupPr",
        "sPrePr",
        "radPr",
        "dPr",
        "naryPr",
        "eqArrPr",
        "mPr",
        "mrPr",
        "barPr",
        "accPr",
        "boxPr",
        "borderBoxPr",
        "funcPr",
        "groupChrPr",
        "limLowPr",
        "limUppPr",
        "phantPr",
        "oMathParaPr",
    }
)


def _xml_local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _xml_child_attr_val(
    parent_pr: Any | None, child_local: str, default: str = ""
) -> str:
    if parent_pr is None:
        return default
    for ch in parent_pr:
        if _xml_local_name(ch.tag) == child_local:
            for k, v in ch.attrib.items():
                if _xml_local_name(k).lower() == "val":
                    return v
            return default
    return default


def _wrap_math_atom(text: str) -> str:
    s = text.strip()
    if not s:
        return ""
    if len(s) <= 1 or s.isalnum():
        return s
    if s.startswith("(") and s.endswith(")"):
        depth = 0
        balanced = True
        for idx, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx < len(s) - 1:
                    balanced = False
                    break
        if balanced and depth == 0:
            return s
    return f"({s})"


def _is_hidden_run(node: Any) -> bool:
    if _xml_local_name(node.tag) != "r":
        return False
    for ch in node:
        if _xml_local_name(ch.tag) == "rPr":
            for prop in ch:
                if _xml_local_name(prop.tag) in ("vanish", "del"):
                    val = next(
                        (
                            v.strip().lower()
                            for k, v in prop.attrib.items()
                            if _xml_local_name(k).lower() == "val"
                        ),
                        "true",
                    )
                    if val not in ("0", "false", "off"):
                        return True
    return False


def _is_wordml_deletion(node: Any) -> bool:
    ln = _xml_local_name(node.tag)
    if ln in ("Fallback", "del", "moveFrom") or _is_hidden_run(node):
        return True
    if ln == "annotation":
        for k, v in node.attrib.items():
            if _xml_local_name(k).lower() == "type" and v.strip().lower().endswith(
                "deletion"
            ):
                return True
    return False


def _render_omath_node(node: Any) -> str:
    ln = _xml_local_name(node.tag)
    if _is_wordml_deletion(node) or ln in _OMATH_PROP_TAGS:
        return ""
    if ln == "t":
        return node.text or ""
    if ln in ("br", "cr"):
        return "\n"
    if ln in ("tab", "ptab"):
        return "\t"
    if ln == "noBreakHyphen":
        return "-"

    def _child_by_local(name: str) -> Any | None:
        return next((ch for ch in node if _xml_local_name(ch.tag) == name), None)

    def _render_child(name: str) -> str:
        ch = _child_by_local(name)
        return _render_omath_node(ch).strip() if ch is not None else ""

    if ln == "f":
        f_pr = _child_by_local("fPr")
        f_type = _xml_child_attr_val(f_pr, "type", "bar").strip().lower()
        num_s = _render_child("num")
        den_s = _render_child("den")
        if f_type == "nobar":
            return f"({num_s}, {den_s})" if num_s or den_s else ""
        if num_s and den_s:
            return f"{_wrap_math_atom(num_s)}/{_wrap_math_atom(den_s)}"
        return num_s or (f"/{_wrap_math_atom(den_s)}" if den_s else "")

    if ln == "sSup":
        base_s = _render_child("e")
        sup_s = _render_child("sup")
        if base_s and sup_s:
            return f"{_wrap_math_atom(base_s)}^{_wrap_math_atom(sup_s)}"
        return base_s or (f"^{_wrap_math_atom(sup_s)}" if sup_s else "")

    if ln == "sSub":
        base_s = _render_child("e")
        sub_s = _render_child("sub")
        if base_s and sub_s:
            return f"{_wrap_math_atom(base_s)}_{_wrap_math_atom(sub_s)}"
        return base_s or (f"_{_wrap_math_atom(sub_s)}" if sub_s else "")

    if ln == "sSubSup":
        base_s = _render_child("e")
        sub_s = _render_child("sub")
        sup_s = _render_child("sup")
        res = _wrap_math_atom(base_s) if (sub_s or sup_s) else base_s
        if sub_s:
            res += f"_{_wrap_math_atom(sub_s)}"
        if sup_s:
            res += f"^{_wrap_math_atom(sup_s)}"
        return res

    if ln == "sPre":
        sub_s = _render_child("sub")
        sup_s = _render_child("sup")
        base_s = _render_child("e")
        res = ""
        if sub_s:
            res += f"_{_wrap_math_atom(sub_s)}"
        if sup_s:
            res += f"^{_wrap_math_atom(sup_s)}"
        res += _wrap_math_atom(base_s) if (sub_s or sup_s) else base_s
        return res

    if ln == "rad":
        rad_pr = _child_by_local("radPr")
        deg_hide = _xml_child_attr_val(rad_pr, "degHide", "").strip().lower()
        deg_s = "" if deg_hide in ("1", "true", "on") else _render_child("deg")
        expr_s = _render_child("e")
        if deg_s:
            return f"root({deg_s}, {expr_s})"
        return f"sqrt({expr_s})"

    if ln == "d":
        d_pr = _child_by_local("dPr")
        beg = _xml_child_attr_val(d_pr, "begChr", "(")
        sep = _xml_child_attr_val(d_pr, "sepChr", "|")
        end = _xml_child_attr_val(d_pr, "endChr", ")")
        items = [
            _render_omath_node(ch).strip()
            for ch in node
            if _xml_local_name(ch.tag) == "e"
        ]
        return f"{beg}{sep.join(items)}{end}"

    if ln == "nary":
        nary_pr = _child_by_local("naryPr")
        op_chr = _xml_child_attr_val(nary_pr, "chr", "∑")
        sub_hide = _xml_child_attr_val(nary_pr, "subHide", "").strip().lower()
        sup_hide = _xml_child_attr_val(nary_pr, "supHide", "").strip().lower()
        sub_s = "" if sub_hide in ("1", "true", "on") else _render_child("sub")
        sup_s = "" if sup_hide in ("1", "true", "on") else _render_child("sup")
        expr_s = _render_child("e")
        res = op_chr
        if sub_s:
            res += f"_{_wrap_math_atom(sub_s)}"
        if sup_s:
            res += f"^{_wrap_math_atom(sup_s)}"
        if expr_s:
            res += f"({expr_s})" if (sub_s or sup_s) else expr_s
        return res

    if ln == "func":
        fname_s = _render_child("fName")
        expr_s = _render_child("e")
        if fname_s and expr_s:
            return f"{fname_s}({expr_s})"
        return fname_s or expr_s

    if ln == "limLow":
        base_s = _render_child("e")
        lim_s = _render_child("lim")
        return f"{base_s}_({lim_s})" if lim_s else base_s

    if ln == "limUpp":
        base_s = _render_child("e")
        lim_s = _render_child("lim")
        return f"{base_s}^({lim_s})" if lim_s else base_s

    if ln == "eqArr":
        items = [
            _render_omath_node(ch).strip()
            for ch in node
            if _xml_local_name(ch.tag) == "e"
        ]
        return "; ".join(it for it in items if it)

    if ln == "m":
        rows_s: list[str] = []
        for mr in node:
            if _xml_local_name(mr.tag) != "mr":
                continue
            cols = [
                _render_omath_node(ch).strip()
                for ch in mr
                if _xml_local_name(ch.tag) == "e"
            ]
            rows_s.append(", ".join(cols))
        return f"[{'; '.join(rows_s)}]"

    if ln == "acc":
        acc_pr = _child_by_local("accPr")
        acc_chr = _xml_child_attr_val(acc_pr, "chr", "̂")
        expr_s = _render_child("e")
        return f"{expr_s}{acc_chr}" if expr_s else ""

    if ln == "bar":
        expr_s = _render_child("e")
        return f"bar({expr_s})" if expr_s else ""

    return "".join(_render_omath_node(ch) for ch in node)


def _iter_visible_docx_paragraphs(node: Any) -> list[Any]:
    out: list[Any] = []
    for ch in node:
        if _is_wordml_deletion(ch):
            continue
        if ch.tag == f"{{{W_NS}}}p":
            out.append(ch)
        else:
            out.extend(_iter_visible_docx_paragraphs(ch))
    return out


def _extract_element_lines_text(elem: Any) -> str:
    """Extract normalized text from `<w:p>`, `<m:oMathPara>`, or `<w:tbl>`, preserving cell, paragraph, equation (`<m:oMath>`), and `<w:br/>` boundaries."""
    paragraphs = (
        [elem]
        if elem.tag in (f"{{{W_NS}}}p", f"{{{M_NS}}}oMathPara", f"{{{M_NS}}}oMath")
        else _iter_visible_docx_paragraphs(elem)
    )
    def _walk_p(node: Any, out: list[str]) -> None:
        if _is_wordml_deletion(node):
            return
        local_name = node.tag.split("}")[-1] if "}" in node.tag else node.tag
        if local_name == "oMath":
            out.append(_render_omath_node(node))
            return
        if node.tag in (f"{{{W_NS}}}t", f"{{{M_NS}}}t"):
            out.append(node.text or "")
        elif node.tag in (f"{{{W_NS}}}br", f"{{{W_NS}}}cr"):
            out.append("\n")
        elif node.tag in (f"{{{W_NS}}}tab", f"{{{W_NS}}}ptab"):
            out.append("\t")
        elif node.tag == f"{{{W_NS}}}noBreakHyphen":
            out.append("-")
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
    """Extract ordered `<w:body>` blocks and all embedded images with coordinates from `.docx` or `.doc` bytes."""
    if len(raw_bytes) < 4 or raw_bytes[:4] != ZIP_MAGIC:
        if _looks_like_legacy_doc_bytes(raw_bytes):
            parsed = _parse_legacy_doc(raw_bytes)
            return parsed.body_blocks, parsed.embedded_images
        return [], []

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
        extracted_count = 0
        seen_digests: set[bytes] = set()
        dedup_bytes = 0
        quota_hit = False

        def _append_embedded_image(
            *,
            location_kind: Literal["table_cell", "paragraph"],
            table_index: int | None,
            row_index: int | None,
            cell_index: int | None,
            block_index: int,
            rel_id: str,
            media_path: str,
        ) -> None:
            nonlocal extracted_count, dedup_bytes, quota_hit
            if quota_hit:
                return
            if media_path not in zip_names:
                return
            if extracted_count >= MAX_IMPORT_IMAGE_COUNT:
                quota_hit = True
                images.append(
                    EmbeddedImage(
                        location_kind=location_kind,
                        table_index=table_index,
                        row_index=row_index,
                        cell_index=cell_index,
                        block_index=block_index,
                        rel_id=rel_id,
                        media_path=media_path,
                        data=b"",
                        quota_exceeded=True,
                    )
                )
                return
            data = _read_media(media_path)
            if data is None:
                return
            digest = hashlib.sha256(data).digest()
            is_new_digest = digest not in seen_digests
            if is_new_digest and dedup_bytes + len(data) > MAX_IMPORT_DEDUP_IMAGE_BYTES:
                quota_hit = True
                media_cache.pop(media_path, None)
                images.append(
                    EmbeddedImage(
                        location_kind=location_kind,
                        table_index=table_index,
                        row_index=row_index,
                        cell_index=cell_index,
                        block_index=block_index,
                        rel_id=rel_id,
                        media_path=media_path,
                        data=b"",
                        quota_exceeded=True,
                    )
                )
                return
            if is_new_digest:
                seen_digests.add(digest)
                dedup_bytes += len(data)
            extracted_count += 1
            images.append(
                EmbeddedImage(
                    location_kind=location_kind,
                    table_index=table_index,
                    row_index=row_index,
                    cell_index=cell_index,
                    block_index=block_index,
                    rel_id=rel_id,
                    media_path=media_path,
                    data=data,
                )
            )

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
                    _append_embedded_image(
                        location_kind="paragraph",
                        table_index=None,
                        row_index=None,
                        cell_index=None,
                        block_index=block_idx,
                        rel_id=rel_id,
                        media_path=media_path,
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
                            _append_embedded_image(
                                location_kind="table_cell",
                                table_index=t_idx,
                                row_index=r_idx,
                                cell_index=c_idx,
                                block_index=block_idx,
                                rel_id=rel_id,
                                media_path=media_path,
                            )

    return blocks, images


def extract_embedded_images(raw_bytes: bytes) -> list[EmbeddedImage]:
    """Return all embedded images from `.docx` or `.doc` bytes with their structural locations."""
    _, images = extract_body_blocks_and_images(raw_bytes)
    return [img for img in images if not img.quota_exceeded]


def extract_word_text(filename: str, raw_bytes: bytes) -> str:
    """Extract readable plain/markdown text (paragraphs and tables) from `.doc` or `.docx` bytes."""
    doc = validate_and_load_docx(filename, raw_bytes)
    if isinstance(doc, ParsedLegacyDoc):
        blocks_out: list[str] = []
        for b in doc.body_blocks:
            if b.kind == "paragraph" and b.text.strip():
                blocks_out.append(b.text.strip())
            elif b.kind == "table" and b.table_index is not None and b.table_index < len(doc.tables):
                grid = doc.tables[b.table_index]
                row_lines = [
                    " | ".join(c.replace("\n", " ") for c in row if c.strip())
                    for row in grid
                ]
                tbl_str = "\n".join(ln for ln in row_lines if ln.strip())
                if tbl_str:
                    blocks_out.append(tbl_str)
        return "\n\n".join(blocks_out)

    # python-docx Document
    def _extract_tc_text(tc_elem: Any) -> str:
        parts: list[str] = []
        for child in _iter_body_elements(tc_elem):
            if child.tag in (
                f"{{{W_NS}}}p",
                f"{{{M_NS}}}oMathPara",
                f"{{{M_NS}}}oMath",
            ):
                p_txt = _extract_element_lines_text(child).strip()
                if p_txt:
                    parts.append(p_txt)
            elif child.tag == f"{{{W_NS}}}tbl":
                sub_tbl = _extract_docx_table_text(child)
                if sub_tbl:
                    parts.append(sub_tbl)
        return "\n".join(parts)

    def _extract_docx_table_text(tbl_elem: Any) -> str:
        tbl_obj = Table(tbl_elem, doc._body)
        row_lines: list[str] = []
        seen_tc: dict[int, Any] = {}
        for row in tbl_obj.rows:
            cells: list[str] = []
            for cell in row.cells:
                tc_obj = cell._tc
                tc_id = id(tc_obj)
                if tc_id in seen_tc:
                    continue
                seen_tc[tc_id] = tc_obj
                c_txt = _extract_tc_text(tc_obj).strip()
                if c_txt:
                    cells.append(c_txt.replace("\n", " "))
            if cells:
                row_lines.append(" | ".join(cells))
        return "\n".join(ln for ln in row_lines if ln.strip())

    out_chunks: list[str] = []
    for elem in _iter_body_elements(doc.element.body):
        if elem.tag in (
            f"{{{W_NS}}}p",
            f"{{{M_NS}}}oMathPara",
            f"{{{M_NS}}}oMath",
        ):
            txt = _extract_element_lines_text(elem).strip()
            if not txt:
                continue
            p_pr = elem.find(f"{{{W_NS}}}pPr")
            p_style = p_pr.find(f"{{{W_NS}}}pStyle") if p_pr is not None else None
            style_val = (
                (p_style.get(f"{{{W_NS}}}val") or "").strip().lower()
                if p_style is not None
                else ""
            )
            if (
                style_val.startswith("heading")
                or "标题" in style_val
                or style_val in ("1", "2", "3")
            ) and not txt.startswith("#"):
                txt = f"## {txt}"
            out_chunks.append(txt)
        elif elem.tag == f"{{{W_NS}}}tbl":
            tbl_str = _extract_docx_table_text(elem)
            if tbl_str:
                out_chunks.append(tbl_str)
    return "\n\n".join(out_chunks)

