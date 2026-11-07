"""End-to-end Web Proxy + ASGI verification for S10–S14 (Spec v2.2 & Phase 1.5/2)."""

from __future__ import annotations

import csv
import io
import json
import time
import uuid
import zipfile
from datetime import date

import docx
import fakeredis.aioredis
import pytest
from app.db import get_engine, get_sessionmaker
from app.importer.pii import assert_no_pii
from app.ingest_worker import process_next_pending
from app.models import LabPerson
from app.seed import seed
from app.smoke import run_smoke
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from tests.lab_v22_helpers import (
    build_proposal_docx_with_10_signatures,
    make_png_bytes,
    make_webp_vp8_bytes,
)

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PW = "LabAgent-Demo-2026-Admin"
INITIAL_PW = "TJPU-ColdStart-2026#Init"
ORIGIN = "http://127.0.0.1:43123"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

FORWARDED_RESPONSE_HEADERS = (
    "content-disposition",
    "etag",
    "x-signature-count-included",
    "x-signature-count-missing",
    "x-export-warnings-count",
    "x-export-warnings",
    "x-content-type-options",
    "content-security-policy",
    "cache-control",
)


async def _drain_pending() -> None:
    from app.main import app

    while (
        await process_next_pending(app.state.embedder, app.state.settings)
        is not None
    ):
        pass


def _build_v22_web_proxy(upstream: AsyncClient) -> FastAPI:
    web_app = FastAPI()

    def _check_origin(req: Request) -> JSONResponse | None:
        if req.method in ("GET", "HEAD"):
            return None
        if req.headers.get("origin") != ORIGIN:
            return JSONResponse(
                {"error": {"code": "forbidden", "message": "跨源请求被拒绝"}},
                status_code=403,
            )
        return None

    @web_app.get("/api/v1/health")
    async def proxy_health() -> Response:
        r = await upstream.get("/api/v1/health")
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type="application/json",
        )

    @web_app.post("/api/session")
    async def post_session(req: Request) -> Response:
        denied = _check_origin(req)
        if denied is not None:
            return denied
        r = await upstream.post(
            "/api/v1/auth/login",
            content=await req.body(),
            headers={"content-type": "application/json"},
        )
        if r.status_code != 200:
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type="application/json",
            )
        data = r.json()
        user_obj = data["user"]
        resp = JSONResponse(
            {
                "user": user_obj,
                "must_change_password": bool(
                    user_obj.get("must_change_password", False)
                ),
            }
        )
        resp.set_cookie("la_session", data["access_token"], httponly=True, path="/")
        return resp

    @web_app.api_route(
        "/api/backend/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy_backend(path: str, req: Request) -> Response:
        denied = _check_origin(req)
        if denied is not None:
            return denied
        if req.method == "GET" and path == "documents":
            await _drain_pending()
        token = req.cookies.get("la_session")
        if not token:
            return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        for req_hdr in ("content-type", "if-match", "if-none-match", "content-length"):
            if req_hdr in req.headers:
                headers[req_hdr] = req.headers[req_hdr]
        body = None if req.method in ("GET", "HEAD") else await req.body()
        qs = f"?{req.url.query}" if req.url.query else ""
        r = await upstream.request(
            req.method,
            f"/api/v1/{path}{qs}",
            content=body,
            headers=headers,
        )
        resp_headers: dict[str, str] = {
            "Cache-Control": r.headers.get("cache-control", "no-cache"),
            "X-Accel-Buffering": "no",
        }
        for hdr in FORWARDED_RESPONSE_HEADERS:
            val = r.headers.get(hdr)
            if val is not None:
                resp_headers[hdr] = val
        return Response(
            content=b"" if r.status_code == 304 else r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
            headers=resp_headers,
        )

    return web_app


def _build_dated_proposal_docx(
    *,
    as_of_text: str,
    wan_specialty: str,
    wan_sig_seed: int,
    proj_title: str,
) -> bytes:
    doc = docx.Document()
    t_cover = doc.add_table(rows=4, cols=2)
    t_cover.cell(0, 0).text = "项目名称"
    t_cover.cell(0, 1).text = proj_title
    t_cover.cell(1, 0).text = "项目申请人"
    t_cover.cell(1, 1).text = "郭建民"
    t_cover.cell(2, 0).text = "所在单位"
    t_cover.cell(2, 1).text = "天津工业大学"
    t_cover.cell(3, 0).text = "填表日期"
    t_cover.cell(3, 1).text = as_of_text

    t_team = doc.add_table(rows=4, cols=8)
    t_team.cell(0, 0).text = "申请人姓名"
    t_team.cell(0, 1).text = "郭建民"
    t_team.cell(0, 2).text = "专业职称"
    t_team.cell(0, 3).text = "正高级实验师"
    t_team.cell(0, 4).text = "工作单位"
    t_team.cell(0, 5).text = "天津工业大学"
    t_team.cell(1, 0).text = "研究专长"
    t_team.cell(1, 1).text = "智能评测与大模型安全"
    t_team.cell(1, 2).text = "最后学位"
    t_team.cell(1, 3).text = "硕士"
    for idx, h in enumerate(
        ["项目组成员", "姓名", "证件号", "专业职务", "研究专长", "学位", "工作单位", "本人签字"]
    ):
        t_team.cell(2, idx).text = h
    t_team.cell(3, 0).text = "项目组成员"
    t_team.cell(3, 1).text = "万振凯"
    t_team.cell(3, 3).text = "教授"
    t_team.cell(3, 4).text = wan_specialty
    t_team.cell(3, 5).text = "博士"
    t_team.cell(3, 6).text = "天津工业大学"
    t_team.cell(3, 7).paragraphs[0].add_run().add_picture(
        io.BytesIO(make_png_bytes(64, 28, fill_byte=wan_sig_seed))
    )
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def test_live_smoke_s10_to_s14_via_web_proxy(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    await seed()
    app.state.settings.evidence_score_min = 0.2475
    app.state.settings.calibration_source = "env"
    limiter_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(app.state.limiter, "redis", limiter_redis)

    web_app = _build_v22_web_proxy(client)
    transport = ASGITransport(app=web_app)
    # S1–S9 baseline smoke suite via ASGI + Web Proxy
    smoke_code, smoke_table = await run_smoke(
        web_url=ORIGIN,
        api_url="http://127.0.0.1:43124",
        offline=True,
        transport=transport,
    )
    assert smoke_code == 0, smoke_table

    async with AsyncClient(
        transport=transport,
        base_url=ORIGIN,
    ) as web_client:
        t_s10 = time.perf_counter()
        login_res = await web_client.post(
            "/api/session",
            headers={"Origin": ORIGIN},
            json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
        )
        assert login_res.status_code == 200, login_res.text

        # ------------------------------------------------------------------
        # S10: Cold-Start .docx with 10 Signatures, skip_signature_ids & 3-in-1 Login
        # ------------------------------------------------------------------
        docx_s10 = build_proposal_docx_with_10_signatures(wrap_alternate_content=True)
        parse_res = await web_client.post(
            "/api/backend/admin/imports/proposal",
            headers={"Origin": ORIGIN},
            files={"file": ("proposal-s10.docx", docx_s10, DOCX_MIME)},
        )
        assert parse_res.status_code == 200, parse_res.text
        job_s10 = parse_res.json()
        job_s10_id = job_s10["job_id"]
        sigs_preview = job_s10["preview"]["signatures"]
        assert len(sigs_preview) == 10
        assert "_signature_blobs" not in parse_res.text

        wu_temp_id = next(
            s["person_temp_id"] for s in sigs_preview if s["person_name"] == "武越"
        )
        confirm_s10 = await web_client.post(
            f"/api/backend/admin/imports/{job_s10_id}/provision",
            headers={"Origin": ORIGIN},
            json={
                "initial_password": INITIAL_PW,
                "include_password": True,
                "skip_signature_ids": [wu_temp_id],
            },
        )
        assert confirm_s10.status_code == 200, confirm_s10.text
        conf_body = confirm_s10.json()
        assert conf_body["signature_result"]["saved"] == 9
        assert conf_body["signature_result"]["skipped"] == 1
        assert_no_pii(json.dumps(conf_body["stats"], ensure_ascii=False))

        job_detail = await web_client.get(f"/api/backend/admin/imports/{job_s10_id}")
        assert job_detail.status_code == 200
        assert "_signature_blobs" not in job_detail.text

        # Tolerant 3-in-1 login by unique Chinese display_name + /change-password
        async with AsyncClient(
            transport=ASGITransport(app=web_app),
            base_url=ORIGIN,
        ) as member_web:
            m_login = await member_web.post(
                "/api/session",
                headers={"Origin": ORIGIN},
                json={
                    "identifier": "万振凯",
                    "password": INITIAL_PW,
                },
            )
            assert m_login.status_code == 200, m_login.text
            assert m_login.json()["must_change_password"] is True

            chg_pw = await member_web.post(
                "/api/backend/auth/change-password",
                headers={"Origin": ORIGIN},
                json={
                    "old_password": INITIAL_PW,
                    "new_password": "NewStrong-2026-Pass!",
                },
            )
            assert chg_pw.status_code == 200, chg_pw.text
            assert chg_pw.json()["user"]["must_change_password"] is False

        # ------------------------------------------------------------------
        # S11: Phase 2 Temporal Merge, Manual Edit Protection & Out-of-Order Revert
        # ------------------------------------------------------------------
        s10_ms = (time.perf_counter() - t_s10) * 1000.0
        t_s11 = time.perf_counter()
        people_list = (await web_client.get("/api/backend/lab/people")).json()["people"]
        wzk = next(p for p in people_list if p["name"] == "万振凯")
        wzk_id = wzk["id"]
        sig_s1_sha = wzk["signature_sha256"]
        assert sig_s1_sha is not None

        # Job A (2024): merge with signature S1 (from S10 docx)
        docx_a = _build_dated_proposal_docx(
            as_of_text="2024年3月1日",
            wan_specialty="早期教育理论2024",
            wan_sig_seed=0x21,
            proj_title="2024阶段申报书",
        )
        job_a_id = (
            await web_client.post(
                "/api/backend/admin/imports/proposal",
                headers={"Origin": ORIGIN},
                files={"file": ("a-2024.docx", docx_a, DOCX_MIME)},
            )
        ).json()["job_id"]
        mp_a = (
            await web_client.post(
                f"/api/backend/admin/imports/{job_a_id}/merge-preview",
                headers={"Origin": ORIGIN},
            )
        ).json()
        assert mp_a["summary"]["time_regression"] >= 1
        merge_a = await web_client.post(
            f"/api/backend/admin/imports/{job_a_id}/merge",
            headers={"Origin": ORIGIN},
            json={"decisions": []},
        )
        assert merge_a.status_code == 200, merge_a.text
        sig_a_sha = next(
            p["signature_sha256"]
            for p in (await web_client.get("/api/backend/lab/people")).json()["people"]
            if p["id"] == wzk_id
        )

        # Job B (2025): upgrades signature to S2 (seed 0x42)
        docx_b = _build_dated_proposal_docx(
            as_of_text="2025年6月1日",
            wan_specialty="早期教育理论2025",
            wan_sig_seed=0x42,
            proj_title="2025阶段申报书",
        )
        job_b_id = (
            await web_client.post(
                "/api/backend/admin/imports/proposal",
                headers={"Origin": ORIGIN},
                files={"file": ("b-2025.docx", docx_b, DOCX_MIME)},
            )
        ).json()["job_id"]
        merge_b = await web_client.post(
            f"/api/backend/admin/imports/{job_b_id}/merge",
            headers={"Origin": ORIGIN},
            json={"decisions": []},
        )
        assert merge_b.status_code == 200, merge_b.text

        # Job C (2027): clockwise upgrade on specialty + re-writes signature S1 (seed 0x21)
        docx_c = _build_dated_proposal_docx(
            as_of_text="2027年6月1日",
            wan_specialty="教育理论/创新教学模式2027",
            wan_sig_seed=0x21,
            proj_title="2027阶段申报书",
        )
        job_c_id = (
            await web_client.post(
                "/api/backend/admin/imports/proposal",
                headers={"Origin": ORIGIN},
                files={"file": ("c-2027.docx", docx_c, DOCX_MIME)},
            )
        ).json()["job_id"]
        merge_c = await web_client.post(
            f"/api/backend/admin/imports/{job_c_id}/merge",
            headers={"Origin": ORIGIN},
            json={"decisions": []},
        )
        assert merge_c.status_code == 200, merge_c.text

        # Out-of-order revert of Job A (A(S1)->B(S2)->C(S1)): C's active signature S1 is preserved via prev_writer_job_id
        rev_a = await web_client.post(
            f"/api/backend/admin/imports/{job_a_id}/revert",
            headers={"Origin": ORIGIN},
        )
        assert rev_a.status_code == 200, rev_a.text
        wzk_after_rev = next(
            p
            for p in (await web_client.get("/api/backend/lab/people")).json()["people"]
            if p["id"] == wzk_id
        )
        assert wzk_after_rev["has_signature"] is True
        assert wzk_after_rev["signature_sha256"] == sig_a_sha

        # Manual edit protection (keep_manual) + field-level restore-imported
        patch_res = await web_client.patch(
            f"/api/backend/lab/people/{wzk_id}",
            headers={"Origin": ORIGIN},
            json={"research_specialty": "手工修订方向-大模型形式化验证"},
        )
        assert patch_res.status_code == 200, patch_res.text
        assert (
            patch_res.json()["person"]["provenance"]["research_specialty"]["source_kind"]
            == "manual"
        )

        restore_res = await web_client.post(
            f"/api/backend/lab/people/{wzk_id}/restore-imported",
            headers={"Origin": ORIGIN},
            json={"field": "research_specialty"},
        )
        assert restore_res.status_code == 200, restore_res.text
        assert (
            restore_res.json()["person"]["research_specialty"]
            == "教育理论/创新教学模式2027"
        )

        # ------------------------------------------------------------------
        # S12: Person Dossier (Attribution, Query Budget <= 4, Deferred Binary)
        # ------------------------------------------------------------------
        s11_ms = (time.perf_counter() - t_s11) * 1000.0
        t_s12 = time.perf_counter()
        whh = next(p for p in people_list if p["name"] == "王秋慧")
        whh_id = whh["id"]
        sql_statements: list[str] = []
        engine = get_engine()

        def _record_sql(
            _conn: object, _cursor: object, statement: str, *_args: object
        ) -> None:
            sql_statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _record_sql)
        try:
            dossier_res = await web_client.get(
                f"/api/backend/lab/people/{whh_id}/dossier"
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record_sql)

        assert dossier_res.status_code == 200, dossier_res.text
        dossier = dossier_res.json()
        dossier_sqls = [
            stmt
            for stmt in sql_statements
            if any(
                tbl in stmt
                for tbl in ("lab_people", "lab_projects", "lab_achievements")
            )
        ]
        assert len(dossier_sqls) <= 4
        assert all("signature_data" not in stmt for stmt in sql_statements)
        assert dossier["name_ambiguous"] is False
        assert any(p["relation_role"] == "pi" for p in dossier["projects"])
        assert any(
            a["relation_role"] == "first_author" for a in dossier["achievements"]
        )

        # Create homonym person ("王 秋 慧") -> verify name_ambiguous=True & match="ambiguous", then clean up
        maker = get_sessionmaker()
        async with maker() as session:
            dup_person = LabPerson(
                name="王 秋 慧",
                title="讲师",
                affiliation="天津工业大学",
                project_role="member",
                entity_fingerprint="f9" * 32,
                as_of_time=date(2026, 4, 26),
                source_import_id=uuid.UUID(job_s10_id),
            )
            session.add(dup_person)
            await session.commit()
            dup_person_id = dup_person.id
        ambig_dossier = (
            await web_client.get(f"/api/backend/lab/people/{whh_id}/dossier")
        ).json()
        assert ambig_dossier["name_ambiguous"] is True
        assert all(p["match"] == "ambiguous" for p in ambig_dossier["projects"])
        async with maker() as session:
            obj = await session.get(LabPerson, dup_person_id)
            if obj is not None:
                await session.delete(obj)
                await session.commit()

        # ------------------------------------------------------------------
        # S13: Electronic Signature CRUD, ETag/304, If-Match/412, 2.5MiB Cap & CSP
        # ------------------------------------------------------------------
        s12_ms = (time.perf_counter() - t_s12) * 1000.0
        t_s13 = time.perf_counter()
        get_sig1 = await web_client.get(f"/api/backend/lab/people/{whh_id}/signature")
        assert get_sig1.status_code == 200
        etag1 = get_sig1.headers.get("etag")
        assert etag1 is not None and etag1.startswith('"')
        assert get_sig1.headers.get("x-content-type-options") == "nosniff"
        assert (
            get_sig1.headers.get("content-security-policy")
            == "default-src 'none'; sandbox"
        )
        assert "attachment;" in (get_sig1.headers.get("content-disposition") or "")

        # Conditional GET with If-None-Match -> 304 Not Modified
        get_304 = await web_client.get(
            f"/api/backend/lab/people/{whh_id}/signature",
            headers={"If-None-Match": etag1},
        )
        assert get_304.status_code == 304
        assert get_304.content == b""
        assert get_304.headers.get("etag") == etag1

        # Stale If-Match on PUT -> 412 PRECONDITION_FAILED
        stale_etag = '"' + ("0" * 64) + '"'
        webp_bytes = make_webp_vp8_bytes(88, 44)
        put_412 = await web_client.put(
            f"/api/backend/lab/people/{whh_id}/signature",
            headers={"Origin": ORIGIN, "If-Match": stale_etag},
            files={"file": ("sig.webp", webp_bytes, "image/webp")},
        )
        assert put_412.status_code == 412
        assert put_412.json()["error"]["code"] == "PRECONDITION_FAILED"

        # Pre-parse > 2.5 MiB stream cap -> 413 SIGNATURE_TOO_LARGE
        oversized = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 2_650_000)
        put_413 = await web_client.put(
            f"/api/backend/lab/people/{whh_id}/signature",
            headers={"Origin": ORIGIN},
            files={"file": ("huge.png", oversized, "image/png")},
        )
        assert put_413.status_code == 413
        assert put_413.json()["error"]["code"] == "SIGNATURE_TOO_LARGE"

        # Matching If-Match on PUT -> 200 upload upgrade
        put_200 = await web_client.put(
            f"/api/backend/lab/people/{whh_id}/signature",
            headers={"Origin": ORIGIN, "If-Match": etag1},
            files={"file": ("sig.webp", webp_bytes, "image/webp")},
        )
        assert put_200.status_code == 200, put_200.text
        assert put_200.json()["signature_source"] == "upload"
        assert put_200.json()["signature_mime"] == "image/webp"

        # DELETE signature with If-Match on another member (杨光) -> 200
        yg = next(p for p in people_list if p["name"] == "杨光")
        yg_etag = f'"{yg["signature_sha256"]}"'
        del_412 = await web_client.delete(
            f"/api/backend/lab/people/{yg['id']}/signature",
            headers={"Origin": ORIGIN, "If-Match": stale_etag},
        )
        assert del_412.status_code == 412
        del_200 = await web_client.delete(
            f"/api/backend/lab/people/{yg['id']}/signature",
            headers={"Origin": ORIGIN, "If-Match": yg_etag},
        )
        assert del_200.status_code == 200
        assert del_200.json()["has_signature"] is False

        # ------------------------------------------------------------------
        # S14: Cross-Tab Reuse Basket (format/batch, signatures:zip, export:selected)
        # ------------------------------------------------------------------
        s13_ms = (time.perf_counter() - t_s13) * 1000.0
        t_s14 = time.perf_counter()
        fac_create = await web_client.post(
            "/api/backend/lab/facilities",
            headers={"Origin": ORIGIN},
            json={
                "name": "高性能AI推理集群",
                "spec": "8x GPU Server",
                "value_wan": 180.0,
            },
        )
        assert fac_create.status_code in (200, 201), fac_create.text
        projects = (await web_client.get("/api/backend/lab/projects")).json()[
            "projects"
        ]
        achievements = (await web_client.get("/api/backend/lab/achievements")).json()[
            "achievements"
        ]
        facilities = (await web_client.get("/api/backend/lab/facilities")).json()[
            "facilities"
        ]
        mixed_items = [
            {"entity": "person", "id": whh_id},
            {"entity": "project", "id": projects[0]["id"]},
            {"entity": "achievement", "id": achievements[0]["id"]},
            {"entity": "facility", "id": facilities[0]["id"]},
        ]

        batch_ok = await web_client.post(
            "/api/backend/lab/format/batch",
            headers={"Origin": ORIGIN},
            json={
                "items": mixed_items,
                "options": {
                    "style": "nsfc_standard",
                    "numbering": "decimal",
                    "group_by_entity": True,
                    "include_person_bio": True,
                    "include_support_note": True,
                    "include_relationship_note": True,
                    "include_signatures_annex": False,
                },
            },
        )
        assert batch_ok.status_code == 200, batch_ok.text
        assert batch_ok.json()["count"] == 4
        assert "王秋慧" in batch_ok.json()["combined_text"]

        # Missing item -> 404 NOT_FOUND with missing list
        missing_uuid = str(uuid.uuid4())
        batch_404 = await web_client.post(
            "/api/backend/lab/format/batch",
            headers={"Origin": ORIGIN},
            json={"items": [{"entity": "project", "id": missing_uuid}]},
        )
        assert batch_404.status_code == 404
        assert batch_404.json()["error"]["code"] == "NOT_FOUND"
        assert batch_404.json()["error"]["missing"] == [
            {"entity": "project", "id": missing_uuid}
        ]

        # Signatures ZIP download via web proxy
        wu_yue = next(p for p in people_list if p["name"] == "武越")
        zip_res = await web_client.post(
            "/api/backend/lab/people/signatures:zip",
            headers={"Origin": ORIGIN},
            json={
                "person_ids": [whh_id, wzk_id, wu_yue["id"]],
                "filename_template": "seq_name",
            },
        )
        assert zip_res.status_code == 200
        assert zip_res.headers.get("x-signature-count-included") == "2"
        assert zip_res.headers.get("x-signature-count-missing") == "1"
        with zipfile.ZipFile(io.BytesIO(zip_res.content), "r") as zf:
            names = zf.namelist()
            assert "manifest.csv" in names
            manifest_raw = zf.read("manifest.csv").decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(manifest_raw)))
            assert [r["status"] for r in rows] == ["included", "included", "missing"]

        # Selected .docx export with embedded signatures via web proxy
        docx_res = await web_client.post(
            "/api/backend/lab/export:selected",
            headers={"Origin": ORIGIN},
            json={
                "title": "S14复用篮导出验证",
                "items": mixed_items
                + [
                    {"entity": "person", "id": wzk_id},
                    {"entity": "person", "id": wu_yue["id"]},
                ],
                "options": {
                    "style": "nsfc_standard",
                    "numbering": "bracket",
                    "group_by_entity": True,
                    "include_person_bio": True,
                    "include_support_note": True,
                    "include_relationship_note": True,
                    "include_signatures_annex": True,
                },
            },
        )
        assert docx_res.status_code == 200
        assert docx_res.headers.get("x-export-warnings-count") == "1"
        with zipfile.ZipFile(io.BytesIO(docx_res.content), "r") as dzf:
            media_entries = [n for n in dzf.namelist() if n.startswith("word/media/")]
            assert len(media_entries) >= 1
        exported_doc = docx.Document(io.BytesIO(docx_res.content))
        assert len(exported_doc.inline_shapes) == 1
        assert exported_doc.inline_shapes[0].width <= docx.shared.Inches(1.51)
        assert exported_doc.inline_shapes[0].height <= docx.shared.Inches(0.46)
        s14_ms = (time.perf_counter() - t_s14) * 1000.0
        print("\n=== S1-S9 SMOKE TABLE ===")
        print(smoke_table)
        print("=== S10-S14 WEB PROXY + ASGI TIMINGS ===")
        print(f"S10 coldstart+10sig : PASS ({s10_ms:.1f} ms) - saved=9, skipped=1, 3in1_login+chg_pw=ok")
        print(f"S11 temporal+revert : PASS ({s11_ms:.1f} ms) - 2024->2025->2027 merge + revert(A) + restore-imported=ok")
        print(f"S12 person dossier  : PASS ({s12_ms:.1f} ms) - sql_queries={len(dossier_sqls)}<=4, defer_sig=True, ambig=True")
        print(f"S13 signature CRUD  : PASS ({s13_ms:.1f} ms) - GET=200(nosniff+CSP), 304, 412, 413, PUT=200, DELETE=200")
        print(f"S14 reuse basket    : PASS ({s14_ms:.1f} ms) - batch=200(404 missing), zip_inc=2/miss=1, docx_warn=1")
