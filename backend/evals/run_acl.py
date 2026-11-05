"""Evaluate ACL leakage across retrieval and agent execution (Spec T3.3 / PR-23)."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import app.agent as agent_mod
import app.tools as tools_mod
from alembic import command
from alembic.config import Config
from app.acl import Principal
from app.agent import run_agent
from app.auth import UNUSABLE_PASSWORD_HASH
from app.config import Settings, get_settings, repo_root
from app.db import dispose_engine, get_sessionmaker
from app.llm import build_llm
from app.models import Document, DocumentACL, Group, GroupMember, User
from app.rag.chunker import parse_front_matter
from app.rag.embedder import build_embedder
from app.rag.index_meta import column_type
from app.rag.ingest import _add_acl_grants, upsert_source
from app.rag.retrieve import search_knowledge
from app.reindex import apply_reindex
from sqlalchemy import delete, select

VALID_ROLES = frozenset({"admin", "member", "viewer"})
VALID_VISIBILITIES = frozenset({"lab", "restricted", "private"})

ACL_OWNER_EMAIL = "acl-eval-owner@qingheng.lab"
ACL_ACTOR_EMAIL = "acl-eval-actor@qingheng.lab"
ACL_OTHER_EMAIL = "acl-eval-other@qingheng.lab"


def _read_dir(path: Path) -> list[tuple[dict[str, str], str]]:
    records: list[tuple[dict[str, str], str]] = []
    if not path.is_dir():
        return records
    for file in sorted(path.glob("*.md")):
        records.append(parse_front_matter(file.read_text(encoding="utf-8")))
    return records


def _read_acl_documents(settings: Settings) -> list[tuple[dict[str, str], str]]:
    return _read_dir(Path(settings.corpus_dir)) + _read_dir(Path(settings.seed_only_dir))


def valid_doc_keys(settings: Settings | None = None) -> set[str]:
    resolved = settings or get_settings()
    records = _read_acl_documents(resolved)
    return {meta["key"] for meta, _ in records if meta.get("key")}


def load_acl_cases(
    path: Path | None = None,
    allowed_doc_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    cases_path = path or (repo_root() / "evals" / "data" / "v1" / "acl_cases.jsonl")
    if not cases_path.is_file():
        raise ValueError(f"ACL cases file not found: {cases_path}")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_no, raw_line in enumerate(cases_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"Line {line_no}: case must be a JSON object")
        required = {"id", "user_role", "user_groups", "grants", "question", "forbidden_doc_keys"}
        missing = required - set(item.keys())
        if missing:
            raise ValueError(f"Line {line_no}: missing required fields {sorted(missing)}")

        case_id = item["id"]
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Line {line_no}: id must be a non-empty string")
        if case_id in seen_ids:
            raise ValueError(f"Line {line_no}: duplicate case id {case_id!r}")
        seen_ids.add(case_id)

        role = item["user_role"]
        if not isinstance(role, str) or role not in VALID_ROLES:
            raise ValueError(f"Case {case_id}: invalid user_role {role!r}")

        user_groups = item["user_groups"]
        if not isinstance(user_groups, list) or not all(
            isinstance(g, str) and g.strip() for g in user_groups
        ):
            raise ValueError(f"Case {case_id}: user_groups must be a list of non-empty strings")

        question = item["question"]
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Case {case_id}: question must be a non-empty string")

        forbidden = item["forbidden_doc_keys"]
        if (
            not isinstance(forbidden, list)
            or not forbidden
            or not all(isinstance(k, str) and k.strip() for k in forbidden)
        ):
            raise ValueError(
                f"Case {case_id}: forbidden_doc_keys must be a non-empty list of doc keys"
            )
        if allowed_doc_keys is not None:
            unknown_forbidden = set(forbidden) - allowed_doc_keys
            if unknown_forbidden:
                raise ValueError(
                    f"Case {case_id}: unknown forbidden_doc_keys {sorted(unknown_forbidden)}"
                )

        grants = item["grants"]
        if not isinstance(grants, dict):
            raise ValueError(f"Case {case_id}: grants must be a dict mapping doc_key to grant spec")
        for doc_key, spec in grants.items():
            if not isinstance(doc_key, str) or not doc_key.strip():
                raise ValueError(f"Case {case_id}: grant key must be a non-empty string")
            if allowed_doc_keys is not None and doc_key not in allowed_doc_keys:
                raise ValueError(f"Case {case_id}: unknown grant doc_key {doc_key!r}")
            if not isinstance(spec, dict):
                raise ValueError(f"Case {case_id}: grant spec for {doc_key!r} must be a dict")
            vis = spec.get("visibility")
            if vis is not None and (not isinstance(vis, str) or vis not in VALID_VISIBILITIES):
                raise ValueError(
                    f"Case {case_id}: invalid visibility {vis!r} for doc_key {doc_key!r}"
                )
            for list_field in ("groups", "roles", "users"):
                field_val = spec.get(list_field)
                if field_val is not None and (
                    not isinstance(field_val, list)
                    or not all(isinstance(v, str) and v.strip() for v in field_val)
                ):
                    raise ValueError(
                        f"Case {case_id}: {list_field} in grant for {doc_key!r} must be a list of strings"
                    )
            for granted_role in spec.get("roles") or []:
                if granted_role not in VALID_ROLES:
                    raise ValueError(
                        f"Case {case_id}: invalid granted role {granted_role!r} for {doc_key!r}"
                    )

        cases.append(item)
    return cases


def _resolve_doc_key(
    entry: Mapping[str, Any],
    doc_id_to_key: Mapping[str, str] | None = None,
) -> str | None:
    raw_key = entry.get("doc_key")
    if isinstance(raw_key, str) and raw_key.strip():
        return raw_key.strip()
    doc_id = entry.get("document_id")
    if doc_id is not None and doc_id_to_key is not None:
        mapped = doc_id_to_key.get(str(doc_id))
        if mapped:
            return mapped
    key_field = entry.get("key")
    if isinstance(key_field, str) and "#" in key_field:
        prefix = key_field.split("#", 1)[0].strip()
        if prefix:
            return prefix
    return None


def evaluate_case_leaks(
    case: Mapping[str, Any],
    retrieved_hits: Sequence[Mapping[str, Any]],
    agent_citations: Sequence[Mapping[str, Any]],
    doc_id_to_key: Mapping[str, str] | None = None,
    *,
    refused: bool = False,
) -> dict[str, Any]:
    forbidden_list = [str(k) for k in case["forbidden_doc_keys"]]
    forbidden_set = set(forbidden_list)

    retrieved_keys: list[str] = []
    leaked_retrieval_keys_set: set[str] = set()
    retrieval_leak_count = 0
    for hit in retrieved_hits:
        doc_key = _resolve_doc_key(hit, doc_id_to_key)
        if doc_key is not None:
            if doc_key not in retrieved_keys:
                retrieved_keys.append(doc_key)
            if doc_key in forbidden_set:
                retrieval_leak_count += 1
                leaked_retrieval_keys_set.add(doc_key)

    cited_keys: list[str] = []
    leaked_citation_keys_set: set[str] = set()
    citation_leak_count = 0
    for citation in agent_citations:
        doc_key = _resolve_doc_key(citation, doc_id_to_key)
        if doc_key is not None:
            if doc_key not in cited_keys:
                cited_keys.append(doc_key)
            if doc_key in forbidden_set:
                citation_leak_count += 1
                leaked_citation_keys_set.add(doc_key)

    leaked = bool(retrieval_leak_count > 0 or citation_leak_count > 0)
    return {
        "id": str(case["id"]),
        "category": str(case.get("category") or "uncategorized"),
        "user_role": str(case["user_role"]),
        "user_groups": [str(g) for g in case.get("user_groups") or []],
        "forbidden_doc_keys": forbidden_list,
        "retrieved_doc_keys": retrieved_keys,
        "cited_doc_keys": cited_keys,
        "retrieval_leaks": retrieval_leak_count,
        "citation_leaks": citation_leak_count,
        "leaked_retrieval_keys": sorted(leaked_retrieval_keys_set),
        "leaked_citation_keys": sorted(leaked_citation_keys_set),
        "leaked": leaked,
        "refused": refused,
    }


def summarize_acl_results(case_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total_cases = len(case_results)
    leaked_cases = sum(1 for item in case_results if bool(item.get("leaked")))
    retrieval_leaks = sum(int(item.get("retrieval_leaks") or 0) for item in case_results)
    citation_leaks = sum(int(item.get("citation_leaks") or 0) for item in case_results)

    by_category: dict[str, dict[str, int]] = {}
    by_role: dict[str, dict[str, int]] = {}

    for item in case_results:
        cat = str(item.get("category") or "uncategorized")
        cat_bucket = by_category.setdefault(
            cat,
            {"total_cases": 0, "leaked_cases": 0, "retrieval_leaks": 0, "citation_leaks": 0},
        )
        cat_bucket["total_cases"] += 1
        cat_bucket["leaked_cases"] += 1 if item.get("leaked") else 0
        cat_bucket["retrieval_leaks"] += int(item.get("retrieval_leaks") or 0)
        cat_bucket["citation_leaks"] += int(item.get("citation_leaks") or 0)

        role = str(item.get("user_role") or "unknown")
        role_bucket = by_role.setdefault(
            role,
            {"total_cases": 0, "leaked_cases": 0, "retrieval_leaks": 0, "citation_leaks": 0},
        )
        role_bucket["total_cases"] += 1
        role_bucket["leaked_cases"] += 1 if item.get("leaked") else 0
        role_bucket["retrieval_leaks"] += int(item.get("retrieval_leaks") or 0)
        role_bucket["citation_leaks"] += int(item.get("citation_leaks") or 0)

    return {
        "total_cases": total_cases,
        "leaked_cases": leaked_cases,
        "retrieval_leaks": retrieval_leaks,
        "citation_leaks": citation_leaks,
        "by_category": by_category,
        "by_role": by_role,
    }


def _git_sha(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


async def _get_or_create_user(
    session: Any,
    *,
    email: str,
    display_name: str,
    role: str,
) -> tuple[User, bool]:
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        existing.role = role
        existing.password_hash = UNUSABLE_PASSWORD_HASH
        existing.is_active = False
        await session.flush()
        return existing, False
    user = User(
        email=email,
        display_name=display_name,
        role=role,
        password_hash=UNUSABLE_PASSWORD_HASH,
        is_active=False,
    )
    session.add(user)
    await session.flush()
    return user, True


async def _run(
    *,
    commit: bool = False,
    check: bool = False,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    root = repo_root()
    slug = settings.embedding_slug
    fingerprint = settings.embedding_fingerprint
    runtime_dir = Path(settings.calibration_dir)
    repo_acl_path = root / "evals" / "results" / "acl" / f"{slug}.json"
    runtime_acl_path = runtime_dir / f"acl-{slug}.json"

    if check and not commit and not repo_acl_path.is_file():
        print(f"Missing committed ACL baseline report: {repo_acl_path}", file=sys.stderr)
        raise SystemExit(3)

    records = _read_acl_documents(settings)
    allowed_keys = {meta["key"] for meta, _ in records if meta.get("key")}
    default_visibilities = {
        meta["key"]: (meta.get("visibility") or "lab")
        for meta, _ in records
        if meta.get("key")
    }

    active_cases = cases if cases is not None else load_acl_cases(allowed_doc_keys=allowed_keys)
    if len(active_cases) < 30:
        raise SystemExit(f"ACL eval set has {len(active_cases)} cases, need at least 30")

    agent_settings = settings.model_copy(
        update={
            "llm_provider": "fake",
            "llm_model": "fake-grounded-v1",
            "evidence_score_min": 0.0,
            "rare_coverage_min": 0.0,
        }
    )

    embedder = build_embedder(agent_settings)
    llm = build_llm(agent_settings)
    maker = get_sessionmaker()

    created_group_ids: list[uuid.UUID] = []
    created_user_ids: list[uuid.UUID] = []
    case_results: list[dict[str, Any]] = []

    try:
        async with maker() as session:
            col_storage, col_dim = await column_type(session)
            if (col_storage, col_dim) != (
                agent_settings.embedding_storage,
                agent_settings.embedding_dim,
            ):
                await apply_reindex(session, agent_settings, embedder, if_needed=True)

            owner, owner_created = await _get_or_create_user(
                session,
                email=ACL_OWNER_EMAIL,
                display_name="ACL评测所有者",
                role="admin",
            )
            actor, actor_created = await _get_or_create_user(
                session,
                email=ACL_ACTOR_EMAIL,
                display_name="ACL评测测试员",
                role="member",
            )
            other_user, other_created = await _get_or_create_user(
                session,
                email=ACL_OTHER_EMAIL,
                display_name="ACL评测其他成员",
                role="member",
            )
            if owner_created:
                created_user_ids.append(owner.id)
            if actor_created:
                created_user_ids.append(actor.id)
            if other_created:
                created_user_ids.append(other_user.id)

            all_group_slugs: set[str] = set()
            for case in active_cases:
                for g_slug in case.get("user_groups") or []:
                    all_group_slugs.add(str(g_slug))
                grants_map = case.get("grants") or {}
                if isinstance(grants_map, dict):
                    for spec in grants_map.values():
                        if isinstance(spec, dict):
                            for g_slug in spec.get("groups") or []:
                                all_group_slugs.add(str(g_slug))

            groups_by_slug: dict[str, Group] = {}
            for g_slug in sorted(all_group_slugs):
                grp = await session.scalar(select(Group).where(Group.slug == g_slug))
                if grp is None:
                    grp = Group(slug=g_slug, name=f"ACL评测组-{g_slug}")
                    session.add(grp)
                    await session.flush()
                    created_group_ids.append(grp.id)
                groups_by_slug[g_slug] = grp

            await upsert_source(
                session,
                embedder=embedder,
                owner_id=owner.id,
                source="eval",
                records=records,
                storage=agent_settings.embedding_storage,
            )
            await session.commit()

            eval_docs = list(
                await session.scalars(select(Document).where(Document.source == "eval"))
            )
            doc_by_key: dict[str, Document] = {
                doc.doc_key: doc for doc in eval_docs if doc.doc_key is not None
            }
            doc_id_to_key: dict[str, str] = {
                str(doc.id): doc.doc_key for doc in eval_docs if doc.doc_key is not None
            }

            try:
                for case in active_cases:
                    role = str(case["user_role"])
                    actor.role = role
                    await session.execute(
                        delete(GroupMember).where(GroupMember.user_id == actor.id)
                    )
                    actor_group_ids: set[str] = set()
                    for g_slug in case.get("user_groups") or []:
                        grp = groups_by_slug[str(g_slug)]
                        session.add(GroupMember(group_id=grp.id, user_id=actor.id))
                        actor_group_ids.add(str(grp.id))

                    # Reset all documents to baseline front-matter visibilities & empty ACL
                    for doc_key, doc in doc_by_key.items():
                        doc.owner_id = owner.id
                        doc.visibility = default_visibilities.get(doc_key, "lab")
                        await session.execute(
                            delete(DocumentACL).where(DocumentACL.document_id == doc.id)
                        )

                    # Apply per-case grants and visibility overrides
                    grants_spec = case.get("grants") or {}
                    if isinstance(grants_spec, dict):
                        for doc_key, spec in grants_spec.items():
                            if not isinstance(spec, dict) or doc_key not in doc_by_key:
                                continue
                            target_doc = doc_by_key[doc_key]
                            vis_override = spec.get("visibility")
                            if isinstance(vis_override, str) and vis_override in VALID_VISIBILITIES:
                                target_doc.visibility = vis_override
                            elif any(spec.get(k) for k in ("users", "roles", "groups")):
                                target_doc.visibility = "restricted"

                            grant_uids: list[uuid.UUID] = []
                            for u_label in spec.get("users") or []:
                                if u_label in {"actor", "self"}:
                                    grant_uids.append(actor.id)
                                elif u_label == "owner":
                                    grant_uids.append(owner.id)
                                else:
                                    grant_uids.append(other_user.id)

                            grant_gids: list[uuid.UUID] = [
                                groups_by_slug[str(g_slug)].id
                                for g_slug in (spec.get("groups") or [])
                                if str(g_slug) in groups_by_slug
                            ]
                            grant_rnames: list[str] = [
                                str(r_name) for r_name in (spec.get("roles") or [])
                            ]
                            _add_acl_grants(
                                session,
                                target_doc.id,
                                grant_user_ids=grant_uids,
                                grant_group_ids=grant_gids,
                                grant_roles=grant_rnames,
                            )

                    await session.commit()

                    frozen_groups = frozenset(actor_group_ids)
                    principal = Principal(id=actor.id, role=actor.role, group_ids=frozen_groups)
                    question = str(case["question"])

                    bundle = await search_knowledge(
                        session,
                        settings=agent_settings,
                        embedder=embedder,
                        user=principal,
                        query=question,
                        source="eval",
                        limit=None,
                    )

                    emitted_events: list[dict[str, object]] = []
                    agent_tool_hits: list[Mapping[str, Any]] = []

                    async def _emit(
                        event: dict[str, object],
                        _sink: list[dict[str, object]] = emitted_events,
                    ) -> None:
                        _sink.append(event)

                    orig_tools_sk = tools_mod.search_knowledge
                    orig_agent_skb = agent_mod.search_knowledge_base

                    async def _wrapped_tools_sk(
                        *args: Any,
                        _orig: Any = orig_tools_sk,
                        _sink: list[Mapping[str, Any]] = agent_tool_hits,
                        **kwargs: Any,
                    ) -> Any:
                        kwargs["source"] = "eval"
                        res_bundle = await _orig(*args, **kwargs)
                        raw_hits = getattr(res_bundle, "hits", None)
                        if isinstance(raw_hits, list):
                            for h in raw_hits:
                                if isinstance(h, Mapping):
                                    _sink.append(h)
                        return res_bundle

                    async def _wrapped_agent_skb(
                        *args: Any,
                        _orig: Any = orig_agent_skb,
                        _sink: list[Mapping[str, Any]] = agent_tool_hits,
                        **kwargs: Any,
                    ) -> Any:
                        found_hits = await _orig(*args, **kwargs)
                        if isinstance(found_hits, list):
                            for h in found_hits:
                                if isinstance(h, Mapping):
                                    _sink.append(h)
                        return found_hits

                    tools_mod.search_knowledge = _wrapped_tools_sk  # type: ignore[assignment]
                    agent_mod.search_knowledge_base = _wrapped_agent_skb  # type: ignore[assignment]

                    try:
                        agent_out = await run_agent(
                            session=session,
                            user=actor,
                            settings=agent_settings,
                            embedder=embedder,
                            llm=llm,
                            question=question,
                            emit=_emit,
                            request_id=f"acl-{case['id']}",
                            group_ids=frozen_groups,
                        )
                    finally:
                        tools_mod.search_knowledge = orig_tools_sk  # type: ignore[assignment]
                        agent_mod.search_knowledge_base = orig_agent_skb  # type: ignore[assignment]
                    await session.commit()

                    combined_hits: list[Mapping[str, Any]] = list(bundle.hits)
                    seen_chunk_ids = {str(h.get("chunk_id")) for h in combined_hits}
                    for h in agent_tool_hits:
                        cid = str(h.get("chunk_id") or "")
                        if not cid or cid not in seen_chunk_ids:
                            combined_hits.append(h)
                            if cid:
                                seen_chunk_ids.add(cid)
                    for ev in emitted_events:
                        if ev.get("type") == "retrieval":
                            ev_hits = ev.get("hits")
                            if isinstance(ev_hits, list):
                                for h in ev_hits:
                                    if isinstance(h, dict):
                                        cid = str(h.get("chunk_id") or "")
                                        if not cid or cid not in seen_chunk_ids:
                                            combined_hits.append(h)
                                            if cid:
                                                seen_chunk_ids.add(cid)

                    raw_citations = agent_out.get("citations")
                    combined_citations: list[Mapping[str, Any]] = (
                        [c for c in raw_citations if isinstance(c, dict)]
                        if isinstance(raw_citations, list)
                        else []
                    )
                    seen_cite_ids = {
                        str(c.get("chunk_id") or c.get("document_id") or "")
                        for c in combined_citations
                    }
                    for ev in emitted_events:
                        if ev.get("type") == "citations":
                            ev_items = ev.get("items")
                            if isinstance(ev_items, list):
                                for c in ev_items:
                                    if isinstance(c, dict):
                                        cid = str(c.get("chunk_id") or c.get("document_id") or "")
                                        if not cid or cid not in seen_cite_ids:
                                            combined_citations.append(c)
                                            if cid:
                                                seen_cite_ids.add(cid)
                        elif ev.get("type") == "refusal":
                            ev_neighbors = ev.get("neighbors")
                            if isinstance(ev_neighbors, list):
                                for c in ev_neighbors:
                                    if isinstance(c, dict):
                                        cid = str(c.get("chunk_id") or c.get("document_id") or "")
                                        if not cid or cid not in seen_cite_ids:
                                            combined_citations.append(c)
                                            if cid:
                                                seen_cite_ids.add(cid)

                    case_eval = evaluate_case_leaks(
                        case,
                        combined_hits,
                        combined_citations,
                        doc_id_to_key,
                        refused=bool(agent_out.get("refused")),
                    )
                    case_results.append(case_eval)
            finally:
                # Reset isolated eval documents and remove temporary group memberships
                await session.execute(delete(GroupMember).where(GroupMember.user_id == actor.id))
                for doc_key, doc in doc_by_key.items():
                    doc.visibility = default_visibilities.get(doc_key, "lab")
                    await session.execute(
                        delete(DocumentACL).where(DocumentACL.document_id == doc.id)
                    )
                for grp_id in created_group_ids:
                    grp_obj = await session.get(Group, grp_id)
                    if grp_obj is not None:
                        await session.delete(grp_obj)
                await session.commit()
    finally:
        await llm.aclose()
        await embedder.aclose()

    summary = summarize_acl_results(case_results)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_sha = _git_sha(root)

    report: dict[str, Any] = {
        "label": f"{slug}-acl-v1",
        "dataset": "v1",
        "suite": "acl",
        "generated_at": generated_at,
        "git_sha": git_sha,
        "embedder_fingerprint": fingerprint,
        "embedder": settings.embedder,
        "llm_provider": agent_settings.llm_provider,
        "llm_model": agent_settings.llm_model,
        "total_cases": summary["total_cases"],
        "leaked_cases": summary["leaked_cases"],
        "retrieval_leaks": summary["retrieval_leaks"],
        "citation_leaks": summary["citation_leaks"],
        "by_category": summary["by_category"],
        "by_role": summary["by_role"],
        "cases": case_results,
    }

    target_path = repo_acl_path if commit else runtime_acl_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "embedder_fingerprint": fingerprint,
                "total_cases": summary["total_cases"],
                "leaked_cases": summary["leaked_cases"],
                "retrieval_leaks": summary["retrieval_leaks"],
                "citation_leaks": summary["citation_leaks"],
                "by_category": summary["by_category"],
                "by_role": summary["by_role"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if check:
        if not repo_acl_path.is_file():
            print(f"Missing committed ACL baseline report: {repo_acl_path}", file=sys.stderr)
            await dispose_engine()
            raise SystemExit(3)
        committed = json.loads(repo_acl_path.read_text(encoding="utf-8"))
        for metric_key in ("leaked_cases", "retrieval_leaks", "citation_leaks"):
            if int(committed.get(metric_key, -1)) != 0:
                await dispose_engine()
                raise SystemExit(
                    f"Committed ACL report {repo_acl_path} has non-zero {metric_key}: {committed.get(metric_key)}"
                )
        if int(committed.get("total_cases", 0)) != summary["total_cases"]:
            await dispose_engine()
            raise SystemExit(
                f"ACL total_cases drifted: current={summary['total_cases']} committed={committed.get('total_cases')}"
            )
        if committed.get("by_category") != summary["by_category"]:
            await dispose_engine()
            raise SystemExit("ACL by_category breakdown drifted from committed report")
        if committed.get("by_role") != summary["by_role"]:
            await dispose_engine()
            raise SystemExit("ACL by_role breakdown drifted from committed report")

    if (
        summary["leaked_cases"] > 0
        or summary["retrieval_leaks"] > 0
        or summary["citation_leaks"] > 0
    ):
        await dispose_engine()
        raise SystemExit(
            f"ACL leakage detected: leaked_cases={summary['leaked_cases']} "
            f"retrieval_leaks={summary['retrieval_leaks']} citation_leaks={summary['citation_leaks']}"
        )

    await dispose_engine()
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run ACL leakage evaluation.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write ACL evaluation report to evals/results/acl/<slug>.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify 0 leaks and check against committed ACL report in evals/results/acl/<slug>.json",
    )
    args = parser.parse_args(argv)
    command.upgrade(Config(str(repo_root() / "backend" / "alembic.ini")), "head")
    asyncio.run(_run(commit=args.commit, check=args.check))


if __name__ == "__main__":
    main()
