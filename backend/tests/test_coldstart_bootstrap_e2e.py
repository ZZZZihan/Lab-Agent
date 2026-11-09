"""Wave 3 End-to-End Acceptance Tests: First-Admin Bootstrap -> Multi-Proposal Batch Cold-Start -> Temporal Merge -> Downstream Usage."""

from __future__ import annotations

import io

import docx
from app.config import get_settings
from app.db import get_sessionmaker
from app.importer.fingerprint import person_fp
from app.models import LabPerson, User
from httpx import AsyncClient
from sqlalchemy import delete, select

PI_NAME = "沈昭"
PI_STAFF_NO = "20180042"
PI_EMAIL = "admin@lab.edu.cn"
PI_PASSWORD = "StrongPassword#2026"
INITIAL_MEMBER_PW = "ColdStart-Member-2026!"


def _build_proposal_docx(
    *,
    project_title: str,
    applicant_name: str,
    applicant_title: str = "教授",
    applicant_affiliation: str = "天津工业大学",
    applicant_email: str = "",
    fill_date: str = "2024年3月15日",
    members: list[tuple[str, str, str, str, str]],
    foundation_text: str | None = None,
) -> bytes:
    """Build a synthetic proposal .docx with configurable applicant, members, and foundation text."""
    doc = docx.Document()

    t1 = doc.add_table(rows=4, cols=2)
    t1.cell(0, 0).text = "项目名称"
    t1.cell(0, 1).text = project_title
    t1.cell(1, 0).text = "申请人"
    t1.cell(1, 1).text = applicant_name
    t1.cell(2, 0).text = "所在单位"
    t1.cell(2, 1).text = applicant_affiliation
    t1.cell(3, 0).text = "填表日期"
    t1.cell(3, 1).text = fill_date

    t2 = doc.add_table(rows=3 + len(members), cols=6)
    t2.cell(0, 0).text = "申请人姓名"
    t2.cell(0, 1).text = applicant_name
    t2.cell(0, 2).text = "专业技术职务"
    t2.cell(0, 3).text = applicant_title
    t2.cell(0, 4).text = "所在单位"
    t2.cell(0, 5).text = applicant_affiliation

    t2.cell(1, 0).text = "最终学位"
    t2.cell(1, 1).text = "博士"
    t2.cell(1, 2).text = "研究专长"
    t2.cell(1, 3).text = "人工智能"
    t2.cell(1, 4).text = "电子信箱"
    t2.cell(1, 5).text = applicant_email

    headers = ["序号", "姓名", "证件号码", "专业技术职务", "所在单位", "电子信箱"]
    for c, h in enumerate(headers):
        t2.cell(2, c).text = h
    for idx, (m_name, m_cert, m_title, m_aff, m_email) in enumerate(members, start=1):
        row = 2 + idx
        t2.cell(row, 0).text = str(idx)
        t2.cell(row, 1).text = m_name
        t2.cell(row, 2).text = m_cert
        t2.cell(row, 3).text = m_title
        t2.cell(row, 4).text = m_aff
        t2.cell(row, 5).text = m_email

    t4 = doc.add_table(rows=1, cols=1)
    t4.cell(0, 0).text = foundation_text or (
        "前期研究基础\n"
        f"1．[学术简历] {applicant_name}，长期从事人工智能与大模型系统研究。\n"
        "2．[前期成果]\n"
        f"（1）{applicant_name}（第一）：《{project_title}前期成果》，中国科学，2024年。\n"
        "3．[承担项目]\n"
        f"（1）《{project_title}先导课题》（AGE240005），{applicant_name}（主持），全国教育科学规划课题，2024年1月立项，在研。\n"
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def _clear_all_users() -> None:
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(delete(User))
        await session.commit()


async def test_full_coldstart_bootstrap_to_batch_provision_and_downstream_usage(
    client: AsyncClient,
) -> None:
    """Full acceptance flow:
    1. Empty DB -> GET /auth/bootstrap-status returns needs_bootstrap=True.
    2. POST /auth/bootstrap-admin creates the initial PI admin account (with staff_no T1 login_name).
    3. GET /auth/bootstrap-status is now permanently closed (needs_bootstrap=False).
    4. Admin uploads 3 multi-year proposals (.docx: 2022, 2023, 2024) with complementary student IDs & emails.
    5. POST /admin/imports/batch/resolve complements T1 student IDs across proposals, tags cluster_id on job.preview.people, and identifies self_candidate for the PI.
    6. POST /admin/imports/batch/provision (submitted out of chronological order, with link_self_cluster_id and a custom login_name override across multiple proposals) provisions all accounts in a single transaction and returns top-level stats + merge_queue.
    7. Phase 2 temporal merge (merge-preview + merge) runs on merge_queue, evolving member titles forward in time without regressing fields.
    8. Downstream verification:
       - /lab/overview returns the populated lab assets & RAG document key;
       - /lab/people/{id}/dossier returns the member's dossier;
       - /lab/basket/format formats selected entities for proposal reuse;
       - Member logs in using their complemented T1 student ID (2021310888) and initial password.
    """
    settings = get_settings()
    orig_env = settings.app_env
    orig_token = settings.bootstrap_token
    orig_oidc = settings.oidc_issuer
    try:
        settings.app_env = "dev"
        settings.bootstrap_token = None
        settings.oidc_issuer = None
        await _clear_all_users()

        # Step 1: Check bootstrap status on empty system
        st_resp = await client.get("/api/v1/auth/bootstrap-status")
        assert st_resp.status_code == 200, st_resp.text
        assert st_resp.json() == {
            "needs_bootstrap": True,
            "setup_token_required": False,
            "oidc_mode": False,
        }

        # Step 2: Register initial PI administrator
        boot_resp = await client.post(
            "/api/v1/auth/bootstrap-admin",
            json={
                "display_name": PI_NAME,
                "email": PI_EMAIL,
                "staff_no": PI_STAFF_NO,
                "password": PI_PASSWORD,
            },
        )
        assert boot_resp.status_code == 200, boot_resp.text
        boot_data = boot_resp.json()
        admin_token = boot_data["access_token"]
        admin_user_id = boot_data["user"]["id"]
        assert boot_data["user"]["role"] == "admin"
        assert boot_data["user"]["login_name"] == PI_STAFF_NO
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # Step 3: Verify bootstrap channel is permanently closed
        st_after = await client.get("/api/v1/auth/bootstrap-status")
        assert st_after.status_code == 200
        assert st_after.json()["needs_bootstrap"] is False

        # Step 4: Upload 3 proposals from 2022, 2023, and 2024
        # - Lin Shuying has NO student_id in 2022 (title "硕士生"), HAS T1 student_id "2021310888" + email in 2023 (title "博士生"),
        #   and NO student_id in 2024 (title "博士候选人").
        # - Zhou Muye appears in 2022 and 2023 without student_id; admin will customize his login_name to "zhoumuye_custom".
        # - Chen Xinghe joins in 2024 with T1 student_id "2024310777".
        docx_2022 = _build_proposal_docx(
            project_title="2022多模态基础模型研究",
            applicant_name=PI_NAME,
            applicant_title="副教授",
            fill_date="2022年3月10日",
            members=[
                ("林疏影", "", "硕士生", "天津工业大学", ""),
                ("周牧野", "", "硕士生", "天津工业大学", ""),
            ],
        )
        docx_2023 = _build_proposal_docx(
            project_title="2023跨模态具身感知关键技术",
            applicant_name=PI_NAME,
            applicant_title="教授",
            applicant_email=PI_EMAIL,
            fill_date="2023年3月15日",
            members=[
                ("林疏影", "2021310888", "博士生", "天津工业大学", "linshuying@lab.local"),
                ("周牧野", "", "博士生", "天津工业大学", "zhoumuye@lab.local"),
            ],
        )
        docx_2024 = _build_proposal_docx(
            project_title="2024通用具身智能大模型平台",
            applicant_name=PI_NAME,
            applicant_title="教授",
            fill_date="2024年3月20日",
            members=[
                ("林疏影", "", "博士候选人", "天津工业大学", ""),
                ("陈星河", "2024310777", "硕士生", "天津工业大学", "chenxinghe@lab.local"),
            ],
        )

        up_2022 = await client.post(
            "/api/v1/admin/imports?as_of_time=2022-03-10",
            headers=admin_headers,
            files={
                "file": (
                    "proposal_2022.docx",
                    docx_2022,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert up_2022.status_code == 200, up_2022.text
        job_2022_id = up_2022.json()["id"]

        up_2023 = await client.post(
            "/api/v1/admin/imports?as_of_time=2023-03-15",
            headers=admin_headers,
            files={
                "file": (
                    "proposal_2023.docx",
                    docx_2023,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert up_2023.status_code == 200, up_2023.text
        job_2023_id = up_2023.json()["id"]

        up_2024 = await client.post(
            "/api/v1/admin/imports?as_of_time=2024-03-20",
            headers=admin_headers,
            files={
                "file": (
                    "proposal_2024.docx",
                    docx_2024,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert up_2024.status_code == 200, up_2024.text
        job_2024_id = up_2024.json()["id"]

        # Step 5: Resolve identities across the 3 proposals
        res_resp = await client.post(
            "/api/v1/admin/imports/batch/resolve",
            headers=admin_headers,
            json={"job_ids": [job_2024_id, job_2022_id, job_2023_id]},
        )
        assert res_resp.status_code == 200, res_resp.text
        res_data = res_resp.json()

        # Verify self_candidate detects PI Shen Zhao
        assert res_data["self_candidate"] is not None
        assert res_data["self_candidate"]["name"] == PI_NAME
        self_cluster_id = res_data["self_candidate"]["cluster_id"]

        # Verify quality metrics and cluster_id presence on job.preview.people
        assert res_data["quality"]["people_total"] == 4
        assert res_data["quality"]["t1_covered"] >= 2
        assert res_data["quality"]["injected_student_ids"] >= 2
        assert res_data["quality"]["injected_emails"] >= 2

        jobs_by_id = {j["id"]: j for j in res_data["jobs"]}
        for jid in (job_2022_id, job_2023_id, job_2024_id):
            for p in jobs_by_id[jid]["preview"]["people"]:
                assert p.get("cluster_id"), f"Missing cluster_id on {p['name']} in job {jid}"

        lin_2022 = next(
            p for p in jobs_by_id[job_2022_id]["preview"]["people"] if p["name"] == "林疏影"
        )
        lin_2024 = next(
            p for p in jobs_by_id[job_2024_id]["preview"]["people"] if p["name"] == "林疏影"
        )
        assert lin_2022["student_id"] == "2021310888"
        assert lin_2022["tier"] == "T1"
        assert lin_2022["login_name"] == "2021310888"
        assert "STUDENT_ID_FROM_SIBLING" in lin_2022["warnings"]
        assert lin_2024["student_id"] == "2021310888"
        assert lin_2024["login_name"] == "2021310888"

        zhou_2022_key = next(
            p["key"] for p in jobs_by_id[job_2022_id]["preview"]["people"] if p["name"] == "周牧野"
        )
        zhou_2023_key = next(
            p["key"] for p in jobs_by_id[job_2023_id]["preview"]["people"] if p["name"] == "周牧野"
        )

        # Step 6: Batch provision (submitted out of order: 2024, 2022, 2023)
        # Also test custom login_name override for Zhou Muye across both 2022 and 2023!
        prov_resp = await client.post(
            "/api/v1/admin/imports/batch/provision",
            headers=admin_headers,
            json={
                "jobs": [
                    {"job_id": job_2024_id, "as_of_time": "2024-03-20"},
                    {
                        "job_id": job_2022_id,
                        "as_of_time": "2022-03-10",
                        "people": [{"key": zhou_2022_key, "login_name": "zhoumuye88"}],
                    },
                    {
                        "job_id": job_2023_id,
                        "as_of_time": "2023-03-15",
                        "people": [{"key": zhou_2023_key, "login_name": "zhoumuye88"}],
                    },
                ],
                "initial_password": INITIAL_MEMBER_PW,
                "include_password": True,
                "link_self_cluster_id": self_cluster_id,
            },
        )
        assert prov_resp.status_code == 200, prov_resp.text
        prov_data = prov_resp.json()

        # Verify top-level aggregated stats & chronological merge_queue
        assert "stats" in prov_data
        assert prov_data["stats"]["users_created"] == 3  # Lin, Zhou, Chen
        assert prov_data["stats"]["users_linked"] == 1  # PI Shen Zhao
        assert prov_data["stats"]["people_inserted"] == 4
        assert prov_data["merge_queue"] == [job_2023_id, job_2024_id]
        assert "2021310888" in prov_data["roster_text"]
        assert "zhoumuye88" in prov_data["roster_text"]
        assert "2024310777" in prov_data["roster_text"]

        # Step 7: Run Phase 2 temporal merge for each job in merge_queue
        for m_jid in prov_data["merge_queue"]:
            mp_resp = await client.post(
                f"/api/v1/admin/imports/{m_jid}/merge-preview",
                headers=admin_headers,
                json={},
            )
            assert mp_resp.status_code == 200, mp_resp.text
            m_exec = await client.post(
                f"/api/v1/admin/imports/{m_jid}/merge",
                headers=admin_headers,
                json={"decisions": []},
            )
            assert m_exec.status_code == 200, m_exec.text

        # Verify database state after temporal merge:
        # - Shen Zhao's title evolved from "副教授" (2022) to "教授" (2023/2024) and is linked to admin_user_id
        # - Lin Shuying's title evolved from "硕士生" (2022) -> "博士生" (2023) -> "博士候选人" (2024)
        maker = get_sessionmaker()
        async with maker() as session:
            people_rows = (await session.execute(select(LabPerson))).scalars().all()
            assert len(people_rows) == 4
            by_name = {p.name: p for p in people_rows}
            assert str(by_name[PI_NAME].user_id) == admin_user_id
            assert by_name[PI_NAME].title == "教授"
            assert by_name["林疏影"].title == "博士候选人"
            assert by_name["林疏影"].entity_fingerprint == person_fp(
                tier="T1",
                login_name="2021310888",
                name="林疏影",
                affiliation="天津工业大学",
            )
            lin_person_id = str(by_name["林疏影"].id)

        # Step 8: Verify downstream Lab Overview, Dossier, Basket Format, and Member T1 Login
        ov_resp = await client.get("/api/v1/lab/overview", headers=admin_headers)
        assert ov_resp.status_code == 200, ov_resp.text
        ov_data = ov_resp.json()
        assert ov_data["counts"]["people"] == 4
        assert ov_data["counts"]["achievements"] >= 3
        assert ov_data["counts"]["projects"] >= 1

        dossier_resp = await client.get(
            f"/api/v1/lab/people/{lin_person_id}/dossier",
            headers=admin_headers,
        )
        assert dossier_resp.status_code == 200, dossier_resp.text
        dossier_data = dossier_resp.json()
        assert dossier_data["person"]["name"] == "林疏影"
        assert dossier_data["person"]["title"] == "博士候选人"

        basket_resp = await client.post(
            "/api/v1/lab/format/batch",
            headers=admin_headers,
            json={
                "items": [{"entity": "person", "id": lin_person_id}],
            },
        )
        assert basket_resp.status_code == 200, basket_resp.text
        assert "林疏影" in basket_resp.json()["combined_text"]

        # Member login with complemented T1 student ID ("2021310888")
        member_login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "2021310888", "password": INITIAL_MEMBER_PW},
        )
        assert member_login.status_code == 200, member_login.text
        member_data = member_login.json()
        assert member_data["user"]["display_name"] == "林疏影"
        assert member_data["user"]["login_name"] == "2021310888"
        assert member_data["user"]["role"] == "member"
        assert member_data["user"]["must_change_password"] is True
    finally:
        settings.app_env = orig_env
        settings.bootstrap_token = orig_token
        settings.oidc_issuer = orig_oidc


async def test_batch_provision_preserves_self_cluster_on_exclusion_and_shared_t1_custom_login(
    client: AsyncClient,
) -> None:
    """Regression test for Codex review findings:
    1. Excluding an earlier cluster (c1) in Step 2 must not shift `link_self_cluster_id` (c2) to link a third person (c3);
       and attempting to link a cluster whose name does not match the admin must fail closed with 422 `link_self_cluster_invalid`.
    2. Customizing `login_name` on a shared T1 person across multiple jobs in a batch must propagate the newly created user_id
       across jobs without raising 409 `login_name_taken` or creating duplicate accounts.
    """
    settings = get_settings()
    orig_env = settings.app_env
    orig_token = settings.bootstrap_token
    orig_oidc = settings.oidc_issuer
    try:
        settings.app_env = "dev"
        settings.bootstrap_token = None
        settings.oidc_issuer = None
        await _clear_all_users()

        boot_resp = await client.post(
            "/api/v1/auth/bootstrap-admin",
            json={
                "display_name": PI_NAME,
                "email": PI_EMAIL,
                "staff_no": PI_STAFF_NO,
                "password": PI_PASSWORD,
            },
        )
        assert boot_resp.status_code == 200, boot_resp.text
        admin_token = boot_resp.json()["access_token"]
        admin_user_id = boot_resp.json()["user"]["id"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # Job 1 has applicant "顾长风" (c1), member PI_NAME "沈昭" (c2), and T1 member "林疏影" (c3, student_id 2021310888)
        docx_1 = _build_proposal_docx(
            project_title="联合基金A课题",
            applicant_name="顾长风",
            applicant_title="教授",
            fill_date="2023年3月10日",
            members=[
                (PI_NAME, "", "教授", "天津工业大学", ""),
                ("林疏影", "2021310888", "博士生", "天津工业大学", ""),
            ],
        )
        # Job 2 has applicant PI_NAME "沈昭" (c2) and T1 member "林疏影" (c3, student_id 2021310888)
        docx_2 = _build_proposal_docx(
            project_title="联合基金B课题",
            applicant_name=PI_NAME,
            applicant_title="教授",
            fill_date="2024年3月10日",
            members=[
                ("林疏影", "2021310888", "博士候选人", "天津工业大学", ""),
            ],
        )

        r1 = await client.post(
            "/api/v1/admin/imports?as_of_time=2023-03-10",
            headers=admin_headers,
            files={
                "file": (
                    "joint_a.docx",
                    docx_1,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r1.status_code == 200, r1.text
        job1_id = r1.json()["id"]

        r2 = await client.post(
            "/api/v1/admin/imports?as_of_time=2024-03-10",
            headers=admin_headers,
            files={
                "file": (
                    "joint_b.docx",
                    docx_2,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r2.status_code == 200, r2.text
        job2_id = r2.json()["id"]

        res_resp = await client.post(
            "/api/v1/admin/imports/batch/resolve",
            headers=admin_headers,
            json={"job_ids": [job1_id, job2_id]},
        )
        assert res_resp.status_code == 200, res_resp.text
        res_data = res_resp.json()

        clusters_by_name = {c["name"]: c for c in res_data["clusters"]}
        assert clusters_by_name["顾长风"]["cluster_id"] == "c1"
        assert clusters_by_name[PI_NAME]["cluster_id"] == "c2"
        assert clusters_by_name["林疏影"]["cluster_id"] == "c3"
        assert res_data["self_candidate"]["cluster_id"] == "c2"

        jobs_by_id = {j["id"]: j for j in res_data["jobs"]}
        gu_key_j1 = next(
            p["key"] for p in jobs_by_id[job1_id]["preview"]["people"] if p["name"] == "顾长风"
        )
        lin_key_j1 = next(
            p["key"] for p in jobs_by_id[job1_id]["preview"]["people"] if p["name"] == "林疏影"
        )
        lin_key_j2 = next(
            p["key"] for p in jobs_by_id[job2_id]["preview"]["people"] if p["name"] == "林疏影"
        )

        # Attempting to link c3 (林疏影) as self_candidate must fail closed with 422 link_self_cluster_invalid
        bad_self_resp = await client.post(
            "/api/v1/admin/imports/batch/provision",
            headers=admin_headers,
            json={
                "jobs": [
                    {"job_id": job1_id, "as_of_time": "2023-03-10"},
                    {"job_id": job2_id, "as_of_time": "2024-03-10"},
                ],
                "initial_password": INITIAL_MEMBER_PW,
                "link_self_cluster_id": "c3",
            },
        )
        assert bad_self_resp.status_code == 422, bad_self_resp.text
        assert bad_self_resp.json()["error"]["code"] == "link_self_cluster_invalid"

        # Now exclude c1 ("顾长风"), keep link_self_cluster_id="c2" (PI_NAME), and set a custom login_name on shared T1 person "林疏影"
        prov_resp = await client.post(
            "/api/v1/admin/imports/batch/provision",
            headers=admin_headers,
            json={
                "jobs": [
                    {
                        "job_id": job1_id,
                        "as_of_time": "2023-03-10",
                        "people": [
                            {"key": gu_key_j1, "exclude": True},
                            {"key": lin_key_j1, "login_name": "lin_custom_t1"},
                        ],
                    },
                    {
                        "job_id": job2_id,
                        "as_of_time": "2024-03-10",
                        "people": [
                            {"key": lin_key_j2, "login_name": "lin_custom_t1"},
                        ],
                    },
                ],
                "initial_password": INITIAL_MEMBER_PW,
                "include_password": True,
                "link_self_cluster_id": "c2",
            },
        )
        assert prov_resp.status_code == 200, prov_resp.text
        prov_data = prov_resp.json()
        assert prov_data["stats"]["users_created"] == 1
        assert prov_data["stats"]["users_linked"] == 1

        maker = get_sessionmaker()
        async with maker() as session:
            people_rows = (await session.execute(select(LabPerson))).scalars().all()
            by_name = {p.name: p for p in people_rows}
            assert "顾长风" not in by_name
            assert str(by_name[PI_NAME].user_id) == admin_user_id
            assert by_name["林疏影"].user_id is not None
            assert str(by_name["林疏影"].user_id) != admin_user_id
            lin_user = await session.get(User, by_name["林疏影"].user_id)
            assert lin_user is not None
            assert lin_user.login_name == "lin_custom_t1"
    finally:
        settings.app_env = orig_env
        settings.bootstrap_token = orig_token
        settings.oidc_issuer = orig_oidc


async def test_batch_provision_preserves_collision_boundaries_and_requires_explicit_decision(
    client: AsyncClient,
) -> None:
    """Three chronological proposals with same name/affiliation (2431101111, missing sid, 2431102222) must not auto-match across clusters."""
    settings = get_settings()
    orig_env = settings.app_env
    orig_token = settings.bootstrap_token
    orig_oidc = settings.oidc_issuer

    try:
        settings.app_env = "dev"
        settings.bootstrap_token = None
        settings.oidc_issuer = None

        await _clear_all_users()

        boot_resp = await client.post(
            "/api/v1/auth/bootstrap-admin",
            json={
                "display_name": PI_NAME,
                "email": PI_EMAIL,
                "staff_no": PI_STAFF_NO,
                "password": PI_PASSWORD,
            },
        )
        assert boot_resp.status_code == 200, boot_resp.text
        admin_token = boot_resp.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        docx_1 = _build_proposal_docx(
            project_title="碰撞边界课题一",
            applicant_name=PI_NAME,
            applicant_email=PI_EMAIL,
            applicant_title="教授",
            fill_date="2023年3月10日",
            members=[
                ("王强", "2431101111", "硕士生", "软件学院", ""),
            ],
        )
        docx_2 = _build_proposal_docx(
            project_title="碰撞边界课题二",
            applicant_name=PI_NAME,
            applicant_email=PI_EMAIL,
            applicant_title="教授",
            fill_date="2024年3月10日",
            members=[
                ("王强", "", "博士生", "软件学院", ""),
            ],
        )
        docx_3 = _build_proposal_docx(
            project_title="碰撞边界课题三",
            applicant_name=PI_NAME,
            applicant_email=PI_EMAIL,
            applicant_title="教授",
            fill_date="2025年3月10日",
            members=[
                ("王强", "2431102222", "讲师", "软件学院", ""),
            ],
        )

        job_ids: list[str] = []
        for idx, (raw_bytes, dt_str) in enumerate(
            ((docx_1, "2023-03-10"), (docx_2, "2024-03-10"), (docx_3, "2025-03-10")),
            start=1,
        ):
            up = await client.post(
                f"/api/v1/admin/imports?as_of_time={dt_str}",
                headers=admin_headers,
                files={
                    "file": (
                        f"col_bound_{idx}.docx",
                        raw_bytes,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert up.status_code == 200, up.text
            job_ids.append(up.json()["id"])

        res_resp = await client.post(
            "/api/v1/admin/imports/batch/resolve",
            headers=admin_headers,
            json={"job_ids": job_ids},
        )
        assert res_resp.status_code == 200, res_resp.text
        res_data = res_resp.json()
        wq_clusters = [c for c in res_data["clusters"] if c["name"] == "王强"]
        assert len(wq_clusters) == 3

        # 1. Attempting to provision without an explicit decision on the middle T2 '王强' must fail with 422
        fail_prov = await client.post(
            "/api/v1/admin/imports/batch/provision",
            headers=admin_headers,
            json={
                "job_ids": job_ids,
                "initial_password": INITIAL_MEMBER_PW,
                "include_password": True,
            },
        )
        assert fail_prov.status_code == 422, fail_prov.text
        assert fail_prov.json()["error"]["code"] == "login_conflict_unresolved"

        # 2. Providing an explicit separate login_name decision for the middle proposal's '王强' succeeds
        # and keeps all three '王强' clusters separated through provisioning and Step 4 merge.
        j2_wq_key = next(
            p["key"]
            for p in res_data["jobs"][1]["preview"]["people"]
            if p["name"] == "王强"
        )
        ok_prov = await client.post(
            "/api/v1/admin/imports/batch/provision",
            headers=admin_headers,
            json={
                "jobs": [
                    {"job_id": job_ids[0], "as_of_time": "2023-03-10"},
                    {
                        "job_id": job_ids[1],
                        "as_of_time": "2024-03-10",
                        "people": [{"key": j2_wq_key, "login_name": "wangqiang2024"}],
                    },
                    {"job_id": job_ids[2], "as_of_time": "2025-03-10"},
                ],
                "initial_password": INITIAL_MEMBER_PW,
                "include_password": True,
            },
        )
        assert ok_prov.status_code == 200, ok_prov.text
        ok_data = ok_prov.json()
        assert ok_data["stats"]["users_created"] == 3

        for m_jid in ok_data["merge_queue"]:
            m_res = await client.post(
                f"/api/v1/admin/imports/{m_jid}/merge",
                headers=admin_headers,
                json={"decisions": []},
            )
            assert m_res.status_code == 200, m_res.text

        maker = get_sessionmaker()
        async with maker() as session:
            wq_people = list(
                await session.scalars(
                    select(LabPerson).where(LabPerson.name == "王强")
                )
            )
            assert len(wq_people) == 3
            titles_by_login: dict[str, str | None] = {}
            for lp in wq_people:
                assert lp.user_id is not None
                u = await session.get(User, lp.user_id)
                assert u is not None and u.login_name is not None
                titles_by_login[u.login_name] = lp.title
            assert titles_by_login == {
                "2431101111": "硕士生",
                "wangqiang2024": "博士生",
                "2431102222": "讲师",
            }
    finally:
        settings.app_env = orig_env
        settings.bootstrap_token = orig_token
        settings.oidc_issuer = orig_oidc

