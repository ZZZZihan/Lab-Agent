#!/usr/bin/env python3
"""Deterministically build the v2 evaluation PDF corpus files in evals/data/v2/corpus/.

Uses pypdf with an embedded Type0 / Identity-H font and ToUnicode CMap so that
pypdf.PdfReader (and app.rag.ingest.extract_pdf_text) extracts exact UTF-8 Chinese text
with zero external font binaries or non-deterministic timestamps.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

PDF_SPECS: dict[str, dict[str, object]] = {
    "manual-cryo-cooling": {
        "title": "机房精密液冷CDU巡检与告警处置手册",
        "collection": "knowledge",
        "visibility": "lab",
        "pages": [
            [
                "机房精密液冷CDU巡检与告警处置手册",
                "一、液冷分配单元（CDU）运行基准参数",
                "清衡实验室地下一层 B-008 机房部署了 2 台冷板式液冷分配单元，主机编号分别为 CDU-North-01 与 CDU-South-02。",
                "一次侧冷冻水供水温度控制范围为 12 摄氏度至 15 摄氏度，二次侧冷却液（25% 丙二醇去离子水溶液）供液温度设定为 32 摄氏度，压差设定值为 145 kPa。",
                "单台 CDU 额定换热功率为 450 kW，主循环泵采用双泵一用一备热切换模式，每 168 小时自动轮换一次主备泵。",
            ],
            [
                "二、日常巡检与水质维护规程",
                "运维值班人员每日上午 09:30 与晚间 21:30 各执行一次现场液冷巡检，重点核对二次侧电导率、pH 值与膨胀水箱液位。",
                "二次侧冷却液电导率不得超过 50 uS/cm，pH 值必须维持在 7.8 至 8.5 之间；若 pH 低于 7.6，需在 24 小时内补加缓蚀剂。",
                "精密Y型过滤器压差超过 35 kPa 时触发黄色预警，必须切换至旁路滤芯并在 48 小时内完成 50 微米不锈钢滤网清洗。",
            ],
            [
                "三、漏液告警与紧急停机处置",
                "机柜底座铺设光电绳式漏液传感器，一旦检测到液体渗漏，BMC 声光报警器会在 3 秒内触发 E-CDU-901 紧急告警并自动关闭对应机柜电磁快速切断阀。",
                "二次侧供液温度若连续 60 秒超过 42 摄氏度，集群调度器将自动驱逐该机柜上的训练作业并执行 GPU 功率封顶（降至 250 W）。",
                "年度大保养定于每年八月第二个周六执行，需排空旧液并使用高纯氮气以 220 kPa 压力保压检漏 120 分钟。",
            ],
        ],
    },
    "manual-eye-tracker": {
        "title": "认知人因室 Tobii Pro 眼动仪校准与采样手册",
        "collection": "knowledge",
        "visibility": "lab",
        "pages": [
            [
                "认知人因室 Tobii Pro 眼动仪校准与采样手册",
                "一、硬件配置与实验环境要求",
                "清衡实验室认知人因实验间位于 C 座 312 室，配备 1 台 Tobii Pro Spectrum 屏幕式眼动仪（资产编号 QH-ET-2025-04）。",
                "眼动仪支持最高 1200 Hz 双眼同步采样率，常规阅读实验推荐设置为 600 Hz，暗瞳与亮瞳双模式自动切换。",
                "被试眼部距离屏幕平面应保持在 62 厘米至 68 厘米之间，实验间环境照度需固定在 300 Lux（色温 5000 K），严禁阳光直射红外传感器。",
            ],
            [
                "二、九点校准与误差验收标准",
                "每次正式采集前必须使用 Tobii Pro Lab 执行标准 9 点网格校准，另加 4 点独立验证测试。",
                "验收门禁：双眼平均视角准确度误差（Accuracy）不得高于 0.45 度，样本间均方根精密度噪声（Precision RMS）不得高于 0.12 度。",
                "若单名被试连续 3 次校准均未达标，需停止实验并记录为硬件剔除样本，不得强行进入正式刺激呈现阶段。",
            ],
            [
                "三、原始数据导出与同步触发",
                "眼动仪通过 TTL 并口同步盒（端口地址 0x378）与脑电采集主机打标同步，硬件触发脉冲宽度固定为 5 毫秒。",
                "原始注视点序列统一导出为 TSV 格式，命名规范为 ET_项目缩写_被试编号_日期.tsv，并上传至内网归档路径 /nas/human-factors/eyetrack/。",
                "光学窗口镜片每周五用无水乙醇镜头纸单向擦拭一次，禁止使用含丙酮溶剂或普通纸巾擦拭红外滤光屏。",
            ],
        ],
    },
    "manual-struct-scanner": {
        "title": "具身智能室结构光三维扫描仪操作手册",
        "collection": "knowledge",
        "visibility": "lab",
        "pages": [
            [
                "具身智能室结构光三维扫描仪操作手册",
                "一、设备规格与工作距离",
                "具身智能实验室在 D 座 105 室配置了 1 台蓝光工业结构光三维扫描仪（型号 QingScan-SL800，设备编号 QH-3D-105）。",
                "扫描仪采用 450 nm 窄带蓝光条纹投影与双 1200 万像素全局快门相机，单幅扫描视场为 400 mm x 300 mm，最佳工作距离为 520 mm。",
                "空间点距精度为 0.035 mm，单幅点云采集耗时 0.8 秒，配套自动转盘最大承重为 25 千克。",
            ],
            [
                "二、双目棋盘格标定与环境温控",
                "当实验室环境温度波动超过正负 2 摄氏度或设备移动后，必须使用碳纤维玻璃标定板（编号 CAL-CF-400）重新执行 15 姿态双目标定。",
                "标定重投影均方根误差必须小于 0.028 像素方可保存参数；标定文件保存在控制工作站 D:/QingScan/Calib/ 目录下。",
                "针对高反光金属或透明物体，允许喷涂二氧化钛显像剂（膜厚不超过 8 微米），扫描结束后必须用异丙醇彻底清洁样品表面。",
            ],
            [
                "三、点云拼接与网格导出规范",
                "多视角点云拼接优先采用标志点全局配准加 ICP 迭代最近点精配准，相邻视角重叠率不得低于 35%。",
                "封装后的三角网格模型统一导出为二进制 PLY 与 OBJ 双格式，法向朝外，顶点坐标单位统一设为毫米（mm）。",
                "扫描仪蓝光投射器连续工作 4 小时后需待机冷却 20 分钟；归还设备钥匙须在 D 座 105 室借用台账上签字登记。",
            ],
        ],
    },
}


def build_unicode_pdf(pages: list[list[str]]) -> bytes:
    """Build deterministic PDF bytes with a ToUnicode CMap for all characters in pages."""
    chars = sorted({ch for page in pages for line in page for ch in line})
    char_to_cid = {ch: idx + 1 for idx, ch in enumerate(chars)}

    bfchar_lines = [f"<{cid:04X}> <{ord(ch):04X}>" for ch, cid in char_to_cid.items()]
    cmap_body = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<0001> <FFFF>\n"
        "endcodespacerange\n"
        f"{len(bfchar_lines)} beginbfchar\n"
        + "\n".join(bfchar_lines)
        + "\n"
        "endbfchar\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end\n"
    )
    writer = PdfWriter()
    writer._info = None
    cmap_stream = DecodedStreamObject()
    cmap_stream.set_data(cmap_body.encode("ascii"))
    cmap_ref = writer._add_object(cmap_stream)

    font_dict = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/Songti"),
            NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/ToUnicode"): cmap_ref,
            NameObject("/DescendantFonts"): ArrayObject([]),
        }
    )
    font_ref = writer._add_object(font_dict)

    for page_lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        page[NameObject("/Resources")] = resources
        ops = ["BT", "/F1 12 Tf", "72 720 Td"]
        for idx, line in enumerate(page_lines):
            if idx > 0:
                ops.append("0 -18 Td")
            hex_str = "".join(f"{char_to_cid[ch]:04X}" for ch in line)
            ops.append(f"<{hex_str}> Tj")
        ops.append("ET")
        content_stream = DecodedStreamObject()
        content_stream.set_data("\n".join(ops).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content_stream)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or check v2 deterministic PDF corpus files.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "evals" / "data" / "v2" / "corpus",
        help="Target directory for generated .pdf files",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify existing .pdf files match deterministic build output byte-for-byte",
    )
    args = parser.parse_args(argv)
    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    for key, spec in sorted(PDF_SPECS.items()):
        pages = spec["pages"]
        assert isinstance(pages, list)
        pdf_bytes = build_unicode_pdf(pages)  # type: ignore[arg-type]
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        target = out_dir / f"{key}.pdf"
        if args.check:
            if not target.is_file():
                print(f"Missing PDF file: {target}", file=sys.stderr)
                return 1
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != digest:
                print(
                    f"PDF drift for {target.name}: expected {digest}, got {actual}",
                    file=sys.stderr,
                )
                return 1
        else:
            target.write_bytes(pdf_bytes)
            print(f"wrote {target.name} ({len(pdf_bytes)} bytes, sha256={digest[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
