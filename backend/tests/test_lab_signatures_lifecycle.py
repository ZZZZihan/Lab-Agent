from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from app.db import get_sessionmaker
from app.models import (
    ImportJob,
    LabPerson,
)
from httpx import AsyncClient
from sqlalchemy import select
from tests.lab_v22_helpers import (
    _seed_user_and_token,
    build_proposal_docx_with_10_signatures,
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
async def test_import_confirm_signatures_lifecycle(client: AsyncClient) -> None:
    _, admin_headers = await _seed_user_and_token(role="admin")
    raw_docx = build_proposal_docx_with_10_signatures()

    # 1) Upload & parse: preview response must NOT contain _signature_blobs or >512 char base64
    r_up = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r_up.status_code == 200
    up_json = r_up.json()
    job_id = up_json["job_id"]
    assert "_signature_blobs" not in up_json["preview"]
    assert len(up_json["preview"]["signatures"]) == 10
    raw_resp_text = r_up.text
    assert "_signature_blobs" not in raw_resp_text

    # Confirm 1st time -> saved == 10, _signature_blobs cleared from DB session
    r_conf1 = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        headers=admin_headers,
        json={},
    )
    assert r_conf1.status_code == 200
    sig_res1 = r_conf1.json()["signature_result"]
    assert sig_res1 == {
        "saved": 10,
        "unchanged": 0,
        "kept_upload": 0,
        "skipped": 0,
        "rejected": 0,
        "person_not_imported": 0,
    }

    maker = get_sessionmaker()
    async with maker() as session:
        db_job = await session.get(ImportJob, uuid.UUID(job_id))
        assert db_job is not None
        assert "_signature_blobs" not in (db_job.preview or {})
        job1_pre_sigs = (db_job.stats or {}).get("_pre_merge_signatures")
        assert isinstance(job1_pre_sigs, dict)
        people_rows = list(await session.scalars(select(LabPerson)))
        assert len(people_rows) == 10
        for lp in people_rows:
            assert lp.signature_mime in ("image/png", "image/jpeg")
            assert lp.signature_sha256 is not None and len(lp.signature_sha256) == 64
            assert lp.signature_updated_at is not None
            assert lp.signature_source == "docx_import"
            assert job1_pre_sigs.get(str(lp.id)) == {
                "signature_data_b64": None,
                "signature_mime": None,
                "signature_sha256": None,
                "signature_updated_at": None,
                "signature_source": None,
                "prev_writer_job_id": None,
                "applied_sha256": lp.signature_sha256,
            }
        first_updated_map = {lp.id: lp.signature_updated_at for lp in people_rows}
        wan_person = next(lp for lp in people_rows if lp.name == "万振凯")
        wan_id = str(wan_person.id)

    # 2) Re-upload same .docx and confirm -> unchanged == 10, signature_updated_at unchanged
    r_up2 = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_reimport.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job2_id = r_up2.json()["job_id"]
    r_conf2 = await client.post(
        f"/api/v1/admin/imports/{job2_id}/provision",
        headers=admin_headers,
        json={},
    )
    assert r_conf2.status_code == 200
    assert r_conf2.json()["signature_result"]["unchanged"] == 10
    async with maker() as session:
        for lp in await session.scalars(select(LabPerson)):
            assert lp.signature_updated_at == first_updated_map[lp.id]

    # 3) Manual PUT /signature on 万振凯 (source='upload'), then re-import with skip_signature_ids,
    #    excluded person, and tampered blob
    manual_png = make_png_bytes(100, 45, fill_byte=0x33)
    r_put = await client.put(
        f"/api/v1/lab/people/{wan_id}/signature",
        headers=admin_headers,
        files={"file": ("manual.png", manual_png, "image/png")},
    )
    assert r_put.status_code == 200
    assert r_put.json()["signature_source"] == "upload"
    manual_sha = r_put.json()["signature_sha256"]

    r_up3 = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_v3.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job3_id = r_up3.json()["job_id"]
    sigs3 = r_up3.json()["preview"]["signatures"]
    p4_sha = next(s["sha256"] for s in sigs3 if s["person_temp_id"] == "p4")

    # Tamper with p4's blob in DB so p4 triggers `rejected`
    async with maker() as session:
        db_job3 = await session.get(ImportJob, uuid.UUID(job3_id))
        assert db_job3 is not None
        prev3 = dict(db_job3.preview or {})
        blobs3 = dict(prev3.get("_signature_blobs") or {})
        blobs3[p4_sha] = "not_valid_base64_!!!"
        prev3["_signature_blobs"] = blobs3
        db_job3.preview = prev3
        await session.commit()

    # Confirm job3 with:
    # - p2 (万振凯) has manual upload -> kept_upload = 1
    # - p3 in skip_signature_ids -> skipped = 1
    # - p4 has tampered blob -> rejected = 1
    # - p5 excluded from people -> person_not_imported = 1
    # - remaining 6 people -> unchanged = 6
    r_conf3 = await client.post(
        f"/api/v1/admin/imports/{job3_id}/provision",
        headers=admin_headers,
        json={
            "skip_signature_ids": ["p3"],
            "people": [{"key": "p5", "exclude": True}],
        },
    )
    assert r_conf3.status_code == 200
    assert "_pre_merge_signatures" not in r_conf3.json()["stats"]
    assert r_conf3.json()["signature_result"] == {
        "saved": 0,
        "unchanged": 6,
        "kept_upload": 1,
        "skipped": 1,
        "rejected": 1,
        "person_not_imported": 1,
    }

    # Verify 万振凯's manual signature was never overwritten
    async with maker() as session:
        wan_after = await session.get(LabPerson, uuid.UUID(wan_id))
        assert wan_after is not None
        assert wan_after.signature_source == "upload"
        assert wan_after.signature_sha256 == manual_sha
        guo_before_merge = next(
            lp for lp in await session.scalars(select(LabPerson)) if lp.name == "郭建民"
        )
        guo_id = str(guo_before_merge.id)
        guo_orig_sha = guo_before_merge.signature_sha256
        # Clear 郭建民's signature before testing merge + revert restoration
        guo_before_merge.signature_data = None
        guo_before_merge.signature_mime = None
        guo_before_merge.signature_sha256 = None
        guo_before_merge.signature_updated_at = None
        guo_before_merge.signature_source = None
        await session.commit()

    # 4) Merge a new job onto existing people -> 郭建民 gets docx_import signature; then revert -> 郭建民's signature is cleared back to None while 万振凯's manual upload stays intact
    r_up4 = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_merge_revert.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job4_id = r_up4.json()["job_id"]
    r_merge4 = await client.post(
        f"/api/v1/admin/imports/{job4_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge4.status_code == 200, r_merge4.text
    async with maker() as session:
        guo_merged = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_merged is not None
        assert guo_merged.signature_sha256 == guo_orig_sha
        assert guo_merged.signature_source == "docx_import"

    r_rev4 = await client.post(
        f"/api/v1/admin/imports/{job4_id}/revert",
        headers=admin_headers,
    )
    assert r_rev4.status_code == 200, r_rev4.text
    async with maker() as session:
        guo_reverted = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_reverted is not None
        assert guo_reverted.signature_sha256 is None
        assert guo_reverted.signature_source is None
        wan_still_upload = await session.get(LabPerson, uuid.UUID(wan_id))
        assert wan_still_upload is not None
        assert wan_still_upload.signature_source == "upload"
        assert wan_still_upload.signature_sha256 == manual_sha

    # 4b) Re-merge the same reverted job4 -> signature blobs are preserved and reapplied (no missing_blob rejection)
    r_remerge4 = await client.post(
        f"/api/v1/admin/imports/{job4_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_remerge4.status_code == 200, r_remerge4.text
    remerge4_sig = r_remerge4.json()["signature_result"]
    assert remerge4_sig["saved"] >= 1
    assert remerge4_sig["rejected"] == 0
    async with maker() as session:
        guo_remerged = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_remerged is not None
        assert guo_remerged.signature_sha256 == guo_orig_sha
        assert guo_remerged.signature_source == "docx_import"

    # Revert job4 again so step 5 starts with guo having no signature
    r_rev4_again = await client.post(
        f"/api/v1/admin/imports/{job4_id}/revert",
        headers=admin_headers,
    )
    assert r_rev4_again.status_code == 200, r_rev4_again.text

    # 5) Out-of-order revert: merge job A (None -> S1) then merge job B (S1 -> S2);
    #    reverting job A first rebases job B's snapshot to None, so reverting job B restores None (not S1)
    r_up_a = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_chain_a.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job_a_id = r_up_a.json()["job_id"]
    r_merge_a = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a.status_code == 200, r_merge_a.text

    r_up_b = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_chain_b.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job_b_id = r_up_b.json()["job_id"]
    # Replace p1 (郭建民) signature in job_b with a distinct PNG (S2)
    s2_bytes = make_png_bytes(120, 50, fill_byte=0x77)
    s2_sha = hashlib.sha256(s2_bytes).hexdigest()
    async with maker() as session:
        db_job_b = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b is not None
        prev_b = dict(db_job_b.preview or {})
        sigs_b = [dict(s) for s in (prev_b.get("signatures") or [])]
        for s_item in sigs_b:
            if s_item.get("person_temp_id") == "p1":
                s_item["sha256"] = s2_sha
                s_item["mime"] = "image/png"
        prev_b["signatures"] = sigs_b
        blobs_b = dict(prev_b.get("_signature_blobs") or {})
        blobs_b[s2_sha] = base64.b64encode(s2_bytes).decode("ascii")
        prev_b["_signature_blobs"] = blobs_b
        db_job_b.preview = prev_b
        await session.commit()

    r_merge_b = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_b.status_code == 200, r_merge_b.text
    async with maker() as session:
        guo_after_b = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_b is not None
        assert guo_after_b.signature_sha256 == s2_sha

    # Re-merge job A with p1 in skip_signature_ids -> must not claim B's active S2
    r_remerge_a_skip = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "skip_signature_ids": ["p1"]},
    )
    assert r_remerge_a_skip.status_code == 200, r_remerge_a_skip.text

    # Revert job A first (while S2 is active) -> S2 stays active, and B's snapshot is rebased to None (S0)
    r_rev_a = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert", headers=admin_headers
    )
    assert r_rev_a.status_code == 200, r_rev_a.text
    async with maker() as session:
        guo_after_rev_a = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_a is not None
        assert guo_after_rev_a.signature_sha256 == s2_sha
        assert guo_after_rev_a.signature_source == "docx_import"

    # Then revert job B -> must restore None (S0), not S1
    r_rev_b = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert", headers=admin_headers
    )
    assert r_rev_b.status_code == 200, r_rev_b.text
    async with maker() as session:
        guo_after_both_reverts = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_both_reverts is not None
        assert guo_after_both_reverts.signature_sha256 is None
        assert guo_after_both_reverts.signature_source is None

    # 5b) Re-merge earlier import A without skipping signature after B replaced S1 with S2:
    #     A writes S1 again and rebases B's pre-merge snapshot to None while updating A's pre-merge snapshot to S2.
    #     Reverting A restores B's active S2, and reverting B then restores None.
    r_merge_a2 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a2.status_code == 200, r_merge_a2.text
    r_merge_b2 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_b2.status_code == 200, r_merge_b2.text
    r_remerge_a_write = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_remerge_a_write.status_code == 200, r_remerge_a_write.text
    async with maker() as session:
        guo_after_remerge_a = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_remerge_a is not None
        assert guo_after_remerge_a.signature_sha256 == guo_orig_sha

    r_rev_a2 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert", headers=admin_headers
    )
    assert r_rev_a2.status_code == 200, r_rev_a2.text
    async with maker() as session:
        guo_after_rev_a2 = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_a2 is not None
        assert guo_after_rev_a2.signature_sha256 == s2_sha
        assert guo_after_rev_a2.signature_source == "docx_import"

    r_rev_b2 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert", headers=admin_headers
    )
    assert r_rev_b2.status_code == 200, r_rev_b2.text
    async with maker() as session:
        guo_after_rev_b2 = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_b2 is not None
        assert guo_after_rev_b2.signature_sha256 is None
        assert guo_after_rev_b2.signature_source is None

    # 5c) Three distinct imports A(S1) -> B(S2) -> C(S1):
    #     Reverting A while C's S1 is active must preserve C's S1 and rebase B's pre-merge snapshot to None;
    #     reverting C then restores B's S2, and reverting B restores None.
    r_merge_a3 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a3.status_code == 200, r_merge_a3.text
    r_merge_b3 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_b3.status_code == 200, r_merge_b3.text
    r_up_c = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_chain_c.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r_up_c.status_code == 200, r_up_c.text
    job_c_id = r_up_c.json()["job_id"]
    r_merge_c = await client.post(
        f"/api/v1/admin/imports/{job_c_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_c.status_code == 200, r_merge_c.text
    async with maker() as session:
        guo_after_c = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_c is not None
        assert guo_after_c.signature_sha256 == guo_orig_sha
        assert guo_after_c.signature_source == "docx_import"

    r_rev_a3 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert", headers=admin_headers
    )
    assert r_rev_a3.status_code == 200, r_rev_a3.text
    async with maker() as session:
        guo_after_rev_a3 = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_a3 is not None
        assert guo_after_rev_a3.signature_sha256 == guo_orig_sha
        assert guo_after_rev_a3.signature_source == "docx_import"
        db_job_b_rebased = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b_rebased is not None
        b_guo_snap = (
            (db_job_b_rebased.stats or {})
            .get("_pre_merge_signatures", {})
            .get(guo_id)
        )
        assert isinstance(b_guo_snap, dict)
        assert b_guo_snap.get("signature_sha256") is None
        assert b_guo_snap.get("prev_writer_job_id") is None

    r_rev_c = await client.post(
        f"/api/v1/admin/imports/{job_c_id}/revert", headers=admin_headers
    )
    assert r_rev_c.status_code == 200, r_rev_c.text
    async with maker() as session:
        guo_after_rev_c = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_c is not None
        assert guo_after_rev_c.signature_sha256 == s2_sha
        assert guo_after_rev_c.signature_source == "docx_import"

    r_rev_b3 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert", headers=admin_headers
    )
    assert r_rev_b3.status_code == 200, r_rev_b3.text
    async with maker() as session:
        guo_after_rev_b3 = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_b3 is not None
        assert guo_after_rev_b3.signature_sha256 is None
        assert guo_after_rev_b3.signature_source is None

    # 6) Conflicting person in exclude_keys does not trigger 422 merge_conflict_unresolved
    async with maker() as session:
        db_job_b2 = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b2 is not None
        prev_b2 = dict(db_job_b2.preview or {})
        ppl_b2 = [dict(p) for p in (prev_b2.get("people") or [])]
        for p_item in ppl_b2:
            if p_item.get("name") == "万振凯":
                p_item["title"] = "特聘冲突教授"
        prev_b2["people"] = ppl_b2
        db_job_b2.preview = prev_b2
        await session.commit()

    r_merge_excl = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "exclude_keys": ["p2"]},
    )
    assert r_merge_excl.status_code == 200, r_merge_excl.text
    async with maker() as session:
        wan_after_excl = await session.get(LabPerson, uuid.UUID(wan_id))
        assert wan_after_excl is not None
        assert wan_after_excl.title == "教授"

    # 7) Newly merged unlinked person who later receives a manual signature upload is preserved on revert
    async with maker() as session:
        db_job_b3 = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b3 is not None
        prev_b3 = dict(db_job_b3.preview or {})
        ppl_b3 = [dict(p) for p in (prev_b3.get("people") or [])]
        ppl_b3.append(
            {
                "key": "p_new_unlinked",
                "temp_id": "p_new_unlinked",
                "name": "赵六",
                "title": "助理研究员",
                "affiliation": "天津工业大学",
                "project_role": "member",
                "tier": "T2",
            }
        )
        prev_b3["people"] = ppl_b3
        db_job_b3.preview = prev_b3
        await session.commit()

    r_merge_new = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "exclude_keys": ["p2"]},
    )
    assert r_merge_new.status_code == 200, r_merge_new.text
    async with maker() as session:
        zhao_p = await session.scalar(select(LabPerson).where(LabPerson.name == "赵六"))
        assert zhao_p is not None
        assert zhao_p.user_id is None
        zhao_id = str(zhao_p.id)

    zhao_manual_png = make_png_bytes(88, 38, fill_byte=0x66)
    zhao_manual_sha = hashlib.sha256(zhao_manual_png).hexdigest()
    r_put_zhao = await client.put(
        f"/api/v1/lab/people/{zhao_id}/signature",
        headers=admin_headers,
        files={"file": ("zhao.png", zhao_manual_png, "image/png")},
    )
    assert r_put_zhao.status_code == 200, r_put_zhao.text

    r_rev_new = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_new.status_code == 200, r_rev_new.text
    async with maker() as session:
        zhao_after_rev = await session.get(LabPerson, uuid.UUID(zhao_id))
        assert zhao_after_rev is not None
        assert zhao_after_rev.signature_source == "upload"
        assert zhao_after_rev.signature_sha256 == zhao_manual_sha

    # 8) Out-of-order revert starting from initial provision job (job_id) that created Xu Guowei (p3) with S1:
    #    job_b replaces p3's signature with S2; reverting job_id first rebases job_b's snapshot from S1 to None,
    #    so reverting job_b afterward clears p3's signature to None instead of resurrecting S1.
    async with maker() as session:
        xu_p = await session.scalar(select(LabPerson).where(LabPerson.name == "徐国伟"))
        assert xu_p is not None
        xu_id = str(xu_p.id)
        xu_orig_sha = xu_p.signature_sha256
        assert xu_orig_sha is not None

        db_job_b4 = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b4 is not None
        prev_b4 = dict(db_job_b4.preview or {})
        sigs_b4 = [dict(s) for s in (prev_b4.get("signatures") or [])]
        for s_item in sigs_b4:
            if s_item.get("person_temp_id") == "p3":
                s_item["sha256"] = s2_sha
                s_item["mime"] = "image/png"
        prev_b4["signatures"] = sigs_b4
        blobs_b4 = dict(prev_b4.get("_signature_blobs") or {})
        blobs_b4[s2_sha] = base64.b64encode(s2_bytes).decode("ascii")
        prev_b4["_signature_blobs"] = blobs_b4
        db_job_b4.preview = prev_b4
        await session.commit()

    r_merge_b4 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "exclude_keys": ["p2"]},
    )
    assert r_merge_b4.status_code == 200, r_merge_b4.text
    async with maker() as session:
        xu_after_b4 = await session.get(LabPerson, uuid.UUID(xu_id))
        assert xu_after_b4 is not None
        assert xu_after_b4.signature_sha256 == s2_sha

    r_rev_job1 = await client.post(
        f"/api/v1/admin/imports/{job_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_job1.status_code == 200, r_rev_job1.text
    async with maker() as session:
        xu_after_rev_job1 = await session.get(LabPerson, uuid.UUID(xu_id))
        assert xu_after_rev_job1 is not None
        assert xu_after_rev_job1.signature_sha256 == s2_sha

    r_rev_job2 = await client.post(
        f"/api/v1/admin/imports/{job2_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_job2.status_code == 200, r_rev_job2.text
    async with maker() as session:
        xu_after_rev_job2 = await session.get(LabPerson, uuid.UUID(xu_id))
        assert xu_after_rev_job2 is not None
        assert xu_after_rev_job2.signature_sha256 == s2_sha

    r_rev_b4 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_b4.status_code == 200, r_rev_b4.text
    async with maker() as session:
        xu_after_rev_b4 = await session.get(LabPerson, uuid.UUID(xu_id))
        assert xu_after_rev_b4 is not None
        assert xu_after_rev_b4.signature_sha256 is None
        assert xu_after_rev_b4.signature_source is None

    # 9) Import A (merge) creates an unlinked person without signature; Import B (provision) writes a signature
    #    for the same fingerprint; reverting A preserves the person and B's signature, and reverting B cleans it up.
    sun_sig_bytes = make_png_bytes(96, 42, fill_byte=0x55)
    sun_sig_sha = hashlib.sha256(sun_sig_bytes).hexdigest()
    sun_person_item = {
        "key": "p_sun_unlinked",
        "temp_id": "p_sun_unlinked",
        "name": "孙七",
        "title": "讲师",
        "affiliation": "天津工业大学",
        "project_role": "member",
        "tier": "T2",
        "login_name": "sunqi",
    }
    async with maker() as session:
        db_job_a9 = await session.get(ImportJob, uuid.UUID(job_a_id))
        assert db_job_a9 is not None
        prev_a9 = dict(db_job_a9.preview or {})
        ppl_a9 = [dict(p) for p in (prev_a9.get("people") or [])]
        ppl_a9.append(dict(sun_person_item))
        prev_a9["people"] = ppl_a9
        db_job_a9.preview = prev_a9

        db_job_c9 = await session.get(ImportJob, uuid.UUID(job_c_id))
        assert db_job_c9 is not None
        prev_c9 = dict(db_job_c9.preview or {})
        ppl_c9 = [dict(p) for p in (prev_c9.get("people") or [])]
        ppl_c9.append(dict(sun_person_item))
        prev_c9["people"] = ppl_c9
        sigs_c9 = [dict(s) for s in (prev_c9.get("signatures") or [])]
        sigs_c9.append(
            {
                "person_temp_id": "p_sun_unlinked",
                "person_name": "孙七",
                "sha256": sun_sig_sha,
                "mime": "image/png",
                "width": 96,
                "height": 42,
                "byte_size": len(sun_sig_bytes),
                "confidence": "high",
                "warnings": [],
            }
        )
        prev_c9["signatures"] = sigs_c9
        blobs_c9 = dict(prev_c9.get("_signature_blobs") or {})
        blobs_c9[sun_sig_sha] = base64.b64encode(sun_sig_bytes).decode("ascii")
        prev_c9["_signature_blobs"] = blobs_c9
        db_job_c9.preview = prev_c9
        await session.commit()

    r_merge_a9 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a9.status_code == 200, r_merge_a9.text
    async with maker() as session:
        sun_p = await session.scalar(select(LabPerson).where(LabPerson.name == "孙七"))
        assert sun_p is not None
        assert sun_p.user_id is None
        assert sun_p.signature_sha256 is None
        sun_id = str(sun_p.id)

    r_prov_c9 = await client.post(
        f"/api/v1/admin/imports/{job_c_id}/provision",
        headers=admin_headers,
        json={},
    )
    assert r_prov_c9.status_code == 200, r_prov_c9.text
    async with maker() as session:
        sun_after_c9 = await session.get(LabPerson, uuid.UUID(sun_id))
        assert sun_after_c9 is not None
        assert sun_after_c9.user_id is not None
        assert sun_after_c9.signature_sha256 == sun_sig_sha
        assert sun_after_c9.signature_source == "docx_import"

    r_rev_a9 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_a9.status_code == 200, r_rev_a9.text
    async with maker() as session:
        sun_after_rev_a9 = await session.get(LabPerson, uuid.UUID(sun_id))
        assert sun_after_rev_a9 is not None
        assert sun_after_rev_a9.signature_sha256 == sun_sig_sha
        assert sun_after_rev_a9.signature_source == "docx_import"

    r_rev_c9 = await client.post(
        f"/api/v1/admin/imports/{job_c_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_c9.status_code == 200, r_rev_c9.text
    async with maker() as session:
        sun_after_rev_c9 = await session.get(LabPerson, uuid.UUID(sun_id))
        assert sun_after_rev_c9 is not None
        assert sun_after_rev_c9.signature_sha256 is None
        assert sun_after_rev_c9.signature_source is None


