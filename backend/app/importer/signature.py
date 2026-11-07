"""Pure-Python signature image validation, .docx anchor binding, and confirm persistence (Spec v2.2 §3)."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.importer.reader import (
    MAX_IMPORT_DEDUP_IMAGE_BYTES,
    MAX_IMPORT_IMAGE_COUNT,
    BodyBlock,
    EmbeddedImage,
    normalize_cell_text,
    normalize_label,
)
from app.models import LabPerson

logger = logging.getLogger(__name__)

MAX_SIGNATURE_BYTES = 2_097_152  # 2 MiB
MIN_SIGNATURE_DIM = 8
MAX_SIGNATURE_DIM = 6000
MAX_SIGNATURE_PIXELS = 20_000_000

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

SIG_HEADER_RE = re.compile(r"(签字|签名|签章)")
PI_ANCHOR_RE = re.compile(
    r"^(?:项目)?(?:申请人|负责人)[（(]?(?:签章|签字|签名)[)）]?"
)


def _is_pi_anchor_block(text: str) -> bool:
    for part in re.split(r"[\r\n\t|]+", text or ""):
        compact = re.sub(r"\s+", "", normalize_cell_text(part))
        if compact and PI_ANCHOR_RE.match(compact):
            return True
    return False


@dataclass(frozen=True)
class SignatureCheck:
    ok: bool
    error_code: str | None
    mime: str | None
    width: int | None
    height: int | None
    size: int
    sha256: str | None


def _parse_png_dimensions(raw: bytes) -> tuple[int, int] | None:
    # Minimum PNG: 8 (magic) + 25 (IHDR chunk) + 12 (IEND chunk) = 45 bytes
    if len(raw) < 45:
        return None
    ihdr_len = int.from_bytes(raw[8:12], "big")
    ihdr_type = raw[12:16]
    if ihdr_type != b"IHDR" or ihdr_len != 13:
        return None
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    if b"IEND" not in raw[-12:]:
        return None
    # Walk PNG chunks to verify structural integrity (no truncated chunk) and require non-empty IDAT
    pos = 8
    saw_idat = False
    saw_iend = False
    while pos < len(raw):
        if pos + 8 > len(raw):
            return None
        c_len = int.from_bytes(raw[pos : pos + 4], "big")
        c_type = raw[pos + 4 : pos + 8]
        next_pos = pos + 8 + c_len + 4
        if next_pos > len(raw):
            return None
        if c_type == b"IDAT" and c_len > 0:
            saw_idat = True
        if c_type == b"IEND":
            if not saw_idat:
                return None
            saw_iend = True
            break
        pos = next_pos
    if not saw_iend or not saw_idat:
        return None
    return width, height


def _parse_jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 12:
        return None
    pos = 2
    width: int | None = None
    height: int | None = None
    saw_sos = False
    saw_eoi = False
    while pos < len(raw):
        if raw[pos] != 0xFF:
            return None
        while pos < len(raw) and raw[pos] == 0xFF:
            pos += 1
        if pos >= len(raw):
            return None
        marker = raw[pos]
        pos += 1
        if marker in (0x00, 0xFF):
            return None
        if marker == 0xD9:
            if not saw_sos or width is None or height is None:
                return None
            saw_eoi = True
            break
        if marker in (0xD8, 0x01) or (0xD0 <= marker <= 0xD7):
            continue
        if pos + 2 > len(raw):
            return None
        seg_len = int.from_bytes(raw[pos : pos + 2], "big")
        if seg_len < 2 or pos + seg_len > len(raw):
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and width is None:
            if seg_len < 8 or pos + 7 > len(raw):
                return None
            height = int.from_bytes(raw[pos + 3 : pos + 5], "big")
            width = int.from_bytes(raw[pos + 5 : pos + 7], "big")
            if width <= 0 or height <= 0:
                return None
        if marker == 0xDA:
            if width is None or height is None:
                return None
            if pos + seg_len >= len(raw):
                return None
            pos += seg_len
            scan_start = pos
            while pos < len(raw):
                if raw[pos] != 0xFF:
                    pos += 1
                    continue
                marker_ff_pos = pos
                while pos < len(raw) and raw[pos] == 0xFF:
                    pos += 1
                if pos >= len(raw):
                    return None
                nxt = raw[pos]
                if nxt == 0x00 or (0xD0 <= nxt <= 0xD7):
                    # Stuffed 0xFF00 byte or RST0..RST7 restart marker inside entropy scan
                    pos += 1
                    continue
                if marker_ff_pos <= scan_start:
                    return None
                pos = marker_ff_pos
                break
            else:
                return None
            saw_sos = True
            continue
        pos += seg_len
    if not saw_sos or not saw_eoi or width is None or height is None:
        return None
    return width, height


def _has_valid_webp_bitstream(chunks_buf: bytes) -> bool:
    c_pos = 0
    found = False
    while c_pos < len(chunks_buf):
        if c_pos + 8 > len(chunks_buf):
            return False
        fcc = chunks_buf[c_pos : c_pos + 4]
        csz = int.from_bytes(chunks_buf[c_pos + 4 : c_pos + 8], "little")
        data_end = c_pos + 8 + csz
        if data_end > len(chunks_buf):
            return False
        cpayload = chunks_buf[c_pos + 8 : data_end]
        if fcc == b"VP8 ":
            if len(cpayload) < 10 or cpayload[3:6] != b"\x9d\x01\x2a":
                return False
            found = True
        elif fcc == b"VP8L":
            if len(cpayload) < 5 or cpayload[0] != 0x2F:
                return False
            found = True
        elif fcc == b"ANMF":
            if len(cpayload) <= 16 or not _has_valid_webp_bitstream(cpayload[16:]):
                return False
            found = True
        c_pos = data_end + (csz & 1)
    return found


def _parse_webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 20:
        return None
    riff_size = int.from_bytes(raw[4:8], "little")
    if riff_size < 12 or riff_size + 8 > len(raw):
        return None
    chunk_fourcc = raw[12:16]
    chunk_size = int.from_bytes(raw[16:20], "little")
    if 20 + chunk_size > len(raw) or 12 + 8 + chunk_size > riff_size + 8:
        return None
    payload = raw[20 : 20 + chunk_size]
    if chunk_fourcc == b"VP8 ":
        if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
    elif chunk_fourcc == b"VP8L":
        if len(payload) < 5 or payload[0] != 0x2F:
            return None
        bits = int.from_bytes(payload[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif chunk_fourcc == b"VP8X":
        if len(payload) < 10:
            return None
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        riff_end = 8 + riff_size
        next_chunk_pos = 20 + chunk_size + (chunk_size & 1)
        if next_chunk_pos >= riff_end or not _has_valid_webp_bitstream(
            raw[next_chunk_pos:riff_end]
        ):
            return None
    else:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def validate_signature_image(data: bytes) -> SignatureCheck:
    """Validate signature image bytes using pure-Python magic-byte and header inspection (Spec v2.2 §3.3)."""
    raw = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else b""
    size = len(raw)

    # 1. Size check
    if size <= 0 or size > MAX_SIGNATURE_BYTES:
        return SignatureCheck(
            ok=False,
            error_code="SIGNATURE_TOO_LARGE",
            mime=None,
            width=None,
            height=None,
            size=size,
            sha256=None,
        )

    # 2. Magic bytes check
    mime: str | None = None
    if raw[:8] == PNG_MAGIC:
        mime = "image/png"
    elif raw[:3] == JPEG_MAGIC:
        mime = "image/jpeg"
    elif size >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return SignatureCheck(
            ok=False,
            error_code="SIGNATURE_REJECTED_FORMAT",
            mime=None,
            width=None,
            height=None,
            size=size,
            sha256=None,
        )

    # 3. Structure & header dimensions
    dims: tuple[int, int] | None = None
    if mime == "image/png":
        dims = _parse_png_dimensions(raw)
    elif mime == "image/jpeg":
        dims = _parse_jpeg_dimensions(raw)
    elif mime == "image/webp":
        dims = _parse_webp_dimensions(raw)

    if dims is None:
        return SignatureCheck(
            ok=False,
            error_code="SIGNATURE_CORRUPT",
            mime=mime,
            width=None,
            height=None,
            size=size,
            sha256=None,
        )

    width, height = dims

    # 4. Dimension range check
    if (
        width < MIN_SIGNATURE_DIM
        or width > MAX_SIGNATURE_DIM
        or height < MIN_SIGNATURE_DIM
        or height > MAX_SIGNATURE_DIM
        or width * height > MAX_SIGNATURE_PIXELS
    ):
        return SignatureCheck(
            ok=False,
            error_code="SIGNATURE_BAD_DIMENSIONS",
            mime=mime,
            width=width,
            height=height,
            size=size,
            sha256=None,
        )

    # 5. SHA-256
    sha256 = hashlib.sha256(raw).hexdigest().lower()
    return SignatureCheck(
        ok=True,
        error_code=None,
        mime=mime,
        width=width,
        height=height,
        size=size,
        sha256=sha256,
    )


def bind_proposal_signatures(
    *,
    people: list[dict[str, object]],
    grids: list[list[list[str]]],
    embedded_images: list[EmbeddedImage],
    body_blocks: list[BodyBlock],
) -> tuple[list[dict[str, object]], dict[str, str], list[dict[str, object]]]:
    """Bind embedded `.docx` images to parsed proposal people (Spec v2.2 §3.4.3–3.4.5).

    Returns `(signatures_meta, signature_blobs_b64, warnings)`.
    """
    warnings: list[dict[str, object]] = []
    blobs: dict[str, str] = {}
    bound_by_temp_id: dict[str, dict[str, object]] = {}

    max_import_images = MAX_IMPORT_IMAGE_COUNT
    max_import_dedup_bytes = MAX_IMPORT_DEDUP_IMAGE_BYTES
    allowed_images: list[EmbeddedImage] = []
    seen_digests: set[bytes] = set()
    dedup_bytes = 0
    quota_exceeded = False

    for img in embedded_images:
        if getattr(img, "quota_exceeded", False):
            quota_exceeded = True
            break
        digest = hashlib.sha256(img.data).digest()
        added_bytes = 0 if digest in seen_digests else len(img.data)
        if (
            len(allowed_images) >= max_import_images
            or dedup_bytes + added_bytes > max_import_dedup_bytes
        ):
            quota_exceeded = True
            break
        seen_digests.add(digest)
        dedup_bytes += added_bytes
        allowed_images.append(img)

    if quota_exceeded:
        warnings.append(
            {
                "code": "SIG_IMPORT_QUOTA",
                "max_images": max_import_images,
                "max_total_bytes": max_import_dedup_bytes,
            }
        )
    embedded_images = allowed_images

    # Index table cell images by (table_index, row_index)
    table_row_images: dict[tuple[int, int], list[tuple[int, EmbeddedImage]]] = {}
    for img_idx, img in enumerate(embedded_images):
        if (
            img.location_kind == "table_cell"
            and img.table_index is not None
            and img.row_index is not None
        ):
            table_row_images.setdefault((img.table_index, img.row_index), []).append(
                (img_idx, img)
            )

    consumed_img_indices: set[int] = set()
    member_table_rows: set[tuple[int, int]] = set()

    # Precompute header signature column per table
    header_sig_col_by_table: dict[tuple[int, int], int | None] = {}

    def _find_sig_col_in_header(t_idx: int, h_idx: int) -> int | None:
        key = (t_idx, h_idx)
        if key in header_sig_col_by_table:
            return header_sig_col_by_table[key]
        if t_idx < 0 or t_idx >= len(grids) or h_idx < 0 or h_idx >= len(grids[t_idx]):
            header_sig_col_by_table[key] = None
            return None
        header_row = grids[t_idx][h_idx]
        matched: int | None = None
        for c_idx, cell_text in enumerate(header_row):
            compact_header = re.sub(r"\s+", "", normalize_cell_text(cell_text))
            if SIG_HEADER_RE.search(compact_header):
                matched = c_idx
                break
        header_sig_col_by_table[key] = matched
        return matched

    _member_header_labels: frozenset[str] = frozenset(
        {
            "姓名",
            "证件号",
            "证件号码",
            "身份证号",
            "学号",
            "工号",
            "学号/工号",
            "专业职务",
            "专业职称",
            "技术职称",
            "专业技术职务",
            "职称",
            "职务",
            "行政职务",
            "担任导师",
            "研究专长",
            "学位",
            "最后学位",
            "最终学位",
            "最高学位",
            "学历学位",
            "工作单位",
            "单位",
            "单位名称",
            "所在单位",
            "所在单位名称",
            "电子信箱",
            "电子邮箱",
            "邮箱",
            "联系电话",
            "手机",
            "电话",
            "出生年月",
            "出生日期",
            "性别",
        }
    )

    # Mark entire recognized member-table regions (from header row to end of table) so unparsed/blank-name
    # member rows are also excluded from PI signature candidates.
    for tbl_i, grid in enumerate(grids):
        for row_i, row in enumerate(grid):
            labels = [normalize_label(c) for c in row if c.strip()]
            if any(lbl in ("男", "女") for lbl in labels):
                continue
            if "姓名" in labels and (
                sum(1 for lbl in labels if lbl in _member_header_labels) >= 3
                or _find_sig_col_in_header(tbl_i, row_i) is not None
            ):
                for mr_idx in range(row_i, len(grid)):
                    member_table_rows.add((tbl_i, mr_idx))
                break

    # Rule 1: Member table row signature cells
    for person in people:
        temp_id = str(person.get("temp_id") or person.get("key") or "")
        p_name = str(person.get("name") or "")
        p_role = "pi" if person.get("project_role") == "applicant" else "member"

        # A person may have a member table row source (either as member or an applicant also in member rows)
        member_src = person.get("member_source") or (
            person.get("source") if p_role == "member" else None
        )
        if not isinstance(member_src, dict):
            continue
        t_idx = member_src.get("table_index")
        r_idx = member_src.get("row_index")
        h_idx = member_src.get("header_row_index")
        if not isinstance(t_idx, int) or not isinstance(r_idx, int):
            continue
        if t_idx < 0 or t_idx >= len(grids) or r_idx < 0 or r_idx >= len(grids[t_idx]):
            continue

        if isinstance(h_idx, int) and 0 <= h_idx < len(grids[t_idx]):
            for mr_idx in range(h_idx, len(grids[t_idx])):
                member_table_rows.add((t_idx, mr_idx))
        member_table_rows.add((t_idx, r_idx))
        row_cells = grids[t_idx][r_idx]
        if not row_cells:
            continue

        sig_col: int
        if isinstance(h_idx, int) and 0 <= h_idx < len(grids[t_idx]):
            header_row = grids[t_idx][h_idx]
            header_sig = _find_sig_col_in_header(t_idx, h_idx)
            offset = 0
            if (
                len(row_cells) + 1 == len(header_row)
                and normalize_label(header_row[0]) in ("项目组成员", "主要参加者", "主要参与者")
            ):
                offset = -1
            if header_sig is not None:
                candidate_col = header_sig + offset
                sig_col = (
                    candidate_col
                    if 0 <= candidate_col < len(row_cells)
                    else len(row_cells) - 1
                )
            else:
                sig_col = len(row_cells) - 1
        else:
            sig_col = len(row_cells) - 1

        row_imgs = table_row_images.get((t_idx, r_idx), [])
        for img_idx, _ in row_imgs:
            consumed_img_indices.add(img_idx)

        sig_cell_imgs = [img for _, img in row_imgs if img.cell_index == sig_col]
        other_cell_imgs = [img for _, img in row_imgs if img.cell_index != sig_col]

        for other_img in other_cell_imgs:
            warnings.append(
                {
                    "code": "SIGNATURE_UNEXPECTED_CELL",
                    "person_temp_id": temp_id,
                    "person_name": p_name,
                    "table_index": t_idx,
                    "row_index": r_idx,
                    "cell_index": other_img.cell_index,
                    "media_path": other_img.media_path,
                }
            )

        if len(sig_cell_imgs) == 0:
            continue
        if len(sig_cell_imgs) >= 2:
            warnings.append(
                {
                    "code": "SIGNATURE_MULTIPLE_IN_ROW",
                    "person_temp_id": temp_id,
                    "person_name": p_name,
                    "table_index": t_idx,
                    "row_index": r_idx,
                }
            )
            continue

        cand_img = sig_cell_imgs[0]
        check = validate_signature_image(cand_img.data)
        if not check.ok or check.sha256 is None or check.mime is None:
            warnings.append(
                {
                    "code": check.error_code or "SIGNATURE_CORRUPT",
                    "person_temp_id": temp_id,
                    "person_name": p_name,
                    "media_path": cand_img.media_path,
                }
            )
            continue

        blobs.setdefault(check.sha256, base64.b64encode(cand_img.data).decode("ascii"))
        bound_by_temp_id[temp_id] = {
            "person_temp_id": temp_id,
            "person_name": p_name,
            "role": p_role,
            "mime": check.mime,
            "size": check.size,
            "width": check.width,
            "height": check.height,
            "sha256": check.sha256,
            "source_kind": "member_table_cell",
            "source_location": {
                "table_index": t_idx,
                "row_index": r_idx,
                "cell_index": sig_col,
                "media_path": cand_img.media_path,
            },
        }

    # Rule 2: PI commitment block signature
    pi_person = next(
        (
            p
            for p in people
            if p.get("project_role") == "applicant" or p.get("role") == "pi"
        ),
        None,
    )
    if pi_person is not None:
        pi_temp_id = str(pi_person.get("temp_id") or pi_person.get("key") or "p1")
        pi_name = str(pi_person.get("name") or "")

        anchor_blocks = [
            blk.block_index for blk in body_blocks if _is_pi_anchor_block(blk.text)
        ]
        if anchor_blocks:
            unconsumed_non_member_imgs: list[tuple[int, EmbeddedImage]] = []
            for img_idx, img in enumerate(embedded_images):
                if img_idx in consumed_img_indices:
                    continue
                if (
                    img.table_index is not None
                    and img.row_index is not None
                    and (img.table_index, img.row_index) in member_table_rows
                ):
                    continue
                unconsumed_non_member_imgs.append((img_idx, img))

            selected_per_anchor: list[tuple[int, EmbeddedImage]] = []
            ambiguous_img_indices: set[int] = set()
            pi_ambiguous = False

            for anchor_b in anchor_blocks:
                in_window = [
                    (idx, img)
                    for idx, img in unconsumed_non_member_imgs
                    if -3 <= (img.block_index - anchor_b) <= 1
                ]
                if not in_window:
                    continue

                def _pi_rank_key(
                    im: EmbeddedImage, ab: int = anchor_b
                ) -> tuple[int, int]:
                    dist = im.block_index - ab
                    return (abs(dist), 0 if dist < 0 else 1)

                best_key = min(_pi_rank_key(im) for _, im in in_window)
                best_candidates = [
                    (idx, im) for idx, im in in_window if _pi_rank_key(im) == best_key
                ]
                if len(best_candidates) >= 2:
                    pi_ambiguous = True
                    for idx, _ in best_candidates:
                        ambiguous_img_indices.add(idx)
                elif len(best_candidates) == 1:
                    selected_per_anchor.append(best_candidates[0])

            distinct_shas = {
                hashlib.sha256(im.data).hexdigest() for _, im in selected_per_anchor
            }
            if pi_ambiguous or len(distinct_shas) >= 2:
                for idx, _ in selected_per_anchor:
                    consumed_img_indices.add(idx)
                consumed_img_indices.update(ambiguous_img_indices)
                warnings.append(
                    {
                        "code": "SIGNATURE_AMBIGUOUS_PI",
                        "person_temp_id": pi_temp_id,
                        "person_name": pi_name,
                        "block_index": anchor_blocks[0],
                    }
                )
            elif len(selected_per_anchor) >= 1:
                winner_idx, pi_img = selected_per_anchor[0]
                consumed_img_indices.add(winner_idx)
                check = validate_signature_image(pi_img.data)
                if not check.ok or check.sha256 is None or check.mime is None:
                    warnings.append(
                        {
                            "code": check.error_code or "SIGNATURE_CORRUPT",
                            "person_temp_id": pi_temp_id,
                            "person_name": pi_name,
                            "media_path": pi_img.media_path,
                        }
                    )
                elif pi_temp_id in bound_by_temp_id:
                    existing_sig = bound_by_temp_id[pi_temp_id]
                    existing_sha = str(existing_sig.get("sha256") or "")
                    if existing_sha != check.sha256:
                        warnings.append(
                            {
                                "code": "SIG_PI_CONFLICT",
                                "person_temp_id": pi_temp_id,
                                "person_name": pi_name,
                                "member_sha256": existing_sha,
                                "pi_sha256": check.sha256,
                                "media_path": pi_img.media_path,
                            }
                        )
                else:
                    blobs.setdefault(
                        check.sha256, base64.b64encode(pi_img.data).decode("ascii")
                    )
                    bound_by_temp_id[pi_temp_id] = {
                        "person_temp_id": pi_temp_id,
                        "person_name": pi_name,
                        "role": "pi",
                        "mime": check.mime,
                        "size": check.size,
                        "width": check.width,
                        "height": check.height,
                        "sha256": check.sha256,
                        "source_kind": "pi_commitment_paragraph",
                        "source_location": {
                            "block_index": pi_img.block_index,
                            "media_path": pi_img.media_path,
                        },
                    }

    # Return signatures ordered by people sequence
    ordered_signatures: list[dict[str, object]] = []
    for person in people:
        t_id = str(person.get("temp_id") or person.get("key") or "")
        if t_id in bound_by_temp_id:
            ordered_signatures.append(bound_by_temp_id[t_id])

    sigs_by_sha: dict[str, list[dict[str, object]]] = {}
    for sig_entry in ordered_signatures:
        sha_val = str(sig_entry.get("sha256") or "")
        if sha_val:
            sigs_by_sha.setdefault(sha_val, []).append(sig_entry)
    for sha_val, shared_group in sigs_by_sha.items():
        if len(shared_group) >= 2:
            warnings.append(
                {
                    "code": "SIG_DUPLICATE_HASH",
                    "sha256": sha_val,
                    "person_temp_ids": [
                        str(g.get("person_temp_id") or "") for g in shared_group
                    ],
                    "person_names": [
                        str(g.get("person_name") or "") for g in shared_group
                    ],
                }
            )

    return ordered_signatures, blobs, warnings


def persist_import_signatures(
    *,
    preview: dict[str, Any],
    imported_people_by_key: dict[str, LabPerson],
    skip_signature_ids: list[str] | None = None,
    ambiguous_keys: set[str] | None = None,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    """Persist bound signatures into `LabPerson` rows within the confirm transaction (Spec v2.2 §3.4.6).

    Returns `(signature_result, extra_warnings)`.
    """
    skip_set = {str(x).strip() for x in (skip_signature_ids or []) if str(x).strip()}
    ambig_set = ambiguous_keys or set()
    raw_sigs = preview.get("signatures")
    sig_entries = [s for s in raw_sigs if isinstance(s, dict)] if isinstance(raw_sigs, list) else []
    raw_blobs = preview.get("_signature_blobs")
    blobs: dict[str, str] = raw_blobs if isinstance(raw_blobs, dict) else {}

    result: dict[str, int] = {
        "saved": 0,
        "unchanged": 0,
        "kept_upload": 0,
        "skipped": 0,
        "rejected": 0,
        "person_not_imported": 0,
    }
    extra_warnings: list[dict[str, object]] = []
    now = datetime.now(UTC)

    for entry in sig_entries:
        temp_id = str(entry.get("person_temp_id") or entry.get("person_key") or "").strip()
        person_name = str(entry.get("person_name") or "").strip()

        # 1. Explicit skip_signature_ids
        if temp_id in skip_set or (person_name and person_name in skip_set):
            result["skipped"] += 1
            continue

        # 5. Ambiguous person match
        if temp_id in ambig_set:
            result["skipped"] += 1
            extra_warnings.append(
                {
                    "code": "SIGNATURE_AMBIGUOUS_PERSON",
                    "person_temp_id": temp_id,
                    "person_name": person_name,
                }
            )
            continue

        # 2. Person not imported in this confirm
        person = imported_people_by_key.get(temp_id)
        if person is None:
            result["person_not_imported"] += 1
            continue

        # 3. Fetch private blob and re-verify with validate_signature_image
        entry_sha = str(entry.get("sha256") or "").strip().lower()
        b64_str = blobs.get(entry_sha) if entry_sha else None
        if not isinstance(b64_str, str) or not b64_str:
            result["rejected"] += 1
            logger.warning(
                "import.signature.rejected",
                extra={
                    "person_temp_id": temp_id,
                    "person_name": person_name,
                    "reason": "missing_blob",
                },
            )
            continue

        try:
            raw_bytes = base64.b64decode(b64_str, validate=True)
        except Exception:
            result["rejected"] += 1
            logger.warning(
                "import.signature.rejected",
                extra={
                    "person_temp_id": temp_id,
                    "person_name": person_name,
                    "reason": "invalid_base64",
                },
            )
            continue

        check = validate_signature_image(raw_bytes)
        if not check.ok or check.sha256 != entry_sha or check.mime is None:
            result["rejected"] += 1
            logger.warning(
                "import.signature.rejected",
                extra={
                    "person_temp_id": temp_id,
                    "person_name": person_name,
                    "reason": check.error_code or "sha256_mismatch",
                },
            )
            continue

        # 4. Compare against target LabPerson's current signature state
        if person.signature_source == "upload":
            result["kept_upload"] += 1
            continue

        if (
            person.signature_source == "docx_import"
            and person.signature_sha256 == check.sha256
        ):
            result["unchanged"] += 1
            continue

        person.signature_data = raw_bytes
        person.signature_mime = check.mime
        person.signature_sha256 = check.sha256
        person.signature_updated_at = now
        person.signature_source = "docx_import"
        result["saved"] += 1

    return result, extra_warnings
