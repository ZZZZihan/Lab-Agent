"""API routes for Lab Person Dossier and Electronic Signatures (/api/v1/lab/people)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from starlette.exceptions import HTTPException

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.basket import (
    MIME_TO_EXT,
    build_rfc5987_content_disposition,
    iter_bytes_chunks,
    sanitize_filename_part,
)
from app.importer.matching import (
    build_ilike_patterns,
    classify_achievement_for_person,
    classify_project_for_person,
)
from app.importer.matching import (
    normalize_person_name as normalize_dossier_name,
)
from app.importer.signature import validate_signature_image
from app.models import (
    LabAchievement,
    LabPerson,
    LabProject,
)
from app.routes import _actor, _client_ip, _settings, _user
from app.routes_lab_schemas import (
    _lab_v22_error,
    _serialize_achievement,
    _serialize_person,
    _serialize_project,
)

router = APIRouter()


def _parse_person_uuid(raw_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw_id).strip())
    except ValueError:
        return None


def _serialize_dossier_project(
    project: LabProject, relation_role: str, match_kind: str = "exact"
) -> dict[str, object]:
    base = _serialize_project(project, provenance={})
    copy_txt = str(base.get("application_text") or "")
    return {
        **base,
        "code": project.grant_no,
        "source": project.funder,
        "relation_role": relation_role,
        "match": match_kind,
        "copy_text": copy_txt,
    }


def _serialize_dossier_achievement(
    ach: LabAchievement,
    relation_role: str,
    author_position: int,
    match_kind: str = "exact",
) -> dict[str, object]:
    base = _serialize_achievement(ach, provenance={})
    copy_txt = str(base.get("application_text") or "")
    return {
        **base,
        "type": ach.kind,
        "authors": ach.authors_text or ach.first_author,
        "relation_role": relation_role,
        "author_position": author_position,
        "match": match_kind,
        "copy_text": copy_txt,
    }


@router.get("/lab/people/{person_id}/dossier", response_model=None)
async def get_person_dossier(
    person_id: str,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    person = await session.get(LabPerson, uid)
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    can_edit = bool(
        actor.role == "admin"
        or (person.user_id is not None and person.user_id == actor.id)
    )
    target_norm = normalize_dossier_name(person.name)
    ilike_pats = build_ilike_patterns(person.name)

    same_name_rows = list(
        await session.execute(
            select(LabPerson.id, LabPerson.name).where(
                or_(*[LabPerson.name.ilike(p, escape="\\") for p in ilike_pats])
            )
        )
    )
    same_name_count = sum(
        1
        for _, cand_name in same_name_rows
        if normalize_dossier_name(cand_name) == target_norm
    )
    name_ambiguous = same_name_count >= 2
    match_kind = "ambiguous" if name_ambiguous else "exact"

    proj_stmt = select(LabProject).where(
        or_(
            *[
                col.ilike(p, escape="\\")
                for p in ilike_pats
                for col in (
                    LabProject.pi_name,
                    LabProject.raw_text,
                    LabProject.relationship_note,
                )
            ]
        )
    )
    cand_projects = list(await session.scalars(proj_stmt))

    matched_proj_tuples: list[tuple[LabProject, str]] = []
    for proj in cand_projects:
        rel_role = classify_project_for_person(proj, target_norm)
        if rel_role is not None:
            matched_proj_tuples.append((proj, rel_role))

    matched_proj_tuples.sort(
        key=lambda item: (
            0 if item[1] == "pi" else 1,
            0 if item[0].start_date is not None else 1,
            -(item[0].start_date.toordinal() if item[0].start_date is not None else 0),
            -item[0].id.int,
        )
    )
    projects_total = len(matched_proj_tuples)
    pi_count = sum(1 for _, rl in matched_proj_tuples if rl == "pi")
    part_count = sum(1 for _, rl in matched_proj_tuples if rl == "participant")
    projects_truncated = projects_total > 100
    matched_proj_tuples = matched_proj_tuples[:100]
    serialized_projects = [
        _serialize_dossier_project(proj, rel_role, match_kind)
        for proj, rel_role in matched_proj_tuples
    ]

    ach_stmt = select(LabAchievement).where(
        or_(
            *[
                col.ilike(p, escape="\\")
                for p in ilike_pats
                for col in (
                    LabAchievement.first_author,
                    LabAchievement.authors_text,
                    LabAchievement.raw_text,
                )
            ]
        )
    )
    cand_achievements = list(await session.scalars(ach_stmt))

    matched_ach_tuples: list[tuple[LabAchievement, str, int]] = []
    for ach in cand_achievements:
        classified = classify_achievement_for_person(ach, target_norm)
        if classified is not None:
            matched_ach_tuples.append((ach, classified[0], classified[1]))

    matched_ach_tuples.sort(
        key=lambda item: (
            0 if item[0].year is not None else 1,
            -(item[0].year if item[0].year is not None else 0),
            0 if item[1] == "first_author" else 1,
            item[2],
            -item[0].id.int,
        )
    )
    outputs_total = len(matched_ach_tuples)
    first_count = sum(1 for _, rl, _ in matched_ach_tuples if rl == "first_author")
    co_count = sum(1 for _, rl, _ in matched_ach_tuples if rl == "coauthor")
    outputs_truncated = outputs_total > 200
    matched_ach_tuples = matched_ach_tuples[:200]
    serialized_outputs = [
        _serialize_dossier_achievement(ach, rel_role, pos, match_kind)
        for ach, rel_role, pos in matched_ach_tuples
    ]

    person_payload = _serialize_person(person, provenance={})
    person_payload["can_edit"] = can_edit
    person_payload["name_ambiguous"] = name_ambiguous

    return {
        "person": person_payload,
        "can_edit": can_edit,
        "name_ambiguous": name_ambiguous,
        "counts": {
            "projects_total": projects_total,
            "projects_as_pi": pi_count,
            "projects_as_participant": part_count,
            "outputs_total": outputs_total,
            "outputs_first_author": first_count,
            "outputs_coauthor": co_count,
            "achievements_total": outputs_total,
            "achievements_first_author": first_count,
            "achievements_coauthor": co_count,
        },
        "projects": serialized_projects,
        "outputs": serialized_outputs,
        "achievements": serialized_outputs,
        "truncated": {
            "projects": projects_truncated,
            "outputs": outputs_truncated,
            "achievements": outputs_truncated,
        },
    }


def _etag_matches(header_val: str | None, sha256_hex: str) -> bool:
    if not header_val:
        return False
    target = sha256_hex.lower()
    for raw_tag in header_val.split(","):
        tag = raw_tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag.strip('"').lower() == target:
            return True
    return False


@router.get("/lab/people/{person_id}/signature", response_model=None)
async def download_person_signature(
    person_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    user = await _user(session, actor)
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    person = await session.scalar(
        select(LabPerson)
        .options(undefer(LabPerson.signature_data))
        .where(LabPerson.id == uid)
    )
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    if (
        person.signature_data is None
        or not person.signature_sha256
        or not person.signature_mime
    ):
        return _lab_v22_error(404, "SIGNATURE_NOT_FOUND", "该成员尚未录入电子签名")

    etag = f'"{person.signature_sha256}"'
    if _etag_matches(request.headers.get("if-none-match"), person.signature_sha256):
        await write_audit(
            settings=_settings(request),
            actor_id=user.id,
            action="lab.signature.read",
            resource_type="lab_person",
            resource_id=str(person.id),
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={
                "person_id": str(person.id),
                "sha256": person.signature_sha256,
                "channel": "single",
            },
        )
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=0, must-revalidate",
            },
        )

    sig_bytes = bytes(person.signature_data)
    check = validate_signature_image(sig_bytes)
    if not check.ok or check.mime is None:
        return _lab_v22_error(
            500,
            "SIGNATURE_CORRUPT_IN_STORAGE",
            "存储中的电子签名数据异常",
        )

    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab.signature.read",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "person_id": str(person.id),
            "sha256": check.sha256,
            "channel": "single",
        },
    )

    ext = MIME_TO_EXT.get(check.mime, "bin")
    safe_name = sanitize_filename_part(person.name)
    content_disp = build_rfc5987_content_disposition(
        f"signature-{person.id}.{ext}",
        f"{safe_name}_签名.{ext}",
    )
    headers = {
        "Content-Type": check.mime,
        "Content-Length": str(len(sig_bytes)),
        "Content-Disposition": content_disp,
        "ETag": etag,
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    return StreamingResponse(
        iter_bytes_chunks(sig_bytes),
        media_type=check.mime,
        headers=headers,
    )


@router.put("/lab/people/{person_id}/signature", response_model=None)
async def upload_person_signature(
    person_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    user = await _user(session, actor)
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    person_pre = await session.scalar(
        select(LabPerson).where(LabPerson.id == uid)
    )
    if person_pre is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    if actor.role != "admin" and (
        person_pre.user_id is None or person_pre.user_id != user.id
    ):
        return _lab_v22_error(
            403,
            "FORBIDDEN",
            "仅管理员或本人可上传或替换电子签名",
        )

    if_match = request.headers.get("if-match")
    if if_match is not None and if_match.strip():
        pre_sha = person_pre.signature_sha256 or ""
        if not pre_sha or not _etag_matches(if_match, pre_sha):
            return _lab_v22_error(
                412,
                "PRECONDITION_FAILED",
                "电子签名已被其他请求更新，请刷新后重试",
            )

    max_multipart_bytes = 2_621_440  # 2.5 MiB (§4.3 Content-Length & stream cap)
    content_length_hdr = request.headers.get("content-length")
    if content_length_hdr is not None and content_length_hdr.strip():
        try:
            if int(content_length_hdr) > max_multipart_bytes:
                return _lab_v22_error(
                    413,
                    "SIGNATURE_TOO_LARGE",
                    "签名图片大小必须在 1 字节到 2 MiB 之间",
                )
        except ValueError:
            return _lab_v22_error(
                400,
                "INVALID_CONTENT_LENGTH",
                "非法的 Content-Length 请求头",
            )

    body_chunks: list[bytes] = []
    total_received = 0
    async for chunk in request.stream():
        total_received += len(chunk)
        if total_received > max_multipart_bytes:
            return _lab_v22_error(
                413,
                "SIGNATURE_TOO_LARGE",
                "签名图片大小必须在 1 字节到 2 MiB 之间",
            )
        body_chunks.append(chunk)
    request._body = b"".join(body_chunks)

    try:
        form = await request.form(
            max_files=1,
            max_fields=4,
            max_part_size=2_097_153,
        )
    except HTTPException as exc:
        if "exceeded maximum size" in str(exc.detail).lower() or exc.status_code == 413:
            return _lab_v22_error(
                413,
                "SIGNATURE_TOO_LARGE",
                "签名图片大小必须在 1 字节到 2 MiB 之间",
            )
        return _lab_v22_error(
            422,
            "SIGNATURE_FILE_MISSING",
            "未提供签名图片文件或表单格式非法",
        )

    try:
        uploaded = form.get("file")
        if uploaded is None or isinstance(uploaded, str) or not hasattr(uploaded, "read"):
            return _lab_v22_error(
                422,
                "SIGNATURE_FILE_MISSING",
                "未提供签名图片文件或文件为空",
            )
        raw_bytes = await uploaded.read(2_097_153)
    finally:
        await form.close()
    check = validate_signature_image(raw_bytes)
    if not check.ok or check.mime is None or check.sha256 is None:
        err_code = check.error_code or "SIGNATURE_CORRUPT"
        if err_code == "SIGNATURE_TOO_LARGE":
            return _lab_v22_error(
                413,
                "SIGNATURE_TOO_LARGE",
                "签名图片大小必须在 1 字节到 2 MiB 之间",
            )
        if err_code == "SIGNATURE_REJECTED_FORMAT":
            return _lab_v22_error(
                415,
                "SIGNATURE_REJECTED_FORMAT",
                "仅支持 PNG、JPEG 或 WebP 格式的电子签名图片",
            )
        return _lab_v22_error(
            422,
            err_code,
            "签名图片文件损坏或尺寸不在允许范围内（8×8 至 6000×6000）",
        )

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    person = await session.scalar(
        select(LabPerson)
        .where(LabPerson.id == uid)
        .options(undefer(LabPerson.signature_data))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")
    if actor.role != "admin" and (person.user_id is None or person.user_id != user.id):
        return _lab_v22_error(
            403,
            "FORBIDDEN",
            "仅管理员或本人可上传或替换电子签名",
        )
    if if_match is not None and if_match.strip():
        cur_sha = person.signature_sha256 or ""
        if not cur_sha or not _etag_matches(if_match, cur_sha):
            return _lab_v22_error(
                412,
                "PRECONDITION_FAILED",
                "电子签名已被其他请求更新，请刷新后重试",
            )

    now_utc = datetime.now(UTC)
    person.signature_data = raw_bytes
    person.signature_mime = check.mime
    person.signature_sha256 = check.sha256
    person.signature_updated_at = now_utc
    person.signature_source = "upload"
    await session.flush()
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab.signature.upload",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "person_id": str(person.id),
            "sha256": check.sha256,
            "size": check.size,
            "mime": check.mime,
            "width": check.width,
            "height": check.height,
        },
        session=session,
    )
    await session.commit()
    return {
        "person_id": str(person.id),
        "has_signature": True,
        "signature_mime": check.mime,
        "signature_sha256": check.sha256,
        "signature_updated_at": now_utc.isoformat(),
        "signature_source": "upload",
        "signature_url": f"/api/v1/lab/people/{person.id}/signature",
        "width": check.width,
        "height": check.height,
        "size": check.size,
    }


@router.delete("/lab/people/{person_id}/signature", response_model=None)
async def delete_person_signature(
    person_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    user = await _user(session, actor)
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    person = await session.scalar(
        select(LabPerson)
        .options(undefer(LabPerson.signature_data))
        .where(LabPerson.id == uid)
        .with_for_update()
    )
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    if actor.role != "admin" and (person.user_id is None or person.user_id != user.id):
        return _lab_v22_error(
            403,
            "FORBIDDEN",
            "仅管理员或本人可删除电子签名",
        )

    if_match = request.headers.get("if-match")
    if if_match is not None and if_match.strip():
        cur_sha = person.signature_sha256 or ""
        if not cur_sha or not _etag_matches(if_match, cur_sha):
            return _lab_v22_error(
                412,
                "PRECONDITION_FAILED",
                "电子签名已被其他请求更新，请刷新后重试",
            )

    prev_sha = person.signature_sha256
    person.signature_data = None
    person.signature_mime = None
    person.signature_sha256 = None
    person.signature_updated_at = None
    person.signature_source = None
    await session.flush()
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab.signature.delete",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "person_id": str(person.id),
            "previous_sha256": prev_sha,
        },
        session=session,
    )
    await session.commit()
    return {
        "person_id": str(person.id),
        "has_signature": False,
        "signature_mime": None,
        "signature_sha256": None,
        "signature_updated_at": None,
        "signature_source": None,
        "signature_url": None,
    }


