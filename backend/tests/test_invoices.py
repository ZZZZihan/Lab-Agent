"""Unit and API tests for PDF invoice auto-recognition, auto-renaming, member upload, and admin summary/export."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import pytest
from app.audit import verify_audit_chain
from app.auth import hash_password
from app.db import get_sessionmaker
from app.importer.invoices import (
    InvoiceValidationError,
    build_invoice_download_filename,
    deduplicate_invoice_filenames,
    extract_invoice_amount,
    extract_invoice_date,
    extract_invoice_item_name,
    extract_invoice_number,
    parse_invoice_pdf,
)
from app.models import User
from httpx import AsyncClient


def build_unicode_invoice_pdf(lines: list[str]) -> bytes:
    """Build a valid single-page PDF with a ToUnicode CMap so pypdf extracts CJK + ASCII text."""
    text = "\n".join(lines)
    chars = sorted(set(text))
    char_to_cid: dict[str, str] = {}
    bfchar_entries: list[str] = []
    for idx, ch in enumerate(chars, start=1):
        cid_hex = f"{idx:04X}"
        uni_hex = ch.encode("utf-16-be").hex().upper()
        char_to_cid[ch] = cid_hex
        bfchar_entries.append(f"<{cid_hex}> <{uni_hex}>")

    bfchar_blocks: list[str] = []
    for i in range(0, len(bfchar_entries), 100):
        chunk = bfchar_entries[i : i + 100]
        bfchar_blocks.append(
            f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar"
        )

    cmap_stream = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CMapName /CustomCMap def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<0001> <FFFF>\n"
        "endcodespacerange\n"
        + "\n".join(bfchar_blocks)
        + "\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\nend"
    ).encode("ascii")

    content_ops = ["BT", "/F1 12 Tf", "50 750 Td", "16 TL"]
    for line in lines:
        hex_str = "".join(char_to_cid[ch] for ch in line)
        content_ops.append(f"<{hex_str}> Tj T*")
    content_ops.append("ET")
    content_stream = "\n".join(content_ops).encode("ascii")

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {len(content_stream)} >>\nstream\n".encode("ascii")
            + content_stream
            + b"\nendstream\nendobj\n"
        ),
        (
            b"5 0 obj\n<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiKakuGo-W5 "
            b"/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 7 0 R >>\nendobj\n"
        ),
        (
            b"6 0 obj\n<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiKakuGo-W5 "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>\nendobj\n"
        ),
        (
            f"7 0 obj\n<< /Length {len(cmap_stream)} >>\nstream\n".encode("ascii")
            + cmap_stream
            + b"\nendstream\nendobj\n"
        ),
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


async def _create_user_and_login(
    client: AsyncClient,
    *,
    email: str,
    display_name: str,
    role: str,
) -> tuple[User, dict[str, str]]:
    pw = f"SafePw-{role}-9021!Aa"
    maker = get_sessionmaker()
    async with maker() as session:
        user = User(
            email=email,
            display_name=display_name,
            role=role,
            password_hash=hash_password(pw),
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": pw},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


def test_invoice_field_extraction_and_renaming_units() -> None:
    # 1. Standard VAT electronic invoice with *类别*具体货物名称
    pdf_bytes = build_unicode_invoice_pdf(
        [
            "电子发票（普通发票）",
            "发票号码：26312000000012345678",
            "开票日期：2026年11月11日",
            "购买方名称：清衡实验室",
            "项目名称 规格型号 单位 数量 单价 金额 税率 税额",
            "*研发和技术服务*高通量测序服务 批 1 1200.00 1200.00 6% 72.00",
            "合 计 ¥1200.00 ¥72.00",
            "价税合计（大写）壹仟贰佰柒拾贰元整 （小写）¥1272.00",
        ]
    )
    parsed = parse_invoice_pdf(pdf_bytes, "seq_invoice.pdf")
    assert parsed.item_name == "高通量测序服务"
    assert parsed.raw_item_name == "*研发和技术服务*高通量测序服务"
    assert parsed.total_amount == Decimal("1272.00")
    assert parsed.invoice_date == date(2026, 11, 11)
    assert parsed.invoice_number == "26312000000012345678"
    assert parsed.warnings == []
    assert (
        build_invoice_download_filename(parsed.item_name, parsed.total_amount)
        == "高通量测序服务+1272.00.pdf"
    )

    # 2. Labeled item format + 合计 line without 价税合计
    text_labeled = (
        "开票日期: 2026-09-05\n"
        "货物或应税劳务、服务名称：超算GPU机时费\n"
        "合计：¥3,580.50\n"
    )
    item, raw_item = extract_invoice_item_name(text_labeled)
    assert item == "超算GPU机时费"
    assert raw_item == "超算GPU机时费"
    assert extract_invoice_amount(text_labeled) == Decimal("3580.50")
    assert extract_invoice_date(text_labeled) == date(2026, 9, 5)
    assert extract_invoice_number(text_labeled) is None

    # 3. Table header followed by non-starred item row
    text_table = (
        "电子发票\n"
        "日期：2026/08/19\n"
        "货物或应税劳务、服务名称\n"
        "规格型号 单位 数量 单价 金额 税率 税额\n"
        "高纯石英样品管 个 10 85.00 850.00 13% 110.50\n"
        "合计金额：960.50\n"
    )
    t_item, _ = extract_invoice_item_name(text_table)
    assert t_item == "高纯石英样品管"
    assert extract_invoice_amount(text_table) == Decimal("960.50")
    assert extract_invoice_date(text_table) == date(2026, 8, 19)

    # 4. Unsafe characters in item name & duplicate filename deduplication
    assert (
        build_invoice_download_filename("*化学试剂*无水乙醇", "1280")
        == "无水乙醇+1280.00.pdf"
    )
    assert (
        build_invoice_download_filename("高纯试剂/A*B:测试?", "1280")
        == "高纯试剂_A_B_测试+1280.00.pdf"
    )
    deduped = deduplicate_invoice_filenames(
        [
            ("餐费", "128.00"),
            ("餐费", "128"),
            ("打车费", "45.50"),
            ("餐费", Decimal("128.00")),
        ]
    )
    assert deduped == [
        "餐费+128.00.pdf",
        "餐费+128.00_2.pdf",
        "打车费+45.50.pdf",
        "餐费+128.00_3.pdf",
    ]

    # 5. Explicit issue-date labels take precedence over earlier `服务日期` / `住宿日期`
    assert (
        extract_invoice_date("服务日期：2026年10月01日\n开票日期：2026年11月11日")
        == date(2026, 11, 11)
    )
    assert (
        extract_invoice_date("服务日期: 2026-10-01\n开票日期: 2026-11-11")
        == date(2026, 11, 11)
    )
    assert (
        extract_invoice_amount("合计金额：1000.00\n价税合计：1060.00")
        == Decimal("1060.00")
    )

    # 6. Validation errors for non-PDF or corrupt bytes
    with pytest.raises(InvoiceValidationError) as exc_info:
        parse_invoice_pdf(b"not a pdf", "bad.txt")
    assert exc_info.value.status == 415

    with pytest.raises(InvoiceValidationError) as exc_info2:
        parse_invoice_pdf(b"not a pdf", "bad.pdf")
    assert exc_info2.value.status == 422


async def test_invoice_lifecycle_acl_and_admin_export(client: AsyncClient) -> None:
    admin_user, admin_headers = await _create_user_and_login(
        client,
        email="inv-admin@lab.local",
        display_name="管理员林可",
        role="admin",
    )
    member1_user, member1_headers = await _create_user_and_login(
        client,
        email="inv-member1@lab.local",
        display_name="成员陈予安",
        role="member",
    )
    member2_user, member2_headers = await _create_user_and_login(
        client,
        email="inv-member2@lab.local",
        display_name="成员周宁",
        role="member",
    )
    _, viewer_headers = await _create_user_and_login(
        client,
        email="inv-viewer@lab.local",
        display_name="访客张敏",
        role="viewer",
    )

    pdf1 = build_unicode_invoice_pdf(
        [
            "电子发票（普通发票）",
            "发票号码：26312000000088881111",
            "开票日期：2026年11月02日",
            "项目名称 规格型号 单位 数量 单价 金额 税率 税额",
            "*化学试剂和助剂*无水乙醇 瓶 10 45.00 450.00 13% 58.50",
            "合 计 ¥450.00 ¥58.50",
            "价税合计（大写）伍佰零捌元伍角整 （小写）¥508.50",
        ]
    )
    pdf2 = build_unicode_invoice_pdf(
        [
            "电子发票（普通发票）",
            "发票号码：26312000000088882222",
            "开票日期：2026年11月10日",
            "项目名称 规格型号 单位 数量 单价 金额 税率 税额",
            "*信息技术服务*算力租赁费 项 1 2000.00 2000.00 6% 120.00",
            "价税合计（大写）贰仟壹佰贰拾元整 （小写）¥2120.00",
        ]
    )

    # 1. Viewer cannot parse or upload invoices
    viewer_parse = await client.post(
        "/api/v1/lab/invoices/parse",
        headers=viewer_headers,
        files={"file": ("inv1.pdf", pdf1, "application/pdf")},
    )
    assert viewer_parse.status_code == 403

    viewer_upload = await client.post(
        "/api/v1/lab/invoices",
        headers=viewer_headers,
        files={"file": ("inv1.pdf", pdf1, "application/pdf")},
    )
    assert viewer_upload.status_code == 403

    # 2. Member 1 parses and uploads pdf1 without manual overrides
    parse_resp = await client.post(
        "/api/v1/lab/invoices/parse",
        headers=member1_headers,
        files={"file": ("raw_001.pdf", pdf1, "application/pdf")},
    )
    assert parse_resp.status_code == 200, parse_resp.text
    preview = parse_resp.json()
    assert preview["item_name"] == "无水乙醇"
    assert preview["total_amount"] == "508.50"
    assert preview["invoice_date"] == "2026-11-02"
    assert preview["invoice_number"] == "26312000000088881111"
    assert preview["renamed_filename"] == "无水乙醇+508.50.pdf"

    up1_resp = await client.post(
        "/api/v1/lab/invoices",
        headers=member1_headers,
        files={"file": ("raw_001.pdf", pdf1, "application/pdf")},
        data={"note": "实验耗材报销"},
    )
    assert up1_resp.status_code == 201, up1_resp.text
    inv1 = up1_resp.json()["invoice"]
    inv1_id = inv1["id"]
    assert inv1["item_name"] == "无水乙醇"
    assert inv1["total_amount"] == "508.50"
    assert inv1["invoice_date"] == "2026-11-02"
    assert inv1["renamed_filename"] == "无水乙醇+508.50.pdf"
    assert inv1["owner_id"] == str(member1_user.id)

    # 3. Member 2 uploads pdf2 twice (to also test duplicate filename disambiguation in ZIP export)
    up2_resp = await client.post(
        "/api/v1/lab/invoices",
        headers=member2_headers,
        files={"file": ("gpu_a.pdf", pdf2, "application/pdf")},
    )
    assert up2_resp.status_code == 201, up2_resp.text
    inv2 = up2_resp.json()["invoice"]
    inv2_id = inv2["id"]
    assert inv2["renamed_filename"] == "算力租赁费+2120.00.pdf"

    up3_resp = await client.post(
        "/api/v1/lab/invoices",
        headers=member2_headers,
        files={"file": ("gpu_b.pdf", pdf2, "application/pdf")},
    )
    assert up3_resp.status_code == 201, up3_resp.text
    inv3 = up3_resp.json()["invoice"]
    inv3_id = inv3["id"]

    # 4. Member 1 lists own invoices; cannot list scope=all or another member's invoices
    m1_list = await client.get("/api/v1/lab/invoices", headers=member1_headers)
    assert m1_list.status_code == 200
    m1_data = m1_list.json()
    assert m1_data["summary"]["total_count"] == 1
    assert m1_data["summary"]["total_amount"] == "508.50"
    assert [r["id"] for r in m1_data["invoices"]] == [inv1_id]

    m1_all_denied = await client.get(
        "/api/v1/lab/invoices?scope=all",
        headers=member1_headers,
    )
    assert m1_all_denied.status_code == 403

    m1_foreign_dl = await client.get(
        f"/api/v1/lab/invoices/{inv2_id}/download",
        headers=member1_headers,
    )
    assert m1_foreign_dl.status_code == 403

    m1_foreign_patch = await client.patch(
        f"/api/v1/lab/invoices/{inv2_id}",
        headers=member1_headers,
        json={"item_name": "篡改名称"},
    )
    assert m1_foreign_patch.status_code == 403

    m1_foreign_del = await client.delete(
        f"/api/v1/lab/invoices/{inv2_id}",
        headers=member1_headers,
    )
    assert m1_foreign_del.status_code == 403

    m1_foreign_zip = await client.post(
        "/api/v1/lab/invoices/export:zip",
        headers=member1_headers,
        json={"invoice_ids": [inv1_id, inv2_id]},
    )
    assert m1_foreign_zip.status_code == 403

    # 5. Member 1 downloads own invoice and gets the auto-renamed filename in Content-Disposition
    dl_resp = await client.get(
        f"/api/v1/lab/invoices/{inv1_id}/download",
        headers=member1_headers,
    )
    assert dl_resp.status_code == 200
    assert dl_resp.content == pdf1
    disp = dl_resp.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in disp
    utf8_part = disp.split("filename*=UTF-8''", 1)[1].split(";", 1)[0]
    assert unquote(utf8_part) == "无水乙醇+508.50.pdf"

    # 6. Member 1 edits item_name and total_amount -> renamed_filename updates automatically
    patch_resp = await client.patch(
        f"/api/v1/lab/invoices/{inv1_id}",
        headers=member1_headers,
        json={"item_name": "分析纯无水乙醇", "total_amount": "510.00"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["invoice"]["renamed_filename"] == "分析纯无水乙醇+510.00.pdf"

    # 7. Admin views all members' invoices, filters by member/date/query, and exports ZIP + CSV
    admin_list = await client.get(
        "/api/v1/lab/invoices?scope=all",
        headers=admin_headers,
    )
    assert admin_list.status_code == 200
    admin_data = admin_list.json()
    assert admin_data["summary"]["total_count"] == 3
    assert admin_data["summary"]["total_amount"] == "4750.00"
    assert admin_data["summary"]["owner_count"] == 2
    assert len(admin_data["owners"]) == 2

    admin_filtered = await client.get(
        f"/api/v1/lab/invoices?scope=all&owner_id={member2_user.id}",
        headers=admin_headers,
    )
    assert admin_filtered.status_code == 200
    assert admin_filtered.json()["summary"]["total_count"] == 2
    assert admin_filtered.json()["summary"]["total_amount"] == "4240.00"

    # Export ZIP of all 3 invoices as admin
    zip_resp = await client.post(
        "/api/v1/lab/invoices/export:zip",
        headers=admin_headers,
        json={"scope": "all"},
    )
    assert zip_resp.status_code == 200
    assert zip_resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zip_resp.content), "r") as zf:
        names = set(zf.namelist())
        assert "分析纯无水乙醇+510.00.pdf" in names
        assert "算力租赁费+2120.00.pdf" in names
        assert "算力租赁费+2120.00_2.pdf" in names
        assert "发票汇总清单.csv" in names
        csv_text = zf.read("发票汇总清单.csv").decode("utf-8-sig")
        assert "分析纯无水乙醇+510.00.pdf" in csv_text
        assert "算力租赁费+2120.00_2.pdf" in csv_text
        assert "成员陈予安" in csv_text
        assert "成员周宁" in csv_text

    # Export CSV summary directly as admin
    csv_resp = await client.get(
        "/api/v1/lab/invoices/export:csv?scope=all",
        headers=admin_headers,
    )
    assert csv_resp.status_code == 200
    assert "分析纯无水乙醇+510.00.pdf" in csv_resp.content.decode("utf-8-sig")

    # 8. Admin deletes inv3 and verifies remaining count
    del_resp = await client.delete(
        f"/api/v1/lab/invoices/{inv3_id}",
        headers=admin_headers,
    )
    assert del_resp.status_code == 200

    # 9. If admin downgrades member1 to viewer, member1 can no longer mutate or delete own invoice
    downgrade_resp = await client.patch(
        f"/api/v1/admin/users/{member1_user.id}",
        headers=admin_headers,
        json={"role": "viewer"},
    )
    assert downgrade_resp.status_code == 200
    m1_downgraded_patch = await client.patch(
        f"/api/v1/lab/invoices/{inv1_id}",
        headers=member1_headers,
        json={"item_name": "降级后尝试修改"},
    )
    assert m1_downgraded_patch.status_code == 403
    m1_downgraded_del = await client.delete(
        f"/api/v1/lab/invoices/{inv1_id}",
        headers=member1_headers,
    )
    assert m1_downgraded_del.status_code == 403

    # 10. Verify hash-chained audit log integrity across all invoice events
    maker = get_sessionmaker()
    async with maker() as session:
        broken = await verify_audit_chain(session)
        assert broken is None
