"use client";


import {
  type BasketEntityKind,
  type BasketItem,
  type BasketOptions,
  type BasketState,
  type LabProjectItem,
} from "@/lib/api";


export type TabKey = "people" | "projects" | "achievements" | "facilities";

export const TAB_TO_BASKET_KIND: Record<TabKey, BasketEntityKind> = {
  people: "person",
  projects: "project",
  achievements: "achievement",
  facilities: "facility",
};

export const BASKET_KIND_LABELS: Record<BasketEntityKind, string> = {
  person: "团队成员",
  project: "承担项目",
  achievement: "前期成果",
  facility: "支撑条件",
};

export const BASKET_MAX_ITEMS = 200;
export const MAX_SIGNATURE_BYTES = 2 * 1024 * 1024;
export const ALLOWED_SIGNATURE_MIMES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
]);

export const DEFAULT_BASKET_OPTIONS: BasketOptions = {
  with_support: true,
  with_relation: true,
  numbering: "bracket",
  group_by_kind: true,
  include_signatures: true,
  naming: "seq_name",
};

export function normalizeBasketEntityKind(raw?: string): BasketEntityKind {
  const lower = (raw || "").trim().toLowerCase();
  if (lower === "project" || lower === "projects") return "project";
  if (
    lower === "achievement" ||
    lower === "achievements" ||
    lower === "output" ||
    lower === "outputs"
  ) {
    return "achievement";
  }
  if (lower === "facility" || lower === "facilities") return "facility";
  return "person";
}

export type EditableFieldConfig = {
  key: string;
  label: string;
  type?: "text" | "textarea" | "select" | "number" | "date";
  options?: Array<{ value: string; label: string }>;
  adminOnly?: boolean;
};

export const EDITABLE_FIELDS: Record<TabKey, EditableFieldConfig[]> = {
  people: [
    { key: "title", label: "职称" },
    { key: "degree", label: "学位" },
    { key: "affiliation", label: "所在单位" },
    { key: "research_specialty", label: "研究专长" },
    { key: "admin_duty", label: "行政职务" },
    { key: "tutor_role", label: "博硕导岗位" },
    { key: "email", label: "邮箱" },
    {
      key: "project_role",
      label: "项目角色",
      type: "select",
      adminOnly: true,
      options: [
        { value: "applicant", label: "课题负责人 (applicant)" },
        { value: "member", label: "项目组成员 (member)" },
      ],
    },
    { key: "bio", label: "个人学术简历", type: "textarea" },
  ],
  projects: [
    { key: "title", label: "项目名称" },
    { key: "funder", label: "资助来源" },
    { key: "grant_no", label: "批准号" },
    { key: "pi_name", label: "项目负责人" },
    {
      key: "status",
      label: "项目状态",
      type: "select",
      options: [
        { value: "ongoing", label: "在研 (ongoing)" },
        { value: "completed", label: "已结题 (completed)" },
        { value: "unknown", label: "未注明 (unknown)" },
      ],
    },
    { key: "start_date", label: "起始日期", type: "date" },
    { key: "end_date", label: "截止日期", type: "date" },
    { key: "amount_wan", label: "经费 (万元)" },
    { key: "relationship_note", label: "与本课题关系说明", type: "textarea" },
  ],
  achievements: [
    {
      key: "kind",
      label: "成果类型",
      type: "select",
      options: [
        { value: "paper", label: "论文" },
        { value: "report", label: "研究报告/资政报告" },
        { value: "patent", label: "发明专利" },
        { value: "software", label: "软件著作权" },
        { value: "award", label: "科研奖励/奖项" },
        { value: "book", label: "学术专著" },
        { value: "exhibition", label: "成果展" },
        { value: "other", label: "其他成果" },
      ],
    },
    { key: "title", label: "成果名称" },
    { key: "first_author", label: "第一作者" },
    { key: "authors_text", label: "全部作者" },
    { key: "venue", label: "刊物/出版社/采纳单位" },
    { key: "year", label: "年份", type: "number" },
    { key: "support_note", label: "支撑价值说明", type: "textarea" },
  ],
  facilities: [
    {
      key: "category",
      label: "设备类别",
      type: "select",
      options: [
        { value: "equipment", label: "科研设备 (equipment)" },
        { value: "compute", label: "算力设备 (compute)" },
      ],
    },
    { key: "name", label: "设备名称" },
    { key: "spec", label: "型号规格与配置" },
    { key: "value_wan", label: "原值 (万元)" },
    { key: "usage_scenario", label: "主要用途说明", type: "textarea" },
  ],
};

export const FIELD_LABEL_MAP: Record<string, string> = Object.values(EDITABLE_FIELDS)
  .flat()
  .reduce<Record<string, string>>((acc, item) => {
    acc[item.key] = item.label;
    return acc;
  }, {});

export function formatProvValue(val: unknown): string {
  if (val === null || val === undefined || val === "") return "（空）";
  if (typeof val === "string") return val;
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    return JSON.stringify(val);
  } catch {
    return String(val);
  }
}

export function normalizeNameKey(raw?: string | null): string {
  if (!raw) return "";
  return raw
    .normalize("NFKC")
    .replace(/\s+/g, "")
    .replace(/([*#†]+|（通讯作者）|\(通讯作者\)|\(correspondingauthor\))$/gi, "")
    .toLowerCase();
}

export function extFromMime(mime?: string | null): string {
  if (mime === "image/jpeg") return "jpg";
  if (mime === "image/webp") return "webp";
  return "png";
}

export function basketKey(kind: BasketEntityKind | string, id: string): string {
  return `${kind}:${id}`;
}

export function getBasketStorageKey(userId?: string | null): string {
  return `labagent.reuseBasket.v1:${userId || "anonymous"}`;
}

export function parseBasketStorage(raw: string | null): BasketState | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<BasketState>;
    if (parsed.version !== 1 || !Array.isArray(parsed.items)) return null;
    const seen = new Set<string>();
    const validKinds = new Set<string>([
      "person",
      "project",
      "achievement",
      "facility",
    ]);
    const cleanItems: BasketItem[] = [];
    for (const item of parsed.items) {
      if (
        !item ||
        typeof item !== "object" ||
        typeof item.kind !== "string" ||
        !validKinds.has(item.kind) ||
        (typeof item.id !== "string" && typeof item.id !== "number")
      ) {
        continue;
      }
      const idStr = String(item.id);
      const k = basketKey(item.kind, idStr);
      if (seen.has(k)) continue;
      seen.add(k);
      cleanItems.push({
        kind: item.kind as BasketEntityKind,
        id: idStr,
        label: typeof item.label === "string" ? item.label : idStr,
      });
      if (cleanItems.length >= BASKET_MAX_ITEMS) break;
    }
    const opts = parsed.options;
    const cleanOptions: BasketOptions = {
      with_support:
        typeof opts?.with_support === "boolean"
          ? opts.with_support
          : DEFAULT_BASKET_OPTIONS.with_support,
      with_relation:
        typeof opts?.with_relation === "boolean"
          ? opts.with_relation
          : DEFAULT_BASKET_OPTIONS.with_relation,
      numbering:
        opts?.numbering === "none" ||
        opts?.numbering === "arabic" ||
        opts?.numbering === "bracket"
          ? opts.numbering
          : DEFAULT_BASKET_OPTIONS.numbering,
      group_by_kind:
        typeof opts?.group_by_kind === "boolean"
          ? opts.group_by_kind
          : DEFAULT_BASKET_OPTIONS.group_by_kind,
      include_signatures:
        typeof opts?.include_signatures === "boolean"
          ? opts.include_signatures
          : DEFAULT_BASKET_OPTIONS.include_signatures,
      naming:
        opts?.naming === "seq_name" ||
        opts?.naming === "name" ||
        opts?.naming === "name_id"
          ? opts.naming
          : DEFAULT_BASKET_OPTIONS.naming,
    };
    return {
      version: 1,
      items: cleanItems,
      options: cleanOptions,
    };
  } catch {
    return null;
  }
}

export type PersonOverrideState = {
  exclude: boolean;
  login_name: string;
};

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

export function extractProjectYear(proj: LabProjectItem): number | null {
  if (proj.start_date && proj.start_date.length >= 4) {
    const y = Number(proj.start_date.slice(0, 4));
    if (!Number.isNaN(y) && y >= 1900 && y <= 2100) return y;
  }
  if (proj.end_date && proj.end_date.length >= 4) {
    const y = Number(proj.end_date.slice(0, 4));
    if (!Number.isNaN(y) && y >= 1900 && y <= 2100) return y;
  }
  const m4 = `${proj.grant_no || ""} ${proj.raw_text || ""}`.match(
    /\b(19\d{2}|20\d{2})\b/,
  );
  if (m4) return Number(m4[1]);
  const mGrant = (proj.grant_no || "").match(/^[A-Za-z]+(\d{2})\d{3,}$/);
  if (mGrant) return 2000 + Number(mGrant[1]);
  return null;
}

export function triggerBlobDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

