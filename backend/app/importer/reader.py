"""Safe .doc / .docx reader with magic-byte, zip-bomb, OLE2, macro, merged-cell, and embedded-image extraction."""

from __future__ import annotations

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
    text = unicodedata.normalize("NFKC", raw or "")
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


def _strip_msdoc_fields(raw: str) -> str:
    """Strip Word field instructions (`\\x13...\\x14`), keeping visible field results (`\\x14...\\x15`)."""
    if "\x13" not in raw and "\x14" not in raw and "\x15" not in raw:
        return raw
    out: list[str] = []
    in_instr = 0
    for ch in raw:
        if ch == "\x13":
            in_instr += 1
        elif ch == "\x14":
            if in_instr > 0:
                in_instr -= 1
        elif ch == "\x15":
            if in_instr > 0:
                in_instr -= 1
        elif in_instr == 0:
            out.append(ch)
    return "".join(out)


def _looks_like_legacy_doc_bytes(raw_bytes: bytes) -> bool:
    if len(raw_bytes) >= 8 and raw_bytes[:8] == OLE2_MAGIC:
        return True
    head = raw_bytes[:2048].lstrip()
    head_lower = head.lower()
    if head_lower.startswith(b"{\\rtf"):
        return True
    if (
        head_lower.startswith(b"<?xml")
        or head_lower.startswith(b"<w:worddocument")
        or head_lower.startswith(b"<pkg:package")
    ) and (b"word" in head_lower or b"<w:" in head_lower):
        return True
    if (
        head_lower.startswith(b"<!doctype html")
        or head_lower.startswith(b"<html")
        or head_lower.startswith(b"<meta")
        or head_lower.startswith(b"<table")
        or head_lower.startswith(b"<body")
    ):
        return True
    return False


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
    num_fat_sectors = struct.unpack_from("<I", raw_bytes, 0x2C)[0]
    first_dir_sector = struct.unpack_from("<I", raw_bytes, 0x30)[0]
    mini_cutoff = struct.unpack_from("<I", raw_bytes, 0x38)[0] or 4096
    first_minifat_sector = struct.unpack_from("<I", raw_bytes, 0x3C)[0]
    num_minifat_sectors = struct.unpack_from("<I", raw_bytes, 0x40)[0]
    first_difat_sector = struct.unpack_from("<I", raw_bytes, 0x44)[0]
    num_difat_sectors = struct.unpack_from("<I", raw_bytes, 0x48)[0]

    def _read_sector(sec_id: int) -> bytes:
        offset = (sec_id + 1) * sector_size
        if offset < 0 or offset + sector_size > len(raw_bytes):
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
    for _ in range(min(num_difat_sectors, 4096)):
        if difat_sec >= 0xFFFFFFFC or difat_sec in visited_difat:
            break
        visited_difat.add(difat_sec)
        sec_bytes = _read_sector(difat_sec)
        vals = struct.unpack_from(f"<{entries_per_sector}I", sec_bytes, 0)
        difat.extend(v for v in vals[:-1] if v < 0xFFFFFFFC)
        difat_sec = vals[-1]

    fat: list[int] = []
    for fat_sec in difat[: max(num_fat_sectors, len(difat))]:
        sec_bytes = _read_sector(fat_sec)
        fat.extend(struct.unpack_from(f"<{entries_per_sector}I", sec_bytes, 0))

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

    for pn_raw in pns:
        pn = pn_raw & 0x003FFFFF
        page_off = pn * 512
        if page_off < 0 or page_off + 512 > len(word_doc):
            continue
        fkp = word_doc[page_off : page_off + 512]
        cpara = fkp[511]
        if cpara <= 0 or 4 * (cpara + 1) + 13 * cpara > 511:
            continue
        rgfc = struct.unpack_from(f"<{cpara + 1}I", fkp, 0)
        bx_base = 4 * (cpara + 1)
        for k in range(cpara):
            fc_end = rgfc[k + 1]
            b_offset = fkp[bx_base + k * 13]
            if b_offset == 0:
                continue
            papx_pos = b_offset * 2
            if papx_pos >= 511:
                continue
            cb = fkp[papx_pos]
            if cb == 0:
                if papx_pos + 2 > 511:
                    continue
                cb_bytes = fkp[papx_pos + 1] * 2
                grp_start = papx_pos + 4  # skip 2-byte cb + 2-byte istd
                grp_end = min(511, papx_pos + 2 + cb_bytes)
            else:
                cb_bytes = cb * 2 - 1
                grp_start = papx_pos + 3  # skip 1-byte cb + 2-byte istd
                grp_end = min(511, papx_pos + 1 + cb_bytes)
            if grp_start >= grp_end:
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

    # If the foundation anchors ([学术简历]/[前期成果]/[承担项目]) are split across paragraphs or 1-col rows,
    # synthesize a consolidated 1x1 table cell so `_extract_t4_foundation` can parse them identically to .docx.
    foundation_markers = ("[学术简历]", "[前期成果]", "[承担项目]", "［学术简历］")
    has_consolidated_t4 = any(
        sum(1 for m in foundation_markers if m in cell) >= 2
        for tbl in cleaned_tables
        for row in tbl
        for cell in row
    )
    if not has_consolidated_t4:
        all_lines: list[str] = [b.text for b in body_blocks if b.text]
        combined_text = "\n".join(all_lines)
        if any(m in combined_text for m in foundation_markers):
            cleaned_tables.append([[combined_text]])

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

    pieces: list[tuple[int, int, int, int, str]] = []  # (cp_start, cp_end, fc_byte, char_width, text)
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
                for i in range(n_pieces):
                    cp_s, cp_e = cps[i], cps[i + 1]
                    if cp_e <= cp_s:
                        continue
                    char_count = cp_e - cp_s
                    fc_val = struct.unpack_from("<I", plc_pcd, pcd_base + i * 8 + 2)[0]
                    compressed = bool(fc_val & 0x40000000)
                    if compressed:
                        fc_byte = (fc_val & 0x3FFFFFFF) // 2
                        byte_len = char_count
                        raw_slice = word_doc[fc_byte : fc_byte + byte_len]
                        txt_slice = _decode_msdoc_compressed(raw_slice)
                        pieces.append((cp_s, cp_s + len(txt_slice), fc_byte, 1, txt_slice))
                    else:
                        fc_byte = fc_val & 0x3FFFFFFF
                        byte_len = char_count * 2
                        raw_slice = word_doc[fc_byte : fc_byte + byte_len]
                        txt_slice = raw_slice.decode("utf-16-le", errors="replace")
                        pieces.append((cp_s, cp_s + len(txt_slice), fc_byte, 2, txt_slice))

    if not pieces:
        fc_min, fc_mac = struct.unpack_from("<II", word_doc, 0x0018)
        if 0 < fc_min < fc_mac <= len(word_doc):
            raw_body = word_doc[fc_min:fc_mac]
            is_16bit = bool(flags & 0x0400) or (b"\x00" in raw_body[:64])
            if is_16bit:
                txt = raw_body.decode("utf-16-le", errors="replace")
                pieces.append((0, len(txt), fc_min, 2, txt))
            else:
                txt = _decode_msdoc_compressed(raw_body)
                pieces.append((0, len(txt), fc_min, 1, txt))

    if not pieces:
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "无法解析 .doc 文档文本流",
        )

    full_text = "".join(p[4] for p in pieces)
    if ccp_text > 0:
        main_text = full_text[:ccp_text]
    else:
        main_text = full_text

    def _cp_to_fc_end(cp_idx: int) -> int:
        for cp_s, cp_e, fc_b, width, _ in pieces:
            if cp_s <= cp_idx < cp_e:
                return fc_b + (cp_idx - cp_s + 1) * width
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
    while idx < n_chars:
        ch = main_text[idx]
        if ch not in ("\r", "\x07"):
            idx += 1
            continue

        raw_seg = _strip_msdoc_fields(main_text[seg_start:idx])
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
        tail = _strip_msdoc_fields(main_text[seg_start:]).strip()
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
    lower_bytes = raw_bytes.lower()
    if b"<!entity" in lower_bytes or b"<!doctype" in lower_bytes:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "文档包含不安全的 XML 实体声明",
        )
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
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
            ln = _local(node.tag)
            if ln == "Fallback":
                return
            if ln == "t" and node.text:
                parts.append(node.text)
            elif ln in ("br", "cr"):
                parts.append("\n")
            for ch in node:
                _walk(ch)

        _walk(p_elem)
        return normalize_cell_text("".join(parts))

    def _iter_wordml_blocks(elem: ET.Element) -> list[ET.Element]:
        out: list[ET.Element] = []
        for ch in elem:
            ln = _local(ch.tag)
            if ln in ("p", "tbl"):
                out.append(ch)
            elif ln in ("sect", "sub-section", "sdt", "sdtContent", "customXml", "body"):
                out.extend(_iter_wordml_blocks(ch))
        return out

    def _iter_wordml_trs(elem: ET.Element) -> list[ET.Element]:
        out: list[ET.Element] = []
        for ch in elem:
            ln = _local(ch.tag)
            if ln == "tr":
                out.append(ch)
            elif ln in ("sdt", "sdtContent", "customXml"):
                out.extend(_iter_wordml_trs(ch))
        return out

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []

    body_nodes = [el for el in root.iter() if _local(el.tag) == "body"]
    container = body_nodes[0] if body_nodes else root

    for child in _iter_wordml_blocks(container):
        ln = _local(child.tag)
        if ln == "p":
            txt = _p_text(child)
            if txt:
                raw_blocks.append(("paragraph", txt, None))
        elif ln == "tbl":
            grid: list[list[str]] = []
            for tr in _iter_wordml_trs(child):
                row_cells: list[str] = []
                for tc in tr:
                    if _local(tc.tag) != "tc":
                        continue
                    cell_ps = [
                        _p_text(p)
                        for p in tc.iter()
                        if _local(p.tag) == "p"
                    ]
                    row_cells.append("\n".join(cp for cp in cell_ps if cp))
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        t = tag.lower()
        if t in ("script", "style", "head"):
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
        elif t == "tr" and self._in_table == 1:
            self._current_row = []
        elif t in ("td", "th") and self._in_table == 1:
            self._current_cell = []
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
        if t in ("script", "style", "head") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t in ("td", "th") and self._in_table == 1:
            if self._current_row is not None and self._current_cell is not None:
                self._current_row.append(normalize_cell_text("".join(self._current_cell)))
            self._current_cell = None
        elif t == "tr" and self._in_table == 1:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = None
        elif t == "table" and self._in_table > 0:
            if self._in_table == 1 and self._current_table:
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
    text = ""
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw_bytes.decode("utf-8", errors="replace")
    parser = _HtmlDocParser()
    parser.feed(text)
    if parser._current_para:
        rem = normalize_cell_text("".join(parser._current_para))
        if rem:
            parser.raw_blocks.append(("paragraph", rem, None))
    return _finalize_legacy_blocks_and_tables(parser.tables, parser.raw_blocks)


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
    raw_str = raw_bytes.decode("latin-1", errors="ignore")

    # Decode hex escapes \'xx as gb18030
    def _replace_hex_runs(m: re.Match[str]) -> str:
        hex_pairs = re.findall(r"\\'([0-9a-fA-F]{2})", m.group(0))
        b = bytes(int(h, 16) for h in hex_pairs)
        return b.decode("gb18030", errors="replace")

    s = re.sub(r"(?:\\'[0-9a-fA-F]{2})+", _replace_hex_runs, raw_str)

    # Decode Unicode escapes \uN?
    def _replace_u(m: re.Match[str]) -> str:
        code = int(m.group(1))
        if code < 0:
            code += 65536
        return chr(code)

    s = re.sub(r"\\u(-?\d+)\??", _replace_u, s)
    s = s.replace("\\cell", "\x07").replace("\\row", "\r\x07")
    s = s.replace("\\par", "\r").replace("\\line", "\n")
    s = re.sub(r"\{\\\*[^{}]*\}", "", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    s = s.replace("{", "").replace("}", "")

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
    if len(raw_bytes) >= 8 and raw_bytes[:8] == OLE2_MAGIC:
        return _parse_msdoc_ole2(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    head = raw_bytes[:2048].lstrip().lower()
    if head.startswith(b"{\\rtf"):
        return _parse_rtf_doc(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    if (
        head.startswith(b"<?xml")
        or head.startswith(b"<w:worddocument")
        or head.startswith(b"<pkg:package")
    ) and (b"word" in head or b"<w:" in head):
        return _parse_word2003_xml_doc(
            raw_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    if (
        head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
        or head.startswith(b"<meta")
        or head.startswith(b"<table")
        or head.startswith(b"<body")
    ):
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
    ccp_text = len(body_text)

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
    """Return `docx.table.Table` objects or legacy `.doc` 2D grids in `<w:body>` traversal order."""
    if isinstance(doc, ParsedLegacyDoc):
        return list(doc.tables)
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
    out_chunks: list[str] = []
    for elem in _iter_body_elements(doc.element.body):
        if elem.tag == f"{{{W_NS}}}p":
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
            tbl_obj = Table(elem, doc._body)
            grid = extract_table_grid(tbl_obj)
            row_lines = [
                " | ".join(c.replace("\n", " ") for c in row if c.strip())
                for row in grid
            ]
            tbl_str = "\n".join(ln for ln in row_lines if ln.strip())
            if tbl_str:
                out_chunks.append(tbl_str)
    return "\n\n".join(out_chunks)

