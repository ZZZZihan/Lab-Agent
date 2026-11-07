"""API routes for Lab Batch Formatting, Markdown/DOCX Export, and Reuse Basket (/api/v1/lab)."""

from __future__ import annotations

import io
from datetime import date
from typing import Annotated

import docx
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.basket import (
    BasketError,
    build_rfc5987_content_disposition,
    build_selected_docx,
    fetch_and_format_batch_items,
    iter_bytes_chunks,
    prepare_signatures_zip,
    sanitize_filename_part,
)
from app.importer.formatters import (
    format_achievement_application,
    format_facility_application,
    format_person_application,
    format_project_application,
    render_lab_profile_markdown,
)
from app.importer.pii import assert_no_pii
from app.models import (
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
)
from app.routes import APIError, _actor, _client_ip, _settings, _user
from app.routes_lab_schemas import (
    BatchFormatBody,
    BatchFormatOptions,
    ExportSelectedBody,
    SignaturesZipBody,
    _lab_v22_error,
)

router = APIRouter()


@router.post("/lab/format/batch", response_model=None)
async def batch_format_lab_assets(
    body: BatchFormatBody,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    await _user(session, actor)

    # v2.2 Basket items + options mode
    if body.items is not None:
        opts = body.options or BatchFormatOptions()
        raw_items = [{"entity": it.entity, "id": it.id} for it in body.items]
        try:
            res = await fetch_and_format_batch_items(
                session,
                raw_items=raw_items,
                style=opts.style,
                numbering=opts.numbering,
                group_by_entity=opts.group_by_entity,
                include_person_bio=opts.include_person_bio,
                include_support_note=opts.include_support_note,
                include_relationship_note=opts.include_relationship_note,
            )
        except BasketError as exc:
            return _lab_v22_error(
                exc.status,
                exc.code,
                exc.message,
                missing=exc.missing,
            )
        combined = str(res["combined_text"] or "")
        assert_no_pii(combined)
        items_out: list[dict[str, object]] = res["items"]  # type: ignore[assignment]
        return {
            "items": items_out,
            "combined_text": combined,
            "text": combined,
            "count": len(items_out),
        }

    if body.tab is None:
        return _lab_v22_error(
            400,
            "BATCH_LIMIT_EXCEEDED",
            "请提供 items 或 tab 参数",
        )

    id_filter = set(body.ids) if body.ids is not None else None
    raw_lines: list[str] = []

    if body.tab == "people":
        people = list(
            await session.scalars(
                select(LabPerson).order_by(
                    LabPerson.project_role.asc(), LabPerson.created_at.asc()
                )
            )
        )
        if id_filter is not None:
            people = [p for p in people if p.id in id_filter]
        raw_lines = [
            format_person_application(p, include_bio=body.include_bio) for p in people
        ]
    elif body.tab == "projects":
        projects = list(
            await session.scalars(
                select(LabProject).order_by(LabProject.created_at.asc())
            )
        )
        ref_dates = [p.as_of_time for p in projects if p.as_of_time is not None]
        ref_year = max(ref_dates).year if ref_dates else date.today().year
        if id_filter is not None:
            projects = [p for p in projects if p.id in id_filter]
        if body.only_ongoing:
            projects = [p for p in projects if p.status == "ongoing"]
        if body.recent_years is not None and body.recent_years > 0:
            cutoff_year = ref_year - body.recent_years
            projects = [
                p
                for p in projects
                if p.start_date is not None and p.start_date.year >= cutoff_year
            ]
        raw_lines = [
            format_project_application(p, include_relationship=body.include_relationship)
            for p in projects
        ]
    elif body.tab == "achievements":
        achievements = list(
            await session.scalars(
                select(LabAchievement).order_by(LabAchievement.created_at.asc())
            )
        )
        ref_dates = [a.as_of_time for a in achievements if a.as_of_time is not None]
        ref_year = max(ref_dates).year if ref_dates else date.today().year
        if id_filter is not None:
            achievements = [a for a in achievements if a.id in id_filter]
        if body.recent_years is not None and body.recent_years > 0:
            cutoff_year = ref_year - body.recent_years
            achievements = [
                a
                for a in achievements
                if a.year is not None and a.year >= cutoff_year
            ]
        raw_lines = [
            format_achievement_application(a, include_support=body.with_support)
            for a in achievements
        ]
    elif body.tab == "facilities":
        facilities = list(
            await session.scalars(
                select(LabFacility).order_by(LabFacility.created_at.asc())
            )
        )
        if id_filter is not None:
            facilities = [f for f in facilities if f.id in id_filter]
        raw_lines = [format_facility_application(f) for f in facilities]

    if body.numbered:
        items = [f"[{idx}] {line}" for idx, line in enumerate(raw_lines, start=1)]
    else:
        items = list(raw_lines)
    text_out = "\n".join(items)
    assert_no_pii(text_out)
    return {
        "tab": body.tab,
        "count": len(items),
        "items": items,
        "text": text_out,
    }


@router.get("/lab/export")
async def export_lab_assets(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    format: str = Query(default="md"),
) -> Response:
    await _user(session, actor)
    fmt = format.strip().lower()
    if fmt not in ("md", "docx"):
        raise APIError(400, "invalid_format", "导出格式仅支持 md 或 docx")

    people = list(
        await session.scalars(
            select(LabPerson).order_by(
                LabPerson.project_role.asc(), LabPerson.created_at.asc()
            )
        )
    )
    projects = list(
        await session.scalars(select(LabProject).order_by(LabProject.created_at.asc()))
    )
    achievements = list(
        await session.scalars(
            select(LabAchievement).order_by(LabAchievement.created_at.asc())
        )
    )
    facilities = list(
        await session.scalars(
            select(LabFacility).order_by(LabFacility.created_at.asc())
        )
    )
    latest_job = await session.scalar(
        select(ImportJob)
        .where(ImportJob.status.in_(("provisioned", "merged")))
        .order_by(ImportJob.provisioned_at.desc(), ImportJob.created_at.desc())
        .limit(1)
    )

    as_of_dates = [
        d
        for d in (
            [p.as_of_time for p in people]
            + [j.as_of_time for j in projects]
            + [a.as_of_time for a in achievements]
            + [f.as_of_time for f in facilities]
        )
        if d is not None
    ]
    as_of_date = (
        (latest_job.as_of_time if latest_job and latest_job.as_of_time else None)
        or (max(as_of_dates) if as_of_dates else date.today())
    )
    project_title = "课题组资料库档案"
    rel_note: str | None = None
    if latest_job is not None:
        preview = dict(latest_job.preview or {})
        raw_cover = preview.get("cover")
        cover_dict = raw_cover if isinstance(raw_cover, dict) else {}
        project_title = (
            str(cover_dict.get("project_title") or "").strip()
            or latest_job.original_filename
            or project_title
        )
        rel_note = str(preview.get("relationship_note") or "").strip() or None

    md = render_lab_profile_markdown(
        project_title=project_title,
        as_of_time=as_of_date,
        people=people,
        projects=projects,
        achievements=achievements,
        facilities=facilities,
        relationship_note=rel_note,
    )
    assert_no_pii(md)

    if fmt == "md":
        return Response(
            content=md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="lab-assets.md"'},
        )

    doc = docx.Document()
    doc.add_heading(f"课题组资料库：{project_title}", level=0)
    doc.add_paragraph(
        f"基准时间：{as_of_date.isoformat()}。团队成员 {len(people)} 人，"
        f"承担项目 {len(projects)} 项，前期成果 {len(achievements)} 项，"
        f"设备与支撑条件 {len(facilities)} 项。"
    )

    doc.add_heading("一、团队成员", level=1)
    for idx, person in enumerate(people, start=1):
        doc.add_paragraph(f"[{idx}] {format_person_application(person, include_bio=True)}")

    doc.add_heading("二、承担项目", level=1)
    for idx, proj in enumerate(projects, start=1):
        doc.add_paragraph(
            f"[{idx}] {format_project_application(proj, include_relationship=True)}"
        )

    doc.add_heading("三、前期成果", level=1)
    for idx, ach in enumerate(achievements, start=1):
        doc.add_paragraph(
            f"[{idx}] {format_achievement_application(ach, include_support=True)}"
        )

    doc.add_heading("四、设备与支撑条件", level=1)
    for idx, fac in enumerate(facilities, start=1):
        doc.add_paragraph(f"[{idx}] {format_facility_application(fac)}")

    if rel_note:
        doc.add_heading("五、与已承担项目的关系", level=1)
        doc.add_paragraph(rel_note)

    all_doc_text = "\n".join(p.text for p in doc.paragraphs)
    assert_no_pii(all_doc_text)

    buf = io.BytesIO()
    doc.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="lab-assets.docx"'},
    )



@router.post("/lab/people/signatures:zip", response_model=None)
async def download_people_signatures_zip(
    body: SignaturesZipBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    user = await _user(session, actor)
    try:
        (
            zip_bytes,
            included_count,
            missing_count,
            included_signatures,
        ) = await prepare_signatures_zip(
            session,
            raw_person_ids=body.person_ids,
            filename_template=body.filename_template,
        )
    except BasketError as exc:
        return _lab_v22_error(exc.status, exc.code, exc.message)

    for sig_item in included_signatures:
        await write_audit(
            settings=_settings(request),
            actor_id=user.id,
            action="lab.signature.read",
            resource_type="lab_person",
            resource_id=sig_item["person_id"],
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={
                "person_id": sig_item["person_id"],
                "sha256": sig_item["sha256"],
                "channel": "zip",
            },
        )

    content_disp = build_rfc5987_content_disposition(
        "lab-signatures.zip",
        "项目组签名.zip",
    )
    headers = {
        "Content-Disposition": content_disp,
        "Cache-Control": "no-store",
        "X-Signature-Count-Included": str(included_count),
        "X-Signature-Count-Missing": str(missing_count),
    }
    return StreamingResponse(
        iter_bytes_chunks(zip_bytes),
        media_type="application/zip",
        headers=headers,
    )


@router.post("/lab/export:selected", response_model=None)
async def export_selected_basket_docx(
    body: ExportSelectedBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    user = await _user(session, actor)
    raw_items = [{"entity": it.entity, "id": it.id} for it in body.items]
    opts = body.options
    try:
        docx_bytes, warnings_count, embedded_signatures = await build_selected_docx(
            session,
            title=body.title,
            raw_items=raw_items,
            style=opts.style,
            numbering=opts.numbering,
            group_by_entity=opts.group_by_entity,
            include_person_bio=opts.include_person_bio,
            include_support_note=opts.include_support_note,
            include_relationship_note=opts.include_relationship_note,
            include_signatures_annex=opts.include_signatures_annex,
        )
    except BasketError as exc:
        return _lab_v22_error(
            exc.status,
            exc.code,
            exc.message,
            missing=exc.missing,
        )

    for sig_item in embedded_signatures:
        await write_audit(
            settings=_settings(request),
            actor_id=user.id,
            action="lab.signature.read",
            resource_type="lab_person",
            resource_id=sig_item["person_id"],
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={
                "person_id": sig_item["person_id"],
                "sha256": sig_item["sha256"],
                "channel": "docx_export",
            },
        )

    doc_title_safe = sanitize_filename_part(body.title or "课题申报素材汇编")
    content_disp = build_rfc5987_content_disposition(
        "proposal-materials.docx",
        f"{doc_title_safe}.docx",
    )
    headers = {
        "Content-Disposition": content_disp,
        "Cache-Control": "no-store",
        "X-Export-Warnings-Count": str(warnings_count),
    }
    return StreamingResponse(
        iter_bytes_chunks(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )

