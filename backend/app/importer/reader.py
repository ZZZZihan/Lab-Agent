"""Safe .doc / .docx reader backed by olefile, python-docx, lxml, defusedxml, striprtf, and charset-normalizer."""

from __future__ import annotations

import bisect
import codecs
import contextlib
import hashlib
import io
import posixpath
import re
import struct
import threading
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import charset_normalizer
import docx
import lxml.etree
import lxml.html
import olefile
import striprtf.striprtf as _striprtf
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from docx.table import Table

ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1f]")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_EMBEDDED_IMAGE_READ_BYTES = 2_097_152 + 1  # Read up to 2 MiB + 1 byte to trigger SIGNATURE_TOO_LARGE safely
MAX_IMPORT_IMAGE_COUNT = 64
MAX_IMPORT_DEDUP_IMAGE_BYTES = 32 * 1024 * 1024  # 32 MiB

# Configure striprtf table/structure markers using Unicode Private Use Area codepoints so user text never collides,
# without mutating striprtf module globals outside scoped _patched_striprtf() calls.
_RTF_PAR_TOK = "\uf00d"
_RTF_CELL_TOK = "\uf007"
_RTF_ROW_TOK = "\uf01e"
_RTF_TROWD_TOK = "\uf01d"

_RTF_LOCK = threading.RLock()
_ORIG_STRIPRTF_CHARSET_MAP = dict(_striprtf.charset_map)
_ORIG_STRIPRTF_HYPERLINKS = _striprtf.HYPERLINKS
_ORIG_STRIPRTF_FONTTABLE = _striprtf.FONTTABLE
_ORIG_STRIPRTF_SPECIALCHARS = dict(_striprtf.specialchars)
_ORIG_STRIPRTF_DESTINATIONS = frozenset(_striprtf.destinations)

_RTF_CHARSET_MAP: dict[int, str] = {**_ORIG_STRIPRTF_CHARSET_MAP, 134: "gb18030"}
_RTF_HYPERLINKS = re.compile(r"(?!x)(x)(x)")
_RTF_FONTTABLE = re.compile(
    r"\\f(\d+)(?:[^{};]|\{[^{}]*\})*?\\fcharset(\d+)(?:[^{};]|\{[^{}]*\})*?([^;{}]+);"
)
_RTF_SPECIALCHARS: dict[str, str] = {
    **_ORIG_STRIPRTF_SPECIALCHARS,
    "par": _RTF_PAR_TOK,
    "sect": _RTF_PAR_TOK,
    "page": _RTF_PAR_TOK,
    "line": "\n",
    "cell": _RTF_CELL_TOK,
    "nestcell": _RTF_CELL_TOK,
    "row": _RTF_ROW_TOK,
    "nestrow": _RTF_ROW_TOK,
    "trowd": _RTF_TROWD_TOK,
    "-": "",
    "_": "-",
    "zwj": "\u200d",
    "zwnj": "\u200c",
    "ltrmark": "\u200e",
    "rtlmark": "\u200f",
    "zwbo": "\u200b",
    "zwnbo": "\ufeff",
}
_RTF_DESTINATIONS = _ORIG_STRIPRTF_DESTINATIONS - {"ud", "upr"}


@contextlib.contextmanager
def _patched_striprtf() -> Iterator[None]:
    with _RTF_LOCK:
        _striprtf.charset_map = _RTF_CHARSET_MAP
        _striprtf.HYPERLINKS = _RTF_HYPERLINKS
        _striprtf.FONTTABLE = _RTF_FONTTABLE
        _striprtf.specialchars = _RTF_SPECIALCHARS
        _striprtf.destinations = _RTF_DESTINATIONS
        try:
            yield
        finally:
            _striprtf.charset_map = dict(_ORIG_STRIPRTF_CHARSET_MAP)
            _striprtf.HYPERLINKS = _ORIG_STRIPRTF_HYPERLINKS
            _striprtf.FONTTABLE = _ORIG_STRIPRTF_FONTTABLE
            _striprtf.specialchars = dict(_ORIG_STRIPRTF_SPECIALCHARS)
            _striprtf.destinations = _ORIG_STRIPRTF_DESTINATIONS


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
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
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
    return "".join(_MSDOC_SPECIAL_BYTES.get(b, chr(b)) for b in chunk)


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
            if field_stack and field_stack.pop():
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

    head_lower = head_str.lstrip("\ufeff \t\r\n").lower()
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
    """Read root-level OLE2 streams and all entry names using `olefile.OleFileIO`."""
    if len(raw_bytes) < 512 or not olefile.isOleFile(raw_bytes):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "不是有效的 .doc (OLE2) 文件",
        )
    header_difat = [
        sec for sec in struct.unpack_from("<109I", raw_bytes, 0x4C) if sec < 0xFFFFFFFC
    ]
    if len(header_difat) != len(set(header_difat)):
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc (OLE2) 重复 FAT 扇区引用",
        )
    try:
        with olefile.OleFileIO(raw_bytes) as ole:
            if ole.num_difat_sectors == 0 and len(header_difat) != ole.num_fat_sectors:
                raise DocxValidationError(
                    415,
                    "import_unsupported_format",
                    "损坏的 .doc (OLE2) FAT 扇区数量不一致",
                )
            for sid in range(len(ole.direntries)):
                if ole.direntries[sid] is None:
                    try:
                        ole._load_direntry(sid)
                    except Exception:
                        continue
            total_stream_bytes = sum(
                int(entry.size)
                for entry in ole.direntries
                if entry is not None and entry.entry_type == olefile.STGTY_STREAM
            )
            if total_stream_bytes > max_uncompressed_bytes:
                raise DocxValidationError(
                    413,
                    "import_uncompressed_too_large",
                    "文档解压后大小超过安全上限",
                )
            all_names_lower = {
                str(entry.name).lower()
                for entry in ole.direntries
                if entry is not None and entry.name
            }
            streams: dict[str, bytes] = {}
            for entry in sorted(ole.root.kids, key=lambda e: e.sid):
                if entry.entry_type == olefile.STGTY_STREAM and entry.name not in streams:
                    data = ole._open(entry.isectStart, entry.size).read(
                        max_uncompressed_bytes + 1
                    )
                    if len(data) > max_uncompressed_bytes:
                        raise DocxValidationError(
                            413,
                            "import_uncompressed_too_large",
                            "文档解压后大小超过安全上限",
                        )
                    streams[entry.name] = data
            return streams, all_names_lower
    except DocxValidationError:
        raise
    except Exception as exc:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "损坏的 .doc (OLE2) 文件结构",
        ) from exc


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
                grp_start = papx_pos + 4
                grp_end = min(511, papx_pos + 2 + cb_bytes)
            else:
                cb_bytes = cb * 2 - 1
                grp_start = papx_pos + 3
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
                else:
                    if sprm == 0xD608:
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
                if sprm in (0x2416, 0x244B) and grpprl[pos] != 0:
                    is_in_tbl = True
                elif sprm == 0x6649:
                    itap = struct.unpack_from("<I", grpprl, pos)[0]
                    if itap > 0:
                        is_in_tbl = True
                elif sprm in (0x2417, 0x244C) and grpprl[pos] != 0:
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
    for kind, txt, t_idx in raw_blocks:
        if kind == "paragraph":
            norm_t = normalize_cell_text(txt)
            if norm_t:
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

    pieces: list[tuple[int, int, int, int, str, bool]] = []
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
                next_has_cell = "\x07" in main_text[idx + 1 : idx + 200]
                is_ttp = (
                    not raw_seg.strip()
                    and not cell_lines
                    and bool(current_row)
                    and (
                        (expected_cols > 0 and len(current_row) >= expected_cols)
                        or not next_has_cell
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


def _decode_html_doc_bytes(raw_bytes: bytes) -> str:
    """Decode HTML / XML `.doc` bytes honoring BOM, `<meta charset>` / `<?xml encoding>`, and `charset-normalizer`."""
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

    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue

    best = charset_normalizer.from_bytes(raw_bytes).best()
    if best is not None:
        return str(best)
    return raw_bytes.decode("cp1252", errors="replace")


def _parse_word2003_xml_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    """Parse Word 2003 XML (`.doc`) using `defusedxml` and unified WordML walkers."""
    if len(raw_bytes) > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档大小超过安全上限",
        )
    decoded_xml = _decode_html_doc_bytes(raw_bytes).lstrip("\ufeff")
    cleaned_xml = re.sub(
        r"(<\?xml\b[^>]*?)\bencoding\s*=\s*[\"'][^\"']*[\"']",
        r"\1",
        decoded_xml,
        count=1,
        flags=re.IGNORECASE,
    )
    try:
        root = DefusedET.fromstring(
            cleaned_xml.encode("utf-8"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except DefusedXmlException as exc:
        raise DocxValidationError(
            415,
            "import_unsupported_format",
            "文档包含不安全的 XML 实体声明",
        ) from exc
    except Exception as exc:
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "无法解析 Word XML (.doc) 文档结构",
        ) from exc

    def _iter_wordml_blocks(elem: Any) -> list[Any]:
        out: list[Any] = []
        for ch in elem:
            if _is_wordml_deletion(ch):
                continue
            ln = _xml_local_name(ch.tag)
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

    def _iter_wordml_trs(elem: Any) -> list[Any]:
        out: list[Any] = []
        for ch in elem:
            if _is_wordml_deletion(ch):
                continue
            ln = _xml_local_name(ch.tag)
            if ln == "tr":
                out.append(ch)
            elif ln in ("sdt", "sdtContent", "customXml", "annotation", "ins"):
                out.extend(_iter_wordml_trs(ch))
        return out

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []

    body_nodes = [el for el in root.iter() if _xml_local_name(el.tag) == "body"]
    container = body_nodes[0] if body_nodes else root

    for child in _iter_wordml_blocks(container):
        ln = _xml_local_name(child.tag)
        if ln in ("p", "oMathPara", "oMath"):
            txt = _extract_element_lines_text(child)
            if txt:
                raw_blocks.append(("paragraph", txt, None))
        elif ln == "tbl":
            grid: list[list[str]] = []
            propagated_chars = 0
            for tr in _iter_wordml_trs(child):
                row_cells: list[str] = []
                for tc in tr:
                    if _xml_local_name(tc.tag) != "tc":
                        continue
                    cell_text = _extract_element_lines_text(tc)
                    tc_pr = next(
                        (ch for ch in tc if _xml_local_name(ch.tag).lower() == "tcpr"),
                        None,
                    )
                    if not cell_text and grid and len(row_cells) < len(grid[-1]):
                        has_vmerge_cont = False
                        if tc_pr is not None:
                            for sub in tc_pr:
                                if _xml_local_name(sub.tag).lower() == "vmerge":
                                    val = next(
                                        (
                                            v
                                            for k, v in sub.attrib.items()
                                            if _xml_local_name(k).lower() == "val"
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
                            if _xml_local_name(sub.tag).lower() == "gridspan":
                                val = next(
                                    (
                                        v
                                        for k, v in sub.attrib.items()
                                        if _xml_local_name(k).lower() == "val"
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


_HTML_BLOCK_TAGS: frozenset[str] = frozenset(
    {"table", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
)


def _parse_html_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    """Parse HTML-backed `.doc` using `lxml.html` DOM traversal instead of a custom HTMLParser state machine."""
    if len(raw_bytes) > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档大小超过安全上限",
        )
    text = _decode_html_doc_bytes(raw_bytes).lstrip("\ufeff").strip()
    text = re.sub(r"^\s*<\?xml\b[^>]*\?>\s*", "", text, flags=re.IGNORECASE)
    if not text:
        return _finalize_legacy_blocks_and_tables([], [])

    doc = lxml.html.fromstring(text)
    lxml.etree.strip_elements(doc, "script", "style", "head", "del", with_tail=False)
    for br in doc.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []

    def _extract_html_table(tbl_el: Any) -> list[list[str]]:
        grid: list[list[str]] = []
        pending_spans: dict[int, tuple[int, str]] = {}
        propagated_chars = 0
        for tr in tbl_el.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr"):
            row: list[str] = []
            col_idx = 0

            def _consume_spans(target_row: list[str]) -> None:
                nonlocal col_idx
                while col_idx in pending_spans:
                    rem, val = pending_spans.pop(col_idx)
                    target_row.append(val)
                    if rem > 1:
                        pending_spans[col_idx] = (rem - 1, val)
                    col_idx += 1

            for td in tr.xpath("./td | ./th"):
                _consume_spans(row)
                for sub_td in td.xpath(".//table//td | .//table//th"):
                    sub_td.text = " " + (sub_td.text or "")
                    sub_td.tail = " " + (sub_td.tail or "")
                for sub_blk in td.xpath(
                    ".//p | .//div | .//li | .//tr | .//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6"
                ):
                    sub_blk.text = "\n" + (sub_blk.text or "")
                    sub_blk.tail = "\n" + (sub_blk.tail or "")
                cell_text = normalize_cell_text(td.text_content())
                try:
                    rs = max(1, min(int(td.get("rowspan") or 1), 256))
                except ValueError:
                    rs = 1
                try:
                    cs = max(1, min(int(td.get("colspan") or 1), 64))
                except ValueError:
                    cs = 1
                for c_off in range(cs):
                    col_val = cell_text if c_off == 0 else ""
                    row.append(col_val)
                    if rs > 1:
                        rem_rows = rs - 1
                        cost = len(col_val) * rem_rows
                        if len(col_val) <= 256 and propagated_chars + cost <= 65_536:
                            span_val = col_val
                            propagated_chars += cost
                        else:
                            span_val = ""
                        pending_spans[col_idx] = (rem_rows, span_val)
                    col_idx += 1
                    _consume_spans(row)
            _consume_spans(row)
            for col_k in sorted(k for k in pending_spans if k >= col_idx):
                while col_idx < col_k:
                    row.append("")
                    col_idx += 1
                _consume_spans(row)
            if row:
                grid.append(row)
        return grid

    def _has_block_descendant(el: Any) -> bool:
        return bool(
            el.xpath(".//table | .//p | .//div | .//li | .//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6")
        )

    def _walk(node: Any) -> None:
        if not isinstance(node.tag, str):
            return
        tag = node.tag.lower()
        if tag == "table":
            grid = _extract_html_table(node)
            if grid:
                t_idx = len(tables)
                tables.append(grid)
                raw_blocks.append(("table", "", t_idx))
            return
        if tag in _HTML_BLOCK_TAGS and not _has_block_descendant(node):
            txt = normalize_cell_text(node.text_content())
            if txt:
                if tag in ("h1", "h2", "h3") and not txt.startswith("#"):
                    txt = f"## {txt}"
                raw_blocks.append(("paragraph", txt, None))
            return

        inline_buf: list[str] = []

        def _flush_inline() -> None:
            if not inline_buf:
                return
            joined = normalize_cell_text("".join(inline_buf))
            inline_buf.clear()
            if joined:
                if tag in ("h1", "h2", "h3") and not joined.startswith("#"):
                    joined = f"## {joined}"
                raw_blocks.append(("paragraph", joined, None))

        if node.text:
            inline_buf.append(node.text)
        for ch in node:
            ch_tag = ch.tag.lower() if isinstance(ch.tag, str) else ""
            if ch_tag in _HTML_BLOCK_TAGS or _has_block_descendant(ch):
                _flush_inline()
                _walk(ch)
                if ch.tail:
                    inline_buf.append(ch.tail)
            else:
                inline_buf.append(ch.text_content())
                if ch.tail:
                    inline_buf.append(ch.tail)
        _flush_inline()

    _walk(doc)
    return _finalize_legacy_blocks_and_tables(tables, raw_blocks)


def _resolve_rtf_default_encoding(raw_str: str, raw_bytes: bytes) -> str:
    cpg_match = re.search(r"\\ansicpg(\d+)(?![a-zA-Z0-9])", raw_str[:4096])
    if cpg_match is not None:
        cpg = int(cpg_match.group(1))
        if cpg in (936, 54936):
            return "gb18030"
        cand = "utf-8" if cpg == 65001 else f"cp{cpg}"
        try:
            codecs.lookup(cand)
            return cand
        except LookupError:
            pass
    if re.search(r"\\mac(?![a-zA-Z])", raw_str[:1024]):
        return "mac_roman"
    if re.search(r"\\pca(?![a-zA-Z])", raw_str[:1024]):
        return "cp850"
    if re.search(r"\\pc(?![a-zA-Z])", raw_str[:1024]):
        return "cp437"
    if re.search(r"\\ansi(?![a-zA-Z])", raw_str[:1024]):
        return "cp1252"
    try:
        raw_bytes.decode("gb18030", errors="strict")
        return "gb18030"
    except UnicodeDecodeError:
        return "cp1252"


_CJK_ENCODINGS: frozenset[str] = frozenset(
    {"gb18030", "cp936", "cp932", "cp949", "cp950", "cp1361", "johab"}
)


def _strip_rtf_bin_runs(raw_bytes: bytes) -> bytes:
    """Remove raw `\\binN` binary payload runs while preserving escaped `\\\\`, `\\{`, and `\\}`."""
    if b"\\bin" not in raw_bytes and b"\\BIN" not in raw_bytes:
        return raw_bytes
    out_b = bytearray()
    i, n = 0, len(raw_bytes)
    while i < n:
        if (
            raw_bytes[i] == 0x5C
            and i + 1 < n
            and raw_bytes[i + 1] in (0x5C, 0x7B, 0x7D)
        ):
            out_b.extend(raw_bytes[i : i + 2])
            i += 2
            continue
        if raw_bytes[i : i + 4].lower() == b"\\bin":
            m = re.match(rb"\\bin(-?\d+) ?", raw_bytes[i : i + 32], re.IGNORECASE)
            if m is not None:
                i = min(n, i + m.end() + max(0, int(m.group(1))))
                continue
        out_b.append(raw_bytes[i])
        i += 1
    return bytes(out_b)


def _normalize_rtf_for_striprtf(s: str, default_enc: str) -> str:
    """Normalize RTF tokens via `striprtf.PATTERN` with group-stack tracking for `\\v`, `\\deleted`, `\\upr`/`\\ud`, and active-font CJK trail bytes."""
    s = re.sub(r"\\fcharset(\d+)\s*\\f(\d+)", r"\\f\2\\fcharset\1", s)
    font_tbl_str = _striprtf.font_table_group(s)
    if not font_tbl_str:
        inline_fonts = re.findall(r"\\f(\d+)\\fcharset(\d+)", s)
        if inline_fonts:
            synth_tbl = (
                "{\\fonttbl"
                + "".join(f"{{\\f{fid}\\fcharset{fcs} f{fid};}}" for fid, fcs in inline_fonts)
                + "}"
            )
            s = re.sub(r"(\\rtf\d*)", r"\1" + synth_tbl, s, count=1)
            font_tbl_str = synth_tbl
    font_enc: dict[str, str] = {}
    for f_id, f_cs, _ in _RTF_FONTTABLE.findall(font_tbl_str):
        font_enc[f_id] = _RTF_CHARSET_MAP.get(int(f_cs), default_enc)

    m_deff = re.search(r"\\deff(-?\d+)(?![a-zA-Z0-9])", s[:4096])
    default_font: str | None = m_deff.group(1) if m_deff is not None else None
    current_font: str | None = default_font
    in_fonttbl = False
    hidden = False
    deleted = False
    ucskip = 1
    curskip = 0
    pending_cjk_lead = False
    upr_expect_ansi = False
    stack: list[tuple[str | None, bool, bool, bool, int, bool]] = []
    out: list[str] = []

    def _active_enc() -> str:
        return font_enc.get(current_font or "", default_enc)

    def _is_cjk_lead(b_val: int) -> bool:
        enc = _active_enc()
        if enc == "cp932":
            return (0x81 <= b_val <= 0x9F) or (0xE0 <= b_val <= 0xFC)
        if enc in _CJK_ENCODINGS:
            return 0x81 <= b_val <= 0xFE
        return False

    def _is_cjk_trail_ascii(ch_ord: int) -> bool:
        enc = _active_enc()
        if enc in ("gb18030", "cp936"):
            return 0x30 <= ch_ord <= 0x7E
        if enc in _CJK_ENCODINGS:
            return 0x40 <= ch_ord <= 0x7E
        return False

    for match in _striprtf.PATTERN.finditer(s):
        word, arg, _hex, char, brace, tchar = match.groups()
        tok = match.group(0)
        if brace:
            pending_cjk_lead = False
            curskip = 0
            if brace == "{":
                stack.append((current_font, in_fonttbl, hidden, deleted, ucskip, False))
                if upr_expect_ansi:
                    hidden = True
                    upr_expect_ansi = False
                out.append("{")
            else:
                inner_font = current_font
                was_in_fonttbl = in_fonttbl
                if stack:
                    (
                        current_font,
                        in_fonttbl,
                        hidden,
                        deleted,
                        ucskip,
                        upr_expect_ansi,
                    ) = stack.pop()
                out.append("}")
                if (
                    not in_fonttbl
                    and not (hidden or deleted)
                    and current_font is not None
                    and (was_in_fonttbl or inner_font != current_font)
                ):
                    out.append(f"\\f{current_font} ")
            continue

        if char:
            if (
                pending_cjk_lead
                and curskip == 0
                and not (hidden or deleted)
                and char in ("\\", "{", "}")
                and _is_cjk_trail_ascii(ord(char))
            ):
                out.append(f"\\'{ord(char):02x}")
                pending_cjk_lead = False
                continue
            pending_cjk_lead = False
            if curskip > 0:
                if char in ("~", "-", "_", "\\", "{", "}"):
                    curskip -= 1
                    if not (hidden or deleted):
                        out.append("?")
                    continue
                curskip = 0
            if char == "*" or not (hidden or deleted):
                out.append(tok)
            continue

        if word:
            pending_cjk_lead = False
            w_low = word.lower()
            if curskip > 0:
                if w_low in (
                    "emdash",
                    "endash",
                    "bullet",
                    "lquote",
                    "rquote",
                    "ldblquote",
                    "rdblquote",
                    "emspace",
                    "enspace",
                    "qmspace",
                ):
                    curskip -= 1
                    if not (hidden or deleted):
                        out.append("?")
                    continue
                curskip = 0
            if w_low == "ansicpg":
                continue
            if w_low == "upr":
                upr_expect_ansi = True
                continue
            if w_low == "ud":
                while out and out[-1].strip() == "":
                    out.pop()
                if out and out[-1] == "\\*":
                    out.pop()
                continue
            if w_low == "v":
                hidden = arg != "0"
                continue
            if w_low == "deleted":
                deleted = arg != "0"
                continue
            if w_low == "plain":
                current_font = default_font
                hidden = False
                deleted = False
                if default_font is not None:
                    out.append(f"\\f{default_font} ")
                continue
            if w_low == "deff" and arg is not None:
                default_font = arg
                current_font = arg
                out.append(f"\\deff{arg}\\f{arg} ")
                continue
            if w_low == "fonttbl":
                in_fonttbl = True
                out.append(tok)
                continue
            if w_low == "f" and arg is not None:
                current_font = arg
                out.append(tok)
                continue
            if w_low == "fcharset" and arg is not None:
                if current_font is not None:
                    font_enc[current_font] = _RTF_CHARSET_MAP.get(int(arg), default_enc)
                out.append(tok)
                continue
            if w_low == "uc" and arg is not None:
                ucskip = max(0, int(arg))
                out.append(tok)
                continue
            if w_low == "u" and arg is not None:
                curskip = ucskip
                if not (hidden or deleted):
                    out.append(tok)
                continue
            if (
                not (hidden or deleted)
                or w_low in _RTF_DESTINATIONS
                or w_low
                in (
                    "rtf",
                    "fonttbl",
                    "fcharset",
                    "trowd",
                    "cell",
                    "nestcell",
                    "row",
                    "nestrow",
                )
            ):
                out.append(tok)
            continue

        if _hex:
            if curskip > 0:
                curskip -= 1
                pending_cjk_lead = False
                if not (hidden or deleted):
                    out.append(tok)
                continue
            if hidden or deleted:
                pending_cjk_lead = False
                continue
            b_val = int(_hex, 16)
            if pending_cjk_lead:
                pending_cjk_lead = False
            elif _is_cjk_lead(b_val):
                pending_cjk_lead = True
            out.append(tok)
            continue

        if tchar:
            if curskip > 0:
                curskip -= 1
                pending_cjk_lead = False
                if not (hidden or deleted):
                    out.append(tok)
                continue
            if hidden or deleted:
                pending_cjk_lead = False
                continue
            if pending_cjk_lead and _is_cjk_trail_ascii(ord(tchar)):
                out.append(f"\\'{ord(tchar):02x}")
                pending_cjk_lead = False
                continue
            pending_cjk_lead = False
            out.append(tok)

    return "".join(out)


def _parse_rtf_doc(
    raw_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
) -> ParsedLegacyDoc:
    """Parse RTF-backed `.doc` using `striprtf` (`rtf_to_text`) with table/cell structural markers."""
    if len(raw_bytes) > max_uncompressed_bytes:
        raise DocxValidationError(
            413,
            "import_uncompressed_too_large",
            "文档大小超过安全上限",
        )
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]

    raw_bytes = _strip_rtf_bin_runs(raw_bytes)
    raw_head = raw_bytes[:4096].decode("latin-1", errors="ignore")
    default_enc = _resolve_rtf_default_encoding(raw_head, raw_bytes)

    escaped = re.sub(
        rb"[\x80-\xff]",
        lambda m: f"\\'{m.group(0)[0]:02x}".encode("ascii"),
        raw_bytes,
    )
    s = _normalize_rtf_for_striprtf(
        escaped.decode("ascii", errors="ignore"),
        default_enc,
    )

    with _patched_striprtf():
        rendered = _striprtf.rtf_to_text(s, encoding=default_enc, errors="replace")
    rendered = rendered.encode("utf-16", "surrogatepass").decode("utf-16", "replace")

    # Convert in-cell paragraph breaks (_RTF_PAR_TOK inside a table row before _RTF_ROW_TOK) into \n
    row_chunks = rendered.split(_RTF_ROW_TOK)
    processed_chunks: list[str] = []
    for chunk in row_chunks[:-1]:
        if _RTF_TROWD_TOK in chunk:
            before_row, in_row = chunk.split(_RTF_TROWD_TOK, 1)
            processed_chunks.append(
                before_row
                + in_row.replace(_RTF_TROWD_TOK, "").replace(_RTF_PAR_TOK, "\n")
            )
        elif _RTF_CELL_TOK in chunk:
            processed_chunks.append(chunk.replace(_RTF_PAR_TOK, "\n"))
        else:
            processed_chunks.append(chunk)
    processed_chunks.append(row_chunks[-1].replace(_RTF_TROWD_TOK, ""))
    joined = _RTF_PAR_TOK.join(processed_chunks)

    tables: list[list[list[str]]] = []
    raw_blocks: list[tuple[Literal["paragraph", "table"], str, int | None]] = []
    cur_table: list[list[str]] = []
    for line in joined.split(_RTF_PAR_TOK):
        if _RTF_CELL_TOK in line:
            parts = [normalize_cell_text(p) for p in line.split(_RTF_CELL_TOK)]
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
    body_text = "".join(parts) or "\r"
    text_utf16 = body_text.encode("utf-16-le")
    ccp_text = len(text_utf16) // 2

    clx_bytes = (
        b"\x02"
        + struct.pack("<I", 16)
        + struct.pack("<II", 0, ccp_text)
        + struct.pack("<HIH", 0, 1024, 0)
    )
    table_stream = clx_bytes.ljust(4096, b"\x00")

    fib = bytearray(1024)
    struct.pack_into("<HH", fib, 0x0000, 0xA5EC, 0x00C1)
    struct.pack_into("<H", fib, 0x000A, 0x0200)
    struct.pack_into("<II", fib, 0x0018, 1024, 1024 + len(text_utf16))
    struct.pack_into("<H", fib, 0x0020, 14)
    offset_lw = 0x0022 + 28
    struct.pack_into("<H", fib, offset_lw, 22)
    rg_lw_start = offset_lw + 2
    struct.pack_into("<I", fib, rg_lw_start + 12, ccp_text)
    offset_fclcb = rg_lw_start + 88
    struct.pack_into("<H", fib, offset_fclcb, 93)
    rg_fclcb_start = offset_fclcb + 2
    struct.pack_into("<II", fib, rg_fclcb_start + 33 * 8, 0, len(clx_bytes))

    word_doc_raw = bytes(fib) + text_utf16
    if len(word_doc_raw) < 4096:
        word_doc_raw = word_doc_raw.ljust(4096, b"\x00")
    if len(word_doc_raw) % 512 != 0:
        word_doc_raw = word_doc_raw.ljust(((len(word_doc_raw) + 511) // 512) * 512, b"\x00")

    n_word_secs = len(word_doc_raw) // 512
    n_tbl_secs = len(table_stream) // 512
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
        buf[67] = 1
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
        fat_entries[f_sec] = 0xFFFFFFFD

    fat_bytes = struct.pack(f"<{len(fat_entries)}I", *fat_entries)

    header = bytearray(512)
    header[:8] = OLE2_MAGIC
    struct.pack_into("<HH", header, 0x18, 0x003E, 0x0003)
    struct.pack_into("<HH", header, 0x1C, 0xFFFE, 9)
    struct.pack_into("<H", header, 0x20, 6)
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
                for info in zf.infolist():
                    lower_entry = info.filename.lower()
                    if not lower_entry.endswith((".xml", ".rels")):
                        continue
                    try:
                        DefusedET.fromstring(
                            zf.read(info.filename),
                            forbid_dtd=True,
                            forbid_entities=True,
                            forbid_external=True,
                        )
                    except DefusedXmlException as exc:
                        raise DocxValidationError(
                            415,
                            "import_unsupported_format",
                            "文档包含不安全的 XML 实体声明",
                        ) from exc
                    except Exception as exc:
                        if info.filename == "word/document.xml":
                            raise DocxValidationError(
                                422,
                                "import_parse_failed",
                                "无法解析 .docx 文档结构",
                            ) from exc
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
        if _xml_local_name(ch.tag) == "p":
            out.append(ch)
        else:
            out.extend(_iter_visible_docx_paragraphs(ch))
    return out


def _extract_element_lines_text(elem: Any) -> str:
    """Extract normalized text from `<w:p>`, `<m:oMathPara>`, `<w:tc>`, or `<w:tbl>` across both `.docx` and Word 2003 XML."""
    paragraphs = (
        [elem]
        if _xml_local_name(elem.tag) in ("p", "oMathPara", "oMath")
        else _iter_visible_docx_paragraphs(elem)
    )

    def _walk_p(node: Any, out: list[str]) -> None:
        if _is_wordml_deletion(node):
            return
        local_name = _xml_local_name(node.tag)
        if local_name == "oMath":
            out.append(_render_omath_node(node))
            return
        if local_name == "t":
            out.append(node.text or "")
        elif local_name in ("br", "cr"):
            out.append("\n")
        elif local_name in ("tab", "ptab"):
            out.append("\t")
        elif local_name == "noBreakHyphen":
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


def _extract_rel_ids_from_element(elem: Any) -> list[str]:
    """Extract embedded image relationship IDs (`a:blip/@r:embed` and `v:imagedata/@r:id`).

    Explicitly ignores external links (`a:blip/@r:link`) and selects at most one branch
    of `<mc:AlternateContent>` (`mc:Choice` preferred over `mc:Fallback`).
    """
    rel_ids: list[str] = []

    def _walk(node: Any) -> None:
        local_name = _xml_local_name(node.tag)
        if local_name == "AlternateContent":
            choices = [ch for ch in node if _xml_local_name(ch.tag) == "Choice"]
            fallbacks = [ch for ch in node if _xml_local_name(ch.tag) == "Fallback"]
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
    """Parse `word/_rels/document.xml.rels` into `{rel_id: normalized_media_path}` using `defusedxml`."""
    rels_path = "word/_rels/document.xml.rels"
    if rels_path not in zf.namelist():
        return {}
    raw_rels = zf.read(rels_path)
    try:
        root = DefusedET.fromstring(
            raw_rels,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    for rel in root.iter():
        if _xml_local_name(rel.tag) != "Relationship":
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
    *,
    max_uncompressed_mb: int = 50,
) -> tuple[list[BodyBlock], list[EmbeddedImage]]:
    """Extract ordered `<w:body>` blocks and all embedded images with coordinates from `.docx` or `.doc` bytes."""
    if len(raw_bytes) < 4 or raw_bytes[:4] != ZIP_MAGIC:
        if _looks_like_legacy_doc_bytes(raw_bytes):
            parsed = _parse_legacy_doc(raw_bytes, max_uncompressed_mb=max_uncompressed_mb)
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
                data = fp.read(MAX_EMBEDDED_IMAGE_READ_BYTES)
            media_cache[media_path] = data
            return data

        try:
            root = DefusedET.fromstring(
                zf.read("word/document.xml"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
        except Exception:
            return blocks, images
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


def extract_word_text(
    filename: str,
    raw_bytes: bytes,
    *,
    max_file_mb: int = 20,
    max_uncompressed_mb: int = 50,
) -> str:
    """Extract readable plain/markdown text (paragraphs and tables) from `.doc` or `.docx` bytes."""
    doc = validate_and_load_docx(
        filename,
        raw_bytes,
        max_file_mb=max_file_mb,
        max_uncompressed_mb=max_uncompressed_mb,
    )
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
