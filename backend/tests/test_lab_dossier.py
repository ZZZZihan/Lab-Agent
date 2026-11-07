from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
from app.db import get_engine, get_sessionmaker
from app.importer.matching import (
    classify_achievement_for_person,
    normalize_person_name,
    split_names,
)
from app.models import (
    ImportJob,
    LabAchievement,
    LabPerson,
    LabProject,
)
from httpx import AsyncClient
from sqlalchemy import event
from tests.lab_v22_helpers import (
    _seed_user_and_token,
)

REAL_SAMPLE_DOCX = Path(
    "/home/carryz/.local/share/alma/herdr/uploads/20261106-054942-6cb60859/"
    "教育考试专属大模型构建与安全应用研究_申请书.docx"
)


# ---------------------------------------------------------------------------
# Pure-Python Synthetic Image & .docx Builders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_dossier_attribution_and_query_budget(
    client: AsyncClient,
) -> None:
    # Unit-test normalize_person_name and split_names edge cases
    assert normalize_person_name("万 振 凯") == "万振凯"
    assert normalize_person_name("郭　建民") == "郭建民"
    assert normalize_person_name("郭建民（负责人）") == "郭建民"
    assert normalize_person_name("万振凯*") == "万振凯"
    assert normalize_person_name("徐国伟(通讯作者)") == "徐国伟"
    assert normalize_person_name("买买提•艾力") == "买买提·艾力"
    assert split_names("万振凯、郭建民*、徐国伟(通讯作者)、等") == [
        "万振凯",
        "郭建民",
        "徐国伟",
    ]
    assert split_names("Alice Smith and Bob Jones & 张三") == [
        "alicesmith",
        "bobjones",
        "张三",
    ]
    assert split_names("张三、欧阳 娜娜") == ["张三", "欧阳娜娜"]
    assert split_names("Roland, et al.") == ["roland"]

    # Unit-test classify_achievement_for_person preserving original authors_text position
    # when first_author metadata differs from or is absent in authors_text (Spec v2.2 §4.1)
    ach_override_in_list = LabAchievement(
        kind="paper",
        title="一作覆盖但保留原位论文",
        authors_text="张三、李四",
        first_author="李四",
        raw_text="张三、李四: 一作覆盖但保留原位论文",
        parse_quality="structured",
        entity_fingerprint="f1" * 32,
        as_of_time=date(2026, 4, 26),
    )
    assert classify_achievement_for_person(ach_override_in_list, "张三") == (
        "first_author",
        1,
    )
    assert classify_achievement_for_person(ach_override_in_list, "李四") == (
        "first_author",
        2,
    )

    ach_override_outside_list = LabAchievement(
        kind="paper",
        title="列表外一作覆盖不降级不偏移论文",
        authors_text="张三、李四",
        first_author="王五",
        raw_text="张三、李四: 列表外一作覆盖不降级不偏移论文",
        parse_quality="structured",
        entity_fingerprint="f2" * 32,
        as_of_time=date(2026, 4, 26),
    )
    assert classify_achievement_for_person(ach_override_outside_list, "张三") == (
        "first_author",
        1,
    )
    assert classify_achievement_for_person(ach_override_outside_list, "李四") == (
        "coauthor",
        2,
    )
    assert classify_achievement_for_person(ach_override_outside_list, "王五") == (
        "first_author",
        1,
    )

    _, admin_headers = await _seed_user_and_token(role="admin", login_name="dos_admin")
    zhang_user, zhang_headers = await _seed_user_and_token(
        role="member", login_name="zhangsan", display_name="张三"
    )
    _, other_headers = await _seed_user_and_token(
        role="member", login_name="lisi", display_name="李四"
    )

    maker = get_sessionmaker()
    async with maker() as session:
        job = ImportJob(
            uploaded_by=zhang_user.id,
            original_filename="dossier.docx",
            content_sha256="d" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.flush()

        p_zhang = LabPerson(
            user_id=zhang_user.id,
            name="张三",
            title="教授",
            affiliation="天津工业大学",
            project_role="applicant",
            entity_fingerprint="a1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p_zhangsanfeng = LabPerson(
            name="张三丰",
            title="副教授",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p_special = LabPerson(
            name="王%_测试",
            title="讲师",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p_zhang, p_zhangsanfeng, p_special])
        await session.flush()

        # Projects:
        # 1. PI match with annotation & space ("张 三（负责人）") + also listed in participants -> deduplicated as "pi"
        proj_pi = LabProject(
            title="张三主持且在参与人列表的项目",
            funder="国家自然科学基金",
            grant_no="NSFC01",
            pi_name="张 三（负责人）",
            lab_role="host",
            start_date=date(2025, 1, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《张三主持且在参与人列表的项目》，张 三（负责人）主持，主要参与人：张三、李四",
            entity_fingerprint="b1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 2. Participant match
        proj_part = LabProject(
            title="李四主持张三参与的项目",
            funder="天津市教委",
            grant_no="TJ02",
            pi_name="李四",
            lab_role="participant",
            start_date=date(2026, 1, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《李四主持张三参与的项目》，李四主持，参与人：王五、张三*、赵六",
            entity_fingerprint="b2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 3. False positive candidate: 张三丰 (must NOT match 张三!)
        proj_false = LabProject(
            title="张三丰主持的项目",
            funder="教育部",
            grant_no="MOE03",
            pi_name="张三丰",
            lab_role="host",
            start_date=date(2026, 3, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《张三丰主持的项目》，张三丰主持，参与人：张三丰、李四",
            entity_fingerprint="b3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([proj_pi, proj_part, proj_false])

        # Achievements:
        # 1. First author (2025)
        ach_first = LabAchievement(
            kind="paper",
            title="张三第一作者论文",
            first_author="张 三*",
            authors_text="张 三*, 李四, 王五",
            venue="计算机学报",
            year=2025,
            raw_text="张三(第一):《张三第一作者论文》,2025。",
            parse_quality="structured",
            entity_fingerprint="c1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 2. Coauthor position 3 (2025)
        ach_co = LabAchievement(
            kind="paper",
            title="张三第三作者论文",
            first_author="李四",
            authors_text="李四、王五、张三(通讯作者)、等",
            venue="软件学报",
            year=2025,
            raw_text="李四:《张三第三作者论文》,2025。",
            parse_quality="structured",
            entity_fingerprint="c2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 3. False positive: 张三丰 (must NOT match 张三!)
        ach_false = LabAchievement(
            kind="paper",
            title="张三丰第一作者论文",
            first_author="张三丰",
            authors_text="张三丰, 李四",
            venue="自动化学报",
            year=2026,
            raw_text="张三丰:《张三丰第一作者论文》,2026。",
            parse_quality="structured",
            entity_fingerprint="c3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([ach_first, ach_co, ach_false])
        await session.commit()
        zhang_id = str(p_zhang.id)
        special_id = str(p_special.id)

    # Measure SQL query count for dossier endpoint (must be <= 4 and never load signature_data)
    executed_sqls: list[str] = []
    sync_engine = get_engine().sync_engine

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        executed_sqls.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        r_dos = await client.get(
            f"/api/v1/lab/people/{zhang_id}/dossier", headers=zhang_headers
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert r_dos.status_code == 200
    dossier_sqls = [
        s
        for s in executed_sqls
        if any(t in s for t in ("lab_people", "lab_projects", "lab_achievements"))
    ]
    assert len(dossier_sqls) <= 4
    assert all("signature_data" not in s for s in executed_sqls)

    dos = r_dos.json()
    assert dos["can_edit"] is True
    assert dos["person"]["can_edit"] is True
    assert dos["name_ambiguous"] is False
    assert dos["person"]["name_ambiguous"] is False
    assert dos["counts"]["projects_total"] == 2
    assert dos["counts"]["projects_as_pi"] == 1
    assert dos["counts"]["projects_as_participant"] == 1
    assert dos["counts"]["outputs_total"] == 2
    assert dos["counts"]["outputs_first_author"] == 1
    assert dos["counts"]["outputs_coauthor"] == 1

    # Verify ordering: PI project first, then participant project; 张三丰 excluded
    assert [p["relation_role"] for p in dos["projects"]] == ["pi", "participant"]
    assert all(p["match"] == "exact" for p in dos["projects"])
    assert all("张三丰" not in p["title"] for p in dos["projects"])

    # Verify outputs ordering: same year 2025 -> first_author before coauthor; 张三丰 excluded
    assert [o["relation_role"] for o in dos["outputs"]] == ["first_author", "coauthor"]
    assert [o["author_position"] for o in dos["outputs"]] == [1, 3]
    assert all(o["match"] == "exact" for o in dos["outputs"])
    assert all("张三丰" not in o["title"] for o in dos["outputs"])

    # Check can_edit=False for other member, and wildcard injection safety for `王%_测试`
    r_other_dos = await client.get(
        f"/api/v1/lab/people/{zhang_id}/dossier", headers=other_headers
    )
    assert r_other_dos.status_code == 200
    assert r_other_dos.json()["can_edit"] is False

    r_spec_dos = await client.get(
        f"/api/v1/lab/people/{special_id}/dossier", headers=admin_headers
    )
    assert r_spec_dos.status_code == 200
    assert r_spec_dos.json()["counts"]["projects_total"] == 0
    assert r_spec_dos.json()["counts"]["outputs_total"] == 0

    r_404 = await client.get(
        f"/api/v1/lab/people/{uuid.uuid4()}/dossier", headers=admin_headers
    )
    assert r_404.status_code == 404

    # Regression check: POST /lab/projects with pi_name="张三" and lab_role="participant" (omitting raw_text)
    # classifies as "participant" in 张三's dossier
    r_create_part_proj = await client.post(
        "/api/v1/lab/projects",
        headers=admin_headers,
        json={
            "title": "手动创建的参与课题（无自由文本）",
            "funder": "天津市科技局",
            "grant_no": "TJ-PART-2026",
            "pi_name": "张三",
            "lab_role": "participant",
            "status": "ongoing",
        },
    )
    assert r_create_part_proj.status_code == 200, r_create_part_proj.text
    r_dos_after_part = await client.get(
        f"/api/v1/lab/people/{zhang_id}/dossier", headers=zhang_headers
    )
    assert r_dos_after_part.status_code == 200
    dos_after_part = r_dos_after_part.json()
    assert dos_after_part["counts"]["projects_as_pi"] == 1
    assert dos_after_part["counts"]["projects_as_participant"] == 2

    # Regression check: Latin/English name with spaces ("John Smith") recalled by SQL ILIKE prefilter and matched
    async with maker() as session:
        p_john = LabPerson(
            name="John Smith",
            title="Visiting Professor",
            affiliation="Tianjin Polytechnic University",
            project_role="member",
            entity_fingerprint="a4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        proj_john = LabProject(
            title="Cross-Modal Reasoning",
            funder="NSFC",
            grant_no="INT-2026",
            pi_name="john smith",
            lab_role="host",
            status="ongoing",
            status_source="explicit",
            raw_text="Cross-Modal Reasoning, PI: john smith",
            entity_fingerprint="b4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach_john = LabAchievement(
            kind="paper",
            title="Spaced Name Attribution Paper",
            first_author="john smith",
            authors_text="john smith, 张三",
            venue="IEEE TPAMI",
            year=2026,
            raw_text="john smith: Spaced Name Attribution Paper, 2026.",
            parse_quality="structured",
            entity_fingerprint="c4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p_john, proj_john, ach_john])
        await session.commit()
        john_id = str(p_john.id)

    r_john_dos = await client.get(
        f"/api/v1/lab/people/{john_id}/dossier", headers=admin_headers
    )
    assert r_john_dos.status_code == 200
    john_dos = r_john_dos.json()
    assert john_dos["counts"]["projects_total"] == 1
    assert john_dos["counts"]["projects_as_pi"] == 1
    assert john_dos["counts"]["outputs_total"] == 1
    assert john_dos["counts"]["outputs_first_author"] == 1

    # Regression check: middle-dot variant ("买买提·艾力" vs "买买提•艾力") and fullwidth Latin variant
    async with maker() as session:
        p_maimaiti = LabPerson(
            name="买买提·艾力",
            title="副教授",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a6" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach_maimaiti = LabAchievement(
            kind="paper",
            title="Middle Dot Variant Attribution Paper",
            first_author="买买提•艾力",
            authors_text="买买提•艾力, 张三",
            venue="自动化学报",
            year=2026,
            raw_text="买买提•艾力: Middle Dot Variant Attribution Paper, 2026.",
            parse_quality="structured",
            entity_fingerprint="c6" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach_john_fw = LabAchievement(
            kind="paper",
            title="Fullwidth Latin Author Variant Paper",
            first_author="Ｊｏｈｎ Ｓｍｉｔｈ",
            authors_text="Ｊｏｈｎ Ｓｍｉｔｈ",
            venue="IEEE TKDE",
            year=2026,
            raw_text="Ｊｏｈｎ Ｓｍｉｔｈ: Fullwidth Latin Author Variant Paper, 2026.",
            parse_quality="structured",
            entity_fingerprint="c7" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p_maimaiti, ach_maimaiti, ach_john_fw])
        await session.commit()
        maimaiti_id = str(p_maimaiti.id)

    r_mmt_dos = await client.get(
        f"/api/v1/lab/people/{maimaiti_id}/dossier", headers=admin_headers
    )
    assert r_mmt_dos.status_code == 200
    assert r_mmt_dos.json()["counts"]["outputs_first_author"] == 1

    r_john_fw_dos = await client.get(
        f"/api/v1/lab/people/{john_id}/dossier", headers=admin_headers
    )
    assert r_john_fw_dos.status_code == 200
    assert r_john_fw_dos.json()["counts"]["outputs_first_author"] == 2

    # Duplicate normalized name ("张 三") triggers name_ambiguous=True and match="ambiguous"
    async with maker() as session:
        p_zhang_dup = LabPerson(
            name="张 三",
            title="讲师",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a5" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add(p_zhang_dup)
        await session.commit()

    r_ambig_dos = await client.get(
        f"/api/v1/lab/people/{zhang_id}/dossier", headers=zhang_headers
    )
    assert r_ambig_dos.status_code == 200
    ambig_dos = r_ambig_dos.json()
    assert ambig_dos["name_ambiguous"] is True
    assert ambig_dos["person"]["name_ambiguous"] is True
    assert all(p["match"] == "ambiguous" for p in ambig_dos["projects"])
    assert all(o["match"] == "ambiguous" for o in ambig_dos["outputs"])


# ---------------------------------------------------------------------------
# 7. Proposal Reuse Basket: Batch Format, Signatures ZIP, Selected .docx (§6 & §8.2)
# ---------------------------------------------------------------------------


