"""PDF invoice parsing, field extraction, auto-renaming, and batch export helpers."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader

from app.importer.basket import build_rfc5987_content_disposition
from app.importer.pii import escape_csv_cell, scrub_free_text

MAX_INVOICE_PDF_BYTES = 10 * 1024 * 1024  # 10 MiB per invoice PDF
MAX_INVOICE_ZIP_COUNT = 100
MAX_INVOICE_ZIP_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MiB total per ZIP export
MAX_INVOICE_AMOUNT = Decimal("9999999999.99")

_UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]+')
_WHITESPACE_RE = re.compile(r"\s+")

# Tax category pattern: *类别*具体货物或服务名称
_TAX_STAR_ITEM_RE = re.compile(
    r"\*\s*([^*\n\r]{1,40}?)\s*\*\s*([^\n\r*]+)"
)

# Explicit label on the same line
_LABELED_ITEM_RE = re.compile(
    r"(?:货物或应税劳务[、，,\s]*服务名称|货物或应税劳务名称|货物或应税劳务|项目名称|商品名称|货物名称|服务名称|消费项目|收费项目)"
    r"\s*[:：]\s*([^\n\r]+)"
)

# Trailing numbers / percentages / currency tokens on an item table line
_TRAILING_NUM_OR_RATE_RE = re.compile(
    r"(?:\s+(?:[¥￥]?-?\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?%|\*\*\*|免税|不征税))+\s*$"
)
_TRAILING_UNIT_RE = re.compile(
    r"\s+(?:项|批|次|个|台|套|件|盒|瓶|箱|包|张|份|本|册|支|根|条|块|片|只|双|对|组|卷|桶|袋|罐|克|千克|公斤|吨|升|毫升|米|千米|平方米|立方米|小时|天|日|月|年|人|人次)\s*$"
)

_HEADER_NOISE_TOKENS = frozenset(
    {
        "货物或应税劳务、服务名称",
        "货物或应税劳务名称",
        "服务名称",
        "项目名称",
        "商品名称",
        "货物名称",
        "规格型号",
        "单位",
        "数量",
        "单价",
        "金额",
        "税率",
        "征收率",
        "税率/征收率",
        "税额",
        "合计",
        "价税合计",
        "备注",
        "购买方",
        "销售方",
        "名称",
        "纳税人识别号",
        "统一社会信用代码",
        "地址、电话",
        "开户行及账号",
    }
)

# Date patterns
_LABELED_DATE_CJK_RE = re.compile(
    r"(?:开\s*票\s*日\s*期|填\s*开\s*日\s*期|发\s*票\s*日\s*期|日\s*期)\s*[:：]?\s*"
    r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_LABELED_DATE_ISO_RE = re.compile(
    r"(?:开\s*票\s*日\s*期|填\s*开\s*日\s*期|发\s*票\s*日\s*期|日\s*期)\s*[:：]?\s*"
    r"((?:19|20)\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"
)
_ANY_DATE_CJK_RE = re.compile(
    r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_ANY_DATE_ISO_RE = re.compile(
    r"(?<![0-9])((?:19|20)\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])(?![0-9])"
)

# Amount patterns
_LOWERCASE_TOTAL_RE = re.compile(
    r"(?:价\s*税\s*合\s*计[\s\S]{0,80}?[（(]\s*小\s*写\s*[)）]|[（(]\s*小\s*写\s*[)）])"
    r"\s*[:：]?\s*(?:RMB|CNY|[¥￥])?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_UPPERCASE_CN_ADJACENT_AMOUNT_RE = re.compile(
    r"[壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]{3,}[\s\S]{0,40}?"
    r"(?:[（(]\s*小\s*写\s*[)）]\s*[:：]?\s*)?(?:RMB|CNY|[¥￥])\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_LABELED_TOTAL_AMOUNT_RE = re.compile(
    r"(?:价\s*税\s*合\s*计|合\s*计\s*金\s*额|金\s*额\s*合\s*计|总\s*金\s*额|实\s*付\s*金\s*额|应\s*付\s*金\s*额|总\s*计)"
    r"\s*[:：]?\s*(?:RMB|CNY|[¥￥])?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_HEJI_LINE_AMOUNT_RE = re.compile(
    r"合\s*计\s*[:：]?\s*(?:RMB|CNY|[¥￥])?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_CURRENCY_AMOUNT_RE = re.compile(
    r"(?:RMB|CNY|[¥￥])\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{1,2})",
    re.IGNORECASE,
)

# Invoice number pattern
_INVOICE_NO_RE = re.compile(
    r"(?:发\s*票\s*号\s*码|发\s*票\s*号|票\s*据\s*号\s*码)\s*[:：]?\s*([0-9]{8,24})"
)


class InvoiceValidationError(Exception):
    """Raised when an uploaded invoice PDF or its metadata is invalid."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedInvoice:
    item_name: str | None
    raw_item_name: str | None
    total_amount: Decimal | None
    invoice_date: date | None
    invoice_number: str | None
    extracted_text: str
    pdf_sha256: str
    pdf_size_bytes: int
    warnings: list[str] = field(default_factory=list)


def sanitize_invoice_item_name(raw_name: str | None) -> str:
    """Sanitize an item/goods name for safe cross-platform PDF filenames."""
    text, _ = scrub_free_text((raw_name or "").strip())
    if not text:
        return "未命名项目"
    # If the user passed a *类别*名称 string directly, prefer the specific item name after the second *
    star_match = _TAX_STAR_ITEM_RE.search(text)
    if star_match:
        cat = star_match.group(1).strip()
        specific = star_match.group(2).strip()
        text = specific or cat
    cleaned = _UNSAFE_FILENAME_RE.sub("_", text)
    cleaned = _WHITESPACE_RE.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    cleaned = cleaned[:60].strip("._ ")
    return cleaned or "未命名项目"


def format_invoice_amount(amount: Decimal | str | float | int) -> str:
    """Format an invoice total amount to 2 decimal places (e.g. '1280.00')."""
    dec = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{dec:.2f}"


def build_invoice_download_filename(
    item_name: str | None,
    total_amount: Decimal | str | float | int | None,
    *,
    suffix: str = "",
) -> str:
    """Build the canonical renamed filename `<货物名称>+<合计金额>.pdf`."""
    safe_item = sanitize_invoice_item_name(item_name)
    amount_str = (
        format_invoice_amount(total_amount)
        if total_amount is not None and str(total_amount).strip() != ""
        else "0.00"
    )
    return f"{safe_item}+{amount_str}{suffix}.pdf"


def _strip_trailing_table_columns(line: str) -> str:
    """Strip trailing quantity/unit-price/amount/tax-rate columns from an invoice item line."""
    cleaned = line.strip()
    cleaned = _TRAILING_NUM_OR_RATE_RE.sub("", cleaned).strip()
    cleaned = _TRAILING_UNIT_RE.sub("", cleaned).strip()
    # Second pass in case a model/spec number preceded the unit
    return cleaned


def extract_invoice_item_name(text: str) -> tuple[str | None, str | None]:
    """Extract `(item_name, raw_item_name)` from PDF invoice text."""
    if not text or not text.strip():
        return None, None

    # 1. Standard Chinese VAT / full-digital invoice `*类别*具体名称`
    for match in _TAX_STAR_ITEM_RE.finditer(text):
        category = _WHITESPACE_RE.sub("", match.group(1).strip())
        rest_raw = match.group(2).strip()
        rest_clean = _strip_trailing_table_columns(rest_raw)
        if not category and not rest_clean:
            continue
        raw_full = f"*{category}*{rest_clean}" if rest_clean else f"*{category}*"
        clean_name = rest_clean if rest_clean else category
        if clean_name and clean_name not in _HEADER_NOISE_TOKENS:
            return clean_name[:200], raw_full[:300]

    # 2. Explicit `货物或应税劳务、服务名称：xxx` or `项目名称：xxx` on the same line
    for match in _LABELED_ITEM_RE.finditer(text):
        candidate_raw = match.group(1).strip()
        candidate = _strip_trailing_table_columns(candidate_raw)
        if candidate and candidate not in _HEADER_NOISE_TOKENS:
            return candidate[:200], candidate_raw[:300]

    # 3. Table header followed by item row on subsequent lines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if any(
            hdr in line
            for hdr in ("货物或应税劳务", "项目名称", "商品名称", "货物名称")
        ):
            for follow in lines[idx + 1 : idx + 8]:
                compact = _WHITESPACE_RE.sub("", follow)
                if not compact or compact in _HEADER_NOISE_TOKENS:
                    continue
                # Skip lines that are purely composed of column header words
                tokens = [tok for tok in _WHITESPACE_RE.split(follow) if tok]
                if tokens and all(tok in _HEADER_NOISE_TOKENS for tok in tokens):
                    continue
                if any(
                    skip in compact
                    for skip in (
                        "纳税人识别号",
                        "统一社会信用代码",
                        "开户行及账号",
                        "价税合计",
                        "发票号码",
                        "开票日期",
                    )
                ):
                    continue
                if compact.startswith("合计"):
                    break
                cleaned = _strip_trailing_table_columns(follow)
                if cleaned and cleaned not in _HEADER_NOISE_TOKENS and not cleaned.isdigit():
                    return cleaned[:200], follow[:300]

    return None, None


def _try_make_date(year_s: str, month_s: str, day_s: str) -> date | None:
    try:
        return date(int(year_s), int(month_s), int(day_s))
    except ValueError:
        return None


def extract_invoice_date(text: str) -> date | None:
    """Extract the invoice issue date (`开票日期`) from PDF text."""
    if not text or not text.strip():
        return None

    for pattern in (
        _LABELED_DATE_CJK_RE,
        _LABELED_DATE_ISO_RE,
        _ANY_DATE_CJK_RE,
        _ANY_DATE_ISO_RE,
    ):
        for match in pattern.finditer(text):
            dt = _try_make_date(match.group(1), match.group(2), match.group(3))
            if dt is not None:
                return dt
    return None


def _try_parse_decimal(raw: str) -> Decimal | None:
    cleaned = (
        raw.strip()
        .replace(",", "")
        .replace("，", "")
        .replace("￥", "")
        .replace("¥", "")
        .strip()
    )
    if not cleaned:
        return None
    try:
        val = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if val < Decimal("0") or val > MAX_INVOICE_AMOUNT:
        return None
    return val


def extract_invoice_amount(text: str) -> Decimal | None:
    """Extract the invoice total amount (`价税合计` / `合计`) from PDF text."""
    if not text or not text.strip():
        return None

    normalized = text.replace("￥", "¥").replace("，", ",")

    # 1. Explicit `价税合计（小写）` or `（小写）`
    for match in _LOWERCASE_TOTAL_RE.finditer(normalized):
        dec = _try_parse_decimal(match.group(1))
        if dec is not None and dec > Decimal("0"):
            return dec

    # 2. Chinese uppercase currency line adjacent to `¥1234.56`
    for match in _UPPERCASE_CN_ADJACENT_AMOUNT_RE.finditer(normalized):
        dec = _try_parse_decimal(match.group(1))
        if dec is not None and dec > Decimal("0"):
            return dec

    # 3. Labeled `价税合计` / `合计金额` / `总金额` / `总计`
    for match in _LABELED_TOTAL_AMOUNT_RE.finditer(normalized):
        dec = _try_parse_decimal(match.group(1))
        if dec is not None and dec > Decimal("0"):
            return dec

    # 4. `合 计` line
    heji_candidates: list[Decimal] = []
    for match in _HEJI_LINE_AMOUNT_RE.finditer(normalized):
        dec = _try_parse_decimal(match.group(1))
        if dec is not None and dec > Decimal("0"):
            heji_candidates.append(dec)

    # 5. Any `¥1234.56` currency amounts in the document (the grand total is the max)
    currency_candidates: list[Decimal] = []
    for match in _CURRENCY_AMOUNT_RE.finditer(normalized):
        dec = _try_parse_decimal(match.group(1))
        if dec is not None and dec > Decimal("0"):
            currency_candidates.append(dec)

    if heji_candidates and currency_candidates:
        # If there is a currency amount >= the `合计` pre-tax line (e.g. `价税合计` with tax),
        # return the largest currency amount; otherwise return the `合计` amount.
        return max(max(heji_candidates), max(currency_candidates))
    if heji_candidates:
        return max(heji_candidates)
    if currency_candidates:
        return max(currency_candidates)

    return None


def extract_invoice_number(text: str) -> str | None:
    """Extract optional 8~24 digit invoice number (`发票号码`) from PDF text."""
    if not text or not text.strip():
        return None
    match = _INVOICE_NO_RE.search(text)
    if match:
        return match.group(1).strip()[:64]
    return None


def parse_invoice_pdf(payload: bytes, filename: str = "") -> ParsedInvoice:
    """Validate a PDF invoice file and extract item_name, total_amount, invoice_date, and invoice_number."""
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix and suffix != ".pdf":
            raise InvoiceValidationError(415, "unsupported_type", "仅支持上传 .pdf 格式的发票文件")

    if not payload:
        raise InvoiceValidationError(400, "empty_file", "上传的 PDF 文件为空")
    if len(payload) > MAX_INVOICE_PDF_BYTES:
        raise InvoiceValidationError(413, "file_too_large", "发票 PDF 文件大小超过 10 MB 上限")
    if not payload.startswith(b"%PDF"):
        raise InvoiceValidationError(422, "pdf_invalid", "不是有效的 PDF 发票文件")

    pdf_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise InvoiceValidationError(422, "pdf_invalid", "PDF 发票文件无法解析") from exc

    if reader.is_encrypted:
        raise InvoiceValidationError(422, "pdf_encrypted", "不支持加密的 PDF 发票文件")

    pages_text: list[str] = []
    try:
        for page in reader.pages:
            pages_text.append((page.extract_text() or "").strip())
    except Exception as exc:
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            raise InvoiceValidationError(422, "pdf_encrypted", "不支持加密的 PDF 发票文件") from exc
        raise InvoiceValidationError(422, "pdf_invalid", "PDF 页面文本提取失败") from exc

    full_text = "\n".join(p for p in pages_text if p)
    item_name, raw_item_name = extract_invoice_item_name(full_text)
    total_amount = extract_invoice_amount(full_text)
    invoice_date = extract_invoice_date(full_text)
    invoice_number = extract_invoice_number(full_text)

    warnings: list[str] = []
    if not full_text.strip():
        warnings.append("未能从 PDF 中提取到文本（可能是扫描件），请手动填写货物名称、合计金额与开票日期")
    else:
        if not item_name:
            warnings.append("未能自动识别货物名称，请手动补充")
        if total_amount is None:
            warnings.append("未能自动识别合计金额，请手动补充")
        if invoice_date is None:
            warnings.append("未能自动识别开票日期，请手动补充")

    return ParsedInvoice(
        item_name=item_name,
        raw_item_name=raw_item_name,
        total_amount=total_amount,
        invoice_date=invoice_date,
        invoice_number=invoice_number,
        extracted_text=full_text,
        pdf_sha256=pdf_sha256,
        pdf_size_bytes=len(payload),
        warnings=warnings,
    )


def deduplicate_invoice_filenames(
    items: Sequence[tuple[str | None, Decimal | str | float | int | None]],
) -> list[str]:
    """Generate renamed PDF filenames for a batch, appending `_2`, `_3` on collisions."""
    seen_counts: dict[str, int] = {}
    result: list[str] = []
    for item_name, total_amount in items:
        base_name = build_invoice_download_filename(item_name, total_amount)
        count = seen_counts.get(base_name, 0) + 1
        seen_counts[base_name] = count
        if count == 1:
            result.append(base_name)
        else:
            result.append(
                build_invoice_download_filename(
                    item_name, total_amount, suffix=f"_{count}"
                )
            )
    return result


def build_invoices_csv_bytes(rows: Sequence[dict[str, object]]) -> bytes:
    """Build a UTF-8-SIG CSV summary of invoices with formula-injection protection and PII scrubbing."""
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(
        [
            "序号",
            "重命名文件名",
            "货物名称",
            "合计金额(元)",
            "开票日期",
            "发票号码",
            "上传成员",
            "原文件名",
            "备注",
            "上传时间",
        ]
    )
    for idx, row in enumerate(rows, start=1):
        item_clean, _ = scrub_free_text(str(row.get("item_name") or ""))
        owner_clean, _ = scrub_free_text(str(row.get("owner_name") or ""))
        orig_clean, _ = scrub_free_text(str(row.get("original_filename") or ""))
        note_clean, _ = scrub_free_text(str(row.get("note") or ""))
        inv_no_clean, _ = scrub_free_text(str(row.get("invoice_number") or ""))
        renamed_clean, _ = scrub_free_text(str(row.get("renamed_filename") or ""))
        writer.writerow(
            [
                str(idx),
                escape_csv_cell(renamed_clean),
                escape_csv_cell(item_clean),
                escape_csv_cell(str(row.get("total_amount") or "0.00")),
                escape_csv_cell(str(row.get("invoice_date") or "")),
                escape_csv_cell(inv_no_clean),
                escape_csv_cell(owner_clean),
                escape_csv_cell(orig_clean),
                escape_csv_cell(note_clean),
                escape_csv_cell(str(row.get("created_at") or "")),
            ]
        )
    return buf.getvalue().encode("utf-8")


def build_invoices_zip_bytes(
    rows: Sequence[dict[str, object]],
    pdf_blobs: Sequence[bytes],
) -> bytes:
    """Build an in-memory ZIP archive with renamed PDF invoices and `发票汇总清单.csv`."""
    out = io.BytesIO()
    pairs = [
        (str(r.get("item_name") or ""), str(r.get("total_amount") or "0.00"))
        for r in rows
    ]
    unique_filenames = deduplicate_invoice_filenames(pairs)
    manifest_rows: list[dict[str, object]] = []

    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row, blob, arcname in zip(rows, pdf_blobs, unique_filenames, strict=True):
            zf.writestr(arcname, blob)
            manifest_rows.append({**row, "renamed_filename": arcname})
        csv_bytes = build_invoices_csv_bytes(manifest_rows)
        zf.writestr("发票汇总清单.csv", csv_bytes)

    return out.getvalue()


def build_invoice_content_disposition(utf8_filename: str, fallback_ascii: str = "invoice.pdf") -> str:
    return build_rfc5987_content_disposition(fallback_ascii, utf8_filename)


def today_compact_utc() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")
