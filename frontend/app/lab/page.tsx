"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "cn";
import {
  ApiError,
  api,
  currentUser,
  reindexLabAssets,
  restoreImportedField,
  updateLabEntity,
  type FieldProvenanceSummary,
  type ImportJobResponse,
  type LabAchievementItem,
  type LabFacilityItem,
  type LabOverview,
  type LabPersonItem,
  type LabProjectItem,
  type ProvisionResult,
  type User,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type TabKey = "people" | "projects" | "achievements" | "facilities";

type EditableFieldConfig = {
  key: string;
  label: string;
  type?: "text" | "textarea" | "select" | "number" | "date";
  options?: Array<{ value: string; label: string }>;
  adminOnly?: boolean;
};

const EDITABLE_FIELDS: Record<TabKey, EditableFieldConfig[]> = {
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

const FIELD_LABEL_MAP: Record<string, string> = Object.values(EDITABLE_FIELDS)
  .flat()
  .reduce<Record<string, string>>((acc, item) => {
    acc[item.key] = item.label;
    return acc;
  }, {});

function formatProvValue(val: unknown): string {
  if (val === null || val === undefined || val === "") return "（空）";
  if (typeof val === "string") return val;
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    return JSON.stringify(val);
  } catch {
    return String(val);
  }
}

type PersonOverrideState = {
  exclude: boolean;
  login_name: string;
};

const DEFAULT_INITIAL_PASSWORD = "LabAgent@2026";
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

function extractProjectYear(proj: LabProjectItem): number | null {
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

export default function LabAssetsPage() {
  const { t } = useI18n();
  const [me, setMe] = useState<User | null>(null);
  const [overview, setOverview] = useState<LabOverview | null>(null);
  const [people, setPeople] = useState<LabPersonItem[]>([]);
  const [projects, setProjects] = useState<LabProjectItem[]>([]);
  const [achievements, setAchievements] = useState<LabAchievementItem[]>([]);
  const [facilities, setFacilities] = useState<LabFacilityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [statusBanner, setStatusBanner] = useState("");

  const [activeTab, setActiveTab] = useState<TabKey>("people");
  const [searchQuery, setSearchQuery] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");

  // Spec §7A.2–7A.3 quick filter & formatting switches
  const [autoNumber, setAutoNumber] = useState(true);
  const [onlyOngoingProjects, setOnlyOngoingProjects] = useState(false);
  const [onlyRecentProjects, setOnlyRecentProjects] = useState(false);
  const [includeProjectRel, setIncludeProjectRel] = useState(false);
  const [onlyRecentAchievements, setOnlyRecentAchievements] = useState(false);
  const [withSupportNote, setWithSupportNote] = useState(true);

  const [selectedIds, setSelectedIds] = useState<Record<string, boolean>>({});
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [expandedBioId, setExpandedBioId] = useState<string | null>(null);
  const [expandedProvId, setExpandedProvId] = useState<string | null>(null);

  const [exportingFormat, setExportingFormat] = useState<"md" | "docx" | null>(
    null,
  );
  const [reindexing, setReindexing] = useState(false);

  // Phase 2 Online Edit & Field Provenance Restore state
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editType, setEditType] = useState<TabKey>("people");
  const [editItem, setEditItem] = useState<
    LabPersonItem | LabProjectItem | LabAchievementItem | LabFacilityItem | null
  >(null);
  const [editForm, setEditForm] = useState<Record<string, string>>({});
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState("");
  const [restoringFieldKey, setRestoringFieldKey] = useState<string | null>(
    null,
  );

  // Admin 3-Step Import Wizard state
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardStep, setWizardStep] = useState<1 | 2 | 3>(1);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadAsOf, setUploadAsOf] = useState("");
  const [parsing, setParsing] = useState(false);
  const [wizardError, setWizardError] = useState("");

  const [importJob, setImportJob] = useState<ImportJobResponse | null>(null);
  const [provisionAsOf, setProvisionAsOf] = useState("");
  const [initialPassword, setInitialPassword] = useState(
    DEFAULT_INITIAL_PASSWORD,
  );
  const [includePassword, setIncludePassword] = useState(true);
  const [overrides, setOverrides] = useState<
    Record<string, PersonOverrideState>
  >({});
  const [provisioning, setProvisioning] = useState(false);
  const [provisionResult, setProvisionResult] =
    useState<ProvisionResult | null>(null);
  const [rosterCopied, setRosterCopied] = useState(false);
  const [hasSavedCredentials, setHasSavedCredentials] = useState(false);

  const loadAll = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const [actor, ov, ppl, projs, achs, facs] = await Promise.all([
        currentUser(),
        api<LabOverview>("lab/overview"),
        api<{ people: LabPersonItem[] }>("lab/people"),
        api<{ projects: LabProjectItem[] }>("lab/projects"),
        api<{ achievements: LabAchievementItem[] }>("lab/achievements"),
        api<{ facilities: LabFacilityItem[] }>("lab/facilities"),
      ]);
      setMe(actor);
      setOverview(ov);
      setPeople(ppl.people);
      setProjects(projs.projects);
      setAchievements(achs.achievements);
      setFacilities(facs.facilities);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setLoading(false);
    }
  }, [t.error]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      currentUser(),
      api<LabOverview>("lab/overview"),
      api<{ people: LabPersonItem[] }>("lab/people"),
      api<{ projects: LabProjectItem[] }>("lab/projects"),
      api<{ achievements: LabAchievementItem[] }>("lab/achievements"),
      api<{ facilities: LabFacilityItem[] }>("lab/facilities"),
    ])
      .then(([actor, ov, ppl, projs, achs, facs]) => {
        if (cancelled) return;
        setMe(actor);
        setOverview(ov);
        setPeople(ppl.people);
        setProjects(projs.projects);
        setAchievements(achs.achievements);
        setFacilities(facs.facilities);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : t.error);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [t.error]);

  // Spec §8.3.3 beforeunload guard when Step 3 has plaintext credentials in memory
  const hasUnsavedPlaintextRoster = Boolean(
    wizardOpen &&
      wizardStep === 3 &&
      provisionResult &&
      includePassword &&
      !hasSavedCredentials,
  );

  useEffect(() => {
    if (!hasUnsavedPlaintextRoster) return;
    function handleBeforeUnload(e: BeforeUnloadEvent) {
      e.preventDefault();
      e.returnValue = t.credentialLeaveConfirm;
      return t.credentialLeaveConfirm;
    }
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
    };
  }, [hasUnsavedPlaintextRoster, t.credentialLeaveConfirm]);

  function tryCloseWizard() {
    if (
      wizardStep === 3 &&
      provisionResult &&
      includePassword &&
      !hasSavedCredentials
    ) {
      const confirmed = window.confirm(t.credentialLeaveConfirm);
      if (!confirmed) return;
    }
    setWizardOpen(false);
    setProvisionResult(null);
  }

  async function copyToClipboard(text: string, tag: string) {
    setError("");
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(tag);
      setTimeout(() => {
        setCopiedId((prev) => (prev === tag ? null : prev));
      }, 1800);
    } catch {
      setError("复制到剪贴板失败，请检查浏览器剪贴板权限或手动复制");
    }
  }

  const baseYear = useMemo(() => {
    const asOf = overview ? overview.as_of_time : null;
    if (asOf && asOf.length >= 4) {
      const y = Number(asOf.slice(0, 4));
      if (!Number.isNaN(y) && y >= 1990) return y;
    }
    return new Date().getFullYear();
  }, [overview]);

  const filteredPeople = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return people.filter((p) => {
      if (roleFilter && p.project_role !== roleFilter) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        (p.research_specialty || "").toLowerCase().includes(q) ||
        (p.title || "").toLowerCase().includes(q) ||
        (p.affiliation || "").toLowerCase().includes(q)
      );
    });
  }, [people, roleFilter, searchQuery]);

  const filteredProjects = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const minYear = baseYear - 5;
    return projects.filter((j) => {
      if (statusFilter && j.status !== statusFilter) return false;
      if (onlyOngoingProjects && j.status !== "ongoing") return false;
      if (onlyRecentProjects) {
        const py = extractProjectYear(j);
        if (py === null || py < minYear) return false;
      }
      if (!q) return true;
      return (
        j.title.toLowerCase().includes(q) ||
        (j.grant_no || "").toLowerCase().includes(q) ||
        (j.funder || "").toLowerCase().includes(q) ||
        (j.pi_name || "").toLowerCase().includes(q)
      );
    });
  }, [
    projects,
    statusFilter,
    onlyOngoingProjects,
    onlyRecentProjects,
    baseYear,
    searchQuery,
  ]);

  const filteredAchievements = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const minYear = baseYear - 5;
    return achievements.filter((a) => {
      if (kindFilter && a.kind !== kindFilter) return false;
      if (onlyRecentAchievements) {
        if (a.year == null || a.year < minYear) return false;
      }
      if (!q) return true;
      return (
        a.title.toLowerCase().includes(q) ||
        (a.first_author || "").toLowerCase().includes(q) ||
        (a.venue || "").toLowerCase().includes(q) ||
        (a.support_note || "").toLowerCase().includes(q)
      );
    });
  }, [achievements, kindFilter, onlyRecentAchievements, baseYear, searchQuery]);

  const filteredFacilities = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return facilities.filter((f) => {
      if (!q) return true;
      return (
        f.name.toLowerCase().includes(q) ||
        (f.spec || "").toLowerCase().includes(q) ||
        (f.usage_scenario || "").toLowerCase().includes(q)
      );
    });
  }, [facilities, searchQuery]);

  function getProjectText(proj: LabProjectItem): string {
    return includeProjectRel
      ? proj.application_text_with_rel
      : proj.application_text;
  }

  function getAchievementText(ach: LabAchievementItem): string {
    return withSupportNote
      ? ach.application_text
      : ach.application_text_plain;
  }

  function formatBatchLines(lines: string[]): string {
    if (lines.length === 0) return "";
    if (autoNumber) {
      return lines.map((line, idx) => `[${idx + 1}] ${line}`).join("\n");
    }
    return lines.join("\n");
  }

  function collectTabLines(onlySelected: boolean): string[] {
    if (activeTab === "people") {
      const items = onlySelected
        ? filteredPeople.filter((item) => selectedIds[item.id])
        : filteredPeople;
      return items.map((item) => item.application_text);
    }
    if (activeTab === "projects") {
      const items = onlySelected
        ? filteredProjects.filter((item) => selectedIds[item.id])
        : filteredProjects;
      return items.map((item) => getProjectText(item));
    }
    if (activeTab === "achievements") {
      const items = onlySelected
        ? filteredAchievements.filter((item) => selectedIds[item.id])
        : filteredAchievements;
      return items.map((item) => getAchievementText(item));
    }
    const items = onlySelected
      ? filteredFacilities.filter((item) => selectedIds[item.id])
      : filteredFacilities;
    return items.map((item) => item.application_text);
  }

  function handleBatchCopySelected() {
    const lines = collectTabLines(true);
    if (lines.length === 0) return;
    copyToClipboard(formatBatchLines(lines), "batch-selected");
  }

  function handleBatchCopyAllTab() {
    const lines = collectTabLines(false);
    if (lines.length === 0) return;
    copyToClipboard(formatBatchLines(lines), "batch-all");
  }

  const currentTabListLength = useMemo(() => {
    if (activeTab === "people") return filteredPeople.length;
    if (activeTab === "projects") return filteredProjects.length;
    if (activeTab === "achievements") return filteredAchievements.length;
    return filteredFacilities.length;
  }, [
    activeTab,
    filteredPeople.length,
    filteredProjects.length,
    filteredAchievements.length,
    filteredFacilities.length,
  ]);

  const selectedCount = useMemo(() => {
    const currentList =
      activeTab === "people"
        ? filteredPeople
        : activeTab === "projects"
          ? filteredProjects
          : activeTab === "achievements"
            ? filteredAchievements
            : filteredFacilities;
    return currentList.filter((item) => selectedIds[item.id]).length;
  }, [
    activeTab,
    filteredPeople,
    filteredProjects,
    filteredAchievements,
    filteredFacilities,
    selectedIds,
  ]);

  function toggleSelectAll() {
    const currentList =
      activeTab === "people"
        ? filteredPeople
        : activeTab === "projects"
          ? filteredProjects
          : activeTab === "achievements"
            ? filteredAchievements
            : filteredFacilities;
    const allChecked =
      currentList.length > 0 &&
      currentList.every((item) => selectedIds[item.id]);
    const next = { ...selectedIds };
    for (const item of currentList) {
      next[item.id] = !allChecked;
    }
    setSelectedIds(next);
  }

  async function handleExportAll(format: "md" | "docx") {
    setError("");
    setStatusBanner("");
    setExportingFormat(format);
    try {
      const response = await fetch(
        `/api/backend/lab/export?format=${format}`,
        {
          method: "GET",
          cache: "no-store",
        },
      );
      if (!response.ok) {
        let msg = t.error;
        try {
          const errBody = (await response.json()) as {
            error?: { message?: string };
          };
          if (errBody.error?.message) msg = errBody.error.message;
        } catch {
          // ignore JSON parse failure
        }
        throw new Error(msg);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = format === "docx" ? "lab-assets.docx" : "lab-assets.md";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : t.error);
    } finally {
      setExportingFormat(null);
    }
  }

  async function handleReindex() {
    setError("");
    setStatusBanner("");
    setReindexing(true);
    try {
      const res = await reindexLabAssets();
      setStatusBanner(`${t.reindexLabSuccess} (${res.rag_doc_key})`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setReindexing(false);
    }
  }

  function handleFileChange(file: File | null) {
    setWizardError("");
    if (!file) {
      setUploadFile(null);
      return;
    }
    const lower = file.name.toLowerCase();
    if (!lower.endsWith(".docx") || lower.endsWith(".docm") || lower.endsWith(".doc")) {
      setWizardError("仅支持上传 .docx 格式的申报书文件（不支持 .doc 或 .docm）");
      setUploadFile(null);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setWizardError("文件大小超过 20 MB 上限");
      setUploadFile(null);
      return;
    }
    setUploadFile(file);
  }

  async function handleParseUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!uploadFile) {
      setWizardError("请先选择一个 .docx 申报书文件");
      return;
    }
    if (!uploadFile.name.toLowerCase().endsWith(".docx")) {
      setWizardError("仅支持上传 .docx 格式的申报书文件");
      return;
    }
    if (uploadFile.size > MAX_UPLOAD_BYTES) {
      setWizardError("文件大小超过 20 MB 上限");
      return;
    }
    setWizardError("");
    setParsing(true);
    try {
      const form = new FormData();
      form.append("file", uploadFile);
      if (uploadAsOf.trim()) {
        form.append("as_of_time", uploadAsOf.trim());
      }
      const parsed = await api<ImportJobResponse>("admin/imports/proposal", {
        method: "POST",
        body: form,
      });
      setImportJob(parsed);
      setProvisionAsOf(parsed.as_of_time || parsed.preview?.as_of_time || "");
      const initOv: Record<string, PersonOverrideState> = {};
      for (const p of parsed.preview?.people || []) {
        initOv[p.key] = {
          exclude: false,
          login_name: p.login_name || "",
        };
      }
      setOverrides(initOv);
      setWizardStep(2);
    } catch (err) {
      setWizardError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setParsing(false);
    }
  }

  async function handleProvision() {
    if (!importJob) return;
    if (!provisionAsOf.trim()) {
      setWizardError("请填写本子基准日期 (as_of_time)");
      return;
    }
    setWizardError("");
    setProvisioning(true);
    try {
      const peoplePayload = (importJob.preview?.people || []).map((p) => {
        const ov = overrides[p.key];
        return {
          key: p.key,
          exclude: Boolean(ov?.exclude),
          ...(ov?.login_name && ov.login_name !== p.login_name
            ? { login_name: ov.login_name.trim() }
            : {}),
        };
      });
      const res = await api<ProvisionResult>(
        `admin/imports/${importJob.job_id}/provision`,
        {
          method: "POST",
          body: JSON.stringify({
            as_of_time: provisionAsOf.trim() || undefined,
            initial_password: initialPassword.trim() || undefined,
            include_password: includePassword,
            people: peoplePayload,
          }),
        },
      );
      setProvisionResult(res);
      setHasSavedCredentials(false);
      setWizardStep(3);
      await loadAll();
    } catch (err) {
      setWizardError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setProvisioning(false);
    }
  }

  function handleDownloadCsv() {
    if (!provisionResult) return;
    const blob = new Blob([provisionResult.roster_csv], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `labagent-accounts-${provisionResult.as_of_time}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    setHasSavedCredentials(true);
  }

  const myAllowedNames = useMemo(() => {
    const names = new Set<string>();
    if (me?.display_name) names.add(me.display_name.trim());
    if (me?.id) {
      for (const p of people) {
        if (p.user_id === me.id && p.name) {
          names.add(p.name.trim());
        }
      }
    }
    return names;
  }, [me, people]);

  function canEditEntity(
    type: TabKey,
    item: LabPersonItem | LabProjectItem | LabAchievementItem | LabFacilityItem,
  ): boolean {
    if (!me) return false;
    if (me.role === "admin") return true;
    if (me.role !== "member") return false;
    if (type === "people") {
      const p = item as LabPersonItem;
      return p.user_id === me.id || myAllowedNames.has((p.name || "").trim());
    }
    if (type === "projects") {
      const proj = item as LabProjectItem;
      return Boolean(
        proj.pi_name && myAllowedNames.has(proj.pi_name.trim()),
      );
    }
    if (type === "achievements") {
      const ach = item as LabAchievementItem;
      return Boolean(
        ach.first_author && myAllowedNames.has(ach.first_author.trim()),
      );
    }
    return false;
  }

  function openEditModal(
    type: TabKey,
    item: LabPersonItem | LabProjectItem | LabAchievementItem | LabFacilityItem,
  ) {
    setEditError("");
    setEditType(type);
    setEditItem(item);
    const initialForm: Record<string, string> = {};
    const fields = EDITABLE_FIELDS[type];
    const rec = item as unknown as Record<string, unknown>;
    for (const f of fields) {
      const val = rec[f.key];
      initialForm[f.key] =
        val === null || val === undefined ? "" : String(val);
    }
    setEditForm(initialForm);
    setEditModalOpen(true);
  }

  async function handleSaveEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editItem) return;
    setEditError("");
    setStatusBanner("");
    setEditSaving(true);
    try {
      const payload: Record<string, unknown> = {};
      for (const f of EDITABLE_FIELDS[editType]) {
        if (f.adminOnly && me?.role !== "admin") continue;
        const rawVal = editForm[f.key] ?? "";
        if (f.type === "number") {
          const trimmed = rawVal.trim();
          payload[f.key] = trimmed ? Number(trimmed) : null;
        } else {
          payload[f.key] = rawVal;
        }
      }
      const res = await updateLabEntity(editType, editItem.id, payload);
      const updatedEntity = (res.person ||
        res.project ||
        res.achievement ||
        res.facility) as
        | LabPersonItem
        | LabProjectItem
        | LabAchievementItem
        | LabFacilityItem
        | undefined;
      if (updatedEntity) {
        setEditItem(updatedEntity);
      }
      setEditModalOpen(false);
      setStatusBanner("已保存修改并记录字段级人工溯源");
      await loadAll();
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setEditSaving(false);
    }
  }

  async function handleRestoreImported(
    type: TabKey,
    itemId: string,
    field: string,
  ) {
    setError("");
    setEditError("");
    setStatusBanner("");
    const opKey = `${itemId}:${field}`;
    setRestoringFieldKey(opKey);
    try {
      const res = await restoreImportedField(type, itemId, field);
      const updatedEntity = (res.person ||
        res.project ||
        res.achievement ||
        res.facility) as
        | LabPersonItem
        | LabProjectItem
        | LabAchievementItem
        | LabFacilityItem
        | undefined;
      if (updatedEntity && editItem && editItem.id === itemId) {
        setEditItem(updatedEntity);
        const rec = updatedEntity as unknown as Record<string, unknown>;
        const restoredVal = rec[field];
        setEditForm((prev) => ({
          ...prev,
          [field]:
            restoredVal === null || restoredVal === undefined
              ? ""
              : String(restoredVal),
        }));
      }
      setStatusBanner(
        `字段「${FIELD_LABEL_MAP[field] || field}」已恢复为申报书导入值`,
      );
      await loadAll();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t.error;
      if (editModalOpen) {
        setEditError(msg);
      } else {
        setError(msg);
      }
    } finally {
      setRestoringFieldKey(null);
    }
  }

  function renderEntitySourceBadges(
    asOfTime?: string | null,
    provenance?: Record<string, FieldProvenanceSummary>,
  ) {
    const provValues = provenance ? Object.values(provenance) : [];
    const hasManual = provValues.some(
      (p) => p.source_kind === "manual" || p.has_manual_override,
    );
    return (
      <>
        <Badge
          variant="outline"
          className="border-border bg-muted/30 font-normal text-[11px] text-muted-foreground"
        >
          {t.provenanceSourceProposal} ({asOfTime || "—"})
        </Badge>
        {hasManual ? (
          <Badge
            variant="secondary"
            className="border-purple-500/40 bg-purple-500/15 font-normal text-[11px] text-purple-800 dark:text-purple-300"
          >
            {t.provenanceSourceManual}
          </Badge>
        ) : null}
      </>
    );
  }

  function renderFieldProvenanceBadge(prov?: FieldProvenanceSummary) {
    if (!prov) return null;
    if (prov.source_kind === "manual") {
      return (
        <span className="inline-flex flex-wrap items-center gap-1.5 text-[11px]">
          <Badge
            variant="secondary"
            className="border-purple-500/40 bg-purple-500/15 px-1.5 py-0 text-[11px] font-normal text-purple-800 dark:text-purple-300"
          >
            {t.provenanceSourceManual}
          </Badge>
          {prov.has_manual_override &&
          prov.imported_value !== undefined &&
          prov.imported_value !== null &&
          prov.imported_value !== "" ? (
            <span className="text-muted-foreground">
              原导入值：{formatProvValue(prov.imported_value)}
            </span>
          ) : null}
        </span>
      );
    }
    if (prov.source_kind === "legacy") {
      return (
        <Badge
          variant="outline"
          className="px-1.5 py-0 text-[11px] font-normal text-muted-foreground"
        >
          {t.provenanceSourceLegacy} ({prov.as_of_time || "—"})
        </Badge>
      );
    }
    return (
      <Badge
        variant="outline"
        className="px-1.5 py-0 text-[11px] font-normal text-muted-foreground"
      >
        {t.provenanceSourceProposal} ({prov.as_of_time || "—"})
      </Badge>
    );
  }

  function renderCardProvenanceSection(
    type: TabKey,
    item: LabPersonItem | LabProjectItem | LabAchievementItem | LabFacilityItem,
  ) {
    const provMap = item.provenance;
    if (!provMap || Object.keys(provMap).length === 0) return null;
    const editable = canEditEntity(type, item);
    const entries = Object.entries(provMap);
    const manualOverrides = entries.filter(
      ([, p]) => p.has_manual_override || p.source_kind === "manual",
    );
    const isExpanded = expandedProvId === item.id;

    return (
      <div className="mt-2 space-y-1.5">
        {manualOverrides.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-purple-500/30 bg-purple-500/5 px-2.5 py-1.5 text-[12px]">
            {manualOverrides.map(([field, p]) => {
              const opKey = `${item.id}:${field}`;
              return (
                <div
                  key={field}
                  className="inline-flex flex-wrap items-center gap-1.5"
                >
                  <span className="font-medium text-foreground">
                    {FIELD_LABEL_MAP[field] || field}：
                  </span>
                  <span className="text-purple-800 dark:text-purple-300">
                    {t.provenanceSourceManual}
                  </span>
                  {p.has_manual_override &&
                  p.imported_value !== undefined &&
                  p.imported_value !== null ? (
                    <span className="text-muted-foreground">
                      （原导入值：{formatProvValue(p.imported_value)}）
                    </span>
                  ) : null}
                  {editable && p.has_manual_override ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="h-6 px-2 text-[11px]"
                      disabled={restoringFieldKey === opKey}
                      onClick={() => handleRestoreImported(type, item.id, field)}
                    >
                      {restoringFieldKey === opKey
                        ? t.loading
                        : t.restoreImportedBtn}
                    </Button>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : null}

        <div>
          <button
            type="button"
            className="text-[11px] text-muted-foreground underline hover:text-foreground"
            onClick={() =>
              setExpandedProvId((prev) => (prev === item.id ? null : item.id))
            }
          >
            {isExpanded
              ? "收起字段溯源明细"
              : `查看字段级溯源 (${entries.length})`}
          </button>
          {isExpanded ? (
            <div className="mt-1.5 grid grid-cols-1 gap-1.5 rounded-md border border-border bg-muted/20 p-2.5 text-[12px] sm:grid-cols-2">
              {entries.map(([field, p]) => {
                const opKey = `${item.id}:${field}`;
                return (
                  <div
                    key={field}
                    className="flex flex-wrap items-center justify-between gap-1.5 rounded border border-border/60 bg-card px-2 py-1"
                  >
                    <span className="font-medium text-foreground">
                      {FIELD_LABEL_MAP[field] || field}
                    </span>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {renderFieldProvenanceBadge(p)}
                      {editable && p.has_manual_override ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-6 px-2 text-[11px]"
                          disabled={restoringFieldKey === opKey}
                          onClick={() =>
                            handleRestoreImported(type, item.id, field)
                          }
                        >
                          {restoringFieldKey === opKey
                            ? t.loading
                            : t.restoreImportedBtn}
                        </Button>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <Shell>
      <div className="space-y-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="font-display text-[20px] font-medium leading-snug tracking-tight text-foreground">
              {t.labAssets}
            </h1>
            <p className="mt-1 font-serif text-[15px] leading-[1.6] text-muted-foreground">
              {t.labAssetsSubtitle}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={exportingFormat !== null}
              onClick={() => handleExportAll("md")}
            >
              {exportingFormat === "md" ? t.loading : t.exportMdBtn}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={exportingFormat !== null}
              onClick={() => handleExportAll("docx")}
            >
              {exportingFormat === "docx" ? t.loading : t.exportDocxBtn}
            </Button>
            {me?.role === "admin" || me?.role === "member" ? (
              <Button
                size="sm"
                variant="outline"
                disabled={reindexing}
                onClick={handleReindex}
              >
                {reindexing ? t.reindexingLab : t.reindexLabBtn}
              </Button>
            ) : null}
            {me?.role === "admin" ? (
              <>
                <Link
                  href="/admin/imports"
                  className={cn(
                    buttonVariants({ variant: "outline", size: "sm" }),
                  )}
                >
                  {t.importsNav}
                </Link>
                <Button
                  size="sm"
                  onClick={() => {
                    setWizardError("");
                    setWizardStep(1);
                    setUploadFile(null);
                    setImportJob(null);
                    setProvisionResult(null);
                    setHasSavedCredentials(false);
                    setWizardOpen(true);
                  }}
                >
                  {t.importProposalBtn}
                </Button>
              </>
            ) : null}
          </div>
        </div>

        {error ? (
          <p role="alert" className="text-[13px] text-destructive">
            {error}
          </p>
        ) : null}

        {statusBanner ? (
          <p role="status" className="text-[13px] font-medium text-emerald-600">
            {statusBanner}
          </p>
        ) : null}

        {overview ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <div className="rounded-lg border border-border bg-card p-3.5">
              <p className="text-[12px] text-muted-foreground">{t.tabPeople}</p>
              <p className="mt-1 font-display text-[22px] font-medium text-foreground">
                {overview.counts.people}
              </p>
            </div>
            <div className="rounded-lg border border-border bg-card p-3.5">
              <p className="text-[12px] text-muted-foreground">{t.tabProjects}</p>
              <div className="mt-1 flex items-baseline gap-2">
                <span className="font-display text-[22px] font-medium text-foreground">
                  {overview.counts.projects}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  在研 {overview.counts.projects_ongoing} · 结题{" "}
                  {overview.counts.projects_completed}
                </span>
              </div>
            </div>
            <div className="rounded-lg border border-border bg-card p-3.5">
              <p className="text-[12px] text-muted-foreground">
                {t.tabAchievements}
              </p>
              <p className="mt-1 font-display text-[22px] font-medium text-foreground">
                {overview.counts.achievements}
              </p>
            </div>
            <div className="rounded-lg border border-border bg-card p-3.5">
              <p className="text-[12px] text-muted-foreground">
                {t.tabFacilities}
              </p>
              <p className="mt-1 font-display text-[22px] font-medium text-foreground">
                {overview.counts.facilities}
              </p>
            </div>
            <div className="col-span-2 rounded-lg border border-border bg-card p-3.5 sm:col-span-1">
              <p className="text-[12px] text-muted-foreground">本子基准时间</p>
              <p className="mt-1 font-mono text-[14px] font-medium text-foreground">
                {overview.as_of_time || "—"}
              </p>
              {overview.latest_import ? (
                <p
                  className="mt-0.5 truncate text-[11px] text-muted-foreground"
                  title={overview.latest_import.original_filename}
                >
                  {overview.latest_import.original_filename}
                </p>
              ) : null}
            </div>
          </div>
        ) : null}

        {/* Tabs & Toolbar */}
        <div className="space-y-2.5 border-b border-border pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap gap-1.5">
              {(
                [
                  ["people", t.tabPeople, people.length],
                  ["projects", t.tabProjects, projects.length],
                  ["achievements", t.tabAchievements, achievements.length],
                  ["facilities", t.tabFacilities, facilities.length],
                ] as const
              ).map(([key, label, count]) => (
                <Button
                  key={key}
                  size="sm"
                  variant={activeTab === key ? "default" : "outline"}
                  className="h-8 px-3 text-[13px]"
                  onClick={() => {
                    setActiveTab(key);
                    setSearchQuery("");
                  }}
                >
                  {label}
                  <span className="ml-1.5 font-mono text-[11px] opacity-80">
                    ({count})
                  </span>
                </Button>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Input
                type="search"
                placeholder="搜索姓名、编号、题目、专长…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-8 w-52 text-[13px]"
              />
              {activeTab === "people" ? (
                <select
                  value={roleFilter}
                  onChange={(e) => setRoleFilter(e.target.value)}
                  className="h-8 rounded-md border border-input bg-card px-2 text-[12px] text-foreground"
                >
                  <option value="">全部角色</option>
                  <option value="applicant">课题负责人/申请人</option>
                  <option value="member">项目组成员</option>
                </select>
              ) : null}
              {activeTab === "projects" ? (
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value)}
                  className="h-8 rounded-md border border-input bg-card px-2 text-[12px] text-foreground"
                >
                  <option value="">全部状态</option>
                  <option value="ongoing">在研</option>
                  <option value="completed">已结题</option>
                </select>
              ) : null}
              {activeTab === "achievements" ? (
                <select
                  value={kindFilter}
                  onChange={(e) => setKindFilter(e.target.value)}
                  className="h-8 rounded-md border border-input bg-card px-2 text-[12px] text-foreground"
                >
                  <option value="">全部类型</option>
                  <option value="report">研究报告/资政报告</option>
                  <option value="exhibition">成果展</option>
                  <option value="paper">论文</option>
                  <option value="patent">发明专利</option>
                  <option value="software">软件著作权</option>
                  <option value="book">学术专著</option>
                </select>
              ) : null}
              <Button
                size="sm"
                variant="outline"
                className="h-8 px-2.5 text-[12px]"
                onClick={toggleSelectAll}
              >
                全选/反选
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-8 px-3 text-[12px]"
                disabled={selectedCount === 0}
                onClick={handleBatchCopySelected}
              >
                {copiedId === "batch-selected"
                  ? t.copiedGrantFormat
                  : `${t.copySelectedGrantFormat} (${selectedCount})`}
              </Button>
              <Button
                size="sm"
                className="h-8 px-3 text-[12px]"
                disabled={currentTabListLength === 0}
                onClick={handleBatchCopyAllTab}
              >
                {copiedId === "batch-all"
                  ? t.copiedGrantFormat
                  : `${t.copyAllTabGrantFormat} (${currentTabListLength})`}
              </Button>
            </div>
          </div>

          {/* Spec §7A.3 Quick filter & formatting switches bar */}
          <div className="flex flex-wrap items-center gap-4 text-[12px] text-muted-foreground">
            <label className="flex cursor-pointer items-center gap-1.5 text-foreground">
              <input
                type="checkbox"
                checked={autoNumber}
                onChange={(e) => setAutoNumber(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-border"
              />
              批量复制自动编号{" "}
              <span className="font-mono text-[11px] text-muted-foreground">
                [1] [2] …
              </span>
            </label>

            {activeTab === "projects" ? (
              <>
                <label className="flex cursor-pointer items-center gap-1.5 text-foreground">
                  <input
                    type="checkbox"
                    checked={onlyOngoingProjects}
                    onChange={(e) => setOnlyOngoingProjects(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  仅在研
                </label>
                <label className="flex cursor-pointer items-center gap-1.5 text-foreground">
                  <input
                    type="checkbox"
                    checked={onlyRecentProjects}
                    onChange={(e) => setOnlyRecentProjects(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  仅近 5 年
                </label>
                <label className="flex cursor-pointer items-center gap-1.5 text-foreground">
                  <input
                    type="checkbox"
                    checked={includeProjectRel}
                    onChange={(e) => setIncludeProjectRel(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  附带与本课题关系说明
                </label>
              </>
            ) : null}

            {activeTab === "achievements" ? (
              <>
                <label className="flex cursor-pointer items-center gap-1.5 text-foreground">
                  <input
                    type="checkbox"
                    checked={onlyRecentAchievements}
                    onChange={(e) =>
                      setOnlyRecentAchievements(e.target.checked)
                    }
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  仅近 5 年
                </label>
                <label className="flex cursor-pointer items-center gap-1.5 text-foreground">
                  <input
                    type="checkbox"
                    checked={withSupportNote}
                    onChange={(e) => setWithSupportNote(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  附带支撑价值说明 (?with_support=1)
                </label>
              </>
            ) : null}
          </div>
        </div>

        {loading ? (
          <p className="text-[13px] text-muted-foreground">{t.loading}</p>
        ) : activeTab === "people" ? (
          <div className="grid gap-3">
            {filteredPeople.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的团队成员记录。
              </p>
            ) : (
              filteredPeople.map((person) => (
                <div
                  key={person.id}
                  className="rounded-lg border border-border bg-card p-4 transition-colors hover:bg-muted/10"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={Boolean(selectedIds[person.id])}
                        onChange={(e) =>
                          setSelectedIds((prev) => ({
                            ...prev,
                            [person.id]: e.target.checked,
                          }))
                        }
                        className="mt-1 h-4 w-4 rounded border-border"
                      />
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium text-[15px] text-foreground">
                            {person.name}
                          </span>
                          <Badge
                            variant={
                              person.project_role === "applicant"
                                ? "secondary"
                                : "outline"
                            }
                          >
                            {person.project_role === "applicant"
                              ? "课题负责人"
                              : "项目组成员"}
                          </Badge>
                          {person.degree ? (
                            <Badge variant="outline">{person.degree}</Badge>
                          ) : null}
                          {person.title && person.title !== "无" ? (
                            <Badge variant="outline">{person.title}</Badge>
                          ) : null}
                          {person.tutor_role && person.tutor_role !== "无" ? (
                            <Badge variant="outline">{person.tutor_role}</Badge>
                          ) : null}
                          {renderEntitySourceBadges(
                            person.as_of_time,
                            person.provenance,
                          )}
                        </div>
                        <p className="mt-1 text-[13px] text-muted-foreground">
                          {[
                            person.affiliation,
                            person.admin_duty && person.admin_duty !== "无"
                              ? person.admin_duty
                              : null,
                            person.research_specialty
                              ? `研究专长：${person.research_specialty}`
                              : null,
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </p>
                        <p className="mt-1.5 font-serif text-[13px] text-foreground/90">
                          {person.application_text}
                        </p>
                        {renderCardProvenanceSection("people", person)}
                        {person.bio ? (
                          <div className="mt-2">
                            <button
                              type="button"
                              className="text-[12px] font-medium text-muted-foreground underline hover:text-foreground"
                              onClick={() =>
                                setExpandedBioId((prev) =>
                                  prev === person.id ? null : person.id,
                                )
                              }
                            >
                              {expandedBioId === person.id
                                ? "收起学术简历"
                                : "展开学术简历"}
                            </button>
                            {expandedBioId === person.id ? (
                              <p className="mt-1.5 rounded-md bg-muted/40 p-2.5 font-serif text-[13px] leading-[1.6] text-foreground">
                                {person.bio}
                              </p>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {canEditEntity("people", person) ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() => openEditModal("people", person)}
                        >
                          {t.editAssetBtn}
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 px-2.5 text-[12px]"
                        onClick={() =>
                          copyToClipboard(person.application_text, person.id)
                        }
                      >
                        {copiedId === person.id
                          ? t.copiedGrantFormat
                          : t.copyGrantFormat}
                      </Button>
                      {person.bio ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            copyToClipboard(
                              person.application_text_with_bio,
                              `${person.id}-bio`,
                            )
                          }
                        >
                          {copiedId === `${person.id}-bio`
                            ? t.copiedGrantFormat
                            : "复制学术简历"}
                        </Button>
                      ) : null}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        ) : activeTab === "projects" ? (
          <div className="grid gap-3">
            {filteredProjects.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的承担项目记录。
              </p>
            ) : (
              filteredProjects.map((proj) => {
                const displayAppText = getProjectText(proj);
                return (
                  <div
                    key={proj.id}
                    className="rounded-lg border border-border bg-card p-4 transition-colors hover:bg-muted/10"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          checked={Boolean(selectedIds[proj.id])}
                          onChange={(e) =>
                            setSelectedIds((prev) => ({
                              ...prev,
                              [proj.id]: e.target.checked,
                            }))
                          }
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium text-[15px] text-foreground">
                              《{proj.title}》
                            </span>
                            {proj.grant_no ? (
                              <Badge variant="outline" className="font-mono">
                                {proj.grant_no}
                              </Badge>
                            ) : null}
                            <Badge
                              variant={
                                proj.status === "ongoing"
                                  ? "secondary"
                                  : "outline"
                              }
                            >
                              {proj.status_label}
                              {proj.status_source === "inferred"
                                ? "（按基准期推断）"
                                : ""}
                            </Badge>
                            {renderEntitySourceBadges(
                              proj.as_of_time,
                              proj.provenance,
                            )}
                          </div>
                          <p className="mt-1 text-[13px] text-muted-foreground">
                            {[
                              proj.pi_name ? `负责人：${proj.pi_name}` : null,
                              proj.funder,
                              proj.start_date
                                ? `立项：${proj.start_date.slice(0, 7)}`
                                : null,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                          <p className="mt-1.5 whitespace-pre-wrap font-serif text-[13px] text-foreground/90">
                            {displayAppText}
                          </p>
                          {renderCardProvenanceSection("projects", proj)}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {canEditEntity("projects", proj) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() => openEditModal("projects", proj)}
                          >
                            {t.editAssetBtn}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            copyToClipboard(displayAppText, proj.id)
                          }
                        >
                          {copiedId === proj.id
                            ? t.copiedGrantFormat
                            : t.copyGrantFormat}
                        </Button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        ) : activeTab === "achievements" ? (
          <div className="grid gap-3">
            {filteredAchievements.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的前期成果记录。
              </p>
            ) : (
              filteredAchievements.map((ach) => {
                const displayAchText = getAchievementText(ach);
                return (
                  <div
                    key={ach.id}
                    className="rounded-lg border border-border bg-card p-4 transition-colors hover:bg-muted/10"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          checked={Boolean(selectedIds[ach.id])}
                          onChange={(e) =>
                            setSelectedIds((prev) => ({
                              ...prev,
                              [ach.id]: e.target.checked,
                            }))
                          }
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge variant="secondary">{ach.kind_label}</Badge>
                            <span className="font-medium text-[15px] text-foreground">
                              《{ach.title}》
                            </span>
                            {ach.year ? (
                              <Badge variant="outline" className="font-mono">
                                {ach.year}
                              </Badge>
                            ) : null}
                            {renderEntitySourceBadges(
                              ach.as_of_time,
                              ach.provenance,
                            )}
                          </div>
                          <p className="mt-1 text-[13px] text-muted-foreground">
                            {[
                              ach.first_author
                                ? `第一作者：${ach.first_author}`
                                : null,
                              ach.venue,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                          <p className="mt-1.5 font-serif text-[13px] text-foreground/90">
                            {displayAchText}
                          </p>
                          {renderCardProvenanceSection("achievements", ach)}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {canEditEntity("achievements", ach) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() => openEditModal("achievements", ach)}
                          >
                            {t.editAssetBtn}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            copyToClipboard(displayAchText, ach.id)
                          }
                        >
                          {copiedId === ach.id
                            ? t.copiedGrantFormat
                            : t.copyGrantFormat}
                        </Button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        ) : (
          <div className="grid gap-3">
            {filteredFacilities.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的设备与支撑条件记录。
              </p>
            ) : (
              filteredFacilities.map((fac) => (
                <div
                  key={fac.id}
                  className="rounded-lg border border-border bg-card p-4 transition-colors hover:bg-muted/10"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={Boolean(selectedIds[fac.id])}
                        onChange={(e) =>
                          setSelectedIds((prev) => ({
                            ...prev,
                            [fac.id]: e.target.checked,
                          }))
                        }
                        className="mt-1 h-4 w-4 rounded border-border"
                      />
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="secondary">
                            {fac.category === "compute"
                              ? "算力设备"
                              : "科研设备"}
                          </Badge>
                          <span className="font-medium text-[15px] text-foreground">
                            {fac.name}
                          </span>
                          {fac.value_wan ? (
                            <Badge variant="outline" className="font-mono">
                              {fac.value_wan} 万元
                            </Badge>
                          ) : null}
                          {renderEntitySourceBadges(
                            fac.as_of_time,
                            fac.provenance,
                          )}
                        </div>
                        <p className="mt-1.5 font-serif text-[13px] text-foreground/90">
                          {fac.application_text}
                        </p>
                        {renderCardProvenanceSection("facilities", fac)}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {canEditEntity("facilities", fac) ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() => openEditModal("facilities", fac)}
                        >
                          {t.editAssetBtn}
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 px-2.5 text-[12px]"
                        onClick={() =>
                          copyToClipboard(fac.application_text, fac.id)
                        }
                      >
                        {copiedId === fac.id
                          ? t.copiedGrantFormat
                          : t.copyGrantFormat}
                      </Button>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        {/* Phase 2 Online Edit & Field Provenance Restore Dialog */}
        <Dialog open={editModalOpen} onOpenChange={setEditModalOpen}>
          <DialogContent className="max-w-2xl max-h-[88vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {t.editAssetBtn} · {t.fieldProvenanceTitle}
              </DialogTitle>
              <DialogDescription>
                修改保存后将自动生成人工溯源记录（优先于新旧申报书导入值）。带有人工覆盖的字段可随时一键「
                {t.restoreImportedBtn}」。
              </DialogDescription>
            </DialogHeader>

            {editError ? (
              <p role="alert" className="text-[13px] text-destructive">
                {editError}
              </p>
            ) : null}

            {editItem ? (
              <form onSubmit={handleSaveEdit} className="space-y-3.5 text-[13px]">
                {EDITABLE_FIELDS[editType].map((fieldCfg) => {
                  if (fieldCfg.adminOnly && me?.role !== "admin") return null;
                  const prov = editItem.provenance?.[fieldCfg.key];
                  const opKey = `${editItem.id}:${fieldCfg.key}`;
                  return (
                    <div key={fieldCfg.key} className="space-y-1">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <Label
                          htmlFor={`edit-field-${fieldCfg.key}`}
                          className="text-[12px] font-medium text-foreground"
                        >
                          {fieldCfg.label}{" "}
                          <span className="font-mono text-[11px] text-muted-foreground">
                            ({fieldCfg.key})
                          </span>
                        </Label>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {renderFieldProvenanceBadge(prov)}
                          {prov?.has_manual_override ? (
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-6 px-2 text-[11px]"
                              disabled={restoringFieldKey === opKey}
                              onClick={() =>
                                handleRestoreImported(
                                  editType,
                                  editItem.id,
                                  fieldCfg.key,
                                )
                              }
                            >
                              {restoringFieldKey === opKey
                                ? t.loading
                                : t.restoreImportedBtn}
                            </Button>
                          ) : null}
                        </div>
                      </div>
                      {fieldCfg.type === "textarea" ? (
                        <textarea
                          id={`edit-field-${fieldCfg.key}`}
                          rows={3}
                          value={editForm[fieldCfg.key] ?? ""}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              [fieldCfg.key]: e.target.value,
                            }))
                          }
                          className="w-full rounded-md border border-input bg-background px-3 py-2 text-[13px] text-foreground"
                        />
                      ) : fieldCfg.type === "select" && fieldCfg.options ? (
                        <select
                          id={`edit-field-${fieldCfg.key}`}
                          value={editForm[fieldCfg.key] ?? ""}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              [fieldCfg.key]: e.target.value,
                            }))
                          }
                          className="h-9 w-full rounded-md border border-input bg-background px-3 text-[13px] text-foreground"
                        >
                          {fieldCfg.options.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <Input
                          id={`edit-field-${fieldCfg.key}`}
                          type={fieldCfg.type || "text"}
                          value={editForm[fieldCfg.key] ?? ""}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              [fieldCfg.key]: e.target.value,
                            }))
                          }
                          className="h-9 text-[13px]"
                        />
                      )}
                    </div>
                  );
                })}

                <DialogFooter className="pt-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setEditModalOpen(false)}
                  >
                    {t.cancel}
                  </Button>
                  <Button type="submit" disabled={editSaving}>
                    {editSaving ? t.saving : t.save}
                  </Button>
                </DialogFooter>
              </form>
            ) : null}
          </DialogContent>
        </Dialog>

        {/* Admin 3-Step Cold-Start Import Wizard */}
        <Dialog
          open={wizardOpen}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) {
              tryCloseWizard();
            } else {
              setWizardOpen(true);
            }
          }}
        >
          <DialogContent className="max-w-3xl max-h-[88vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {wizardStep === 1
                  ? "步骤 1 / 3：上传申报书 (.docx) 解析课题组资产"
                  : wizardStep === 2
                    ? "步骤 2 / 3：核对解析结果与全组冷启动账号"
                    : "步骤 3 / 3：开号完成 · 复制群发名单"}
              </DialogTitle>
              <DialogDescription>
                {wizardStep === 1
                  ? "上传既往项目申报书（仅支持 .docx）。系统会自动剥离全部身份证号与手机号，提取成员、项目、成果与设备。"
                  : wizardStep === 2
                    ? "核对三级登录标识（T1 学工号 / T2 姓名全拼 / T3 姓名全拼+邮箱）。所有新建账号角色固定为普通成员 (member)。"
                    : "全组账号与课题组资料库已落盘，并已同步写入 RAG 知识库。"}
              </DialogDescription>
            </DialogHeader>

            {wizardError ? (
              <p role="alert" className="text-[13px] text-destructive">
                {wizardError}
              </p>
            ) : null}

            {wizardStep === 1 ? (
              <form onSubmit={handleParseUpload} className="grid gap-4">
                <div>
                  <Label
                    htmlFor="proposal-docx"
                    className="text-[12px] font-medium text-muted-foreground"
                  >
                    申报书文件 (.docx，上限 20 MB)
                  </Label>
                  <Input
                    id="proposal-docx"
                    type="file"
                    accept=".docx"
                    onChange={(e) =>
                      handleFileChange(e.target.files?.[0] || null)
                    }
                    required
                  />
                </div>
                <div>
                  <Label
                    htmlFor="upload-as-of"
                    className="text-[12px] font-medium text-muted-foreground"
                  >
                    本子基准日期（选填，留空则从封面自动提取）
                  </Label>
                  <Input
                    id="upload-as-of"
                    type="date"
                    value={uploadAsOf}
                    onChange={(e) => setUploadAsOf(e.target.value)}
                  />
                </div>
                <DialogFooter>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setWizardOpen(false)}
                  >
                    {t.cancel}
                  </Button>
                  <Button type="submit" disabled={parsing || !uploadFile}>
                    {parsing ? "正在解析与脱敏…" : "上传并解析"}
                  </Button>
                </DialogFooter>
              </form>
            ) : null}

            {wizardStep === 2 && importJob ? (
              <div className="grid gap-4 text-[13px]">
                <div className="rounded-md border border-[var(--accent-warm)]/40 bg-[var(--accent-warm)]/10 px-3 py-2 text-[12px] text-foreground">
                  已物理剥离并丢弃{" "}
                  <span className="font-mono font-semibold">
                    {importJob.stats.pii_dropped ?? 0}
                  </span>{" "}
                  处身份证号/手机号敏感信息（零入库）。识别出成员{" "}
                  <span className="font-mono font-semibold">
                    {importJob.preview.people?.length ?? 0}
                  </span>{" "}
                  人、承担项目{" "}
                  <span className="font-mono font-semibold">
                    {importJob.preview.projects?.length ?? 0}
                  </span>{" "}
                  项、前期成果{" "}
                  <span className="font-mono font-semibold">
                    {importJob.preview.achievements?.length ?? 0}
                  </span>{" "}
                  项、设备{" "}
                  <span className="font-mono font-semibold">
                    {importJob.preview.facilities?.length ?? 0}
                  </span>{" "}
                  项。
                </div>

                {/* Spec §2.4 & §8.3.2 Default password non-dismissible warning */}
                {initialPassword.trim() === DEFAULT_INITIAL_PASSWORD ? (
                  <div
                    role="alert"
                    className="rounded-md border border-destructive bg-destructive/10 px-3.5 py-2.5 text-[12px] font-semibold text-destructive"
                  >
                    ⚠ {t.defaultPasswordWarning}
                  </div>
                ) : null}

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <div>
                    <Label
                      htmlFor="prov-as-of"
                      className="text-[12px] text-muted-foreground"
                    >
                      本子基准日期 (as_of_time)
                    </Label>
                    <Input
                      id="prov-as-of"
                      type="date"
                      value={provisionAsOf}
                      onChange={(e) => setProvisionAsOf(e.target.value)}
                      required
                    />
                  </div>
                  <div>
                    <Label
                      htmlFor="prov-pw"
                      className="text-[12px] text-muted-foreground"
                    >
                      全组统一初始口令（7天内有效）
                    </Label>
                    <Input
                      id="prov-pw"
                      type="text"
                      value={initialPassword}
                      onChange={(e) => setInitialPassword(e.target.value)}
                      required
                    />
                  </div>
                  <div className="flex items-end pb-2">
                    <label className="flex cursor-pointer items-center gap-2 text-[12px] text-foreground">
                      <input
                        type="checkbox"
                        checked={includePassword}
                        onChange={(e) => setIncludePassword(e.target.checked)}
                        className="h-4 w-4 rounded border-border"
                      />
                      群发名单中包含初始口令明文
                    </label>
                  </div>
                </div>

                <div className="overflow-x-auto rounded-md border border-border">
                  <table className="w-full text-left text-[12px]">
                    <thead className="border-b border-border bg-muted/40 text-muted-foreground">
                      <tr>
                        <th className="px-2.5 py-2">开通</th>
                        <th className="px-2.5 py-2">姓名</th>
                        <th className="px-2.5 py-2">挡位</th>
                        <th className="px-2.5 py-2">登录名 (login_name)</th>
                        <th className="px-2.5 py-2">邮箱</th>
                        <th className="px-2.5 py-2">状态</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {(importJob.preview.people || []).map((p) => {
                        const ov = overrides[p.key] || {
                          exclude: false,
                          login_name: p.login_name,
                        };
                        return (
                          <tr key={p.key}>
                            <td className="px-2.5 py-2">
                              <input
                                type="checkbox"
                                checked={!ov.exclude}
                                onChange={(e) =>
                                  setOverrides((prev) => ({
                                    ...prev,
                                    [p.key]: {
                                      ...ov,
                                      exclude: !e.target.checked,
                                    },
                                  }))
                                }
                              />
                            </td>
                            <td className="px-2.5 py-2 font-medium text-foreground">
                              {p.name}
                              {p.project_role === "applicant" ? (
                                <span className="ml-1 text-[11px] text-muted-foreground">
                                  (申请人)
                                </span>
                              ) : null}
                            </td>
                            <td className="px-2.5 py-2 font-mono">{p.tier}</td>
                            <td className="px-2.5 py-2">
                              <Input
                                value={ov.login_name}
                                disabled={ov.exclude}
                                onChange={(e) =>
                                  setOverrides((prev) => ({
                                    ...prev,
                                    [p.key]: {
                                      ...ov,
                                      login_name: e.target.value,
                                    },
                                  }))
                                }
                                className="h-7 font-mono text-[12px]"
                              />
                            </td>
                            <td className="px-2.5 py-2 font-mono text-muted-foreground">
                              {p.email || "NULL"}
                            </td>
                            <td className="px-2.5 py-2">
                              <Badge
                                variant={
                                  p.match?.status === "conflict"
                                    ? "outline"
                                    : "secondary"
                                }
                              >
                                {p.match?.status === "existing"
                                  ? "关联已有"
                                  : p.match?.status === "conflict"
                                    ? "冲突待处理"
                                    : "新建 member"}
                              </Badge>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <DialogFooter>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setWizardStep(1)}
                  >
                    返回上一步
                  </Button>
                  <Button
                    type="button"
                    disabled={provisioning || !provisionAsOf.trim()}
                    onClick={handleProvision}
                  >
                    {provisioning
                      ? "正在开号并写入知识库…"
                      : "确认一键开号并入库"}
                  </Button>
                </DialogFooter>
              </div>
            ) : null}

            {wizardStep === 3 && provisionResult ? (
              <div className="grid gap-4 text-[13px]">
                {/* Spec §8.3.3 Yellow Warning Banner */}
                <div
                  role="alert"
                  className="rounded-md border border-amber-500/50 bg-amber-500/15 px-3.5 py-2.5 text-[12px] font-semibold text-amber-900 dark:text-amber-200"
                >
                  ⚠ {t.credentialOnceBanner}
                </div>

                <div className="rounded-md border border-border bg-muted/30 p-3 text-[12px]">
                  已新建账号{" "}
                  <span className="font-mono font-semibold">
                    {provisionResult.stats.users_created}
                  </span>{" "}
                  个（关联已有{" "}
                  <span className="font-mono font-semibold">
                    {provisionResult.stats.users_linked}
                  </span>{" "}
                  个），入库成员{" "}
                  <span className="font-mono font-semibold">
                    {provisionResult.stats.people_inserted}
                  </span>{" "}
                  人、项目{" "}
                  <span className="font-mono font-semibold">
                    {provisionResult.stats.projects_inserted}
                  </span>{" "}
                  项、成果{" "}
                  <span className="font-mono font-semibold">
                    {provisionResult.stats.achievements_inserted}
                  </span>{" "}
                  项、设备{" "}
                  <span className="font-mono font-semibold">
                    {provisionResult.stats.facilities_inserted}
                  </span>{" "}
                  项，并已生成 RAG 文档{" "}
                  <span className="font-mono">
                    {provisionResult.rag_doc_key}
                  </span>
                  。
                </div>

                <div>
                  <Label className="text-[12px] text-muted-foreground">
                    微信群/飞书群一键通知文本
                  </Label>
                  <pre className="mt-1.5 max-h-60 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-muted/40 p-3 font-mono text-[12px] leading-relaxed text-foreground">
                    {provisionResult.roster_text}
                  </pre>
                </div>

                <DialogFooter className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleDownloadCsv}
                  >
                    下载账号清单 CSV (UTF-8 BOM)
                  </Button>
                  <Button
                    type="button"
                    onClick={async () => {
                      setWizardError("");
                      try {
                        await navigator.clipboard.writeText(
                          provisionResult.roster_text,
                        );
                        setRosterCopied(true);
                        setHasSavedCredentials(true);
                        setTimeout(() => setRosterCopied(false), 2000);
                      } catch {
                        setWizardError(
                          "复制到剪贴板失败，请手动全选复制或点击下载 CSV",
                        );
                      }
                    }}
                  >
                    {rosterCopied ? "已复制群发文本" : "一键复制群发文本"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={tryCloseWizard}
                  >
                    完成并关闭
                  </Button>
                </DialogFooter>
              </div>
            ) : null}
          </DialogContent>
        </Dialog>
      </div>
    </Shell>
  );
}
