"""Tests for extraction, chunker fallbacks, async ingest worker, and idempotent seed upsert."""

from __future__ import annotations

import io

import pytest
from app.auth import hash_password
from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest_worker import process_next_pending, recover_processing
from app.models import Document, User
from app.rag.chunker import chunk_document, chunk_markdown, chunk_pdf, chunk_plain
from app.rag.embedder import EmbeddingError, HashingEmbedder
from app.rag.extract import UploadRejected, extract_upload
from app.rag.ingest import create_document, upsert_source
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select, text


def make_pdf_bytes(pages_text: list[str], *, encrypt_password: str | None = None) -> bytes:
    """Create a valid PDF with optional per-page Latin-1 text streams using pypdf."""
    writer = PdfWriter()
    for page_text in pages_text:
        page = writer.add_blank_page(width=612, height=792)
        if page_text:
            font_dict = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_dict})}
            )
            page[NameObject("/Resources")] = resources
            stream = DecodedStreamObject()
            safe = (
                page_text.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
            )
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypt_password:
        writer.encrypt(encrypt_password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def _make_user(email: str = "admin@lab.local", role: str = "admin") -> User:
    maker = get_sessionmaker()
    async with maker() as session:
        user = User(
            email=email,
            display_name="沈昭",
            role=role,
            password_hash=hash_password("LabAgent-Demo-2026-Admin"),
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return user


def test_chunk_plain_3000_chars_without_headings() -> None:
    sentence = "实验记录第零一条：清衡实验室超算节点每日定时巡检并同步温度与功耗数据。"
    paragraphs = [sentence * 10 for _ in range(10)]
    raw_text = "\n\n".join(paragraphs)
    assert len(raw_text) >= 3000

    chunks = chunk_plain(raw_text, max_chars=800, overlap=80)
    assert len(chunks) >= 4
    for idx, chunk in enumerate(chunks, start=1):
        assert chunk.heading == f"第 {idx} 段"
        assert len(chunk.text) <= 800 + 80


def test_chunk_markdown_fallback_splits_oversized_single_paragraph() -> None:
    long_para = ("这是一句超过上限测试的话，用于验证单段超长时的标点切分兜底。" * 40) + ("A" * 950)
    body = f"前言说明\n\n## 超长章节\n\n{long_para}"
    chunks = chunk_markdown(body, max_chars=800)
    assert len(chunks) >= 3
    assert chunks[0].heading == "概述"
    for chunk in chunks[1:]:
        assert chunk.heading == "超长章节"
        assert len(chunk.text) <= 800


def test_pdf_extraction_and_page_chunk_headings() -> None:
    page1 = "Page 1: Qingheng Lab cluster A100 node count is 8 and reservation limit is 24 hours."
    page2 = ("Page 2: Safety protocol requires rotating access tokens every 30 days. " * 20).strip()
    pdf_bytes = make_pdf_bytes([page1, page2])

    extracted = extract_upload("handbook.pdf", pdf_bytes, max_chars=300_000)
    assert extracted.source_format == "pdf"
    assert "\f" in extracted.text

    chunks = chunk_document(extracted.text, extracted.source_format)
    headings = [c.heading for c in chunks]
    assert "第 1 页" in headings
    assert any(h == "第 2 页" or h.startswith("第 2 页（") for h in headings)

    multi_page = chunk_pdf("Short page one text here.\f" + ("Sentence on page two. " * 80), max_chars=800, overlap=80)
    multi_headings = [c.heading for c in multi_page]
    assert multi_headings[0] == "第 1 页"
    assert "第 2 页（1）" in multi_headings
    assert "第 2 页（2）" in multi_headings


def test_scanned_or_encrypted_pdf_rejected_with_422() -> None:
    scanned_bytes = make_pdf_bytes(["", "   "])
    with pytest.raises(UploadRejected) as exc_scanned:
        extract_upload("scan.pdf", scanned_bytes, max_chars=300_000)
    assert exc_scanned.value.status == 422
    assert exc_scanned.value.code == "pdf_no_text"
    assert "扫描版 PDF" in exc_scanned.value.message

    encrypted_bytes = make_pdf_bytes(
        ["Confidential page content with enough characters."],
        encrypt_password="secret",
    )
    with pytest.raises(UploadRejected) as exc_enc:
        extract_upload("secret.pdf", encrypted_bytes, max_chars=300_000)
    assert exc_enc.value.status == 422
    assert exc_enc.value.code == "pdf_encrypted"


def test_gb18030_text_decoded_and_unsupported_rejected() -> None:
    gb_text = "清衡实验室设备采购与安全管理规定（GB18030 编码测试）。"
    payload = gb_text.encode("gb18030")
    extracted = extract_upload("notes.txt", payload, max_chars=300_000)
    assert extracted.source_format == "text"
    assert extracted.text == gb_text

    with pytest.raises(UploadRejected) as exc_ext:
        extract_upload("archive.docx", b"hello", max_chars=300_000)
    assert exc_ext.value.status == 415
    assert exc_ext.value.code == "unsupported_type"

    with pytest.raises(UploadRejected) as exc_long:
        extract_upload("huge.txt", ("字" * 100).encode("utf-8"), max_chars=50)
    assert exc_long.value.status == 413
    assert exc_long.value.code == "document_too_long"

    with pytest.raises(UploadRejected) as exc_bad:
        extract_upload("corrupt.txt", b"\xff\xfe\x81\x00\x82\x00", max_chars=300_000)
    assert exc_bad.value.status == 422
    assert exc_bad.value.code == "bad_encoding"


def test_doc_and_docx_upload_extraction(tmp_path) -> None:
    import struct

    import docx
    from app.importer.parser import _extract_people, _extract_t4_foundation
    from app.importer.reader import (
        DocxValidationError,
        _parse_papx_table_flags,
        build_ole2_doc_bytes,
        extract_table_grid,
        iter_document_tables,
        validate_and_load_docx,
    )
    from app.rag.ingest import read_corpus_dir

    # 1. Binary OLE2 .doc extraction
    ole2_bytes = build_ole2_doc_bytes(
        paragraphs=["## 实验室管理制度", "第一条：所有实验人员进入超净间必须佩戴防静电手环。"],
        tables=[[["设备名称", "负责人"], ["飞秒激光器", "沈昭"]]],
    )
    extracted_doc = extract_upload("handbook.doc", ole2_bytes, max_chars=300_000)
    assert extracted_doc.source_format == "markdown"
    assert "实验室管理制度" in extracted_doc.text
    assert "飞秒激光器 | 沈昭" in extracted_doc.text
    chunks = chunk_document(extracted_doc.text, extracted_doc.source_format)
    assert any(c.heading == "实验室管理制度" for c in chunks)

    # 2. .docx extraction (including <w:tab/> preservation and <m:oMath>/<m:t> native equations)
    from docx.oxml import parse_xml

    d = docx.Document()
    d.add_heading("设备巡检规程", level=2)
    p_tab = d.add_paragraph("每日上午九点检查液氮罐液位。")
    p_tab.add_run("\tSample\tTemperature ")
    p_tab._p.append(
        parse_xml(
            '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
            "<m:r><m:t>E=m</m:t></m:r>"
            "<m:sSup><m:e><m:r><m:t>c</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup>"
            "<m:r><m:t>+</m:t></m:r>"
            "<m:f><m:num><m:sSub><m:e><m:r><m:t>a</m:t></m:r></m:e><m:sub><m:r><m:t>i</m:t></m:r></m:sub></m:sSub></m:num>"
            "<m:den><m:rad><m:radPr><m:degHide m:val=\"1\"/></m:radPr><m:deg/><m:e><m:r><m:t>x+1</m:t></m:r></m:e></m:rad></m:den></m:f>"
            "</m:oMath>"
        )
    )
    p_tab._p.append(
        parse_xml(
            '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:rPr><w:vanish/></w:rPr><w:t>SECRET_VANISHED</w:t></w:r>"
        )
    )
    p_tab._p.append(
        parse_xml(
            '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:t> 10</w:t><w:noBreakHyphen/><w:t>20 mg</w:t></w:r>"
        )
    )
    tbl = d.add_table(rows=2, cols=2)
    tbl.rows[0].cells[0].text = "项目"
    tbl.rows[0].cells[1].text = "阈值"
    tbl.rows[1].cells[0].text = "液位"
    tbl.rows[1].cells[1].text = "80%"
    tbl.rows[1].cells[1].paragraphs[0]._p.append(
        parse_xml(
            '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
            "<m:r><m:t>+ΔT</m:t></m:r></m:oMath>"
        )
    )
    inner_tbl = tbl.rows[1].cells[1].add_table(rows=1, cols=2)
    inner_tbl.rows[0].cells[0].text = "温度"
    inner_tbl.rows[0].cells[1].text = "-196C"
    buf = io.BytesIO()
    d.save(buf)
    docx_bytes = buf.getvalue()
    extracted_docx = extract_upload("sop.docx", docx_bytes, max_chars=300_000)
    assert extracted_docx.source_format == "markdown"
    assert "每日上午九点检查液氮罐液位" in extracted_docx.text
    assert "Sample Temperature E=mc^2+(a_i)/(sqrt(x+1)) 10-20 mg" in extracted_docx.text
    assert "SECRET_VANISHED" not in extracted_docx.text
    assert "液位 | 80%+ΔT 温度 | -196C" in extracted_docx.text
    docx_grids = [
        extract_table_grid(t)
        for t in iter_document_tables(validate_and_load_docx("sop.docx", docx_bytes))
    ]
    assert docx_grids[0][1][1].startswith("80%+ΔT")

    # 3. RTF-backed .doc with \ansicpg1252 hex escapes, \ucN fallback counts, literal braces/backslashes, surrogate pairs, and multi-row tables
    rtf_bytes = (
        b"{\\rtf1\\ansi\\ansicpg1252\\uc1 Important text\\par"
        b"{\\fonttbl{\\f0 Arial;}}"
        b"\\pard First paragraph \\u-10179?\\u-8704? and lone \\u-10179?\\par"
        b"\\pard {\\uc1 caf\\u233e} {\\uc0 caf\\u233?} A=\\u123?x\\u125?; path=\\u92?folder\\par"
        b"\\trowd\\cellx1000\\cellx2000\\pard\\intbl caf\\'e9\\cell R\\u233?sum\\u233?\\cell\\row"
        b"\\trowd\\cellx1000\\cellx2000\\pard\\intbl Alice\\cell alice@example.com\\cell\\row"
        b"\\pard Trailing paragraph\\par"
        b"}"
    )
    rtf_doc = validate_and_load_docx("intl.doc", rtf_bytes)
    assert [b.text for b in rtf_doc.body_blocks if b.kind == "paragraph"] == [
        "Important text",
        "First paragraph 😀 and lone �",
        "café café? A={x}; path=\\folder",
        "Trailing paragraph",
    ]
    rtf_grids = [extract_table_grid(t) for t in iter_document_tables(rtf_doc)]
    assert rtf_grids == [[["café", "Résumé"], ["Alice", "alice@example.com"]]]
    rtf_uploaded = extract_upload("intl.doc", rtf_bytes, max_chars=300_000)
    assert rtf_uploaded.text.encode("utf-8")
    assert "Important text" in rtf_uploaded.text
    assert "😀" in rtf_uploaded.text
    assert "café café? A={x}; path=\\folder" in rtf_uploaded.text
    assert "Alice | alice@example.com" in rtf_uploaded.text

    # 3a. RTF with literal non-ASCII Windows-1251 bytes under \ansicpg1251 and special char control words
    rtf_cp1251 = (
        b"{\\rtf1\\ansi\\ansicpg1251 \\pard "
        + "Привет мир".encode("cp1251")
        + b"\\par}"
    )
    assert extract_upload("ru.doc", rtf_cp1251, max_chars=300_000).text.strip() == "Привет мир"
    rtf_special_cw = (
        b"{\\rtf1\\ansi\\ansicpg1252 \\pard "
        b"\\bullet Item 1: 2020\\endash 2024\\emdash \\ldblquote Quoted\\rdblquote and \\lquote Single\\rquote\\par}"
    )
    assert (
        extract_upload("cw.doc", rtf_special_cw, max_chars=300_000).text.strip()
        == "•Item 1: 2020–2024—“Quoted”and ‘Single’"
    )
    rtf_font_charset = (
        b"{\\rtf1\\ansi\\ansicpg1252"
        b"{\\fonttbl{\\f0\\fcharset134 SimSun;}{\\f1\\fcharset0 Arial;}}"
        b"\\pard \\f1 caf\\'e9 {\\f0 \\'d6\\'d0\\'ce\\'c4} \\f1 r\\'e9sum\\'e9\\par}"
    )
    assert (
        extract_upload("fcharset.doc", rtf_font_charset, max_chars=300_000).text.strip()
        == "café 中文 résumé"
    )
    rtf_unterminated_rows = (
        b"{\\rtf1\\ansi\\ansicpg1252 " + (b"\\trowd " * 20000) + b"\\pard Hello\\par}"
    )
    assert (
        extract_upload("unterminated_rows.doc", rtf_unterminated_rows, max_chars=300_000).text.strip()
        == "Hello"
    )
    rtf_ansi_default = "{\\rtf1\\ansi résumé naïve\\par}".encode("cp1252")
    assert (
        extract_upload("ansi_default.doc", rtf_ansi_default, max_chars=300_000).text.strip()
        == "résumé naïve"
    )
    rtf_field_and_bin = (
        b"{\\rtf1\\ansi "
        b"{\\field{\\fldinst HYPERLINK \"https://example.com\"}{\\fldrslt Click here}} "
        b"{\\pict\\pngblip\\bin8 a{b}c\\\\d}After image\\par}"
    )
    assert (
        extract_upload("field_bin.doc", rtf_field_and_bin, max_chars=300_000).text.strip()
        == "Click here After image"
    )
    rtf_upr_ud = (
        b"{\\rtf1\\ansi {\\upr{??}{\\*\\ud \\u20013?\\u25991?}}\\par"
        b"\\pard{\\*\\bkmkstart start}Important text{\\*\\bkmkend start}\\par"
        b"\\pard \\u160\\~A\\u8212\\emdash B\\u8209\\-C\\par"
        b"\\pard Visible {\\v HiddenGroup \\u20013? \\'e9} StillVisible \\v HiddenRun \\v0 EndVisible \\v HiddenPlain \\plain ResetVisible\\par"
        b"\\pard Dose: {\\deleted 100 mg }\\deleted 50 mg \\deleted0 10 mg \\deleted 20 mg \\plain 5 mg\\par}"
    )
    assert (
        extract_upload("upr.doc", rtf_upr_ud, max_chars=300_000).text.strip()
        == "中文\n\nImportant text\n\nA—B‐C\n\nVisible StillVisible EndVisible ResetVisible\n\nDose: 10 mg 5 mg"
    )
    rtf_cp932_escaped_trail = (
        b"{\\rtf1\\ansi\\ansicpg932 \\pard \x95\\\\ and \\'95\\\\ and \\'95\\'5c\\par}"
    )
    assert (
        extract_upload("cp932.doc", rtf_cp932_escaped_trail, max_chars=300_000).text.strip()
        == "表 and 表 and 表"
    )
    rtf_gb18030_4byte = (
        b"{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}{\\f1\\fcharset134 SimSun;}}"
        b"\\pard \\f0 caf\\'e9 {\\f1 "
        + "清衡实验室𠀀".encode("gb18030")
        + b"}\\par}"
    )
    assert (
        extract_upload("gb4.doc", rtf_gb18030_4byte, max_chars=300_000).text.strip()
        == "café 清衡实验室𠀀"
    )

    # 3b. HTML-backed .doc declaring windows-1252 charset, UTF-8 BOM, UTF-16 BOM, rowspan/colspan tables, and GB2312 Word 2003 XML
    html_cp1252 = (
        '<html><head><meta charset="windows-1252"></head>'
        "<body><p>café résumé</p></body></html>"
    ).encode("cp1252")
    html_uploaded = extract_upload("cp1252.doc", html_cp1252, max_chars=300_000)
    assert "café résumé" in html_uploaded.text
    for bom_enc in ("utf-8-sig", "utf-16"):
        bom_html = "<html><body><p>Test text</p></body></html>".encode(bom_enc)
        assert "Test text" in extract_upload("bom.doc", bom_html, max_chars=300_000).text
    html_rowspan_doc = (
        "<html><body><table>"
        "<tr><th>姓名</th><th>职称</th><th>工作单位</th><th>邮箱</th></tr>"
        '<tr><td>张三</td><td>教授</td><td rowspan="2">天津工业大学</td><td>zhang@example.com</td></tr>'
        "<tr><td>李四</td><td>讲师</td><td>li@example.com</td></tr>"
        '<tr><td>王五</td><td colspan="2"></td><td>wang@example.com</td></tr>'
        "</table></body></html>"
    ).encode()
    parsed_rowspan = validate_and_load_docx("roster.doc", html_rowspan_doc)
    rowspan_grids = [extract_table_grid(t) for t in iter_document_tables(parsed_rowspan)]
    assert rowspan_grids == [
        [
            ["姓名", "职称", "工作单位", "邮箱"],
            ["张三", "教授", "天津工业大学", "zhang@example.com"],
            ["李四", "讲师", "天津工业大学", "li@example.com"],
            ["王五", "", "", "wang@example.com"],
        ]
    ]
    roster_people, _ = _extract_people(rowspan_grids)
    assert [(p["name"], p["title"], p["affiliation"], p["email"]) for p in roster_people] == [
        ("张三", "教授", "天津工业大学", "zhang@example.com"),
        ("李四", "讲师", "天津工业大学", "li@example.com"),
        ("王五", None, None, "wang@example.com"),
    ]
    # Verify PAPX FKP page deduplication on repeated page numbers
    fake_fkp = bytearray(512)
    fake_fkp[511] = 1
    struct.pack_into("<II", fake_fkp, 0, 1024, 1030)
    fake_fkp[8] = 10  # b_offset = 10 -> papx_pos = 20
    fake_fkp[20] = 3  # cb = 3 -> 5 bytes (2-byte istd + 3-byte sprmPFInTable)
    struct.pack_into("<HHB", fake_fkp, 21, 0, 0x2416, 1)
    plc_rep = struct.pack("<4001I", *range(4001)) + struct.pack("<4000I", *([0] * 4000))
    in_tbl_fcs, _ = _parse_papx_table_flags(bytes(fake_fkp), plc_rep, 0, len(plc_rep))
    assert in_tbl_fcs == {1030}
    xml_gb2312 = (
        '<?xml version="1.0" encoding="gb2312"?>'
        '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml" '
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        "<w:body><w:p><w:r><w:t>中文GB2312测试 </w:t></w:r>"
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDEN_XML</w:t></w:r>"
        "<m:oMath><m:f><m:num><m:r><m:t>1</m:t></m:r></m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f></m:oMath>"
        "</w:p></w:body></w:wordDocument>"
    ).encode("gb18030")
    xml_gb2312_text = extract_upload("gb2312.doc", xml_gb2312, max_chars=300_000).text
    assert "中文GB2312测试 1/2" in xml_gb2312_text
    assert "HIDDEN_XML" not in xml_gb2312_text
    xml_vmerge_large = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml">'
        "<w:body><w:tbl>"
        f"<w:tr><w:tc><w:tcPr><w:vMerge w:val=\"restart\"/></w:tcPr><w:p><w:r><w:t>{'A' * 4096}</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>R1</w:t></w:r></w:p></w:tc></w:tr>"
        + "".join(
            f"<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>R{k}</w:t></w:r></w:p></w:tc></w:tr>"
            for k in range(2, 50)
        )
        + "</w:tbl></w:body></w:wordDocument>"
    ).encode()
    vmerge_extracted = extract_upload("vmerge_large.doc", xml_vmerge_large, max_chars=300_000)
    assert len(vmerge_extracted.text) < 10_000
    xml_nested_span = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml">'
        "<w:body><w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>姓名</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>职称</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>工作单位</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>邮箱</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>张三</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>教授</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr><w:p><w:r><w:t>研究所</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc>"
        "<w:tc><w:p><w:r><w:t>zhang@example.com</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl></w:body></w:wordDocument>"
    ).encode()
    parsed_xml_nested = validate_and_load_docx("nested_span.doc", xml_nested_span)
    xml_nested_grids = [extract_table_grid(t) for t in iter_document_tables(parsed_xml_nested)]
    xml_nested_people, _ = _extract_people(xml_nested_grids)
    assert [(p["name"], p["affiliation"], p["email"]) for p in xml_nested_people] == [
        ("张三", "研究所", "zhang@example.com")
    ]

    # 3d. Binary OLE2 .doc with supplementary character in main story followed by footnote story
    fn_ole2 = bytearray(build_ole2_doc_bytes(paragraphs=["Main 😀", "Sub 🚀", "FOOTNOTE"]))
    # Declare ccpText = 15 UTF-16 code units ("Main 😀\rSub 🚀\r" = 8 + 7 = 15) in FIB rgLw[3] (offset 512 + 0x4C)
    struct.pack_into("<I", fn_ole2, 512 + 0x4C, 15)
    fn_extracted = extract_upload("footnote.doc", bytes(fn_ole2), max_chars=300_000)
    assert fn_extracted.text.strip() == "Main 😀\n\nSub 🚀"
    assert "FOOTNOTE" not in fn_extracted.text
    nested_field_ole2 = build_ole2_doc_bytes(
        paragraphs=[
            '\x13 IF \x13 REF x \x14 1 \x15 = 1 "Yes" "No" \x14Yes\x15',
            "\x13 IF X",
            ' = 1 "Yes" "No" \x14MultiParaYes\x15',
            ("\x13\x14" * 2000) + "DeepResult" + ("\x15" * 2000),
        ]
    )
    assert (
        extract_upload("nested_field.doc", nested_field_ole2, max_chars=300_000).text.strip()
        == "Yes\n\nMultiParaYes\n\nDeepResult"
    )

    # 3c. OLE2 .doc with an embedded ObjectPool child storage containing a nested WordDocument stream
    embedded_ole2 = bytearray(ole2_bytes)
    dir_sec = struct.unpack_from("<I", embedded_ole2, 0x30)[0]
    dir_off = (dir_sec + 1) * 512
    # Link entry 2 (1Table) right_sib -> entry 3 (ObjectPool storage, child_id=4)
    struct.pack_into("<I", embedded_ole2, dir_off + 2 * 128 + 72, 3)
    obj_pool = bytearray(128)
    pool_name = "ObjectPool\x00".encode("utf-16-le")
    obj_pool[: len(pool_name)] = pool_name
    struct.pack_into("<H", obj_pool, 64, len(pool_name))
    obj_pool[66] = 1  # storage
    obj_pool[67] = 1
    struct.pack_into("<III", obj_pool, 68, 0xFFFFFFFF, 0xFFFFFFFF, 4)  # child_id = 4
    # Entry 4 is a nested WordDocument stream pointing at invalid/empty sector 0xFFFFFFFE
    nested_wd = bytearray(128)
    wd_name = "WordDocument\x00".encode("utf-16-le")
    nested_wd[: len(wd_name)] = wd_name
    struct.pack_into("<H", nested_wd, 64, len(wd_name))
    nested_wd[66] = 2  # stream
    nested_wd[67] = 1
    struct.pack_into("<III", nested_wd, 68, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", nested_wd, 116, 0xFFFFFFFE)
    struct.pack_into("<Q", nested_wd, 120, 4096)
    embedded_ole2[dir_off + 3 * 128 : dir_off + 4 * 128] = obj_pool
    # Expand directory sector in-place by replacing entry 3 with nested WordDocument if it were flat,
    # or test both child storage (entry 3 -> child 4) and duplicate stream name prevention:
    embedded_ole2[dir_off + 3 * 128 : dir_off + 4 * 128] = nested_wd
    extracted_with_embed = extract_upload("with_embed.doc", bytes(embedded_ole2), max_chars=300_000)
    assert "实验室管理制度" in extracted_with_embed.text

    # 4. Crafted OLE2 .doc with duplicate FAT sector references in DIFAT is rejected
    crafted_ole2 = bytearray(ole2_bytes)
    struct.pack_into("<I", crafted_ole2, 0x2C, 2)  # declare 2 FAT sectors
    first_fat_sec = struct.unpack_from("<I", crafted_ole2, 0x4C)[0]
    struct.pack_into("<I", crafted_ole2, 0x50, first_fat_sec)  # duplicate FAT sector in DIFAT[1]
    with pytest.raises(DocxValidationError) as exc_fat:
        validate_and_load_docx("bomb.doc", bytes(crafted_ole2))
    assert exc_fat.value.status == 415

    # 5. Legacy .doc with foundation markers split across separate rows extracts all sections
    split_foundation_doc = build_ole2_doc_bytes(
        tables=[
            [
                ["[学术简历]"],
                ["张三，天津工业大学教授，长期从事智能评测研究。"],
                ["[前期成果]"],
                ["（1）张三. 智能评测方法研究[J]. 计算机学报, 2025."],
                ["[承担项目]"],
                ["1. 国家自然科学基金面上项目，项目名称：智能评测关键技术，编号：62570001，2025-01至2028-12，50万元，在研，主持。"],
            ]
        ]
    )
    parsed_split = validate_and_load_docx("split-foundation.doc", split_foundation_doc)
    split_grids = [extract_table_grid(t) for t in iter_document_tables(parsed_split)]
    bio, achs, projs, _, _, t4_warns = _extract_t4_foundation(split_grids, as_of_date=None)
    assert t4_warns == []
    assert bio is not None and "智能评测研究" in bio
    assert len(achs) == 1
    assert len(projs) == 1
    complete_cell = (
        "[学术简历]\n张三，教授。\n"
        "[前期成果]\n（1）张三. 论文[J]. 学报, 2025.\n"
        "[承担项目]\n1. 基金项目，2025-01至2028-12，50万元，在研，主持。\n"
        "[与本项目关系]\n紧密相关。"
    )
    _, _, _, rel_note, _, _ = _extract_t4_foundation(
        [[[complete_cell], ["填写说明：请勿填写此栏。"]]],
        as_of_date=None,
    )
    assert rel_note == "紧密相关。"

    # 6. read_corpus_dir discovers .doc and .docx alongside .md
    (tmp_path / "note.md").write_text("# Note\n\nMarkdown body.", encoding="utf-8")
    (tmp_path / "legacy.doc").write_bytes(ole2_bytes)
    (tmp_path / "modern.docx").write_bytes(docx_bytes)
    corpus_items = read_corpus_dir(tmp_path)
    loaded_keys = {meta["key"] for meta, _ in corpus_items}
    assert loaded_keys == {"note", "legacy", "modern"}

    # 7. defusedxml blocks DTD/XXE in Word 2003 XML .doc and lxml.html handles headings/del/inline-block mixes
    xxe_wordml = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml">'
        b"<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:wordDocument>"
    )
    with pytest.raises(DocxValidationError) as exc_xxe_xml:
        validate_and_load_docx("xxe.doc", xxe_wordml)
    assert exc_xxe_xml.value.status == 415

    html_mixed = (
        "<html><head><title>Ignored</title><script>evil()</script></head>"
        "<body><h2>章节标题</h2><div><b>前言</b>说明<p>正文<del>删除内容</del>保留内容<br>第二行</p>尾部说明</div>"
        "<table><tr><td>第一段<div>第二段</div></td></tr></table></body></html>"
    ).encode()
    extracted_html_mixed = extract_upload("mixed.doc", html_mixed, max_chars=300_000)
    assert "## 章节标题" in extracted_html_mixed.text
    assert "前言说明" in extracted_html_mixed.text
    assert "正文保留内容\n第二行" in extracted_html_mixed.text
    assert "删除内容" not in extracted_html_mixed.text
    assert "尾部说明" in extracted_html_mixed.text
    parsed_html_mixed = validate_and_load_docx("mixed.doc", html_mixed)
    assert extract_table_grid(iter_document_tables(parsed_html_mixed)[0]) == [["第一段\n第二段"]]

    # 8. RTF edge cases: escaped backslashes before 'bin64', nested groups inside \v/\deleted, and CJK font under \ansicpg1252
    rtf_edge = (
        rb"{\rtf1\ansi\ansicpg1252\deff1{\fonttbl{\f0\fnil Arial;}{\f1\fcharset128 MS Gothic;}}"
        rb"{\f0 Path C:\\bin64\\tool "
        rb"before {\v secret {\b nested} still secret} after "
        rb"{\deleted old {\i inner} gone}} "
        rb"\'83e\'81@\'83X\'81[\par}"
    )
    extracted_rtf_edge = extract_upload("edge.doc", rtf_edge, max_chars=300_000)
    assert r"Path C:\bin64\tool" in extracted_rtf_edge.text
    assert "before after" in extracted_rtf_edge.text
    assert "secret" not in extracted_rtf_edge.text
    assert "nested" not in extracted_rtf_edge.text
    assert "gone" not in extracted_rtf_edge.text
    assert "テ スー" in extracted_rtf_edge.text


async def test_worker_processes_pending_and_retries_to_failed() -> None:
    user = await _make_user()
    settings = get_settings()
    maker = get_sessionmaker()

    async with maker() as session:
        doc_ok = await create_document(
            session,
            owner_id=user.id,
            title="正常入库文档",
            text="说明\n\n## 规模\n\n共有 8 台节点。",
            source_format="markdown",
        )
        await session.commit()
        ok_id = doc_ok.id

    processed = await process_next_pending(HashingEmbedder(512), settings)
    assert processed == ok_id

    async with maker() as session:
        refreshed = await session.get(Document, ok_id)
        assert refreshed is not None
        assert refreshed.status == "ready"
        assert refreshed.attempts == 1
        assert refreshed.error_code is None
        audit_row = await session.execute(
            text(
                "SELECT detail FROM audit_events WHERE action = 'document_indexed' AND resource_id = :rid"
            ),
            {"rid": str(ok_id)},
        )
        detail = audit_row.scalar_one()
        assert detail["chunks"] >= 1
        assert "ms" in detail

    # Now test failing embedder: 3 attempts -> pending, pending, failed
    class FailingEmbedder:
        name = "failing"
        kind = "hashing"
        dim = 512

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError("timeout")

        async def embed_query(self, text: str) -> list[float]:
            raise EmbeddingError("timeout")

        async def aclose(self) -> None:
            return None

    async with maker() as session:
        doc_fail = await create_document(
            session,
            owner_id=user.id,
            title="会失败的文档",
            text="这是一段纯文本内容，用于测试重试三次后标记为失败。",
            source_format="text",
        )
        await session.commit()
        fail_id = doc_fail.id

    failing_embedder = FailingEmbedder()
    for expected_attempt, expected_status in [(1, "pending"), (2, "pending"), (3, "failed")]:
        claimed = await process_next_pending(failing_embedder, settings)
        assert claimed == fail_id
        async with maker() as session:
            row = await session.get(Document, fail_id)
            assert row is not None
            assert row.attempts == expected_attempt
            assert row.status == expected_status
            assert row.error_code == "timeout"

    # 4th call finds nothing pending
    assert await process_next_pending(failing_embedder, settings) is None

    async with maker() as session:
        failed_audits = list(
            await session.execute(
                text(
                    "SELECT detail FROM audit_events WHERE action = 'document_index_failed' AND resource_id = :rid ORDER BY id"
                ),
                {"rid": str(fail_id)},
            )
        )
        assert len(failed_audits) == 3
        assert failed_audits[-1][0]["error"] == "timeout"
        assert failed_audits[-1][0]["attempts"] == 3


async def test_recover_processing_resets_to_pending() -> None:
    user = await _make_user()
    maker = get_sessionmaker()
    async with maker() as session:
        doc = await create_document(
            session,
            owner_id=user.id,
            title="卡住的文档",
            text="说明\n\n## 一\n\n内容。",
            source_format="markdown",
        )
        doc.status = "processing"
        await session.commit()
        doc_id = doc.id

    reset_count = await recover_processing()
    assert reset_count == 1

    async with maker() as session:
        refreshed = await session.get(Document, doc_id)
        assert refreshed is not None
        assert refreshed.status == "pending"


async def test_upsert_source_preserves_id_and_skips_reembedding_when_unchanged() -> None:
    user = await _make_user()
    maker = get_sessionmaker()

    class CountingEmbedder(HashingEmbedder):
        def __init__(self) -> None:
            super().__init__(dim=512)
            self.doc_calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.doc_calls += 1
            return await super().embed_documents(texts)

    embedder = CountingEmbedder()
    records = [
        (
            {"key": "doc-alpha", "title": "甲文档", "collection": "knowledge", "visibility": "lab"},
            "概述\n\n## 第一节\n\n甲文档的初始内容。",
        ),
        (
            {"key": "doc-beta", "title": "乙文档", "collection": "project", "visibility": "private"},
            "概述\n\n## 第一节\n\n乙文档的初始内容。",
        ),
    ]

    async with maker() as session:
        stats1 = await upsert_source(
            session,
            embedder=embedder,
            owner_id=user.id,
            source="seed",
            records=records,
        )
        await session.commit()
        assert stats1.created == 2
        assert stats1.unchanged == 0
        first_calls = embedder.doc_calls
        assert first_calls == 2

        docs_first = {
            d.doc_key: d.id
            for d in await session.scalars(select(Document).where(Document.source == "seed"))
        }

    # Run upsert_source a second time with identical content -> IDs preserved, 0 new embed calls
    async with maker() as session:
        stats2 = await upsert_source(
            session,
            embedder=embedder,
            owner_id=user.id,
            source="seed",
            records=records,
        )
        await session.commit()
        assert stats2.created == 0
        assert stats2.updated == 0
        assert stats2.unchanged == 2
        assert embedder.doc_calls == first_calls

        docs_second = {
            d.doc_key: d.id
            for d in await session.scalars(select(Document).where(Document.source == "seed"))
        }
        assert docs_second == docs_first

    # Modify doc-alpha and remove doc-beta -> doc-alpha keeps ID and re-embeds, doc-beta is deleted
    updated_records = [
        (
            {"key": "doc-alpha", "title": "甲文档（修订）", "collection": "knowledge", "visibility": "lab"},
            "概述\n\n## 第一节\n\n甲文档修改后的新内容。",
        ),
    ]
    async with maker() as session:
        stats3 = await upsert_source(
            session,
            embedder=embedder,
            owner_id=user.id,
            source="seed",
            records=updated_records,
        )
        await session.commit()
        assert stats3.updated == 1
        assert stats3.deleted == 1
        assert embedder.doc_calls == first_calls + 1

        remaining = list(await session.scalars(select(Document).where(Document.source == "seed")))
        assert len(remaining) == 1
        assert remaining[0].id == docs_first["doc-alpha"]
        assert remaining[0].title == "甲文档（修订）"
