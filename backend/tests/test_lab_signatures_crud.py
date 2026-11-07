from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import date
from pathlib import Path

import pytest
from app.db import get_engine, get_sessionmaker
from app.models import (
    ImportJob,
    LabPerson,
    User,
)
from httpx import AsyncClient
from sqlalchemy import event, select
from tests.lab_v22_helpers import (
    _seed_user_and_token,
    make_jpeg_bytes,
    make_png_bytes,
)

REAL_SAMPLE_DOCX = Path(
    "/home/carryz/.local/share/alma/herdr/uploads/20261106-054942-6cb60859/"
    "教育考试专属大模型构建与安全应用研究_申请书.docx"
)


# ---------------------------------------------------------------------------
# Pure-Python Synthetic Image & .docx Builders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_confirm_signature_merge_and_keep_upload(
    client: AsyncClient,
) -> None:
    admin_user, admin_headers = await _seed_user_and_token(
        role="admin", login_name="admin_sig_unlinked"
    )
    maker = get_sessionmaker()
    sig_b_bytes = make_png_bytes(92, 40, fill_byte=0x4A)
    sig_b_sha = hashlib.sha256(sig_b_bytes).hexdigest()
    sig_b_b64 = base64.b64encode(sig_b_bytes).decode("ascii")

    # 1) Unlinked person created by Import A (merge) without signature, then Import B (merge without field provenance)
    #    writes a signature; reverting A preserves the unlinked person and B's signature, and reverting B cleans up.
    async with maker() as session:
        job_a = ImportJob(
            uploaded_by=admin_user.id,
            original_filename="unlinked_a.docx",
            content_sha256="1" * 64,
            status="parsed",
            as_of_time=date(2026, 4, 20),
            preview={
                "as_of_time": "2026-04-20",
                "people": [
                    {
                        "key": "p_zhou",
                        "temp_id": "p_zhou",
                        "name": "周八",
                        "affiliation": "天津工业大学",
                        "project_role": "member",
                        "tier": "T2",
                    }
                ],
                "projects": [],
                "achievements": [],
                "facilities": [],
                "signatures": [],
                "warnings": [],
            },
        )
        job_b = ImportJob(
            uploaded_by=admin_user.id,
            original_filename="unlinked_b.docx",
            content_sha256="2" * 64,
            status="parsed",
            as_of_time=date(2026, 4, 21),
            preview={
                "as_of_time": "2026-04-21",
                "people": [
                    {
                        "key": "p_zhou",
                        "temp_id": "p_zhou",
                        "name": "周八",
                        "affiliation": "天津工业大学",
                        "project_role": "member",
                        "tier": "T2",
                        "login_name": "zhouba",
                    }
                ],
                "projects": [],
                "achievements": [],
                "facilities": [],
                "signatures": [
                    {
                        "person_temp_id": "p_zhou",
                        "person_name": "周八",
                        "sha256": sig_b_sha,
                        "mime": "image/png",
                        "width": 92,
                        "height": 40,
                        "byte_size": len(sig_b_bytes),
                        "confidence": "high",
                        "warnings": [],
                    }
                ],
                "_signature_blobs": {sig_b_sha: sig_b_b64},
                "warnings": [],
            },
        )
        session.add_all([job_a, job_b])
        await session.commit()
        job_a_id = str(job_a.id)
        job_b_id = str(job_b.id)

    r_merge_a = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a.status_code == 200, r_merge_a.text
    async with maker() as session:
        zhou_p = await session.scalar(select(LabPerson).where(LabPerson.name == "周八"))
        assert zhou_p is not None
        assert zhou_p.user_id is None
        assert zhou_p.signature_sha256 is None
        zhou_id = str(zhou_p.id)

    r_prov_b = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/provision",
        headers=admin_headers,
        json={},
    )
    assert r_prov_b.status_code == 200, r_prov_b.text
    async with maker() as session:
        zhou_after_b = await session.get(LabPerson, uuid.UUID(zhou_id))
        assert zhou_after_b is not None
        assert zhou_after_b.signature_sha256 == sig_b_sha
        assert zhou_after_b.signature_source == "docx_import"

    # Also test the pure unlinked + no-remaining-provenance path by detaching user_id & B's field provenance
    async with maker() as session:
        from app.models import FieldProvenance

        zhou_row = await session.get(LabPerson, uuid.UUID(zhou_id))
        assert zhou_row is not None
        zhou_row.user_id = None
        b_prov_rows = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.source_job_id == uuid.UUID(job_b_id)
                )
            )
        )
        for r in b_prov_rows:
            r.superseded = True
        await session.commit()

    r_rev_a = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_a.status_code == 200, r_rev_a.text
    async with maker() as session:
        zhou_after_rev_a = await session.get(LabPerson, uuid.UUID(zhou_id))
        assert zhou_after_rev_a is not None
        assert zhou_after_rev_a.signature_sha256 == sig_b_sha
        assert zhou_after_rev_a.signature_source == "docx_import"
        assert zhou_after_rev_a.source_import_id == uuid.UUID(job_b_id)
        # Re-attach user_id so reverting B leaves the person row with cleared signature
        linked_u = await session.scalar(select(User).where(User.login_name == "zhouba"))
        assert linked_u is not None
        zhou_after_rev_a.user_id = linked_u.id
        await session.commit()

    r_rev_b = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_b.status_code == 200, r_rev_b.text
    async with maker() as session:
        zhou_after_rev_b = await session.get(LabPerson, uuid.UUID(zhou_id))
        assert zhou_after_rev_b is not None
        assert zhou_after_rev_b.signature_sha256 is None
        assert zhou_after_rev_b.signature_source is None


# ---------------------------------------------------------------------------
# 5. Signature CRUD, ETag/If-Match, Security Headers & Deferred Loading (§5 & §8.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signature_crud_headers_concurrency_and_deferred_sql(
    client: AsyncClient,
) -> None:
    _, admin_headers = await _seed_user_and_token(role="admin", login_name="admin_sig")
    self_user, self_headers = await _seed_user_and_token(
        role="member", login_name="wan_self", display_name="万振凯"
    )
    _, other_member_headers = await _seed_user_and_token(
        role="member", login_name="other_mem", display_name="徐国伟"
    )
    _, viewer_headers = await _seed_user_and_token(
        role="viewer", login_name="viewer_u", display_name="访客"
    )

    maker = get_sessionmaker()
    async with maker() as session:
        job = ImportJob(
            uploaded_by=self_user.id,
            original_filename="seed.docx",
            content_sha256="c" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.flush()
        person = LabPerson(
            user_id=self_user.id,
            name="万振凯",
            title="教授",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="f" * 64,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add(person)
        await session.commit()
        person_id = str(person.id)

    # 1) GET before signature exists -> 404 SIGNATURE_NOT_FOUND; non-existent person -> 404 NOT_FOUND
    r_missing = await client.get(
        f"/api/v1/lab/people/{person_id}/signature", headers=viewer_headers
    )
    assert r_missing.status_code == 404
    assert r_missing.json()["detail"]["code"] == "SIGNATURE_NOT_FOUND"

    r_no_person = await client.get(
        f"/api/v1/lab/people/{uuid.uuid4()}/signature", headers=viewer_headers
    )
    assert r_no_person.status_code == 404
    assert r_no_person.json()["detail"]["code"] == "NOT_FOUND"

    # 2) Permission checks: other member and viewer get 403 on PUT/DELETE
    png1 = make_png_bytes(90, 40, fill_byte=0x44)
    for forbidden_hdr in (other_member_headers, viewer_headers):
        r_forb = await client.put(
            f"/api/v1/lab/people/{person_id}/signature",
            headers=forbidden_hdr,
            files={"file": ("sig.png", png1, "image/png")},
        )
        assert r_forb.status_code == 403
        r_del_forb = await client.delete(
            f"/api/v1/lab/people/{person_id}/signature",
            headers=forbidden_hdr,
        )
        assert r_del_forb.status_code == 403

    # 3) Self member uploads signature -> 200
    r_put1 = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=self_headers,
        files={"file": ("sig.png", png1, "image/png")},
    )
    assert r_put1.status_code == 200
    put1_data = r_put1.json()
    sha1 = put1_data["signature_sha256"]
    assert put1_data["has_signature"] is True
    assert put1_data["signature_source"] == "upload"
    assert put1_data["width"] == 90
    assert put1_data["height"] == 40

    # 4) GET signature stream -> verify bytes, ETag, Content-Disposition, nosniff, CSP, and 304 on If-None-Match
    r_get = await client.get(
        f"/api/v1/lab/people/{person_id}/signature", headers=viewer_headers
    )
    assert r_get.status_code == 200
    assert r_get.content == png1
    assert r_get.headers["content-type"] == "image/png"
    assert r_get.headers["etag"] == f'"{sha1}"'
    assert r_get.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r_get.headers["content-security-policy"]
    assert "filename*=UTF-8''" in r_get.headers["content-disposition"]

    r_304 = await client.get(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**viewer_headers, "If-None-Match": f'"{sha1}"'},
    )
    assert r_304.status_code == 304
    assert r_304.content == b""

    # 5) If-Match optimistic concurrency on PUT: wrong ETag -> 412, matching ETag -> 200
    jpg2 = make_jpeg_bytes(110, 48, seed=7)
    r_412 = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**admin_headers, "If-Match": '"0000000000000000"'},
        files={"file": ("sig.jpg", jpg2, "image/jpeg")},
    )
    assert r_412.status_code == 412
    assert r_412.json()["detail"]["code"] == "PRECONDITION_FAILED"

    r_put2 = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**admin_headers, "If-Match": f'"{sha1}"'},
        files={"file": ("sig.jpg", jpg2, "image/jpeg")},
    )
    assert r_put2.status_code == 200
    assert r_put2.json()["signature_mime"] == "image/jpeg"

    # 6) Upload validation errors: 415 SVG, 413 >2MiB, 422 corrupt, 422 bad dimensions
    r_svg = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("evil.png", b"<svg></svg>", "image/png")},
    )
    assert r_svg.status_code == 415
    assert r_svg.json()["detail"]["code"] == "SIGNATURE_REJECTED_FORMAT"

    r_big = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * 2_097_150, "image/png")},
    )
    assert r_big.status_code == 413
    assert r_big.json()["detail"]["code"] == "SIGNATURE_TOO_LARGE"

    r_over_multipart = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("huge.png", b"\x89PNG\r\n\x1a\n" + b"0" * 3_000_000, "image/png")},
    )
    assert r_over_multipart.status_code == 413
    assert r_over_multipart.json()["detail"]["code"] == "SIGNATURE_TOO_LARGE"

    async def _chunked_oversized_stream():
        for _ in range(30):
            yield b"x" * 100_000

    r_over_stream = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={
            **admin_headers,
            "Content-Type": "multipart/form-data; boundary=---bound",
        },
        content=_chunked_oversized_stream(),
    )
    assert r_over_stream.status_code == 413
    assert r_over_stream.json()["detail"]["code"] == "SIGNATURE_TOO_LARGE"

    r_missing_file = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        data={"other_field": "no_file"},
    )
    assert r_missing_file.status_code == 422
    assert r_missing_file.json()["detail"]["code"] == "SIGNATURE_FILE_MISSING"

    r_corrupt = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("bad.png", b"\x89PNG\r\n\x1a\ntrun", "image/png")},
    )
    assert r_corrupt.status_code == 422
    assert r_corrupt.json()["detail"]["code"] == "SIGNATURE_CORRUPT"

    r_dim = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("tiny.png", make_png_bytes(4, 4), "image/png")},
    )
    assert r_dim.status_code == 422
    assert r_dim.json()["detail"]["code"] == "SIGNATURE_BAD_DIMENSIONS"

    # 7) Verify list_lab_people and get_person_dossier NEVER select signature_data in SQL
    executed_sqls: list[str] = []
    sync_engine = get_engine().sync_engine

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        executed_sqls.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        r_list = await client.get("/api/v1/lab/people", headers=viewer_headers)
        assert r_list.status_code == 200
        assert "signature_data" not in r_list.json()["people"][0]
        assert r_list.json()["people"][0]["has_signature"] is True

        r_dos = await client.get(
            f"/api/v1/lab/people/{person_id}/dossier", headers=viewer_headers
        )
        assert r_dos.status_code == 200
        assert "signature_data" not in r_dos.json()["person"]
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert len(executed_sqls) > 0
    for stmt in executed_sqls:
        assert "signature_data" not in stmt

    # 8) DELETE signature (If-Match mismatch -> 412; matching/omitted -> 200, idempotent), subsequent GET -> 404
    r_del_412 = await client.delete(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**self_headers, "If-Match": '"0000000000000000"'},
    )
    assert r_del_412.status_code == 412
    assert r_del_412.json()["detail"]["code"] == "PRECONDITION_FAILED"

    r_del1 = await client.delete(
        f"/api/v1/lab/people/{person_id}/signature", headers=self_headers
    )
    assert r_del1.status_code == 200
    assert r_del1.json()["has_signature"] is False

    r_del2 = await client.delete(
        f"/api/v1/lab/people/{person_id}/signature", headers=self_headers
    )
    assert r_del2.status_code == 200

    r_after_del = await client.get(
        f"/api/v1/lab/people/{person_id}/signature", headers=viewer_headers
    )
    assert r_after_del.status_code == 404


# ---------------------------------------------------------------------------
# 6. Person Dossier Attribution & Query Budget (§4 & §8.2 test_dossier)
# ---------------------------------------------------------------------------


