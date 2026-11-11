"""LLM-assisted structured re-parsing for proposal achievements and projects."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.importer.fingerprint import achievement_fp, project_fp
from app.importer.parser import parse_date_string, reparse_preview_payload
from app.importer.pii import assert_no_pii, scrub_free_text
from app.llm import CAPTURED
from app.models import LabPerson, User

ALLOWED_ACHIEVEMENT_KINDS: frozenset[str] = frozenset(
    {"report", "exhibition", "paper", "patent", "software", "award", "book", "other"}
)
ALLOWED_PROJECT_ROLES: frozenset[str] = frozenset({"host", "participant", "unknown"})
ALLOWED_PROJECT_STATUSES: frozenset[str] = frozenset({"ongoing", "completed", "unknown"})

_REPARSE_SYSTEM_PROMPT = (
    "你是科研项目申报书结构化信息抽取助手。"
    "请严格依据输入的前期成果与承担项目条目，只输出合法 JSON 对象，不要输出 Markdown 代码块或任何解释。"
    "JSON 顶层结构必须为："
    '{"achievements":[{"index":1,"kind":"paper|patent|software|award|book|report|exhibition|other",'
    '"title":"...","first_author":"...|null","authors_text":"...|null","venue":"...|null",'
    '"year":2024,"support_note":"...|null"}],'
    '"projects":[{"index":1,"title":"...","grant_no":"...|null","pi_name":"...|null",'
    '"lab_role":"host|participant|unknown","funder":"...|null",'
    '"start_date":"YYYY-MM-DD|null","end_date":"YYYY-MM-DD|null",'
    '"amount_wan":"15.00|null","status":"ongoing|completed|unknown"}]}。'
    "禁止编造输入中不存在的事实。"
)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMReparseError(RuntimeError):
    """Raised when online LLM reparse fails or returns invalid structured JSON."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_opt_str(val: object, *, max_len: int = 500) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "null":
        return None
    scrubbed, _ = scrub_free_text(s)
    cleaned = scrubbed.strip()[:max_len]
    return cleaned or None


def _extract_json_object(raw_content: str) -> dict[str, Any]:
    stripped = _CODE_FENCE_RE.sub("", (raw_content or "").strip()).strip()
    if not stripped:
        raise LLMReparseError("llm_reparse_failed", "在线大模型返回空内容")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise LLMReparseError("llm_reparse_failed", "在线大模型未返回合法 JSON") from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMReparseError("llm_reparse_failed", "在线大模型 JSON 解析失败") from exc
    if not isinstance(parsed, dict):
        raise LLMReparseError("llm_reparse_failed", "在线大模型返回格式必须为 JSON 对象")
    return parsed


async def _call_llm_json(
    *,
    prompt: str,
    settings: Settings,
    llm_obj: Any,
) -> dict[str, Any]:
    assert_no_pii(prompt)
    if settings.llm_capture:
        CAPTURED.append(prompt)

    base_url = (settings.llm_base_url or "").rstrip("/")
    if not base_url:
        raise LLMReparseError("llm_reparse_unavailable", "未配置大模型服务地址")

    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.llm_api_key.get_secret_value()}"

    payload: dict[str, object] = {
        "model": getattr(llm_obj, "model", None) or settings.llm_model,
        "messages": [
            {"role": "system", "content": _REPARSE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": max(1024, int(settings.llm_max_tokens)),
        "stream": False,
    }
    url = f"{base_url}/chat/completions"
    shared_client: httpx.AsyncClient | None = getattr(llm_obj, "_client", None)

    async def _post(http_client: httpx.AsyncClient) -> httpx.Response:
        return await http_client.post(url, headers=headers, json=payload)

    try:
        if isinstance(shared_client, httpx.AsyncClient):
            resp = await _post(shared_client)
        else:
            timeout = httpx.Timeout(timeout=float(settings.llm_timeout_seconds), connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as temp_client:
                resp = await _post(temp_client)
    except httpx.HTTPError as exc:
        raise LLMReparseError(
            "llm_reparse_failed",
            "连接在线大模型服务失败，请稍后重试或切换为 fsm 模式",
        ) from exc

    if resp.status_code >= 400:
        raise LLMReparseError(
            "llm_reparse_failed",
            f"在线大模型服务返回错误状态码 ({resp.status_code})，请稍后重试或切换为 fsm 模式",
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMReparseError("llm_reparse_failed", "在线大模型响应不是有效 JSON") from exc

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LLMReparseError("llm_reparse_failed", "在线大模型响应缺少 choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    raw_msg = first.get("message")
    message = raw_msg if isinstance(raw_msg, dict) else {}
    content = str(message.get("content") or "")
    return _extract_json_object(content)


async def reparse_preview_payload_with_llm(
    preview: dict[str, Any],
    *,
    as_of_date: date | None,
    settings: Settings,
    llm_obj: Any,
    existing_users: list[User] | None = None,
    existing_people: list[LabPerson] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Run deterministic FSM baseline, then refine achievements & projects via real LLM JSON extraction."""
    preview, recomputed_stats = reparse_preview_payload(
        preview,
        as_of_date=as_of_date,
        mode="fsm",
        existing_users=existing_users,
        existing_people=existing_people,
    )

    achievements_list: list[dict[str, Any]] = [
        dict(a) for a in (preview.get("achievements") or []) if isinstance(a, dict)
    ]
    projects_list: list[dict[str, Any]] = [
        dict(j) for j in (preview.get("projects") or []) if isinstance(j, dict)
    ]

    if achievements_list or projects_list:
        prompt_payload = {
            "as_of_time": as_of_date.isoformat() if as_of_date else None,
            "achievements": [
                {
                    "index": idx,
                    "raw_text": scrub_free_text(str(a.get("raw_text") or a.get("title") or ""))[0],
                }
                for idx, a in enumerate(achievements_list, start=1)
            ],
            "projects": [
                {
                    "index": idx,
                    "raw_text": scrub_free_text(str(j.get("raw_text") or j.get("title") or ""))[0],
                }
                for idx, j in enumerate(projects_list, start=1)
            ],
        }
        prompt_str = json.dumps(prompt_payload, ensure_ascii=False)
        llm_data = await _call_llm_json(
            prompt=prompt_str,
            settings=settings,
            llm_obj=llm_obj,
        )

        llm_achs = llm_data.get("achievements")
        if isinstance(llm_achs, list):
            by_idx_ach = {
                int(item["index"]): item
                for item in llm_achs
                if isinstance(item, dict) and isinstance(item.get("index"), int)
            }
            for idx, base_ach in enumerate(achievements_list, start=1):
                cand = by_idx_ach.get(idx)
                if not isinstance(cand, dict):
                    continue
                cand_title = _clean_opt_str(cand.get("title"), max_len=500)
                if cand_title:
                    base_ach["title"] = cand_title.strip("《》")
                cand_kind = _clean_opt_str(cand.get("kind"), max_len=32)
                if cand_kind and cand_kind.lower() in ALLOWED_ACHIEVEMENT_KINDS:
                    base_ach["kind"] = cand_kind.lower()
                for opt_key, cap in (
                    ("first_author", 120),
                    ("authors_text", 500),
                    ("venue", 300),
                    ("support_note", 500),
                ):
                    if opt_key in cand:
                        cleaned_opt = _clean_opt_str(cand.get(opt_key), max_len=cap)
                        if cleaned_opt is not None:
                            base_ach[opt_key] = cleaned_opt
                raw_year = cand.get("year")
                if isinstance(raw_year, int) and 1990 <= raw_year <= 2035:
                    base_ach["year"] = raw_year
                elif isinstance(raw_year, str) and raw_year.strip().isdigit():
                    yr = int(raw_year.strip())
                    if 1990 <= yr <= 2035:
                        base_ach["year"] = yr
                base_ach["parse_quality"] = "structured"
                base_ach["fingerprint"] = achievement_fp(
                    kind=str(base_ach.get("kind") or "other"),
                    title=str(base_ach.get("title") or ""),
                    year=base_ach.get("year") if isinstance(base_ach.get("year"), int) else None,
                    raw_text=str(base_ach.get("raw_text") or ""),
                    parse_quality=str(base_ach.get("parse_quality") or "structured"),
                )

        llm_projs = llm_data.get("projects")
        if isinstance(llm_projs, list):
            by_idx_proj = {
                int(item["index"]): item
                for item in llm_projs
                if isinstance(item, dict) and isinstance(item.get("index"), int)
            }
            for idx, base_proj in enumerate(projects_list, start=1):
                cand = by_idx_proj.get(idx)
                if not isinstance(cand, dict):
                    continue
                cand_title = _clean_opt_str(cand.get("title"), max_len=500)
                if cand_title:
                    base_proj["title"] = cand_title.strip("《》")
                for opt_key, cap in (
                    ("grant_no", 80),
                    ("pi_name", 120),
                    ("funder", 240),
                ):
                    if opt_key in cand:
                        cleaned_opt = _clean_opt_str(cand.get(opt_key), max_len=cap)
                        if cleaned_opt is not None:
                            base_proj[opt_key] = cleaned_opt
                cand_role = _clean_opt_str(cand.get("lab_role"), max_len=32)
                if cand_role and cand_role.lower() in ALLOWED_PROJECT_ROLES:
                    base_proj["lab_role"] = cand_role.lower()
                for date_key in ("start_date", "end_date"):
                    raw_d = _clean_opt_str(cand.get(date_key), max_len=32)
                    if raw_d:
                        parsed_d = parse_date_string(raw_d)
                        if parsed_d is not None:
                            base_proj[date_key] = parsed_d.isoformat()
                raw_amt = _clean_opt_str(cand.get("amount_wan"), max_len=32)
                if raw_amt:
                    m_amt = re.match(r"^(\d+(?:\.\d+)?)", raw_amt)
                    if m_amt:
                        try:
                            dec = Decimal(m_amt.group(1))
                            if dec > 0:
                                base_proj["amount_wan"] = f"{dec:.2f}"
                        except InvalidOperation:
                            pass
                cand_status = _clean_opt_str(cand.get("status"), max_len=32)
                if cand_status and cand_status.lower() in ALLOWED_PROJECT_STATUSES:
                    st_low = cand_status.lower()
                    if st_low in ("ongoing", "completed"):
                        base_proj["status"] = st_low
                        base_proj["status_source"] = "explicit"
                    elif base_proj.get("end_date") and as_of_date is not None:
                        end_d = parse_date_string(str(base_proj["end_date"]))
                        if end_d is not None:
                            base_proj["status"] = (
                                "completed" if end_d < as_of_date else "ongoing"
                            )
                            base_proj["status_source"] = "inferred"
                base_proj["fingerprint"] = project_fp(
                    grant_no=str(base_proj.get("grant_no") or "") or None,
                    title=str(base_proj.get("title") or ""),
                    funder=str(base_proj.get("funder") or "") or None,
                )

    preview["achievements"] = achievements_list
    preview["projects"] = projects_list
    preview["parser_version"] = "v2.1-llm"
    preview["extraction_mode"] = "rule+llm"
    assert_no_pii(preview)
    recomputed_stats["achievements"] = len(achievements_list)
    recomputed_stats["achievements_parsed"] = len(achievements_list)
    recomputed_stats["projects"] = len(projects_list)
    recomputed_stats["projects_parsed"] = len(projects_list)
    return preview, recomputed_stats
