"""Tests for synthetic Qingheng Lab showcase seeding (app.seed_demo_lab)."""

from __future__ import annotations

from app.db import get_sessionmaker
from app.importer.pii import assert_no_pii
from app.importer.provenance import build_merge_preview
from app.importer.signature import validate_signature_image
from app.models import (
    ApiToken,
    AuditEvent,
    Conversation,
    FieldProvenance,
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
)
from app.seed import seed
from app.seed_demo_lab import make_calligraphic_signature_png
from sqlalchemy import select


def test_make_calligraphic_signature_png_valid() -> None:
    png_a = make_calligraphic_signature_png(11)
    png_b = make_calligraphic_signature_png(23)
    assert png_a != png_b
    meta_a = validate_signature_image(png_a)
    assert meta_a.mime == "image/png"
    assert meta_a.width == 220
    assert meta_a.height == 76


async def test_seed_with_lab_demo_populates_synthetic_qingheng_fixtures(
    client: object,
) -> None:
    _ = client  # ensure test database fixture is initialized
    await seed(include_lab_demo=True)
    # Second run must be idempotent
    await seed(include_lab_demo=True)

    maker = get_sessionmaker()
    async with maker() as session:
        people = list(await session.scalars(select(LabPerson)))
        projects = list(await session.scalars(select(LabProject)))
        achievements = list(await session.scalars(select(LabAchievement)))
        facilities = list(await session.scalars(select(LabFacility)))
        jobs = list(await session.scalars(select(ImportJob)))
        prov_rows = list(await session.scalars(select(FieldProvenance)))
        convs = list(await session.scalars(select(Conversation)))
        tokens = list(await session.scalars(select(ApiToken)))
        audits = list(await session.scalars(select(AuditEvent)))

        assert len(people) == 8
        assert sum(1 for p in people if p.signature_sha256) == 7
        names = {p.name for p in people}
        assert names == {
            "陈予安",
            "沈昭",
            "陆星河",
            "林可",
            "顾清辞",
            "周宁",
            "苏景行",
            "许知微",
        }
        for p in people:
            assert_no_pii(p.name)
            assert_no_pii(p.research_specialty or "")
            assert_no_pii(p.bio or "")

        assert len(projects) == 5
        assert len(achievements) == 5
        assert len(facilities) == 3
        assert len(jobs) == 3
        assert {j.status for j in jobs} == {"provisioned", "merged", "parsed"}
        assert any(r.source_kind == "manual" for r in prov_rows)
        assert len(convs) == 2
        assert len(tokens) == 2
        assert len(audits) >= 11

        parsed_job = next(j for j in jobs if j.status == "parsed")
        preview_diff = await build_merge_preview(
            session,
            job_id=parsed_job.id,
            as_of_time_override=parsed_job.as_of_time,
        )
        changes = preview_diff["changes"]
        added_entities = preview_diff["added_entities"]
        assert isinstance(changes, list)
        assert isinstance(added_entities, list)
        assert len(added_entities) == 1
        categories = {item["category"] for item in changes}
        assert "updated" in categories
        assert "fill_blank" in categories
        assert "manual_protected" in categories
