"""Seed 100% synthetic Qingheng Lab (/lab, /admin/imports, /chat, /settings, /audit) demo fixtures.

No real personal data or real proposal content is used here. All researchers,
grant numbers, papers, signatures, and conversations belong to the fictional
Qingheng Lab (清衡实验室).
"""

from __future__ import annotations

import base64
import hashlib
import math
import struct
import uuid
import zlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.config import Settings
from app.importer.fingerprint import (
    achievement_fp,
    facility_fp,
    person_fp,
    project_fp,
)
from app.importer.provenance import (
    MergeRequest,
    execute_merge,
    record_manual_field_edit,
    sync_lab_profile_rag,
)
from app.importer.provision import (
    PersonOverride,
    ProvisionRequest,
    provision_import_job,
)
from app.models import (
    ApiToken,
    Conversation,
    Document,
    ImportJob,
    LabPerson,
    Message,
    User,
)
from app.rag.embedder import Embedder
from app.rag.ingest import upsert_source

DEMO_LAB_INITIAL_PW = "QingHeng-Demo-2026#Init"


def make_calligraphic_signature_png(
    seed_num: int,
    *,
    width: int = 220,
    height: int = 76,
) -> bytes:
    """Generate a valid RGB PNG depicting a synthetic ink signature on warm paper."""
    bg_r, bg_g, bg_b = (250, 248, 243)
    ink_r, ink_g, ink_b = (26, 46, 40)
    accent_r, accent_g, accent_b = (180, 52, 42)

    grid = bytearray(width * height * 3)
    for idx in range(width * height):
        off = idx * 3
        grid[off] = bg_r
        grid[off + 1] = bg_g
        grid[off + 2] = bg_b

    def _put_dot(cx: float, cy: float, radius: float, r: int, g: int, b: int) -> None:
        x0 = max(0, int(cx - radius - 1))
        x1 = min(width - 1, int(cx + radius + 1))
        y0 = max(0, int(cy - radius - 1))
        y1 = min(height - 1, int(cy + radius + 1))
        r2 = radius * radius
        for py in range(y0, y1 + 1):
            for px in range(x0, x1 + 1):
                d2 = (px - cx) ** 2 + (py - cy) ** 2
                if d2 <= r2:
                    alpha = max(0.15, 1.0 - (d2 / (r2 + 0.01)) * 0.65)
                    off = (py * width + px) * 3
                    grid[off] = int(grid[off] * (1.0 - alpha) + r * alpha)
                    grid[off + 1] = int(grid[off + 1] * (1.0 - alpha) + g * alpha)
                    grid[off + 2] = int(grid[off + 2] * (1.0 - alpha) + b * alpha)

    # Draw 3 distinct character-like calligraphic gestures parameterized by seed_num
    phase_a = (seed_num * 1.37) % (2 * math.pi)
    phase_b = (seed_num * 2.41) % (2 * math.pi)
    freq_a = 2.2 + (seed_num % 3) * 0.45
    freq_b = 3.1 + ((seed_num >> 1) % 3) * 0.35

    for step in range(420):
        t = step / 419.0
        # Stroke group 1 (left character)
        if t < 0.34:
            u = t / 0.34
            x = 24 + u * 46 + math.sin(u * math.pi * freq_a + phase_a) * 9.5
            y = 20 + u * 36 + math.cos(u * math.pi * 2.5 + phase_b) * 11.0
            rad = 1.65 - 0.55 * u
            _put_dot(x, y, rad, ink_r, ink_g, ink_b)
        # Stroke group 2 (middle character)
        elif t < 0.68:
            u = (t - 0.34) / 0.34
            x = 78 + u * 54 + math.cos(u * math.pi * freq_b + phase_b) * 8.0
            y = 48 - u * 24 + math.sin(u * math.pi * 3.0 + phase_a) * 12.5
            rad = 1.75 - 0.6 * abs(u - 0.5)
            _put_dot(x, y, rad, ink_r, ink_g, ink_b)
        # Stroke group 3 (right character + flourish)
        else:
            u = (t - 0.68) / 0.32
            x = 136 + u * 58 + math.sin(u * math.pi * 2.0 + phase_a) * 6.0
            y = 38 + math.sin(u * math.pi * 2.4 + phase_b) * 14.0 - u * 8.0
            rad = 1.6 - 0.7 * u
            _put_dot(x, y, rad, ink_r, ink_g, ink_b)

    # Small red seal mark in bottom-right corner
    seal_x0, seal_y0 = width - 26, height - 24
    for sy in range(seal_y0, seal_y0 + 14):
        for sx in range(seal_x0, seal_x0 + 14):
            is_edge = (
                sx in (seal_x0, seal_x0 + 13)
                or sy in (seal_y0, seal_y0 + 13)
                or ((sx + sy + seed_num) % 5 == 0)
            )
            if is_edge and 0 <= sx < width and 0 <= sy < height:
                off = (sy * width + sx) * 3
                grid[off] = accent_r
                grid[off + 1] = accent_g
                grid[off + 2] = accent_b

    raw_rows = bytearray()
    row_stride = width * 3
    for y in range(height):
        raw_rows.append(0)
        start = y * row_stride
        raw_rows.extend(grid[start : start + row_stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_rows), level=6)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def _sig_entry(
    person_key: str,
    person_name: str,
    seed_num: int,
    blobs: dict[str, str],
) -> dict[str, Any]:
    png_bytes = make_calligraphic_signature_png(seed_num)
    sha = hashlib.sha256(png_bytes).hexdigest()
    blobs[sha] = base64.b64encode(png_bytes).decode("ascii")
    return {
        "person_temp_id": person_key,
        "person_name": person_name,
        "mime": "image/png",
        "sha256": sha,
        "size": len(png_bytes),
        "byte_size": len(png_bytes),
        "width": 220,
        "height": 76,
        "source": "docx_import",
        "warnings": [],
    }


def _build_job1_preview(as_of_str: str) -> dict[str, Any]:
    people: list[dict[str, Any]] = [
        {
            "key": "p1",
            "temp_id": "p1",
            "name": "陈予安",
            "project_role": "applicant",
            "tier": "T2",
            "login_name": "chenyuan",
            "title": "研究员",
            "admin_duty": "课题组组长",
            "tutor_role": "博士生导师",
            "research_specialty": "极低温超导磁体、液氦闭环冷却、异构算力集群协同",
            "degree": "博士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": (
                "陈予安，清衡实验室极低温超导与科学工程方向研究员、博士生导师。"
                "长期从事 20T 级超导磁体失超预警、液氦闭环联锁控制与高密度实验数据流处理研究，"
                "主持国家自然科学基金重点项目与赫利俄斯（HX-17）工程课题。"
            ),
            "warnings": [],
        },
        {
            "key": "p2",
            "temp_id": "p2",
            "name": "沈昭",
            "project_role": "member",
            "tier": "T3",
            "login_name": "shenzhao",
            "title": "研究员",
            "admin_duty": "实验室主任",
            "tutor_role": "博士生导师",
            "research_specialty": "异构算力调度、分布式训练系统、高可靠存储架构",
            "degree": "博士",
            "affiliation": "清衡实验室",
            "email": "admin@lab.local",
            "bio": (
                "沈昭，清衡实验室主任、研究员，主要研究方向为大规模异构 GPU 集群调度、"
                "容错通信与科研数据安全合规审计架构。"
            ),
            "warnings": [],
        },
        {
            "key": "p3",
            "temp_id": "p3",
            "name": "陆星河",
            "project_role": "member",
            "tier": "T2",
            "login_name": "luxinghe",
            "title": "助理研究员",
            "admin_duty": "无",
            "tutor_role": "无",
            "research_specialty": "可信检索增强生成、证据门控校准",
            "degree": "博士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": (
                "陆星河，清衡实验室可信检索方向骨干成员，专注于混合稠密/稀疏检索（RRF）、"
                "余弦拒答阈值校准与跨模态科学文献解析。"
            ),
            "warnings": [],
        },
        {
            "key": "p4",
            "temp_id": "p4",
            "name": "林可",
            "project_role": "member",
            "tier": "T3",
            "login_name": "linke",
            "title": "副研究员",
            "admin_duty": "无",
            "tutor_role": "硕士生导师",
            "research_specialty": "低温杜瓦联锁控制、超导量子干涉测量、实验数据采集",
            "degree": "博士",
            "affiliation": "清衡实验室",
            "email": "member@lab.local",
            "bio": (
                "林可，清衡实验室副研究员，负责赫利俄斯 HX-17 低温杜瓦压力联锁与瞬态测量标定。"
            ),
            "warnings": [],
        },
        {
            "key": "p5",
            "temp_id": "p5",
            "name": "顾清辞",
            "project_role": "member",
            "tier": "T2",
            "login_name": "guqingci",
            "title": "高级工程师",
            "admin_duty": "",
            "tutor_role": "无",
            "research_specialty": "高速 RDMA 网络互连、NVMe-oF 并行文件系统、容器化算力编排",
            "degree": "硕士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": None,
            "warnings": [],
        },
        {
            "key": "p6",
            "temp_id": "p6",
            "name": "周宁",
            "project_role": "member",
            "tier": "T3",
            "login_name": "zhouning",
            "title": "工程师",
            "admin_duty": "无",
            "tutor_role": "无",
            "research_specialty": "科研仪器采购合规、低温耗材供应链管理、资产全生命周期审计",
            "degree": "硕士",
            "affiliation": "清衡实验室",
            "email": "zhou@lab.local",
            "bio": None,
            "warnings": [],
        },
        {
            "key": "p7",
            "temp_id": "p7",
            "name": "苏景行",
            "project_role": "member",
            "tier": "T1",
            "student_id": "20260108",
            "cert": "20260108",
            "login_name": "20260108",
            "title": "无",
            "admin_duty": "无",
            "tutor_role": "无",
            "research_specialty": "稠密向量两阶段 HNSW 检索、混合 RRF 融合评测",
            "degree": "学士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": None,
            "warnings": [],
        },
        {
            "key": "p8",
            "temp_id": "p8",
            "name": "许知微",
            "project_role": "member",
            "tier": "T1",
            "student_id": "20260115",
            "cert": "20260115",
            "login_name": "20260115",
            "title": "无",
            "admin_duty": "无",
            "tutor_role": "无",
            "research_specialty": "科学表格结构化抽取、版面跨页拼接与字段溯源",
            "degree": "学士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": None,
            "warnings": [],
        },
    ]

    projects: list[dict[str, Any]] = [
        {
            "key": "j1",
            "title": "面向百千瓦级极低温超导磁体的闭环液氦联锁与智能诊断研究",
            "funder": "国家自然科学基金重点项目",
            "grant_no": "NSFC-62530117",
            "pi_name": "陈予安",
            "lab_role": "host",
            "start_date": "2025-01-01",
            "end_date": "2028-12-31",
            "amount_wan": "285.00",
            "status": "ongoing",
            "status_source": "explicit",
            "relationship_note": "本课题在 NSFC-62530117 超导磁体闭环监测基础上扩展跨模态在线诊断与知识库问答能力。",
            "raw_text": "《面向百千瓦级极低温超导磁体的闭环液氦联锁与智能诊断研究》（NSFC-62530117），陈予安（主持），国家自然科学基金重点项目，2025.01-2028.12，经费285万元，在研。",
            "warnings": [],
        },
        {
            "key": "j2",
            "title": "高密度异构算力集群的能效感知作业调度与容错通信机制",
            "funder": "国家重点研发计划青年科学家项目",
            "grant_no": "MOST-2025YFB0402",
            "pi_name": "沈昭",
            "lab_role": "host",
            "start_date": "2025-05-01",
            "end_date": "2028-04-30",
            "amount_wan": "320.00",
            "status": "ongoing",
            "status_source": "explicit",
            "relationship_note": "为课题组 4096 维稠密向量检索与科学大模型训练提供底层异构算力调度支撑。",
            "raw_text": "《高密度异构算力集群的能效感知作业调度与容错通信机制》（MOST-2025YFB0402），沈昭（主持），国家重点研发计划青年科学家项目，2025.05-2028.04，经费320万元，在研。",
            "warnings": [],
        },
        {
            "key": "j3",
            "title": "赫利俄斯 HX-17 脉冲磁场发生器快速消磁保护装置研制",
            "funder": "实验室自主立项工程课题",
            "grant_no": "JS-2023-084",
            "pi_name": "陈予安",
            "lab_role": "host",
            "start_date": "2023-03-01",
            "end_date": "2025-02-28",
            "amount_wan": "86.00",
            "status": "completed",
            "status_source": "explicit",
            "relationship_note": "已完成 185 A 额定电流与 35% 液氦联锁硬件闭环验收。",
            "raw_text": "《赫利俄斯 HX-17 脉冲磁场发生器快速消磁保护装置研制》（JS-2023-084），陈予安（主持），实验室自主立项工程课题，2023.03-2025.02，经费86万元，已结题。",
            "warnings": [],
        },
        {
            "key": "j4",
            "title": "多源异构科学文献与实验记录跨模态对齐方法研究",
            "funder": "国家自然科学基金青年科学基金项目",
            "grant_no": "NSFC-62402198",
            "pi_name": "林可",
            "lab_role": "participant",
            "start_date": "2024-01-01",
            "end_date": "2026-12-31",
            "amount_wan": "30.00",
            "status": "ongoing",
            "status_source": "explicit",
            "relationship_note": None,
            "raw_text": "《多源异构科学文献与实验记录跨模态对齐方法研究》（NSFC-62402198），林可（参与），国家自然科学基金青年科学基金项目，2024.01-2026.12，经费30万元，在研。",
            "warnings": [],
        },
    ]
    for p_item in projects:
        p_item["fingerprint"] = project_fp(
            grant_no=str(p_item["grant_no"]) if p_item.get("grant_no") else None,
            title=str(p_item["title"]),
            funder=str(p_item["funder"]) if p_item.get("funder") else None,
            warnings=[],
        )

    achievements: list[dict[str, Any]] = [
        {
            "key": "a1",
            "kind": "paper",
            "title": "Closed-Loop Quench Precursor Detection for 20-Tesla Cryogenic Superconducting Magnets",
            "first_author": "陈予安",
            "authors_text": "陈予安*, 林可, 沈昭",
            "venue": "IEEE Transactions on Applied Superconductivity",
            "year": 2025,
            "support_note": "验证了主磁体 185 A 阈值下的毫秒级失超前兆检测算法。",
            "raw_text": "陈予安（第一）：《Closed-Loop Quench Precursor Detection for 20-Tesla Cryogenic Superconducting Magnets》，IEEE Transactions on Applied Superconductivity，2025年。",
            "parse_quality": "structured",
            "warnings": [],
        },
        {
            "key": "a2",
            "kind": "patent",
            "title": "一种面向极低温超导杜瓦的多级联锁保护与快速泄压控制方法",
            "first_author": "陈予安",
            "authors_text": "陈予安, 林可, 顾清辞",
            "venue": "国家发明专利（授权号 ZL202510384920.X）",
            "year": 2025,
            "support_note": "支撑赫利俄斯 HX-17 液氦回路在 350 kPa 压力上限内的可靠运行。",
            "raw_text": "陈予安（第一）：《一种面向极低温超导杜瓦的多级联锁保护与快速泄压控制方法》，国家发明专利（授权号 ZL202510384920.X），2025年。",
            "parse_quality": "structured",
            "warnings": [],
        },
        {
            "key": "a3",
            "kind": "award",
            "title": "高可靠异构智算集群调度与科研知识底座关键技术及应用",
            "first_author": "沈昭",
            "authors_text": "沈昭, 陈予安, 陆星河, 顾清辞",
            "venue": "省部级科技进步奖一等奖",
            "year": 2025,
            "support_note": "清衡实验室为第一完成单位。",
            "raw_text": "沈昭（第一）：《高可靠异构智算集群调度与科研知识底座关键技术及应用》，省部级科技进步奖一等奖，2025年。",
            "parse_quality": "structured",
            "warnings": [],
        },
    ]
    for a_item in achievements:
        a_item["fingerprint"] = achievement_fp(
            kind=str(a_item["kind"]),
            title=str(a_item["title"]),
            year=int(str(a_item["year"])),
            raw_text=str(a_item["raw_text"]),
            parse_quality="structured",
        )

    facilities: list[dict[str, Any]] = [
        {
            "key": "f1",
            "category": "equipment",
            "name": "清衡-A100 高性能异构计算集群",
            "spec": "4 台计算节点（qh-gpu-01 至 qh-gpu-04），每台 8×NVIDIA A100-SXM4-80GB，400Gb/s InfiniBand 互连",
            "value_wan": "680.00",
            "usage_scenario": "支撑 4096 维 Qwen3-Embedding-8B 向量索引构建、混合检索评测与科学模型训练",
            "raw_text": "清衡-A100 高性能异构计算集群（4 台 8×A100-80GB 节点），原值 680.00 万元，用于高维向量检索与模型训练。",
            "warnings": [],
        },
        {
            "key": "f2",
            "category": "equipment",
            "name": "赫利俄斯 HX-17 20T 超导磁体与闭环液氦低温测试平台",
            "spec": "中心场强 20 Tesla，运行温度 4.2 K，额定工作电流上限 185 A，双路冗余液位计",
            "value_wan": "540.00",
            "usage_scenario": "支撑极低温超导材料临界电流测试、失超保护与杜瓦压力联锁实验",
            "raw_text": "赫利俄斯 HX-17 20T 超导磁体与闭环液氦低温测试平台，原值 540.00 万元，用于极低温超导测试。",
            "warnings": [],
        },
        {
            "key": "f3",
            "category": "equipment",
            "name": "流明 LM-04 飞秒瞬态光谱与光学标定平台",
            "spec": "波长覆盖 240–2600 nm，时间分辨率 < 120 fs，气浮隔振光学台",
            "value_wan": "310.00",
            "usage_scenario": "支撑低温光电器件瞬态光谱表征与高精度传感器标定",
            "raw_text": "流明 LM-04 飞秒瞬态光谱与光学标定平台，原值 310.00 万元，用于瞬态光谱表征与传感器标定。",
            "warnings": [],
        },
    ]
    for f_item in facilities:
        f_item["fingerprint"] = facility_fp(
            category=str(f_item["category"]),
            name=str(f_item["name"]),
            spec=str(f_item["spec"]),
        )

    sig_blobs: dict[str, str] = {}
    signatures = [
        _sig_entry("p1", "陈予安", 11, sig_blobs),
        _sig_entry("p2", "沈昭", 23, sig_blobs),
        _sig_entry("p3", "陆星河", 37, sig_blobs),
        _sig_entry("p4", "林可", 49, sig_blobs),
        _sig_entry("p5", "顾清辞", 61, sig_blobs),
        _sig_entry("p6", "周宁", 73, sig_blobs),
        _sig_entry("p7", "苏景行", 89, sig_blobs),
    ]

    return {
        "as_of_time": as_of_str,
        "cover": {
            "project_title": "面向百千瓦级极低温超导磁体的闭环液氦联锁与智能诊断研究",
            "applicant_name": "陈予安",
            "affiliation": "清衡实验室",
            "fill_date": as_of_str,
        },
        "people": people,
        "projects": projects,
        "achievements": achievements,
        "facilities": facilities,
        "signatures": signatures,
        "_signature_blobs": sig_blobs,
        "warnings": [],
    }


def _build_job2_preview(as_of_str: str) -> dict[str, Any]:
    people: list[dict[str, Any]] = [
        {
            "key": "p1",
            "temp_id": "p1",
            "name": "陆星河",
            "project_role": "applicant",
            "tier": "T2",
            "login_name": "luxinghe",
            "title": "副研究员",
            "admin_duty": "可信检索组负责人",
            "tutor_role": "无",
            "research_specialty": "可信检索增强生成、证据门控校准、多模态科学文献解析",
            "degree": "博士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": (
                "陆星河，清衡实验室副研究员、可信检索组负责人，主持省部级人工智能重大专项（QH-2026-ZX09），"
                "研究方向为混合 RRF 检索、两阶段前缀 HNSW 索引与零幻觉证据门控。"
            ),
            "warnings": [],
        },
        {
            "key": "p2",
            "temp_id": "p2",
            "name": "苏景行",
            "project_role": "member",
            "tier": "T1",
            "student_id": "20260108",
            "cert": "20260108",
            "login_name": "20260108",
            "title": "博士研究生",
            "admin_duty": "无",
            "tutor_role": "无",
            "research_specialty": "稠密向量两阶段 HNSW 检索、混合 RRF 融合评测、低延迟向量量化",
            "degree": "硕士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": None,
            "warnings": [],
        },
    ]
    projects: list[dict[str, Any]] = [
        {
            "key": "j1",
            "title": "面向科研合规的细粒度权限隔离检索与零幻觉证据门控技术",
            "funder": "省部级人工智能重大专项",
            "grant_no": "QH-2026-ZX09",
            "pi_name": "陆星河",
            "lab_role": "host",
            "start_date": "2026-01-01",
            "end_date": "2027-12-31",
            "amount_wan": "120.00",
            "status": "ongoing",
            "status_source": "explicit",
            "relationship_note": "构建清衡实验室统一知识助手与跨项目科研资产中枢的核心支撑课题。",
            "raw_text": "《面向科研合规的细粒度权限隔离检索与零幻觉证据门控技术》（QH-2026-ZX09），陆星河（主持），省部级人工智能重大专项，2026.01-2027.12，经费120万元，在研。",
            "warnings": [],
        },
    ]
    for p_item in projects:
        p_item["fingerprint"] = project_fp(
            grant_no=str(p_item["grant_no"]) if p_item.get("grant_no") else None,
            title=str(p_item["title"]),
            funder=str(p_item["funder"]) if p_item.get("funder") else None,
            warnings=[],
        )

    achievements: list[dict[str, Any]] = [
        {
            "key": "a1",
            "kind": "paper",
            "title": "Calibrated Two-Stage Prefix HNSW and SQL-Pushed ACL for Zero-Leakage Scientific RAG",
            "first_author": "陆星河",
            "authors_text": "陆星河*, 苏景行, 陈予安, 沈昭",
            "venue": "Proceedings of ACL",
            "year": 2026,
            "support_note": "提出 1024 维前缀 HNSW 粗筛 + 4096 维 halfvec 精排算法，延迟降低 26.3 倍。",
            "raw_text": "陆星河（第一）：《Calibrated Two-Stage Prefix HNSW and SQL-Pushed ACL for Zero-Leakage Scientific RAG》，Proceedings of ACL，2026年。",
            "parse_quality": "structured",
            "warnings": [],
        },
        {
            "key": "a2",
            "kind": "software",
            "title": "清衡科研申报书冷启动结构化抽取与字段溯源系统 V2.2",
            "first_author": "陆星河",
            "authors_text": "陆星河, 许知微, 沈昭",
            "venue": "国家版权局计算机软件著作权（登记号 2026SR0418290）",
            "year": 2026,
            "support_note": "实现申报书六大表格解析、内嵌签名绑定与字段级时间线合并。",
            "raw_text": "陆星河（第一）：《清衡科研申报书冷启动结构化抽取与字段溯源系统 V2.2》，软件著作权，登记号 2026SR0418290，2026年。",
            "parse_quality": "structured",
            "warnings": [],
        },
    ]
    for a_item in achievements:
        a_item["fingerprint"] = achievement_fp(
            kind=str(a_item["kind"]),
            title=str(a_item["title"]),
            year=int(str(a_item["year"])),
            raw_text=str(a_item["raw_text"]),
            parse_quality="structured",
        )

    return {
        "as_of_time": as_of_str,
        "cover": {
            "project_title": "面向科研合规的细粒度权限隔离检索与零幻觉证据门控技术",
            "applicant_name": "陆星河",
            "affiliation": "清衡实验室",
            "fill_date": as_of_str,
        },
        "people": people,
        "projects": projects,
        "achievements": achievements,
        "facilities": [],
        "signatures": [],
        "warnings": [],
    }


def _build_job3_parsed_preview(as_of_str: str) -> dict[str, Any]:
    """Build a third parsed import job showcasing all diff categories in Merge Preview."""
    people: list[dict[str, Any]] = [
        {
            "key": "p1",
            "temp_id": "p1",
            "name": "陈予安",
            "project_role": "applicant",
            "tier": "T2",
            "login_name": "chenyuan",
            "title": "研究员",
            "admin_duty": "实验室副主任",  # triggers manual_protected vs manual '极低温超导组负责人'
            "tutor_role": "博士生导师",
            "research_specialty": "极低温超导磁体、液氦闭环冷却、异构算力集群协同",
            "degree": "博士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": None,
            "warnings": [],
        },
        {
            "key": "p2",
            "temp_id": "p2",
            "name": "周宁",
            "project_role": "member",
            "tier": "T3",
            "login_name": "zhouning",
            "title": "高级工程师",  # triggers updated from '工程师' (2025-03 -> 2026-09)
            "admin_duty": "科研资产主管",
            "tutor_role": "无",
            "research_specialty": "科研仪器采购合规、低温耗材供应链管理、资产全生命周期审计",
            "degree": "硕士",
            "affiliation": "清衡实验室",
            "email": "zhou@lab.local",
            "bio": None,
            "warnings": [],
        },
        {
            "key": "p3",
            "temp_id": "p3",
            "name": "顾清辞",
            "project_role": "member",
            "tier": "T2",
            "login_name": "guqingci",
            "title": "高级工程师",
            "admin_duty": "集群运维组负责人",  # triggers fill_blank
            "tutor_role": "无",
            "research_specialty": "高速 RDMA 网络互连、NVMe-oF 并行文件系统、容器化算力编排",
            "degree": "硕士",
            "affiliation": "清衡实验室",
            "email": None,
            "bio": "负责清衡-A100 异构计算集群与 PB 级全闪存储阵列的高可用运维。",
            "warnings": [],
        },
    ]
    for p in people:
        raw_em = p.get("email")
        p["fingerprint"] = person_fp(
            tier=str(p["tier"]),
            login_name=str(p["login_name"]),
            name=str(p["name"]),
            affiliation=str(p["affiliation"]),
            email=str(raw_em) if isinstance(raw_em, str) and raw_em else None,
        )

    projects: list[dict[str, Any]] = [
        {
            "key": "j1",
            "title": "面向跨模态科学实验记录的自动知识图谱构建与溯源验证",
            "funder": "国家自然科学基金面上项目",
            "grant_no": "NSFC-62670312",
            "pi_name": "陈予安",
            "lab_role": "host",
            "start_date": "2027-01-01",
            "end_date": "2030-12-31",
            "amount_wan": "58.00",
            "status": "ongoing",
            "status_source": "explicit",
            "relationship_note": "在现有 RAG 知识底座之上扩展实验波形与表征图谱的跨模态关联。",
            "raw_text": "《面向跨模态科学实验记录的自动知识图谱构建与溯源验证》（NSFC-62670312），陈予安（主持），国家自然科学基金面上项目，2027.01-2030.12，经费58万元，在研。",
            "warnings": [],
        }
    ]
    for p_item in projects:
        p_item["fingerprint"] = project_fp(
            grant_no=str(p_item["grant_no"]) if p_item.get("grant_no") else None,
            title=str(p_item["title"]),
            funder=str(p_item["funder"]) if p_item.get("funder") else None,
            warnings=[],
        )

    job3_blobs: dict[str, str] = {}
    job3_sigs = [_sig_entry("p3", "顾清辞", 97, job3_blobs)]
    return {
        "as_of_time": as_of_str,
        "cover": {
            "project_title": "面向跨模态科学实验记录的自动知识图谱构建与溯源验证",
            "applicant_name": "陈予安",
            "affiliation": "清衡实验室",
            "fill_date": as_of_str,
        },
        "people": people,
        "projects": projects,
        "achievements": [],
        "facilities": [],
        "signatures": job3_sigs,
        "_signature_blobs": job3_blobs,
        "warnings": [],
    }


async def seed_demo_lab(
    session: AsyncSession,
    *,
    settings: Settings,
    embedder: Embedder,
    users: dict[str, User],
) -> None:
    """Populate synthetic Qingheng Lab assets, provenance history, conversations, tokens, and audit events."""
    admin = users["admin@lab.local"]
    linke = users["member@lab.local"]
    zhouning = users["zhou@lab.local"]
    wangmin = users["viewer@lab.local"]

    # Skip if synthetic lab jobs are already seeded
    existing_job = await session.scalar(
        select(ImportJob).where(
            ImportJob.original_filename
            == "清衡实验室_超导磁体与算力基座_2025春季申报书.docx"
        )
    )
    if existing_job is not None:
        return

    # 1. Job 1 (2025-03-15): Baseline cold-start import & provisioning
    job1_preview = _build_job1_preview("2025-03-15")
    job1 = ImportJob(
        uploaded_by=admin.id,
        original_filename="清衡实验室_超导磁体与算力基座_2025春季申报书.docx",
        content_sha256=hashlib.sha256(b"qingheng-synth-proposal-2025-spring").hexdigest(),
        parser_version="v2.2-hybrid",
        as_of_time=date(2025, 3, 15),
        status="parsed",
        preview=job1_preview,
        stats={
            "people_parsed": len(job1_preview["people"]),
            "projects_parsed": len(job1_preview["projects"]),
            "achievements_parsed": len(job1_preview["achievements"]),
            "facilities_parsed": len(job1_preview["facilities"]),
            "signatures_extracted": len(job1_preview["signatures"]),
            "pii_dropped": 4,
        },
    )
    session.add(job1)
    await session.flush()

    await provision_import_job(
        session,
        job_id=job1.id,
        actor_id=admin.id,
        body=ProvisionRequest(
            as_of_time=date(2025, 3, 15),
            initial_password=DEMO_LAB_INITIAL_PW,
            include_password=False,
            people=[
                PersonOverride(key="p2", link_user_id=admin.id),
                PersonOverride(key="p4", link_user_id=linke.id),
                PersonOverride(key="p6", link_user_id=zhouning.id),
            ],
        ),
        settings=settings,
        embedder=embedder,
    )

    # 2. Job 2 (2026-03-20): Temporal merge upgrading titles & adding 2026 projects/achievements
    job2_preview = _build_job2_preview("2026-03-20")
    job2 = ImportJob(
        uploaded_by=admin.id,
        original_filename="清衡实验室_可信检索与低温联锁联合攻关_2026春季申报书.docx",
        content_sha256=hashlib.sha256(b"qingheng-synth-proposal-2026-spring").hexdigest(),
        parser_version="v2.2-hybrid",
        as_of_time=date(2026, 3, 20),
        status="parsed",
        preview=job2_preview,
        stats={
            "people_parsed": len(job2_preview["people"]),
            "projects_parsed": len(job2_preview["projects"]),
            "achievements_parsed": len(job2_preview["achievements"]),
            "facilities_parsed": 0,
            "signatures_extracted": 0,
            "pii_dropped": 2,
        },
    )
    session.add(job2)
    await session.flush()

    await execute_merge(
        session,
        job_id=job2.id,
        actor_id=admin.id,
        body=MergeRequest(as_of_time=date(2026, 3, 20)),
        embedder=embedder,
    )

    # 3. Record manual edits on 陈予安 and 陆星河 to showcase `手工修订` badges and `[恢复为导入值]`
    chen = await session.scalar(select(LabPerson).where(LabPerson.name == "陈予安"))
    if chen is not None:
        old_duty = chen.admin_duty
        chen.admin_duty = "极低温超导组负责人"
        await record_manual_field_edit(
            session,
            entity_type="person",
            entity=chen,
            field="admin_duty",
            old_val=old_duty,
            new_val=chen.admin_duty,
            actor_id=admin.id,
            force_record=True,
        )

    lu = await session.scalar(select(LabPerson).where(LabPerson.name == "陆星河"))
    if lu is not None:
        old_tutor = lu.tutor_role
        lu.tutor_role = "硕士生导师"
        await record_manual_field_edit(
            session,
            entity_type="person",
            entity=lu,
            field="tutor_role",
            old_val=old_tutor,
            new_val=lu.tutor_role,
            actor_id=admin.id,
            force_record=True,
        )

    await sync_lab_profile_rag(session, embedder=embedder, actor_id=admin.id)

    # 4. Job 3 (2026-09-15): Keep in `parsed` state for Temporal Merge Preview Diff Drawer
    job3_preview = _build_job3_parsed_preview("2026-09-15")
    job3 = ImportJob(
        uploaded_by=admin.id,
        original_filename="清衡实验室_多模态科学文献知识图谱_2026秋季申报书.docx",
        content_sha256=hashlib.sha256(b"qingheng-synth-proposal-2026-autumn").hexdigest(),
        parser_version="v2.2-hybrid",
        as_of_time=date(2026, 9, 15),
        status="parsed",
        preview=job3_preview,
        stats={
            "people_parsed": len(job3_preview["people"]),
            "projects_parsed": len(job3_preview["projects"]),
            "achievements_parsed": 0,
            "facilities_parsed": 0,
            "signatures_extracted": 1,
            "pii_dropped": 1,
        },
    )
    session.add(job3)
    await session.flush()

    # 5. Multi-format synthetic documents (.pdf and .docx) in `documents`
    extra_records = [
        (
            {
                "doc_key": "cryo-interlock-sop-pdf",
                "title": "低温超导杜瓦安全联锁操作规程（v3.2 修订版）",
                "collection": "knowledge",
                "visibility": "lab",
            },
            (
                "## 第 1 页 · 额定压力与液位联锁阈值\n"
                "赫利俄斯 HX-17 低温杜瓦罐液氮补给阀额定压力上限为 350 kPa，每 14 天由林可带队执行一次气密性巡检。"
                "主磁体额定工作电流上限为 185 A；当液氦液位低于 35% 时，双路冗余液位计将在 20 ms 内触发硬件联锁停机并启动快速消磁回路。\n\n"
                "## 第 2 页 · 失超泄压与应急处置\n"
                "若发生失超前兆报警，系统自动切换至双通道泄压阀组，值班实验员须在 15 分钟内记录杜瓦回温曲线并归档至项目赫利俄斯笔记库。"
            ),
        ),
        (
            {
                "doc_key": "compute-quota-autumn-docx",
                "title": "2026 秋季异构算力配额与存储归档周会纪要",
                "collection": "meeting",
                "visibility": "lab",
            },
            (
                "## 算力队列与并发上限调整\n"
                "清衡-A100 集群共 4 台 8×A100-80GB 节点（32 张 GPU）。自 2026 年秋季学期起，交互调试分区 `interactive` 单任务最长 4 小时，最多申请 2 张 GPU；"
                "训练分区 `train` 单用户并发上限为 8 张 GPU，最长 72 小时。\n\n"
                "## 向量索引与冷备份要求\n"
                "超过 5,000 个知识块的专题库推荐启用 `EMBEDDING_INDEX=hnsw_prefix`（1024 维前缀 HNSW 粗筛 + 4096 维 halfvec 精排），"
                "由顾清辞与苏景行每周五晚执行一次基准回归核验。"
            ),
        ),
    ]
    await upsert_source(
        session,
        embedder=embedder,
        owner_id=admin.id,
        source="upload",
        records=extra_records,
        grants={},
        group_grants={},
    )
    pdf_doc = await session.scalar(
        select(Document).where(Document.doc_key == "cryo-interlock-sop-pdf")
    )
    if pdf_doc is not None:
        pdf_doc.source_format = "pdf"
        pdf_doc.original_filename = "清衡实验室_低温超导杜瓦安全联锁操作规程_v3.2.pdf"

    docx_doc = await session.scalar(
        select(Document).where(Document.doc_key == "compute-quota-autumn-docx")
    )
    if docx_doc is not None:
        docx_doc.source_format = "markdown"
        docx_doc.original_filename = "清衡实验室_2026秋季算力配额与周会纪要.docx"

    # 6. Seed synthetic Conversations & Messages for Admin (沈昭) and Member (林可)
    helios_doc = await session.scalar(
        select(Document).where(Document.doc_key == "project-helios")
    )
    profile_people_doc = await session.scalar(
        select(Document).where(Document.doc_key == "lab-profile-people")
    )
    gpu_doc = await session.scalar(
        select(Document).where(Document.doc_key == "cluster-gpu")
    )

    await session.execute(
        delete(Conversation).where(Conversation.user_id.in_([admin.id, linke.id]))
    )

    conv1 = Conversation(
        user_id=admin.id,
        title="陈予安与陆星河研究方向及赫利俄斯 HX-17 联锁参数",
    )
    conv2 = Conversation(
        user_id=admin.id,
        title="清衡-A100 算力集群节点规模与并发配额规范",
    )
    session.add_all([conv1, conv2])
    await session.flush()

    cite_profile_id = str(profile_people_doc.id) if profile_people_doc else str(uuid.uuid4())
    cite_helios_id = str(helios_doc.id) if helios_doc else str(uuid.uuid4())
    cite_gpu_id = str(gpu_doc.id) if gpu_doc else str(uuid.uuid4())

    msg1_q = Message(
        conversation_id=conv1.id,
        role="user",
        content="课题组里陈予安和陆星河的研究专长与主持项目分别是什么？赫利俄斯 HX-17 的主磁体额定工作电流上限是多少？",
        refused=False,
        citations=[],
    )
    msg1_a = Message(
        conversation_id=conv1.id,
        role="assistant",
        content=(
            "根据课题组资料库档案，陈予安（研究员、博士生导师、极低温超导组负责人）的研究专长为极低温超导磁体、液氦闭环冷却与异构算力集群协同，"
            "目前主持国家自然科学基金重点项目《面向百千瓦级极低温超导磁体的闭环液氦联锁与智能诊断研究》（NSFC-62530117）及赫利俄斯 HX-17 工程课题 [1]。"
            "陆星河（副研究员、硕士生导师、可信检索组负责人）的研究专长为可信检索增强生成、证据门控校准与多模态科学文献解析，"
            "主持省部级人工智能重大专项《面向科研合规的细粒度权限隔离检索与零幻觉证据门控技术》（QH-2026-ZX09）[1]。\n\n"
            "在项目赫利俄斯（内部代号 HX-17）测试规程中，主磁体额定工作电流上限为 185 A；当液氦液位低于 35% 时将自动触发联锁停机保护 [2]。"
        ),
        refused=False,
        citations=[
            {
                "index": 1,
                "document_id": cite_profile_id,
                "doc_key": "lab-profile-people",
                "title": "课题组资料库：团队成员与研究专长",
                "heading": "团队成员（Lab People）",
                "excerpt": "陈予安，研究员，博士生导师，极低温超导组负责人；研究专长：极低温超导磁体、液氦闭环冷却、异构算力集群协同。陆星河，副研究员，硕士生导师；研究专长：可信检索增强生成、证据门控校准、多模态科学文献解析。",
                "score": 0.91,
            },
            {
                "index": 2,
                "document_id": cite_helios_id,
                "doc_key": "project-helios",
                "title": "项目赫利俄斯：低温超导磁体测试记录",
                "heading": "代号与范围",
                "excerpt": "赫利俄斯项目内部代号为 HX-17，由陈予安负责。主磁体额定工作电流上限为 185 A，液氦液位低于 35% 时触发联锁停机。",
                "score": 0.88,
            },
        ],
    )

    msg2_q = Message(
        conversation_id=conv2.id,
        role="user",
        content="清衡-A100 一共有多少台节点？单用户训练队列最多并发多少张 GPU？",
        refused=False,
        citations=[],
    )
    msg2_a = Message(
        conversation_id=conv2.id,
        role="assistant",
        content=(
            "清衡实验室主力 GPU 集群代号为「清衡-A100」，共配备 4 台计算节点（qh-gpu-01 至 qh-gpu-04），每台节点含 8 张 A100-SXM4-80GB 加速卡，通过 400Gb/s InfiniBand 互连 [1]。\n\n"
            "在 Slurm 队列规则中，交互分区 `interactive` 单任务最长 4 小时、最多 2 张 GPU；训练分区 `train` 单任务最长 72 小时，单用户并发上限为 8 张 GPU [1]。"
        ),
        refused=False,
        citations=[
            {
                "index": 1,
                "document_id": cite_gpu_id,
                "doc_key": "cluster-gpu",
                "title": "清衡 GPU 集群使用约定",
                "heading": "节点与分区",
                "excerpt": "清衡实验室主力集群代号为 清衡-A100，共 4 台计算节点（qh-gpu-01 至 qh-gpu-04），每台配备 8 张 80GB 加速卡。训练分区 train 单用户并发上限为 8 张 GPU。",
                "score": 0.93,
            }
        ],
    )
    msg2_refuse_q = Message(
        conversation_id=conv2.id,
        role="user",
        content="周三学术例会在哪间实体会议室举行？供应商采购合同的付款账号是多少？",
        refused=False,
        citations=[],
    )
    msg2_refuse_a = Message(
        conversation_id=conv2.id,
        role="assistant",
        content=(
            "证据门控拦截（insufficient_evidence）：当前授权可访问的知识库分块最高相关性得分（0.31）低于校准门控阈值（0.58），且关键实体未命中可靠证据。"
            "实验室周会固定在线上举行（未登记周三实体会议室），且未授权的受限采购合同文档已在 SQL 检索阶段执行 ACL 物理隔离。"
        ),
        refused=True,
        citations=[],
    )
    session.add_all([msg1_q, msg1_a, msg2_q, msg2_a, msg2_refuse_q, msg2_refuse_a])

    # 7. Seed Personal Access Tokens in `api_tokens` for Admin
    await session.execute(delete(ApiToken).where(ApiToken.user_id == admin.id))
    now_utc = datetime.now(UTC)
    pat_active = ApiToken(
        user_id=admin.id,
        name="Claude Desktop MCP (Workstation)",
        token_hash=hashlib.sha256(b"synth-demo-pat-active-qingheng").hexdigest(),
        prefix="la_pat_qh9x82k1",
        created_at=now_utc - timedelta(days=5),
        last_used_at=now_utc - timedelta(hours=2),
        expires_at=now_utc + timedelta(days=85),
        revoked_at=None,
    )
    pat_revoked = ApiToken(
        user_id=admin.id,
        name="Nightly Eval Pipeline Runner",
        token_hash=hashlib.sha256(b"synth-demo-pat-revoked-qingheng").hexdigest(),
        prefix="la_pat_ci7m30p4",
        created_at=now_utc - timedelta(days=28),
        last_used_at=now_utc - timedelta(days=14),
        expires_at=now_utc + timedelta(days=62),
        revoked_at=now_utc - timedelta(days=10),
    )
    session.add_all([pat_active, pat_revoked])
    await session.commit()

    # 8. Write hash-chained PII-free audit events via `write_audit()`
    audit_samples: list[tuple[uuid.UUID | None, str, str | None, str | None, dict[str, Any]]] = [
        (
            admin.id,
            "login",
            "user",
            str(admin.id),
            {"method": "password", "role": "admin"},
        ),
        (
            admin.id,
            "import_proposal_parse",
            "import_job",
            str(job1.id),
            {"people_parsed": 8, "projects_parsed": 4, "achievements_parsed": 3, "pii_dropped": 4},
        ),
        (
            admin.id,
            "import_proposal_provision",
            "import_job",
            str(job1.id),
            {"accounts_created": 5, "accounts_linked": 3, "signatures_saved": 7},
        ),
        (
            admin.id,
            "import_proposal_merge",
            "import_job",
            str(job2.id),
            {"adopted_count": 4, "added_entities": 3, "as_of_time": "2026-03-20"},
        ),
        (
            linke.id,
            "document_upload",
            "document",
            str(pdf_doc.id) if pdf_doc else "doc-pdf",
            {"collection": "knowledge", "visibility": "lab", "source_format": "pdf"},
        ),
        (
            linke.id,
            "document_indexed",
            "document",
            str(pdf_doc.id) if pdf_doc else "doc-pdf",
            {"chunks": 2, "status": "ready"},
        ),
        (
            admin.id,
            "retrieval",
            "conversation",
            str(conv1.id),
            {"mode": "hybrid_rrf", "top_score": 0.91, "threshold": 0.58, "hits": 5},
        ),
        (
            admin.id,
            "llm_call",
            "conversation",
            str(conv1.id),
            {
                "model": "claude-sonnet-5-5",
                "prompt_tokens": 1140,
                "completion_tokens": 268,
                "citations_used": 2,
                "redaction_count": 0,
                "status": "ok",
            },
        ),
        (
            zhouning.id,
            "permission_denied",
            "document",
            cite_helios_id,
            {"operation": "get_document", "reason": "acl_filtered"},
        ),
        (
            wangmin.id,
            "answer_refused",
            "conversation",
            str(conv2.id),
            {"reason": "low_evidence_score", "top_score": 0.31, "threshold": 0.58},
        ),
        (
            admin.id,
            "token_create",
            "api_token",
            str(pat_active.id),
            {"prefix": "la_pat_qh9x82k1", "expires_days": 90},
        ),
    ]
    for actor_uid, act, r_type, r_id, det in audit_samples:
        await write_audit(
            settings=settings,
            actor_id=actor_uid,
            action=act,
            resource_type=r_type,
            resource_id=r_id,
            request_id=f"req-{uuid.uuid4().hex[:12]}",
            ip="127.0.0.1",
            detail=det,
        )
