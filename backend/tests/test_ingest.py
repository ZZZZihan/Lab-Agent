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
