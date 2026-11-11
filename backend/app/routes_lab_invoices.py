"""API routes for PDF invoice upload, auto-recognition, auto-renaming, and admin summary/export."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.audit import scrub_audit_str, write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.invoices import (
    MAX_INVOICE_AMOUNT,
    MAX_INVOICE_PDF_BYTES,
    MAX_INVOICE_ZIP_COUNT,
    MAX_INVOICE_ZIP_TOTAL_BYTES,
    InvoiceValidationError,
    build_invoice_content_disposition,
    build_invoice_download_filename,
    build_invoices_csv_bytes,
    build_invoices_zip_bytes,
    format_invoice_amount,
    parse_invoice_pdf,
    today_compact_utc,
)
from app.importer.pii import scrub_free_text
from app.models import LabInvoice, User
from app.routes import APIError, _actor, _client_ip, _settings, _user

router = APIRouter(prefix="/api/v1")


class UpdateInvoiceBody(BaseModel):
    item_name: str | None = Field(default=None, max_length=200)
    total_amount: Decimal | str | float | int | None = None
    invoice_date: str | None = None
    invoice_number: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)


class ExportInvoicesZipBody(BaseModel):
    invoice_ids: list[uuid.UUID] | None = None
    scope: str | None = None
    owner_id: uuid.UUID | None = None
    q: str | None = None
    date_from: date | None = None
    date_to: date | None = None


def _parse_amount_input(raw: Decimal | str | float | int | None) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("，", "").replace("¥", "").replace("￥", "")
    if not text:
        return None
    try:
        dec = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise APIError(400, "invalid_amount", "合计金额格式不正确，请输入有效数字") from exc
    if dec < Decimal("0") or dec > MAX_INVOICE_AMOUNT:
        raise APIError(400, "invalid_amount", "合计金额超出允许范围（0 ~ 9,999,999,999.99）")
    return dec


def _parse_date_input(raw: str | None) -> date | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    normalized = (
        cleaned.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .replace(".", "-")
        .strip()
    )
    parts = [p.strip() for p in normalized.split("-") if p.strip()]
    if len(parts) == 3:
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError as exc:
            raise APIError(400, "invalid_date", "开票日期格式不正确，请使用 YYYY-MM-DD") from exc
    raise APIError(400, "invalid_date", "开票日期格式不正确，请使用 YYYY-MM-DD")


def _serialize_invoice(inv: LabInvoice, owner: User | None) -> dict[str, object]:
    renamed = build_invoice_download_filename(inv.item_name, inv.total_amount)
    return {
        "id": str(inv.id),
        "owner_id": str(inv.owner_id),
        "owner_name": owner.display_name if owner is not None else "",
        "owner_email": (owner.email or owner.login_name or "") if owner is not None else "",
        "original_filename": inv.original_filename,
        "item_name": inv.item_name,
        "raw_item_name": inv.raw_item_name,
        "total_amount": format_invoice_amount(inv.total_amount),
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date is not None else None,
        "invoice_number": inv.invoice_number,
        "note": inv.note,
        "renamed_filename": renamed,
        "pdf_sha256": inv.pdf_sha256,
        "pdf_size_bytes": inv.pdf_size_bytes,
        "created_at": inv.created_at.isoformat() if inv.created_at is not None else None,
        "updated_at": inv.updated_at.isoformat() if inv.updated_at is not None else None,
    }


async def _deny_invoice_access(
    request: Request,
    actor_id: uuid.UUID,
    operation: str,
    resource_id: str | None = None,
    message: str = "没有权限执行此发票操作",
) -> None:
    await write_audit(
        settings=_settings(request),
        actor_id=actor_id,
        action="permission_denied",
        resource_type="invoice",
        resource_id=resource_id,
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"operation": operation},
    )
    raise APIError(403, "forbidden", message)


@router.post("/lab/invoices/parse")
async def parse_invoice_endpoint(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    if user.role == "viewer":
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_parse",
            message="查看者不能上传发票",
        )

    allowed = await request.app.state.limiter.allow(
        f"invoice_parse:{user.id}",
        max(settings.upload_rate_limit * 2, 10),
        settings.upload_rate_window_seconds,
    )
    if not allowed:
        raise APIError(429, "rate_limited", "发票解析请求过于频繁，请稍后再试")

    original_filename = (file.filename or "invoice.pdf").strip()[:255] or "invoice.pdf"
    payload = await file.read(MAX_INVOICE_PDF_BYTES + 1)
    try:
        parsed = await asyncio.to_thread(parse_invoice_pdf, payload, original_filename)
    except InvoiceValidationError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    return {
        "original_filename": original_filename,
        "item_name": parsed.item_name or "",
        "raw_item_name": parsed.raw_item_name,
        "total_amount": (
            format_invoice_amount(parsed.total_amount)
            if parsed.total_amount is not None
            else ""
        ),
        "invoice_date": (
            parsed.invoice_date.isoformat() if parsed.invoice_date is not None else None
        ),
        "invoice_number": parsed.invoice_number,
        "renamed_filename": build_invoice_download_filename(
            parsed.item_name, parsed.total_amount
        ),
        "pdf_sha256": parsed.pdf_sha256,
        "pdf_size_bytes": parsed.pdf_size_bytes,
        "warnings": parsed.warnings,
    }


@router.post("/lab/invoices", status_code=201)
async def upload_invoice(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    item_name: Annotated[str | None, Form()] = None,
    total_amount: Annotated[str | None, Form()] = None,
    invoice_date: Annotated[str | None, Form()] = None,
    invoice_number: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    if user.role == "viewer":
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_upload",
            message="查看者不能上传发票",
        )

    allowed = await request.app.state.limiter.allow(
        f"upload:{user.id}",
        settings.upload_rate_limit,
        settings.upload_rate_window_seconds,
    )
    if not allowed:
        raise APIError(429, "rate_limited", "上传过于频繁，请稍后再试")

    original_filename = (file.filename or "invoice.pdf").strip()[:255] or "invoice.pdf"
    payload = await file.read(MAX_INVOICE_PDF_BYTES + 1)
    try:
        parsed = await asyncio.to_thread(parse_invoice_pdf, payload, original_filename)
    except InvoiceValidationError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    raw_item = (
        item_name.strip()[:200]
        if item_name is not None and item_name.strip()
        else (parsed.item_name or "").strip()[:200]
    )
    final_item, _ = scrub_free_text(raw_item)
    final_item = final_item.strip()[:200]
    if not final_item:
        raise APIError(
            422,
            "missing_item_name",
            "未能自动识别出货物名称，请核对并手动填写货物名称后再提交",
        )

    explicit_amount = _parse_amount_input(total_amount)
    final_amount = explicit_amount if explicit_amount is not None else parsed.total_amount
    if final_amount is None:
        raise APIError(
            422,
            "missing_total_amount",
            "未能自动识别出合计金额，请核对并手动填写合计金额后再提交",
        )

    explicit_date = _parse_date_input(invoice_date)
    final_date = explicit_date if explicit_date is not None else parsed.invoice_date

    final_number = (
        invoice_number.strip()[:64]
        if invoice_number is not None and invoice_number.strip()
        else parsed.invoice_number
    )
    if final_number:
        final_number, _ = scrub_free_text(final_number)
        final_number = final_number[:64]

    raw_note = note.strip()[:500] if note is not None and note.strip() else None
    final_note: str | None = None
    if raw_note:
        scrubbed_note, _ = scrub_free_text(raw_note)
        final_note = scrubbed_note[:500]

    raw_item_line: str | None = None
    if parsed.raw_item_name:
        scrubbed_raw_item, _ = scrub_free_text(parsed.raw_item_name)
        raw_item_line = scrubbed_raw_item[:300]

    inv = LabInvoice(
        owner_id=user.id,
        original_filename=original_filename,
        item_name=final_item,
        raw_item_name=raw_item_line,
        total_amount=final_amount,
        invoice_date=final_date,
        invoice_number=final_number,
        note=final_note,
        pdf_data=payload,
        pdf_sha256=parsed.pdf_sha256,
        pdf_size_bytes=parsed.pdf_size_bytes,
    )
    session.add(inv)
    await session.flush()

    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="invoice_uploaded",
        resource_type="invoice",
        resource_id=str(inv.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "item_name": scrub_audit_str(final_item, max_len=80),
            "total_amount": float(final_amount),
            "invoice_date": final_date.isoformat() if final_date is not None else "",
            "pdf_sha256": parsed.pdf_sha256,
            "size_bytes": parsed.pdf_size_bytes,
        },
        session=session,
    )
    await session.commit()
    await session.refresh(inv)
    return {
        "invoice": _serialize_invoice(inv, user),
        "warnings": parsed.warnings,
    }


def _apply_invoice_filters(
    stmt: Any,
    *,
    effective_owner_id: uuid.UUID | None,
    q: str | None,
    date_from: date | None,
    date_to: date | None,
) -> Any:
    if effective_owner_id is not None:
        stmt = stmt.where(LabInvoice.owner_id == effective_owner_id)
    if q is not None and q.strip():
        escaped = (
            q.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                LabInvoice.item_name.ilike(pattern, escape="\\"),
                LabInvoice.original_filename.ilike(pattern, escape="\\"),
                LabInvoice.invoice_number.ilike(pattern, escape="\\"),
                LabInvoice.note.ilike(pattern, escape="\\"),
            )
        )
    if date_from is not None:
        stmt = stmt.where(LabInvoice.invoice_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(LabInvoice.invoice_date <= date_to)
    return stmt


@router.get("/lab/invoices")
async def list_invoices(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[str | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> dict[str, object]:
    user = await _user(session, actor)
    effective_scope = scope or ("all" if user.role == "admin" else "mine")
    if effective_scope not in {"mine", "all"}:
        raise APIError(400, "invalid_scope", "不支持的查询范围")

    if user.role != "admin":
        if effective_scope == "all" or (owner_id is not None and owner_id != user.id):
            await _deny_invoice_access(
                request,
                user.id,
                "invoice_list_all",
                message="只有管理员可以汇总查看全部成员的发票",
            )
        effective_owner_id: uuid.UUID | None = user.id
    else:
        effective_owner_id = user.id if effective_scope == "mine" else owner_id

    stmt = (
        select(LabInvoice, User)
        .join(User, User.id == LabInvoice.owner_id)
        .order_by(LabInvoice.invoice_date.desc().nulls_last(), LabInvoice.created_at.desc())
    )
    stmt = _apply_invoice_filters(
        stmt,
        effective_owner_id=effective_owner_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )
    rows = list((await session.execute(stmt)).all())

    serialized: list[dict[str, object]] = []
    total_sum = Decimal("0.00")
    distinct_owners: set[uuid.UUID] = set()
    for inv, owner in rows:
        serialized.append(_serialize_invoice(inv, owner))
        total_sum += Decimal(str(inv.total_amount))
        distinct_owners.add(inv.owner_id)

    owners_list: list[dict[str, str]] = []
    if user.role == "admin":
        owner_users = list(
            await session.scalars(
                select(User)
                .where(
                    User.id.in_(
                        select(func.distinct(LabInvoice.owner_id))
                    )
                )
                .order_by(User.display_name.asc(), User.created_at.asc())
            )
        )
        owners_list = [
            {
                "id": str(u.id),
                "display_name": u.display_name,
                "email": u.email or u.login_name or str(u.id),
            }
            for u in owner_users
        ]

    return {
        "invoices": serialized,
        "summary": {
            "total_count": len(serialized),
            "total_amount": format_invoice_amount(total_sum),
            "owner_count": len(distinct_owners),
        },
        "owners": owners_list,
    }


@router.patch("/lab/invoices/{invoice_id}")
async def update_invoice(
    invoice_id: uuid.UUID,
    body: UpdateInvoiceBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    if user.role == "viewer":
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_update",
            resource_id=str(invoice_id),
            message="查看者不能修改发票",
        )
    inv = await session.get(LabInvoice, invoice_id)
    if inv is None:
        raise APIError(404, "not_found", "发票记录不存在")

    if user.role != "admin" and inv.owner_id != user.id:
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_update",
            resource_id=str(invoice_id),
            message="只能修改自己上传的发票",
        )

    if "item_name" in body.model_fields_set:
        if body.item_name is None:
            raise APIError(400, "invalid_item_name", "货物名称不能为空")
        scrubbed_item, _ = scrub_free_text(body.item_name.strip()[:200])
        cleaned_item = scrubbed_item.strip()[:200]
        if not cleaned_item:
            raise APIError(400, "invalid_item_name", "货物名称不能为空")
        inv.item_name = cleaned_item

    if "total_amount" in body.model_fields_set:
        parsed_amount = _parse_amount_input(body.total_amount)
        if parsed_amount is None:
            raise APIError(400, "invalid_amount", "合计金额不能为空")
        inv.total_amount = parsed_amount

    if "invoice_date" in body.model_fields_set:
        inv.invoice_date = _parse_date_input(body.invoice_date)

    if "invoice_number" in body.model_fields_set:
        if body.invoice_number and body.invoice_number.strip():
            scrubbed_no, _ = scrub_free_text(body.invoice_number.strip()[:64])
            inv.invoice_number = scrubbed_no[:64]
        else:
            inv.invoice_number = None

    if "note" in body.model_fields_set:
        if body.note and body.note.strip():
            scrubbed_note, _ = scrub_free_text(body.note.strip()[:500])
            inv.note = scrubbed_note[:500]
        else:
            inv.note = None

    await session.flush()
    owner_user = await session.get(User, inv.owner_id)
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="invoice_updated",
        resource_type="invoice",
        resource_id=str(inv.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "item_name": scrub_audit_str(inv.item_name, max_len=80),
            "total_amount": float(inv.total_amount),
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else "",
        },
        session=session,
    )
    await session.commit()
    await session.refresh(inv)
    return {"invoice": _serialize_invoice(inv, owner_user)}


@router.delete("/lab/invoices/{invoice_id}")
async def delete_invoice(
    invoice_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    settings = _settings(request)
    user = await _user(session, actor)
    if user.role == "viewer":
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_delete",
            resource_id=str(invoice_id),
            message="查看者不能删除发票",
        )
    inv = await session.get(LabInvoice, invoice_id)
    if inv is None:
        raise APIError(404, "not_found", "发票记录不存在")

    if user.role != "admin" and inv.owner_id != user.id:
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_delete",
            resource_id=str(invoice_id),
            message="只能删除自己上传的发票",
        )

    item_snapshot = scrub_audit_str(inv.item_name, max_len=80)
    amount_snapshot = float(inv.total_amount)
    await session.delete(inv)
    await session.flush()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="invoice_deleted",
        resource_type="invoice",
        resource_id=str(invoice_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "item_name": item_snapshot,
            "total_amount": amount_snapshot,
        },
        session=session,
    )
    await session.commit()
    return {"status": "deleted", "id": str(invoice_id)}


@router.get("/lab/invoices/{invoice_id}/download")
async def download_invoice_pdf(
    invoice_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    settings = _settings(request)
    user = await _user(session, actor)
    inv = await session.scalar(
        select(LabInvoice)
        .options(undefer(LabInvoice.pdf_data))
        .where(LabInvoice.id == invoice_id)
    )
    if inv is None:
        raise APIError(404, "not_found", "发票记录不存在")

    if user.role != "admin" and inv.owner_id != user.id:
        await _deny_invoice_access(
            request,
            user.id,
            "invoice_download",
            resource_id=str(invoice_id),
            message="只能下载自己上传的发票",
        )

    renamed = build_invoice_download_filename(inv.item_name, inv.total_amount)
    pdf_bytes = bytes(inv.pdf_data)
    pdf_sha = inv.pdf_sha256

    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="invoice_downloaded",
        resource_type="invoice",
        resource_id=str(inv.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "renamed_filename": scrub_audit_str(renamed, max_len=120),
            "size_bytes": len(pdf_bytes),
        },
        session=session,
    )
    await session.commit()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_invoice_content_disposition(
                renamed, fallback_ascii="invoice.pdf"
            ),
            "ETag": f'"{pdf_sha}"',
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/lab/invoices/export:zip")
async def export_invoices_zip(
    body: ExportInvoicesZipBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    settings = _settings(request)
    user = await _user(session, actor)

    effective_scope = body.scope or ("all" if user.role == "admin" else "mine")
    if user.role != "admin":
        if effective_scope == "all" or (
            body.owner_id is not None and body.owner_id != user.id
        ):
            await _deny_invoice_access(
                request,
                user.id,
                "invoice_export_zip_all",
                message="只有管理员可以批量导出全部成员的发票",
            )
        effective_owner_id: uuid.UUID | None = user.id
    else:
        effective_owner_id = user.id if effective_scope == "mine" else body.owner_id

    stmt = (
        select(LabInvoice, User)
        .join(User, User.id == LabInvoice.owner_id)
        .order_by(LabInvoice.invoice_date.desc().nulls_last(), LabInvoice.created_at.desc())
    )
    if body.invoice_ids is not None:
        if not body.invoice_ids:
            raise APIError(400, "empty_selection", "请至少选择一张发票导出")
        if len(body.invoice_ids) > MAX_INVOICE_ZIP_COUNT:
            raise APIError(
                400,
                "batch_limit_exceeded",
                f"单次最多打包导出 {MAX_INVOICE_ZIP_COUNT} 张发票",
            )
        stmt = stmt.where(LabInvoice.id.in_(body.invoice_ids))

    stmt = _apply_invoice_filters(
        stmt,
        effective_owner_id=effective_owner_id,
        q=body.q,
        date_from=body.date_from,
        date_to=body.date_to,
    )
    meta_rows = list((await session.execute(stmt.limit(MAX_INVOICE_ZIP_COUNT + 1))).all())

    if body.invoice_ids is not None and user.role != "admin":
        found_ids = {inv.id for inv, _ in meta_rows}
        if len(found_ids) != len(set(body.invoice_ids)):
            await _deny_invoice_access(
                request,
                user.id,
                "invoice_export_zip_foreign",
                message="包含无权导出的他人发票",
            )

    if not meta_rows:
        raise APIError(404, "no_invoices", "没有符合条件的发票可导出")
    if len(meta_rows) > MAX_INVOICE_ZIP_COUNT:
        raise APIError(
            400,
            "batch_limit_exceeded",
            f"单次最多打包导出 {MAX_INVOICE_ZIP_COUNT} 张发票，请缩小筛选范围",
        )

    total_bytes = sum(int(inv.pdf_size_bytes) for inv, _ in meta_rows)
    if total_bytes > MAX_INVOICE_ZIP_TOTAL_BYTES:
        raise APIError(
            413,
            "export_too_large",
            "所选发票总体积超过 100 MB 上限，请分批导出",
        )

    target_ids = [inv.id for inv, _ in meta_rows]
    blob_rows = (
        await session.execute(
            select(LabInvoice.id, LabInvoice.pdf_data).where(
                LabInvoice.id.in_(target_ids)
            )
        )
    ).all()
    blob_by_id = {row_id: bytes(data) for row_id, data in blob_rows}

    serialized_rows = [_serialize_invoice(inv, owner) for inv, owner in meta_rows]
    blobs = [blob_by_id[inv.id] for inv, _ in meta_rows]
    zip_bytes = await asyncio.to_thread(build_invoices_zip_bytes, serialized_rows, blobs)
    rows = meta_rows

    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="invoice_exported",
        resource_type="invoice",
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "format": "zip",
            "count": len(rows),
            "scope": effective_scope,
        },
        session=session,
    )
    await session.commit()

    zip_filename = f"发票重命名汇总_{today_compact_utc()}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": build_invoice_content_disposition(
                zip_filename, fallback_ascii="invoices_renamed.zip"
            ),
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/lab/invoices/export:csv")
async def export_invoices_csv(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[str | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> Response:
    settings = _settings(request)
    user = await _user(session, actor)

    effective_scope = scope or ("all" if user.role == "admin" else "mine")
    if user.role != "admin":
        if effective_scope == "all" or (owner_id is not None and owner_id != user.id):
            await _deny_invoice_access(
                request,
                user.id,
                "invoice_export_csv_all",
                message="只有管理员可以导出全部成员的发票汇总表",
            )
        effective_owner_id: uuid.UUID | None = user.id
    else:
        effective_owner_id = user.id if effective_scope == "mine" else owner_id

    stmt = (
        select(LabInvoice, User)
        .join(User, User.id == LabInvoice.owner_id)
        .order_by(LabInvoice.invoice_date.desc().nulls_last(), LabInvoice.created_at.desc())
    )
    stmt = _apply_invoice_filters(
        stmt,
        effective_owner_id=effective_owner_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )
    rows = list((await session.execute(stmt)).all())
    serialized_rows = [_serialize_invoice(inv, owner) for inv, owner in rows]
    csv_bytes = build_invoices_csv_bytes(serialized_rows)

    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="invoice_exported",
        resource_type="invoice",
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "format": "csv",
            "count": len(rows),
            "scope": effective_scope,
        },
        session=session,
    )
    await session.commit()

    csv_filename = f"发票汇总清单_{today_compact_utc()}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": build_invoice_content_disposition(
                csv_filename, fallback_ascii="invoices_summary.csv"
            ),
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
