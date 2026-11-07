"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

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
  deletePersonSignature,
  downloadSignaturesZip,
  exportSelectedLabAssets,
  formatLabBatch,
  getPersonDossier,
  getPersonSignatureUrl,
  mapLabErrorCode,
  parseFilenameFromDisposition,
  reindexLabAssets,
  restoreImportedField,
   updateLabEntity,
  uploadPersonSignature,
  type BasketEntityKind,
  type BasketItem,
  type BasketOptions,
  type BasketState,
  type DossierAchievementItem,
  type DossierProjectItem,
  type FieldProvenanceSummary,
  type ImportJobResponse,
  type ImportPreviewSignatureItem,
  type LabAchievementItem,
  type LabFacilityItem,
  type LabOverview,
  type LabPersonItem,
  type LabProjectItem,
  type PersonDossierResponse,
  type ProvisionResult,
  type User,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type TabKey = "people" | "projects" | "achievements" | "facilities";

const TAB_TO_BASKET_KIND: Record<TabKey, BasketEntityKind> = {
  people: "person",
  projects: "project",
  achievements: "achievement",
  facilities: "facility",
};

const BASKET_KIND_LABELS: Record<BasketEntityKind, string> = {
  person: "团队成员",
  project: "承担项目",
  achievement: "前期成果",
  facility: "支撑条件",
};

const BASKET_MAX_ITEMS = 200;
const MAX_SIGNATURE_BYTES = 2 * 1024 * 1024;
const ALLOWED_SIGNATURE_MIMES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
]);

const DEFAULT_BASKET_OPTIONS: BasketOptions = {
  with_support: true,
  with_relation: true,
  numbering: "bracket",
  group_by_kind: true,
  include_signatures: true,
  naming: "seq_name",
};

function normalizeBasketEntityKind(raw?: string): BasketEntityKind {
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

function normalizeNameKey(raw?: string | null): string {
  if (!raw) return "";
  return raw
    .normalize("NFKC")
    .replace(/\s+/g, "")
    .replace(/([*#†]+|（通讯作者）|\(通讯作者\)|\(correspondingauthor\))$/gi, "")
    .toLowerCase();
}

function extFromMime(mime?: string | null): string {
  if (mime === "image/jpeg") return "jpg";
  if (mime === "image/webp") return "webp";
  return "png";
}

function basketKey(kind: BasketEntityKind | string, id: string): string {
  return `${kind}:${id}`;
}

function getBasketStorageKey(userId?: string | null): string {
  return `labagent.reuseBasket.v1:${userId || "anonymous"}`;
}

function parseBasketStorage(raw: string | null): BasketState | null {
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

function triggerBlobDownload(blob: Blob, filename: string) {
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
  const lastClickedIndexRef = useRef<Record<TabKey, number | null>>({
    people: null,
    projects: null,
    achievements: null,
    facilities: null,
  });

  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [expandedBioId, setExpandedBioId] = useState<string | null>(null);
  const [expandedProvId, setExpandedProvId] = useState<string | null>(null);

  const [exportingFormat, setExportingFormat] = useState<"md" | "docx" | null>(
    null,
  );
  const [reindexing, setReindexing] = useState(false);

  // ==========================================================================
  // Feature 1: Person-Centric Dossier Drawer & Signature Management State
  // ==========================================================================
  const [dossierOpen, setDossierOpen] = useState(false);
  const [dossierPersonId, setDossierPersonId] = useState<string | null>(null);
  const [dossier, setDossier] = useState<PersonDossierResponse | null>(null);
  const [dossierLoading, setDossierLoading] = useState(false);
  const [dossierError, setDossierError] = useState("");
  const [dossierNotFound, setDossierNotFound] = useState(false);
  const [dossierCheckedProjects, setDossierCheckedProjects] = useState<
    Record<string, boolean>
  >({});
  const [dossierCheckedAchievements, setDossierCheckedAchievements] = useState<
    Record<string, boolean>
  >({});
  const [sigUploading, setSigUploading] = useState(false);
  const [sigDeleting, setSigDeleting] = useState(false);
  const [sigConfirmDelete, setSigConfirmDelete] = useState(false);
  const [sigError, setSigError] = useState("");
  const [sigStatusMsg, setSigStatusMsg] = useState("");
  const dossierAbortRef = useRef<AbortController | null>(null);
  const dossierReqSeqRef = useRef(0);
  const sigFileInputRef = useRef<HTMLInputElement | null>(null);

  // ==========================================================================
  // Feature 2: Multi-Select Proposal Reuse Basket ("写本子素材篮") State
  // ==========================================================================
  const [basketItems, setBasketItems] = useState<BasketItem[]>([]);
  const [basketOptions, setBasketOptions] = useState<BasketOptions>(
    DEFAULT_BASKET_OPTIONS,
  );
  const [basketHydratedForUser, setBasketHydratedForUser] = useState<
    string | null
  >(null);
  const [basketPanelOpen, setBasketPanelOpen] = useState(false);
  const [basketConfirmClear, setBasketConfirmClear] = useState(false);
  const [basketActionLoading, setBasketActionLoading] = useState<
    "copy" | "zip" | "docx" | null
  >(null);
  const [basketMissingIds, setBasketMissingIds] = useState<string[]>([]);
  const [clipboardFallbackText, setClipboardFallbackText] = useState<
    string | null
  >(null);

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
  const editSessionSeqRef = useRef(0);

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
  const [skipSignatureIds, setSkipSignatureIds] = useState<
    Record<string, boolean>
  >({});
  const [provisioning, setProvisioning] = useState(false);
  const [provisionResult, setProvisionResult] =
    useState<ProvisionResult | null>(null);
  const [rosterCopied, setRosterCopied] = useState(false);
  const [hasSavedCredentials, setHasSavedCredentials] = useState(false);

  function hydrateBasketFromStorage(userId: string) {
    if (typeof window === "undefined") return;
    const storageKey = getBasketStorageKey(userId);
    const parsed = parseBasketStorage(window.localStorage.getItem(storageKey));
    if (parsed) {
      setBasketItems(parsed.items);
      setBasketOptions(parsed.options);
      const nextSelected: Record<string, boolean> = {};
      for (const it of parsed.items) {
        nextSelected[it.id] = true;
      }
      setSelectedIds(nextSelected);
    } else {
      setBasketItems([]);
      setBasketOptions(DEFAULT_BASKET_OPTIONS);
      setSelectedIds({});
    }
    setBasketHydratedForUser(userId);
  }

  function persistBasketToStorage(
    nextItems: BasketItem[],
    nextOptions: BasketOptions,
    userIdOverride?: string | null,
  ) {
    if (typeof window === "undefined") return;
    const uid = userIdOverride ?? me?.id ?? "anonymous";
    const storageKey = getBasketStorageKey(uid);
    const payload: BasketState = {
      version: 1,
      items: nextItems.map(({ kind, id, label }) => ({ kind, id, label })),
      options: nextOptions,
    };
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(payload));
    } catch {
      // Ignore storage quota errors
    }
  }

  // Listen for cross-tab storage changes on the user's reuseBasket key
  useEffect(() => {
    if (typeof window === "undefined") return;
    const uid = me?.id || "anonymous";
    const storageKey = getBasketStorageKey(uid);
    function handleStorage(e: StorageEvent) {
      if (e.key !== storageKey) return;
      const parsed = parseBasketStorage(e.newValue);
      if (parsed) {
        setBasketItems(parsed.items);
        setBasketOptions(parsed.options);
        const nextSelected: Record<string, boolean> = {};
        for (const it of parsed.items) {
          nextSelected[it.id] = true;
        }
        setSelectedIds(nextSelected);
      } else {
        setBasketItems([]);
        setBasketOptions(DEFAULT_BASKET_OPTIONS);
        setSelectedIds({});
      }
    }
    window.addEventListener("storage", handleStorage);
    return () => {
      window.removeEventListener("storage", handleStorage);
    };
  }, [me?.id]);

  async function fetchDossierById(personId: string) {
    if (dossierAbortRef.current) {
      dossierAbortRef.current.abort();
    }
    const controller = new AbortController();
    dossierAbortRef.current = controller;
    const reqSeq = ++dossierReqSeqRef.current;

    setDossierOpen(true);
    setDossierPersonId(personId);
    setDossier((prev) => (prev?.person.id === personId ? prev : null));
    setDossierLoading(true);
    setDossierError("");
    setDossierNotFound(false);
    setSigError("");
    setSigStatusMsg("");
    setSigConfirmDelete(false);

    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("person", personId);
      window.history.replaceState(null, "", url.toString());
    }

    try {
      const data = await getPersonDossier(personId, controller.signal);
      if (controller.signal.aborted || dossierReqSeqRef.current !== reqSeq) {
        return;
      }
      setDossier(data);
      const initProjChecks: Record<string, boolean> = {};
      for (const proj of data.projects || []) {
        initProjChecks[proj.id] = proj.match !== "ambiguous";
      }
      setDossierCheckedProjects(initProjChecks);
      const achList = data.achievements || data.outputs || [];
      const initAchChecks: Record<string, boolean> = {};
      for (const ach of achList) {
        initAchChecks[ach.id] = ach.match !== "ambiguous";
      }
      setDossierCheckedAchievements(initAchChecks);
    } catch (err) {
      if (controller.signal.aborted || dossierReqSeqRef.current !== reqSeq) {
        return;
      }
      if (
        err instanceof ApiError &&
        (err.status === 404 ||
          err.code.toUpperCase() === "PERSON_NOT_FOUND" ||
          err.code === "not_found")
      ) {
        setDossierNotFound(true);
        setDossier(null);
      } else {
        const msg =
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : "无法加载成员全息档案袋，请稍后重试";
        setDossierError(msg);
      }
    } finally {
      if (!controller.signal.aborted && dossierReqSeqRef.current === reqSeq) {
        setDossierLoading(false);
      }
    }
  }

  function closePersonDossier() {
    dossierReqSeqRef.current += 1;
    if (dossierAbortRef.current) {
      dossierAbortRef.current.abort();
      dossierAbortRef.current = null;
    }
    setDossierOpen(false);
    setDossierPersonId(null);
    setSigConfirmDelete(false);
    setSigError("");
    setSigStatusMsg("");
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      if (url.searchParams.has("person")) {
        url.searchParams.delete("person");
        window.history.replaceState(null, "", url.toString());
      }
    }
  }

  async function loadAll() {
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
      if (actor?.id && basketHydratedForUser !== actor.id) {
        hydrateBasketFromStorage(actor.id);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (typeof window !== "undefined") {
      const initialPerson = new URLSearchParams(window.location.search).get(
        "person",
      );
      if (
        initialPerson &&
        initialPerson.trim() &&
        dossierReqSeqRef.current === 0
      ) {
        void fetchDossierById(initialPerson.trim());
      }
    }
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
        const uid = actor?.id || "anonymous";
        if (typeof window !== "undefined") {
          const parsed = parseBasketStorage(
            window.localStorage.getItem(getBasketStorageKey(uid)),
          );
          if (parsed) {
            setBasketItems(parsed.items);
            setBasketOptions(parsed.options);
            const nextSelected: Record<string, boolean> = {};
            for (const it of parsed.items) {
              nextSelected[it.id] = true;
            }
            setSelectedIds(nextSelected);
          } else {
            setBasketItems([]);
            setBasketOptions(DEFAULT_BASKET_OPTIONS);
            setSelectedIds({});
          }
          setBasketHydratedForUser(uid);
        }
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
      if (dossierAbortRef.current) {
        dossierAbortRef.current.abort();
        dossierAbortRef.current = null;
      }
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
      setStatusBanner("已复制到剪贴板");
      setTimeout(() => {
        setCopiedId((prev) => (prev === tag ? null : prev));
      }, 1800);
    } catch {
      setClipboardFallbackText(text);
    }
  }

  // ==========================================================================
  // Reuse Basket Helpers
  // ==========================================================================
  const basketKeySet = useMemo(() => {
    const s = new Set<string>();
    for (const it of basketItems) {
      s.add(basketKey(it.kind, it.id));
    }
    return s;
  }, [basketItems]);

  const basketCounts = useMemo(() => {
    const counts: Record<BasketEntityKind, number> = {
      person: 0,
      project: 0,
      achievement: 0,
      facility: 0,
    };
    for (const it of basketItems) {
      if (it.kind in counts) {
        counts[it.kind] += 1;
      }
    }
    return counts;
  }, [basketItems]);

  function isItemSelected(kind: BasketEntityKind, id: string): boolean {
    return basketKeySet.has(basketKey(kind, id)) || Boolean(selectedIds[id]);
  }

  function updateBasketItems(
    nextItems: BasketItem[],
    nextOptions: BasketOptions = basketOptions,
  ) {
    setBasketItems(nextItems);
    const nextSelected: Record<string, boolean> = {};
    for (const it of nextItems) {
      nextSelected[it.id] = true;
    }
    setSelectedIds(nextSelected);
    persistBasketToStorage(nextItems, nextOptions);
  }

  function updateBasketOption<K extends keyof BasketOptions>(
    key: K,
    value: BasketOptions[K],
  ) {
    const nextOptions: BasketOptions = {
      ...basketOptions,
      [key]: value,
    };
    setBasketOptions(nextOptions);
    persistBasketToStorage(basketItems, nextOptions);
  }

  function addBatchToBasket(
    candidates: BasketItem[],
    announceToast: boolean = true,
  ): boolean {
    setError("");
    const existingKeys = new Set(
      basketItems.map((it) => basketKey(it.kind, it.id)),
    );
    const toAdd: BasketItem[] = [];
    let duplicateCount = 0;
    for (const cand of candidates) {
      const k = basketKey(cand.kind, cand.id);
      if (existingKeys.has(k)) {
        duplicateCount += 1;
        continue;
      }
      existingKeys.add(k);
      toAdd.push({
        kind: cand.kind,
        id: String(cand.id),
        label: cand.label,
      });
    }
    if (basketItems.length + toAdd.length > BASKET_MAX_ITEMS) {
      setError(`素材篮最多 ${BASKET_MAX_ITEMS} 项，当前 ${basketItems.length} 项`);
      return false;
    }
    const nextItems = [...basketItems, ...toAdd];
    updateBasketItems(nextItems);
    if (announceToast) {
      if (toAdd.length > 0 && duplicateCount > 0) {
        setStatusBanner(
          `已加入素材篮 ${toAdd.length} 项（${duplicateCount} 项已存在）`,
        );
      } else if (toAdd.length > 0) {
        setStatusBanner(`已加入素材篮 ${toAdd.length} 项`);
      } else if (duplicateCount > 0) {
        setStatusBanner(`所选 ${duplicateCount} 项已在写本子素材篮中`);
      }
    }
    return true;
  }

  function removeItemsFromBasket(keysToRemove: Set<string>) {
    const nextItems = basketItems.filter(
      (it) => !keysToRemove.has(basketKey(it.kind, it.id)),
    );
    updateBasketItems(nextItems);
  }

  function moveBasketItem(index: number, direction: -1 | 1) {
    const targetIndex = index + direction;
    if (targetIndex < 0 || targetIndex >= basketItems.length) return;
    const nextItems = [...basketItems];
    const [moved] = nextItems.splice(index, 1);
    nextItems.splice(targetIndex, 0, moved);
    updateBasketItems(nextItems);
  }

  function handleRowCheckboxClick(
    tab: TabKey,
    indexInList: number,
    listItems: BasketItem[],
    checked: boolean,
    shiftKey: boolean,
  ) {
    const prevIndex = lastClickedIndexRef.current[tab];
    lastClickedIndexRef.current[tab] = indexInList;

    let targetSlice: BasketItem[] = [listItems[indexInList]];
    if (
      shiftKey &&
      prevIndex !== null &&
      prevIndex >= 0 &&
      prevIndex < listItems.length
    ) {
      const start = Math.min(prevIndex, indexInList);
      const end = Math.max(prevIndex, indexInList);
      targetSlice = listItems.slice(start, end + 1);
    }

    if (checked) {
      addBatchToBasket(targetSlice, false);
    } else {
      const keys = new Set(targetSlice.map((it) => basketKey(it.kind, it.id)));
      removeItemsFromBasket(keys);
    }
  }

  // Name-to-person lookup map for clicking PI / first_author chips in Projects & Achievements tabs
  const personByNormalizedName = useMemo(() => {
    const map = new Map<string, LabPersonItem>();
    for (const p of people) {
      const norm = normalizeNameKey(p.name);
      if (norm && !map.has(norm)) {
        map.set(norm, p);
      }
    }
    return map;
  }, [people]);

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

  const currentTabBasketCandidates = useMemo<BasketItem[]>(() => {
    if (activeTab === "people") {
      return filteredPeople.map((p) => ({
        kind: "person",
        id: p.id,
        label: `${p.name}${p.title && p.title !== "无" ? `（${p.title}）` : ""}`,
      }));
    }
    if (activeTab === "projects") {
      return filteredProjects.map((j) => ({
        kind: "project",
        id: j.id,
        label: `《${j.title}》`,
      }));
    }
    if (activeTab === "achievements") {
      return filteredAchievements.map((a) => ({
        kind: "achievement",
        id: a.id,
        label: `《${a.title}》`,
      }));
    }
    return filteredFacilities.map((f) => ({
      kind: "facility",
      id: f.id,
      label: f.name,
    }));
  }, [
    activeTab,
    filteredPeople,
    filteredProjects,
    filteredAchievements,
    filteredFacilities,
  ]);

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
        ? filteredPeople.filter((item) => isItemSelected("person", item.id))
        : filteredPeople;
      return items.map((item) => item.application_text);
    }
    if (activeTab === "projects") {
      const items = onlySelected
        ? filteredProjects.filter((item) => isItemSelected("project", item.id))
        : filteredProjects;
      return items.map((item) => getProjectText(item));
    }
    if (activeTab === "achievements") {
      const items = onlySelected
        ? filteredAchievements.filter((item) =>
            isItemSelected("achievement", item.id),
          )
        : filteredAchievements;
      return items.map((item) => getAchievementText(item));
    }
    const items = onlySelected
      ? filteredFacilities.filter((item) => isItemSelected("facility", item.id))
      : filteredFacilities;
    return items.map((item) => item.application_text);
  }

  function handleBatchCopySelected() {
    const lines = collectTabLines(true);
    if (lines.length === 0) return;
    void copyToClipboard(formatBatchLines(lines), "batch-selected");
  }

  function handleBatchCopyAllTab() {
    const lines = collectTabLines(false);
    if (lines.length === 0) return;
    void copyToClipboard(formatBatchLines(lines), "batch-all");
  }

  const currentTabListLength = currentTabBasketCandidates.length;

  const selectedCount = useMemo(() => {
    const kind = TAB_TO_BASKET_KIND[activeTab];
    return currentTabBasketCandidates.filter((item) =>
      isItemSelected(kind, item.id),
    ).length;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, currentTabBasketCandidates, basketKeySet, selectedIds]);

  function toggleSelectAllCurrentList() {
    if (currentTabBasketCandidates.length === 0) return;
    const allSelected = currentTabBasketCandidates.every((it) =>
      basketKeySet.has(basketKey(it.kind, it.id)),
    );
    if (allSelected) {
      const keys = new Set(
        currentTabBasketCandidates.map((it) => basketKey(it.kind, it.id)),
      );
      removeItemsFromBasket(keys);
      setStatusBanner("已取消当前列表的选择");
    } else {
      addBatchToBasket(currentTabBasketCandidates, true);
    }
  }

  function handleQuickSelectOngoingProjects() {
    const ongoing = projects
      .filter((j) => j.status === "ongoing")
      .map<BasketItem>((j) => ({
        kind: "project",
        id: j.id,
        label: `《${j.title}》`,
      }));
    if (ongoing.length === 0) {
      setStatusBanner("当前暂无在研项目可加入素材篮");
      return;
    }
    addBatchToBasket(ongoing, true);
  }

  function handleQuickSelectRecentAchievements() {
    const minYear = baseYear - 5;
    const recent = achievements
      .filter((a) => !a.year || a.year >= minYear)
      .map<BasketItem>((a) => ({
        kind: "achievement",
        id: a.id,
        label: `《${a.title}》`,
      }));
    if (recent.length === 0) {
      setStatusBanner("当前暂无近 5 年成果可加入素材篮");
      return;
    }
    addBatchToBasket(recent, true);
  }

  // ==========================================================================
  // Person Signature Download / Upload / Delete Handlers
  // ==========================================================================
  async function handleDownloadPersonSignature(
    personId: string,
    personName: string,
    mime?: string | null,
    sha256?: string | null,
  ) {
    setError("");
    setSigError("");
    try {
      const url = getPersonSignatureUrl(personId, "attachment", sha256);
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) {
        let code = "SIGNATURE_NOT_FOUND";
        let msg = "该成员暂无签名";
        try {
          const errBody = (await response.json()) as {
            error?: { code?: string; message?: string };
            detail?: { code?: string; message?: string };
          };
          code =
            errBody.error?.code ||
            errBody.detail?.code ||
            "SIGNATURE_NOT_FOUND";
          msg =
            errBody.error?.message ||
            errBody.detail?.message ||
            msg;
        } catch {
          // ignore JSON parse failure
        }
        throw new ApiError(response.status, code, msg);
      }
      const blob = await response.blob();
      const ext = extFromMime(response.headers.get("content-type") || mime);
      const fallbackName = `${personName}-电子签名.${ext}`;
      const filename = parseFilenameFromDisposition(
        response.headers.get("content-disposition"),
        fallbackName,
      );
      triggerBlobDownload(blob, filename);
      setStatusBanner(`已下载 ${personName} 的电子签名`);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? mapLabErrorCode(err.code, err.message)
          : "下载签名失败，请稍后重试";
      if (dossierOpen) {
        setSigError(msg);
      } else {
        setError(msg);
      }
    }
  }

  async function handleSignatureFileSelect(
    e: React.ChangeEvent<HTMLInputElement>,
  ) {
    const file = e.target.files?.[0] || null;
    e.target.value = "";
    if (!file || !dossier) return;

    setSigError("");
    setSigStatusMsg("");

    // Client-side precheck (§6.2): size <= 2 MiB & PNG/JPEG/WebP
    if (file.size <= 0 || file.size > MAX_SIGNATURE_BYTES) {
      setSigError(mapLabErrorCode("SIGNATURE_TOO_LARGE"));
      return;
    }
    const lowerName = file.name.toLowerCase();
    const validExt =
      lowerName.endsWith(".png") ||
      lowerName.endsWith(".jpg") ||
      lowerName.endsWith(".jpeg") ||
      lowerName.endsWith(".webp");
    if (
      (file.type && !ALLOWED_SIGNATURE_MIMES.has(file.type)) ||
      (!file.type && !validExt)
    ) {
      setSigError(mapLabErrorCode("SIGNATURE_REJECTED_FORMAT"));
      return;
    }

    const currentSha =
      dossier.signature?.signature_sha256 ??
      dossier.signature?.sha256 ??
      dossier.person.signature_sha256 ??
      null;

    setSigUploading(true);
    try {
      await uploadPersonSignature(dossier.person.id, file, currentSha);
      setSigStatusMsg("电子签名已上传更新");
      setStatusBanner(`已更新 ${dossier.person.name} 的电子签名`);
      await Promise.all([fetchDossierById(dossier.person.id), loadAll()]);
    } catch (err) {
      if (
        err instanceof ApiError &&
        (err.status === 412 ||
          err.code.toUpperCase() === "PRECONDITION_FAILED" ||
          err.code.toUpperCase() === "SIGNATURE_PRECONDITION_FAILED")
      ) {
        setSigError(mapLabErrorCode("SIGNATURE_PRECONDITION_FAILED"));
        await fetchDossierById(dossier.person.id);
      } else {
        setSigError(
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : "上传电子签名失败，请稍后重试",
        );
      }
    } finally {
      setSigUploading(false);
    }
  }

  async function handleConfirmDeleteSignature() {
    if (!dossier) return;
    setSigError("");
    setSigStatusMsg("");
    setSigDeleting(true);
    const currentSha =
      dossier.signature?.signature_sha256 ??
      dossier.signature?.sha256 ??
      dossier.person.signature_sha256 ??
      null;
    try {
      await deletePersonSignature(dossier.person.id, currentSha);
      setSigConfirmDelete(false);
      setSigStatusMsg("电子签名已删除");
      setStatusBanner(`已删除 ${dossier.person.name} 的电子签名`);
      await Promise.all([fetchDossierById(dossier.person.id), loadAll()]);
    } catch (err) {
      if (
        err instanceof ApiError &&
        (err.status === 412 ||
          err.code.toUpperCase() === "PRECONDITION_FAILED" ||
          err.code.toUpperCase() === "SIGNATURE_PRECONDITION_FAILED")
      ) {
        setSigError(mapLabErrorCode("SIGNATURE_PRECONDITION_FAILED"));
        await fetchDossierById(dossier.person.id);
      } else {
        setSigError(
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : "删除电子签名失败，请稍后重试",
        );
      }
    } finally {
      setSigDeleting(false);
    }
  }

  // ==========================================================================
  // Dossier Basket Actions
  // ==========================================================================
  function handleAddPersonOnlyFromDossier() {
    if (!dossier) return;
    const p = dossier.person;
    addBatchToBasket(
      [
        {
          kind: "person",
          id: p.id,
          label: `${p.name}${p.title && p.title !== "无" ? `（${p.title}）` : ""}`,
        },
      ],
      true,
    );
  }

  function handleAddCheckedDossierToBasket(selectAllNonAmbiguous: boolean) {
    if (!dossier) return;
    const p = dossier.person;
    const batch: BasketItem[] = [
      {
        kind: "person",
        id: p.id,
        label: `${p.name}${p.title && p.title !== "无" ? `（${p.title}）` : ""}`,
      },
    ];
    const projList = dossier.projects || [];
    const achList = dossier.achievements || dossier.outputs || [];

    if (selectAllNonAmbiguous) {
      const nextProjChecks: Record<string, boolean> = {
        ...dossierCheckedProjects,
      };
      for (const proj of projList) {
        if (proj.match !== "ambiguous") {
          nextProjChecks[proj.id] = true;
          batch.push({
            kind: "project",
            id: proj.id,
            label: `《${proj.title}》`,
          });
        }
      }
      setDossierCheckedProjects(nextProjChecks);

      const nextAchChecks: Record<string, boolean> = {
        ...dossierCheckedAchievements,
      };
      for (const ach of achList) {
        if (ach.match !== "ambiguous") {
          nextAchChecks[ach.id] = true;
          batch.push({
            kind: "achievement",
            id: ach.id,
            label: `《${ach.title}》`,
          });
        }
      }
      setDossierCheckedAchievements(nextAchChecks);
    } else {
      for (const proj of projList) {
        if (dossierCheckedProjects[proj.id]) {
          batch.push({
            kind: "project",
            id: proj.id,
            label: `《${proj.title}》`,
          });
        }
      }
      for (const ach of achList) {
        if (dossierCheckedAchievements[ach.id]) {
          batch.push({
            kind: "achievement",
            id: ach.id,
            label: `《${ach.title}》`,
          });
        }
      }
    }
    addBatchToBasket(batch, true);
  }

  // ==========================================================================
  // Reuse Basket 3 Batch Actions (§6.4)
  // ==========================================================================
  function buildLocalFallbackBatchText(
    sourceItems: BasketItem[] = basketItems,
  ): string {
    const peopleMap = new Map(people.map((p) => [p.id, p]));
    const projectMap = new Map(projects.map((p) => [p.id, p]));
    const achievementMap = new Map(achievements.map((a) => [a.id, a]));
    const facilityMap = new Map(facilities.map((f) => [f.id, f]));

    const orderedItems = basketOptions.group_by_kind
      ? ([
          ...sourceItems.filter((i) => i.kind === "person"),
          ...sourceItems.filter((i) => i.kind === "project"),
          ...sourceItems.filter((i) => i.kind === "achievement"),
          ...sourceItems.filter((i) => i.kind === "facility"),
        ] as BasketItem[])
      : sourceItems;

    const blocks: string[] = [];
    let seq = 0;
    let currentKind: BasketEntityKind | null = null;

    for (const item of orderedItems) {
      let rawText = "";
      if (item.kind === "person") {
        const p = peopleMap.get(item.id);
        rawText = p?.copy_text || p?.application_text || item.label;
      } else if (item.kind === "project") {
        const j = projectMap.get(item.id);
        rawText = j
          ? basketOptions.with_relation
            ? j.application_text_with_rel
            : j.application_text
          : item.label;
      } else if (item.kind === "achievement") {
        const a = achievementMap.get(item.id);
        rawText = a
          ? basketOptions.with_support
            ? a.application_text
            : a.application_text_plain
          : item.label;
      } else {
        const f = facilityMap.get(item.id);
        rawText = f?.application_text || item.label;
      }
      if (!rawText) continue;
      if (basketOptions.group_by_kind && item.kind !== currentKind) {
        currentKind = item.kind;
        blocks.push(`【${BASKET_KIND_LABELS[item.kind]}】`);
      }
      seq += 1;
      const prefix =
        basketOptions.numbering === "bracket"
          ? `[${seq}] `
          : basketOptions.numbering === "arabic"
            ? `${seq}. `
            : "";
      blocks.push(`${prefix}${rawText}`);
    }
    return blocks.join("\n\n");
  }

  async function handleBasketBatchCopy() {
    if (basketItems.length === 0) return;
    setError("");
    setStatusBanner("");
    setBasketMissingIds([]);
    setBasketActionLoading("copy");
    try {
      const res = await formatLabBatch(
        basketItems.map((it) => ({ kind: it.kind, id: it.id })),
        {
          with_support: basketOptions.with_support,
          with_relation: basketOptions.with_relation,
          numbering: basketOptions.numbering,
          separator: "blank_line",
          group_by_kind: basketOptions.group_by_kind,
        },
      );
      const finalText = res.text || buildLocalFallbackBatchText();
      try {
        await navigator.clipboard.writeText(finalText);
      } catch {
        setClipboardFallbackText(finalText);
      }
      const okCount = res.stats?.ok ?? res.count ?? basketItems.length;
      const missingCount = res.stats?.missing ?? 0;
      if (missingCount > 0) {
        const missingKeys = (res.items || [])
          .filter((it) => !it.ok)
          .map((it) => basketKey(it.kind, String(it.id)));
        setBasketMissingIds(missingKeys);
        setStatusBanner(
          `已复制 ${okCount} 项（${missingCount} 项已不存在，可从素材篮移除）`,
        );
      } else {
        setStatusBanner(`已复制 ${okCount} 项标准申报书格式文本`);
      }
    } catch (err) {
      if (
        err instanceof ApiError &&
        Array.isArray(err.missing) &&
        err.missing.length > 0
      ) {
        const missingKeys = Array.from(
          new Set(
            err.missing.map((m) =>
              basketKey(
                normalizeBasketEntityKind(m.entity || m.kind),
                String(m.id),
              ),
            ),
          ),
        );
        setBasketMissingIds(missingKeys);
        const missingSet = new Set(missingKeys);
        const validItems = basketItems.filter(
          (it) => !missingSet.has(basketKey(it.kind, it.id)),
        );
        if (validItems.length > 0) {
          try {
            const retryRes = await formatLabBatch(
              validItems.map((it) => ({ kind: it.kind, id: it.id })),
              {
                with_support: basketOptions.with_support,
                with_relation: basketOptions.with_relation,
                numbering: basketOptions.numbering,
                separator: "blank_line",
                group_by_kind: basketOptions.group_by_kind,
              },
            );
            const retryText =
              retryRes.text || buildLocalFallbackBatchText(validItems);
            try {
              await navigator.clipboard.writeText(retryText);
            } catch {
              setClipboardFallbackText(retryText);
            }
            const okCount =
              retryRes.stats?.ok ?? retryRes.count ?? validItems.length;
            setStatusBanner(
              `已复制 ${okCount} 项（${missingKeys.length} 项已不存在，可从素材篮移除）`,
            );
            return;
          } catch {
            // Fall through to error message while keeping basketMissingIds populated
          }
        }
        setError(
          `${mapLabErrorCode(err.code, err.message)}（共 ${missingKeys.length} 项缺失，可从素材篮移除后重试）`,
        );
      } else {
        setError(
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : t.error,
        );
      }
    } finally {
      setBasketActionLoading(null);
    }
  }

  async function handleBasketDownloadSignaturesZip() {
    const personIds = basketItems
      .filter((it) => it.kind === "person")
      .map((it) => it.id);
    if (personIds.length === 0) return;
    setError("");
    setStatusBanner("");
    setBasketActionLoading("zip");
    try {
      const res = await downloadSignaturesZip(
        personIds,
        basketOptions.naming,
        true,
      );
      triggerBlobDownload(res.blob, res.filename);
      const inc = res.includedCount ?? personIds.length;
      const miss = res.missingCount ?? 0;
      if (miss > 0) {
        setStatusBanner(
          `已打包 ${inc} 个签名（${miss} 人无签名，详见 manifest.csv）`,
        );
      } else {
        setStatusBanner(`已打包 ${inc} 个签名`);
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? mapLabErrorCode(err.code, err.message)
          : "打包下载电子签名失败，请稍后重试",
      );
    } finally {
      setBasketActionLoading(null);
    }
  }

  async function handleBasketExportSelectedDocx() {
    if (basketItems.length === 0) return;
    setError("");
    setStatusBanner("");
    setBasketMissingIds([]);
    setBasketActionLoading("docx");
    try {
      const res = await exportSelectedLabAssets(
        basketItems.map((it) => ({ kind: it.kind, id: it.id })),
        {
          with_support: basketOptions.with_support,
          with_relation: basketOptions.with_relation,
          numbering: basketOptions.numbering,
          group_by_kind: basketOptions.group_by_kind,
          include_signatures: basketOptions.include_signatures,
          signature_width_cm: 3.0,
          title: "写本子素材汇总",
        },
      );
      triggerBlobDownload(res.blob, res.filename);
      if (res.warnings.length > 0) {
        const warnTexts = res.warnings.map((w) => mapLabErrorCode(w));
        setStatusBanner(
          `已导出所选素材 Word 文档（提示：${warnTexts.join("；")}）`,
        );
      } else {
        setStatusBanner(
          `已导出所选素材 Word 文档（共 ${basketItems.length} 项）`,
        );
      }
    } catch (err) {
      if (
        err instanceof ApiError &&
        Array.isArray(err.missing) &&
        err.missing.length > 0
      ) {
        const missingKeys = Array.from(
          new Set(
            err.missing.map((m) =>
              basketKey(
                normalizeBasketEntityKind(m.entity || m.kind),
                String(m.id),
              ),
            ),
          ),
        );
        setBasketMissingIds(missingKeys);
        setError(
          `${mapLabErrorCode(err.code, err.message)}（共 ${missingKeys.length} 项缺失，可从素材篮移除后重试）`,
        );
      } else {
        setError(
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : "导出所选素材失败，请稍后重试",
        );
      }
    } finally {
      setBasketActionLoading(null);
    }
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
      triggerBlobDownload(
        blob,
        format === "docx" ? "lab-assets.docx" : "lab-assets.md",
      );
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
    if (
      !lower.endsWith(".docx") ||
      lower.endsWith(".docm") ||
      lower.endsWith(".doc")
    ) {
      setWizardError(
        "仅支持上传 .docx 格式的申报书文件（不支持 .doc 或 .docm）",
      );
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
      setSkipSignatureIds({});
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
      const skippedSigs = Object.entries(skipSignatureIds)
        .filter(([, skipped]) => skipped)
        .map(([sigId]) => sigId);
      const res = await api<ProvisionResult>(
        `admin/imports/${importJob.job_id}/provision`,
        {
          method: "POST",
          body: JSON.stringify({
            as_of_time: provisionAsOf.trim() || undefined,
            initial_password: initialPassword.trim() || undefined,
            include_password: includePassword,
            people: peoplePayload,
            ...(skippedSigs.length > 0
              ? { skip_signature_ids: skippedSigs }
              : {}),
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
    triggerBlobDownload(
      blob,
      `labagent-accounts-${provisionResult.as_of_time}.csv`,
    );
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
      return Boolean(p.user_id && p.user_id === me.id);
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
    editSessionSeqRef.current += 1;
    setEditSaving(false);
    setRestoringFieldKey(null);
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
    if (!editItem || editSaving || restoringFieldKey !== null) return;
    const capturedSeq = editSessionSeqRef.current;
    setEditError("");
    setStatusBanner("");
    setEditSaving(true);
    try {
      const payload: Record<string, unknown> = {};
      const rec = editItem as unknown as Record<string, unknown>;
      for (const f of EDITABLE_FIELDS[editType]) {
        if (f.adminOnly && me?.role !== "admin") continue;
        const origVal = rec[f.key];
        const origStr =
          origVal === null || origVal === undefined ? "" : String(origVal);
        const rawVal = editForm[f.key] ?? "";
        if (rawVal === origStr) continue;
        if (f.type === "number") {
          const trimmed = rawVal.trim();
          payload[f.key] = trimmed ? Number(trimmed) : null;
        } else {
          payload[f.key] = rawVal;
        }
      }
      if (Object.keys(payload).length === 0) {
        if (editSessionSeqRef.current === capturedSeq) {
          setEditModalOpen(false);
        }
        return;
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
      if (editSessionSeqRef.current === capturedSeq) {
        if (updatedEntity) {
          setEditItem(updatedEntity);
        }
        setEditModalOpen(false);
      }
      setStatusBanner("已保存修改并记录字段级人工溯源");
      await loadAll();
    } catch (err) {
      if (editSessionSeqRef.current === capturedSeq) {
        setEditError(err instanceof ApiError ? err.message : t.error);
      }
    } finally {
      if (editSessionSeqRef.current === capturedSeq) {
        setEditSaving(false);
      }
    }
  }

  async function handleRestoreImported(
    type: TabKey,
    itemId: string,
    field: string,
  ) {
    if (restoringFieldKey !== null || editSaving) return;
    setError("");
    setEditError("");
    setStatusBanner("");
    const capturedSeq = editSessionSeqRef.current;
    const wasEditingTarget =
      editModalOpen &&
      editItem !== null &&
      editItem.id === itemId &&
      editType === type;
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
      if (
        updatedEntity &&
        wasEditingTarget &&
        editSessionSeqRef.current === capturedSeq
      ) {
        const rec = updatedEntity as unknown as Record<string, unknown>;
        const restoredVal = rec[field];
        setEditItem((prev) =>
          prev && prev.id === itemId
            ? ({
                ...prev,
                [field]: restoredVal,
                provenance: updatedEntity.provenance,
              } as
                | LabPersonItem
                | LabProjectItem
                | LabAchievementItem
                | LabFacilityItem)
            : prev,
        );
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
      if (wasEditingTarget && editSessionSeqRef.current === capturedSeq) {
        setEditError(msg);
      } else {
        setError(msg);
      }
    } finally {
      if (editSessionSeqRef.current === capturedSeq) {
        setRestoringFieldKey((prev) => (prev === opKey ? null : prev));
      }
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
                      disabled={restoringFieldKey !== null || editSaving}
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
                          disabled={restoringFieldKey !== null || editSaving}
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

  // Resolved Dossier values
  const dossierHasSig = Boolean(
    dossier?.signature?.has_signature ?? dossier?.person?.has_signature,
  );
  const dossierSigSha =
    dossier?.signature?.signature_sha256 ??
    dossier?.signature?.sha256 ??
    dossier?.person?.signature_sha256 ??
    null;
  const dossierSigMime =
    dossier?.signature?.signature_mime ??
    dossier?.signature?.mime ??
    dossier?.person?.signature_mime ??
    "image/png";
  const dossierSigSource =
    dossier?.signature?.signature_source ??
    dossier?.signature?.source ??
    dossier?.person?.signature_source ??
    null;
  const dossierSigUpdatedAt =
    dossier?.signature?.signature_updated_at ??
    dossier?.signature?.updated_at ??
    dossier?.person?.signature_updated_at ??
    null;
  const dossierCanEditSig = Boolean(
    dossier?.signature?.can_edit ??
      dossier?.can_edit ??
      dossier?.person?.can_edit ??
      (me &&
        dossier?.person &&
        (me.role === "admin" ||
          (me.role === "member" && dossier.person.user_id === me.id))),
  );
  const dossierProjects: DossierProjectItem[] = dossier?.projects || [];
  const dossierAchievements: DossierAchievementItem[] =
    dossier?.achievements || dossier?.outputs || [];

  const previewSignatures = useMemo<ImportPreviewSignatureItem[]>(() => {
    const rawSigs = importJob?.preview?.signatures;
    if (!rawSigs) return [];
    const list = Array.isArray(rawSigs) ? rawSigs : rawSigs.items || [];
    const jobWarnings = importJob?.preview?.warnings || [];
    return list.map((sig, idx) => {
      const sigKey =
        sig.person_temp_id ||
        sig.person_key ||
        sig.sig_id ||
        sig.person_ref ||
        sig.person_name ||
        `sig-${idx + 1}`;
      const status = sig.status || "bound";
      const matchedWarnCodes = jobWarnings
        .filter(
          (w) =>
            (w.person_temp_id && w.person_temp_id === sigKey) ||
            (Array.isArray(w.person_temp_ids) &&
              w.person_temp_ids.includes(sigKey)) ||
            (sig.person_name &&
              ((w.person_name && w.person_name === sig.person_name) ||
                (Array.isArray(w.person_names) &&
                  w.person_names.includes(sig.person_name)))),
        )
        .map((w) => w.code);
      const combinedWarnings = Array.from(
        new Set([...(sig.warnings || []), ...matchedWarnCodes]),
      );
      return {
        ...sig,
        sig_id: sigKey,
        person_temp_id: sig.person_temp_id || sigKey,
        status,
        warnings: combinedWarnings,
      };
    });
  }, [importJob]);

  return (
    <Shell>
      <div className="space-y-6 pb-36">
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
          <div
            role="alert"
            className="flex flex-wrap items-center justify-between gap-2 text-[13px] text-destructive"
          >
            <span>{error}</span>
            {basketMissingIds.length > 0 && !statusBanner ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-6 px-2 text-[11px]"
                onClick={() => {
                  removeItemsFromBasket(new Set(basketMissingIds));
                  setBasketMissingIds([]);
                  setError("");
                  setStatusBanner("已从写本子素材篮移除不存在的条目");
                }}
              >
                从素材篮移除缺失项 ({basketMissingIds.length})
              </Button>
            ) : null}
          </div>
        ) : null}

        {statusBanner ? (
          <div
            role="status"
            aria-live="polite"
            className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[13px] font-medium text-emerald-700 dark:text-emerald-300"
          >
            <span>{statusBanner}</span>
            {basketMissingIds.length > 0 ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-6 px-2 text-[11px]"
                onClick={() => {
                  removeItemsFromBasket(new Set(basketMissingIds));
                  setBasketMissingIds([]);
                  setError("");
                  setStatusBanner("已从写本子素材篮移除不存在的条目");
                }}
              >
                从素材篮移除缺失项 ({basketMissingIds.length})
              </Button>
            ) : null}
          </div>
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
                onClick={toggleSelectAllCurrentList}
              >
                {t.selectAllCurrentListBtn}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-8 px-2.5 text-[12px]"
                onClick={handleQuickSelectOngoingProjects}
              >
                {t.selectOnlyOngoingProjectsBtn}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-8 px-2.5 text-[12px]"
                onClick={handleQuickSelectRecentAchievements}
              >
                {t.selectOnlyRecentAchievementsBtn}
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
          <div className="flex flex-wrap items-center justify-between gap-4 text-[12px] text-muted-foreground">
            <div className="flex flex-wrap items-center gap-4">
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
                      onChange={(e) =>
                        setOnlyOngoingProjects(e.target.checked)
                      }
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

            {selectedCount > 0 ? (
              <div className="flex items-center gap-2 text-[12px] text-foreground">
                <span>当前列表已勾选 {selectedCount} 项</span>
                <button
                  type="button"
                  className="text-muted-foreground underline hover:text-foreground"
                  onClick={() => {
                    const keys = new Set(
                      currentTabBasketCandidates.map((it) =>
                        basketKey(it.kind, it.id),
                      ),
                    );
                    removeItemsFromBasket(keys);
                  }}
                >
                  取消当前列表选择
                </button>
              </div>
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
              filteredPeople.map((person, idx) => {
                const checked = isItemSelected("person", person.id);
                const hasSig = Boolean(person.has_signature);
                return (
                  <div
                    key={person.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择成员 ${person.name}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "people",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              onClick={() => void fetchDossierById(person.id)}
                              className="text-left font-medium text-[15px] text-foreground underline-offset-4 hover:underline"
                              title="点击打开个人全息档案袋"
                            >
                              {person.name}
                            </button>
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
                            {hasSig ? (
                              <Badge
                                variant="secondary"
                                className="border-emerald-500/40 bg-emerald-500/15 text-[11px] font-normal text-emerald-800 dark:text-emerald-300"
                              >
                                {t.signatureExtractedBadge}
                              </Badge>
                            ) : (
                              <Badge
                                variant="outline"
                                className="border-border bg-muted/20 text-[11px] font-normal text-muted-foreground"
                              >
                                {t.signatureMissingBadge}
                              </Badge>
                            )}
                            {person.degree ? (
                              <Badge variant="outline">{person.degree}</Badge>
                            ) : null}
                            {person.title && person.title !== "无" ? (
                              <Badge variant="outline">{person.title}</Badge>
                            ) : null}
                            {person.tutor_role && person.tutor_role !== "无" ? (
                              <Badge variant="outline">
                                {person.tutor_role}
                              </Badge>
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
                        <Button
                          size="sm"
                          variant="secondary"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() => void fetchDossierById(person.id)}
                        >
                          {t.dossierTitle}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          disabled={!hasSig}
                          title={
                            hasSig
                              ? `下载 ${person.name} 的电子签名`
                              : "该成员尚未上传或提取电子签名"
                          }
                          onClick={() =>
                            void handleDownloadPersonSignature(
                              person.id,
                              person.name,
                              person.signature_mime,
                              person.signature_sha256,
                            )
                          }
                        >
                          {t.downloadSignatureBtn}
                        </Button>
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
                            void copyToClipboard(
                              person.application_text,
                              person.id,
                            )
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
                              void copyToClipboard(
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
                );
              })
            )}
          </div>
        ) : activeTab === "projects" ? (
          <div className="grid gap-3">
            {filteredProjects.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的承担项目记录。
              </p>
            ) : (
              filteredProjects.map((proj, idx) => {
                const displayAppText = getProjectText(proj);
                const checked = isItemSelected("project", proj.id);
                const matchedPi = proj.pi_name
                  ? personByNormalizedName.get(normalizeNameKey(proj.pi_name))
                  : undefined;
                return (
                  <div
                    key={proj.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择项目 ${proj.title}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "projects",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
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
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
                            {proj.pi_name ? (
                              matchedPi ? (
                                <span>
                                  负责人：
                                  <button
                                    type="button"
                                    className="font-medium text-foreground underline underline-offset-2 hover:text-primary"
                                    onClick={() =>
                                      void fetchDossierById(matchedPi.id)
                                    }
                                  >
                                    {proj.pi_name}
                                  </button>
                                </span>
                              ) : (
                                <span>负责人：{proj.pi_name}</span>
                              )
                            ) : null}
                            {proj.funder ? <span>· {proj.funder}</span> : null}
                            {proj.start_date ? (
                              <span>· 立项：{proj.start_date.slice(0, 7)}</span>
                            ) : null}
                          </div>
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
                            void copyToClipboard(displayAppText, proj.id)
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
              filteredAchievements.map((ach, idx) => {
                const displayAchText = getAchievementText(ach);
                const checked = isItemSelected("achievement", ach.id);
                const matchedAuthor = ach.first_author
                  ? personByNormalizedName.get(
                      normalizeNameKey(ach.first_author),
                    )
                  : undefined;
                return (
                  <div
                    key={ach.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择成果 ${ach.title}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "achievements",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
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
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
                            {ach.first_author ? (
                              matchedAuthor ? (
                                <span>
                                  第一作者：
                                  <button
                                    type="button"
                                    className="font-medium text-foreground underline underline-offset-2 hover:text-primary"
                                    onClick={() =>
                                      void fetchDossierById(matchedAuthor.id)
                                    }
                                  >
                                    {ach.first_author}
                                  </button>
                                </span>
                              ) : (
                                <span>第一作者：{ach.first_author}</span>
                              )
                            ) : null}
                            {ach.venue ? <span>· {ach.venue}</span> : null}
                          </div>
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
                            void copyToClipboard(displayAchText, ach.id)
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
              filteredFacilities.map((fac, idx) => {
                const checked = isItemSelected("facility", fac.id);
                return (
                  <div
                    key={fac.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择设备 ${fac.name}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "facilities",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
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
                            void copyToClipboard(fac.application_text, fac.id)
                          }
                        >
                          {copiedId === fac.id
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
        )}

        {/* ===================================================================
            Feature 1: Person-Centric Dossier Modal / Drawer ("个人全息档案袋")
            =================================================================== */}
        <Dialog
          open={dossierOpen}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) {
              closePersonDossier();
            }
          }}
        >
          <DialogContent
            aria-labelledby="person-dossier-title"
            className="max-w-3xl max-h-[90vh] overflow-y-auto"
          >
            <DialogHeader>
              <div className="flex flex-wrap items-center justify-between gap-2 pr-6">
                <DialogTitle id="person-dossier-title" className="text-[18px]">
                  {dossier?.person
                    ? `${dossier.person.name} · ${t.dossierTitle}`
                    : t.dossierTitle}
                </DialogTitle>
                {people.length > 1 ? (
                  <select
                    aria-label="切换查看团队成员"
                    value={dossierPersonId || ""}
                    onChange={(e) => {
                      if (e.target.value) {
                        void fetchDossierById(e.target.value);
                      }
                    }}
                    className="h-7 rounded-md border border-input bg-background px-2 text-[12px] text-foreground"
                  >
                    {people.map((p) => (
                      <option key={p.id} value={p.id}>
                        切换成员：{p.name}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              <DialogDescription>
                汇聚成员个人电子签名、申报书标准简历以及按姓名确定性匹配的承担项目与前期成果。
              </DialogDescription>
            </DialogHeader>

            {dossierLoading ? (
              <div className="space-y-3 py-6 text-[13px] text-muted-foreground">
                <p>正在读取个人全息档案袋…</p>
                <div className="h-20 animate-pulse rounded-md bg-muted/40" />
                <div className="h-28 animate-pulse rounded-md bg-muted/40" />
              </div>
            ) : dossierNotFound ? (
              <div className="space-y-4 py-6 text-center text-[13px]">
                <p role="alert" className="text-destructive font-medium">
                  成员不存在或已删除
                </p>
                <Button
                  type="button"
                  variant="outline"
                  onClick={closePersonDossier}
                >
                  关闭
                </Button>
              </div>
            ) : dossierError ? (
              <div className="space-y-3 py-4 text-[13px]">
                <p role="alert" className="text-destructive">
                  {dossierError}
                </p>
                <div className="flex gap-2">
                  {dossierPersonId ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => void fetchDossierById(dossierPersonId)}
                    >
                      重试
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={closePersonDossier}
                  >
                    关闭
                  </Button>
                </div>
              </div>
            ) : dossier ? (
              <div className="space-y-5 text-[13px]">
                {/* 1. Header Metadata */}
                <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/20 px-3.5 py-2.5">
                  <span className="font-semibold text-[15px] text-foreground">
                    {dossier.person.name}
                  </span>
                  <Badge
                    variant={
                      dossier.person.project_role === "applicant"
                        ? "secondary"
                        : "outline"
                    }
                  >
                    {dossier.person.project_role === "applicant"
                      ? "课题负责人"
                      : "项目组成员"}
                  </Badge>
                  {dossier.person.title && dossier.person.title !== "无" ? (
                    <Badge variant="outline">{dossier.person.title}</Badge>
                  ) : null}
                  {dossier.person.degree ? (
                    <Badge variant="outline">{dossier.person.degree}</Badge>
                  ) : null}
                  {dossier.person.affiliation || dossier.person.org ? (
                    <span className="text-[12px] text-muted-foreground">
                      {dossier.person.affiliation || dossier.person.org}
                    </span>
                  ) : null}
                  {dossier.person.name_ambiguous ? (
                    <Badge
                      variant="outline"
                      title="关联结果可能包含同名者"
                      className="border-amber-500/50 bg-amber-500/15 text-[11px] text-amber-900 dark:text-amber-200"
                    >
                      存在同名成员（关联结果可能包含同名者）
                    </Badge>
                  ) : null}
                </div>

                {/* 2. Signature Card ("电子签名卡片") */}
                <div className="rounded-lg border border-border bg-card p-4 space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="font-medium text-[14px] text-foreground">
                        电子签名卡片
                      </h3>
                      {dossierHasSig ? (
                        <>
                          <Badge
                            variant="secondary"
                            className="border-emerald-500/40 bg-emerald-500/15 text-[11px] font-normal text-emerald-800 dark:text-emerald-300"
                          >
                            {t.signatureExtractedBadge}
                          </Badge>
                          <Badge
                            variant="outline"
                            className="text-[11px] font-normal text-muted-foreground"
                          >
                            {dossierSigSource === "upload"
                              ? "来源：手动上传"
                              : "来源：申报书导入"}
                          </Badge>
                          {dossierSigUpdatedAt ? (
                            <span className="font-mono text-[11px] text-muted-foreground">
                              更新于 {dossierSigUpdatedAt.slice(0, 10)}
                            </span>
                          ) : null}
                        </>
                      ) : (
                        <Badge
                          variant="outline"
                          className="text-[11px] font-normal text-muted-foreground"
                        >
                          {t.signatureMissingBadge}
                        </Badge>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-1.5">
                      {dossierHasSig ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            void handleDownloadPersonSignature(
                              dossier.person.id,
                              dossier.person.name,
                              dossierSigMime,
                              dossierSigSha,
                            )
                          }
                        >
                          {t.downloadSignatureBtn} (
                          {dossier.person.name}-电子签名.
                          {extFromMime(dossierSigMime)})
                        </Button>
                      ) : null}

                      {/* Strictly omit edit buttons from DOM when can_edit is false (§6.2) */}
                      {dossierCanEditSig ? (
                        <>
                          <input
                            ref={sigFileInputRef}
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            className="hidden"
                            onChange={(e) => void handleSignatureFileSelect(e)}
                          />
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            disabled={sigUploading}
                            onClick={() => sigFileInputRef.current?.click()}
                          >
                            {sigUploading
                              ? "正在上传…"
                              : dossierHasSig
                                ? t.replaceSignatureBtn
                                : t.uploadSignatureBtn}
                          </Button>
                          {dossierHasSig ? (
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-7 px-2.5 text-[12px] text-destructive hover:text-destructive"
                              disabled={sigDeleting}
                              onClick={() => setSigConfirmDelete(true)}
                            >
                              {t.deleteSignatureBtn}
                            </Button>
                          ) : null}
                        </>
                      ) : null}
                    </div>
                  </div>

                  {sigError ? (
                    <p role="alert" className="text-[12px] text-destructive">
                      {sigError}
                    </p>
                  ) : null}

                  {sigStatusMsg ? (
                    <p
                      role="status"
                      aria-live="polite"
                      className="text-[12px] text-emerald-600"
                    >
                      {sigStatusMsg}
                    </p>
                  ) : null}

                  {dossierCanEditSig && sigConfirmDelete ? (
                    <div
                      role="alert"
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[12px]"
                    >
                      <span className="font-medium text-destructive">
                        {t.deleteSignatureConfirm}
                      </span>
                      <div className="flex items-center gap-1.5">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-6 px-2 text-[11px]"
                          onClick={() => setSigConfirmDelete(false)}
                        >
                          {t.cancel}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="destructive"
                          className="h-6 px-2 text-[11px]"
                          disabled={sigDeleting}
                          onClick={() => void handleConfirmDeleteSignature()}
                        >
                          {sigDeleting ? "正在删除…" : "确认删除签名"}
                        </Button>
                      </div>
                    </div>
                  ) : null}

                  {dossierHasSig ? (
                    <div className="flex items-center justify-center rounded-md border border-dashed border-border bg-white p-3">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={getPersonSignatureUrl(
                          dossier.person.id,
                          "inline",
                          dossierSigSha,
                        )}
                        alt={`${dossier.person.name}的签名`}
                        className="max-h-24 w-auto object-contain"
                      />
                    </div>
                  ) : (
                    <div className="flex h-20 items-center justify-center rounded-md border border-dashed border-border bg-muted/20 text-[12px] text-muted-foreground">
                      暂无签名（支持 PNG / JPEG / WebP，≤ 2 MB）
                    </div>
                  )}
                </div>

                {/* 3. Profile & Bio ("简介与学术简历") */}
                <div className="rounded-lg border border-border bg-card p-4 space-y-3">
                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between gap-2">
                      <h3 className="font-medium text-[14px] text-foreground">
                        一行式申报书标准简介
                      </h3>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-7 px-2.5 text-[12px]"
                        onClick={() =>
                          void copyToClipboard(
                            dossier.person.copy_text ||
                              dossier.person.application_text,
                            `dossier-copy-${dossier.person.id}`,
                          )
                        }
                      >
                        {copiedId === `dossier-copy-${dossier.person.id}`
                          ? "已复制"
                          : "复制简介"}
                      </Button>
                    </div>
                    <p className="rounded-md bg-muted/30 p-2.5 font-serif text-[13px] text-foreground">
                      {dossier.person.copy_text ||
                        dossier.person.application_text}
                    </p>
                  </div>

                  {dossier.person.bio_text ||
                  dossier.person.bio ||
                  dossier.person.application_text_with_bio ? (
                    <div className="space-y-1.5 pt-1">
                      <div className="flex items-center justify-between gap-2">
                        <h4 className="font-medium text-[13px] text-foreground">
                          个人学术简历全文
                        </h4>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            void copyToClipboard(
                              dossier.person.bio_text ||
                                dossier.person.application_text_with_bio ||
                                dossier.person.bio ||
                                "",
                              `dossier-bio-${dossier.person.id}`,
                            )
                          }
                        >
                          {copiedId === `dossier-bio-${dossier.person.id}`
                            ? "已复制"
                            : "复制学术简历"}
                        </Button>
                      </div>
                      <p className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md bg-muted/30 p-2.5 font-serif text-[12px] leading-relaxed text-foreground">
                        {dossier.person.bio_text ||
                          dossier.person.bio ||
                          dossier.person.application_text_with_bio}
                      </p>
                    </div>
                  ) : null}
                </div>

                {/* 4. Associated Projects ("自动关联承担项目") */}
                <div className="rounded-lg border border-border bg-card p-4 space-y-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="font-medium text-[14px] text-foreground">
                      关联承担项目 (
                      {dossier.counts?.projects_total ??
                        dossier.counts?.projects ??
                        dossierProjects.length}
                      )
                    </h3>
                    {dossierProjects.length > 0 ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-7 px-2.5 text-[12px]"
                        onClick={() => {
                          const nonAmbiguous = dossierProjects.filter(
                            (p) => p.match !== "ambiguous",
                          );
                          const allChecked =
                            nonAmbiguous.length > 0 &&
                            nonAmbiguous.every(
                              (p) => dossierCheckedProjects[p.id],
                            );
                          const next: Record<string, boolean> = {
                            ...dossierCheckedProjects,
                          };
                          for (const p of nonAmbiguous) {
                            next[p.id] = !allChecked;
                          }
                          setDossierCheckedProjects(next);
                        }}
                      >
                        全选非同名项目
                      </Button>
                    ) : null}
                  </div>

                  {dossierProjects.length === 0 ? (
                    <p className="text-[12px] text-muted-foreground">
                      暂无自动关联的承担项目记录。
                    </p>
                  ) : (
                    <div className="divide-y divide-border rounded-md border border-border">
                      {dossierProjects.map((proj) => {
                        const projCopy =
                          proj.copy_text ||
                          proj.application_text ||
                          `《${proj.title}》`;
                        const projRelation =
                          proj.relation_role ?? proj.relation;
                        const isProjLeader =
                          projRelation === "pi" || projRelation === "host";
                        return (
                          <div
                            key={proj.id}
                            className="flex flex-wrap items-start justify-between gap-2 p-2.5"
                          >
                            <label className="flex flex-1 cursor-pointer items-start gap-2">
                              <input
                                type="checkbox"
                                checked={Boolean(
                                  dossierCheckedProjects[proj.id],
                                )}
                                onChange={(e) =>
                                  setDossierCheckedProjects((prev) => ({
                                    ...prev,
                                    [proj.id]: e.target.checked,
                                  }))
                                }
                                className="mt-1 h-4 w-4 rounded border-border"
                              />
                              <div className="space-y-1">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <Badge
                                    variant={
                                      isProjLeader ? "secondary" : "outline"
                                    }
                                    className="text-[11px]"
                                  >
                                    {isProjLeader ? "负责人" : "参与"}
                                  </Badge>
                                  <span className="font-medium text-foreground">
                                    《{proj.title}》
                                  </span>
                                  {proj.year ? (
                                    <span className="font-mono text-[11px] text-muted-foreground">
                                      ({proj.year})
                                    </span>
                                  ) : null}
                                  {proj.match === "ambiguous" ? (
                                    <Badge
                                      variant="outline"
                                      className="border-amber-500/50 bg-amber-500/15 text-[10px] text-amber-900 dark:text-amber-200"
                                    >
                                      同名待核实
                                    </Badge>
                                  ) : null}
                                </div>
                                <p className="font-serif text-[12px] text-muted-foreground">
                                  {projCopy}
                                </p>
                              </div>
                            </label>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-6 px-2 text-[11px]"
                              onClick={() =>
                                void copyToClipboard(
                                  projCopy,
                                  `dossier-proj-${proj.id}`,
                                )
                              }
                            >
                              {copiedId === `dossier-proj-${proj.id}`
                                ? "已复制"
                                : "复制"}
                            </Button>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {dossier.truncated?.projects ? (
                    <p className="text-[11px] text-muted-foreground">
                      仅显示最近 200 条，共{" "}
                      {dossier.counts?.projects_total ??
                        dossier.counts?.projects ??
                        200}{" "}
                      条
                    </p>
                  ) : null}
                </div>

                {/* 5. Associated Achievements ("自动关联前期成果") */}
                <div className="rounded-lg border border-border bg-card p-4 space-y-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="font-medium text-[14px] text-foreground">
                      关联前期成果 (
                      {dossier.counts?.achievements_total ??
                        dossier.counts?.outputs_total ??
                        dossier.counts?.achievements ??
                        dossier.counts?.outputs ??
                        dossierAchievements.length}
                      )
                    </h3>
                    {dossierAchievements.length > 0 ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-7 px-2.5 text-[12px]"
                        onClick={() => {
                          const nonAmbiguous = dossierAchievements.filter(
                            (a) => a.match !== "ambiguous",
                          );
                          const allChecked =
                            nonAmbiguous.length > 0 &&
                            nonAmbiguous.every(
                              (a) => dossierCheckedAchievements[a.id],
                            );
                          const next: Record<string, boolean> = {
                            ...dossierCheckedAchievements,
                          };
                          for (const a of nonAmbiguous) {
                            next[a.id] = !allChecked;
                          }
                          setDossierCheckedAchievements(next);
                        }}
                      >
                        全选非同名成果
                      </Button>
                    ) : null}
                  </div>

                  {dossierAchievements.length === 0 ? (
                    <p className="text-[12px] text-muted-foreground">
                      暂无自动关联的前期成果记录。
                    </p>
                  ) : (
                    <div className="divide-y divide-border rounded-md border border-border">
                      {dossierAchievements.map((ach) => {
                        const achCopy =
                          ach.copy_text ||
                          ach.application_text ||
                          `《${ach.title}》`;
                        const achRelation =
                          ach.relation_role ?? ach.relation;
                        const isFirstAuthor = achRelation === "first_author";
                        const relLabel = isFirstAuthor
                          ? "第一作者"
                          : ach.author_position && ach.author_position > 1
                            ? `第 ${ach.author_position} 作者`
                            : "共同作者";
                        return (
                          <div
                            key={ach.id}
                            className="flex flex-wrap items-start justify-between gap-2 p-2.5"
                          >
                            <label className="flex flex-1 cursor-pointer items-start gap-2">
                              <input
                                type="checkbox"
                                checked={Boolean(
                                  dossierCheckedAchievements[ach.id],
                                )}
                                onChange={(e) =>
                                  setDossierCheckedAchievements((prev) => ({
                                    ...prev,
                                    [ach.id]: e.target.checked,
                                  }))
                                }
                                className="mt-1 h-4 w-4 rounded border-border"
                              />
                              <div className="space-y-1">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <Badge
                                    variant={
                                      isFirstAuthor ? "secondary" : "outline"
                                    }
                                    className="text-[11px]"
                                  >
                                    {relLabel}
                                  </Badge>
                                  <span className="font-medium text-foreground">
                                    《{ach.title}》
                                  </span>
                                  {ach.year ? (
                                    <span className="font-mono text-[11px] text-muted-foreground">
                                      ({ach.year})
                                    </span>
                                  ) : null}
                                  {ach.match === "ambiguous" ? (
                                    <Badge
                                      variant="outline"
                                      className="border-amber-500/50 bg-amber-500/15 text-[10px] text-amber-900 dark:text-amber-200"
                                    >
                                      同名待核实
                                    </Badge>
                                  ) : null}
                                </div>
                                <p className="font-serif text-[12px] text-muted-foreground">
                                  {achCopy}
                                </p>
                              </div>
                            </label>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-6 px-2 text-[11px]"
                              onClick={() =>
                                void copyToClipboard(
                                  achCopy,
                                  `dossier-ach-${ach.id}`,
                                )
                              }
                            >
                              {copiedId === `dossier-ach-${ach.id}`
                                ? "已复制"
                                : "复制"}
                            </Button>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {dossier.truncated?.achievements ||
                  dossier.truncated?.outputs ? (
                    <p className="text-[11px] text-muted-foreground">
                      仅显示最近 200 条，共{" "}
                      {dossier.counts?.achievements_total ??
                        dossier.counts?.outputs_total ??
                        dossier.counts?.achievements ??
                        dossier.counts?.outputs ??
                        200}{" "}
                      条
                    </p>
                  ) : null}
                </div>

                {/* 6. Dossier Footer Action Bar */}
                <DialogFooter className="flex flex-wrap items-center justify-between gap-2 pt-2">
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={handleAddPersonOnlyFromDossier}
                    >
                      将此人加入素材篮
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => handleAddCheckedDossierToBasket(false)}
                    >
                      将勾选加入素材篮
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => handleAddCheckedDossierToBasket(true)}
                    >
                      {t.addAllToBasketBtn}
                    </Button>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={closePersonDossier}
                  >
                    关闭
                  </Button>
                </DialogFooter>
              </div>
            ) : null}
          </DialogContent>
        </Dialog>

        {/* ===================================================================
            Feature 2: Floating Bottom Proposal Reuse Basket ("写本子素材篮")
            =================================================================== */}
        <div
          role="region"
          aria-label={`写本子素材篮，${basketItems.length} 项`}
          className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/85 shadow-lg"
        >
          <div className="mx-auto max-w-6xl px-4 py-2.5 space-y-2">
            {/* Expandable Basket Item Ordering & Removal Panel */}
            {basketPanelOpen ? (
              <div className="rounded-lg border border-border bg-background p-3 space-y-2.5 max-h-64 overflow-y-auto">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-[13px] text-foreground">
                      {t.reuseBasketTitle}清单 ({basketItems.length} /{" "}
                      {BASKET_MAX_ITEMS})
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      支持上下调整导出顺序或单条移除
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      签名包命名：
                      <select
                        value={basketOptions.naming}
                        onChange={(e) =>
                          updateBasketOption(
                            "naming",
                            e.target.value as BasketOptions["naming"],
                          )
                        }
                        className="h-6 rounded border border-input bg-card px-1.5 text-[11px] text-foreground"
                      >
                        <option value="seq_name">01_姓名.ext (seq_name)</option>
                        <option value="name">姓名.ext (name)</option>
                        <option value="name_id">姓名_ID.ext (name_id)</option>
                      </select>
                    </label>
                    {basketItems.length > 0 ? (
                      basketConfirmClear ? (
                        <div className="inline-flex items-center gap-1">
                          <span className="text-[11px] text-destructive">
                            确认清空？
                          </span>
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            className="h-6 px-2 text-[11px]"
                            onClick={() => {
                              updateBasketItems([]);
                              setBasketConfirmClear(false);
                              setStatusBanner("已清空写本子素材篮");
                            }}
                          >
                            确定清空
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            className="h-6 px-2 text-[11px]"
                            onClick={() => setBasketConfirmClear(false)}
                          >
                            {t.cancel}
                          </Button>
                        </div>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-6 px-2 text-[11px] text-destructive"
                          onClick={() => setBasketConfirmClear(true)}
                        >
                          清空素材篮
                        </Button>
                      )
                    ) : null}
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="h-6 px-2 text-[11px]"
                      onClick={() => setBasketPanelOpen(false)}
                    >
                      收起清单
                    </Button>
                  </div>
                </div>

                {basketItems.length === 0 ? (
                  <p className="py-3 text-center text-[12px] text-muted-foreground">
                    素材篮为空。请在上方 4 个标签页或个人全息档案袋中勾选需要复用的成员、项目、成果或设备。
                  </p>
                ) : (
                  <div className="divide-y divide-border rounded border border-border text-[12px]">
                    {basketItems.map((it, idx) => (
                      <div
                        key={basketKey(it.kind, it.id)}
                        className="flex items-center justify-between gap-2 px-2.5 py-1.5"
                      >
                        <div className="flex items-center gap-2 truncate">
                          <span className="font-mono text-[11px] text-muted-foreground">
                            #{idx + 1}
                          </span>
                          <Badge variant="outline" className="text-[10px]">
                            {BASKET_KIND_LABELS[it.kind]}
                          </Badge>
                          <span className="truncate font-medium text-foreground">
                            {it.label}
                          </span>
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px]"
                            disabled={idx === 0}
                            aria-label={`上移 ${it.label}`}
                            onClick={() => moveBasketItem(idx, -1)}
                          >
                            ↑
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px]"
                            disabled={idx === basketItems.length - 1}
                            aria-label={`下移 ${it.label}`}
                            onClick={() => moveBasketItem(idx, 1)}
                          >
                            ↓
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px] text-destructive"
                            aria-label={`移除 ${it.label}`}
                            onClick={() =>
                              removeItemsFromBasket(
                                new Set([basketKey(it.kind, it.id)]),
                              )
                            }
                          >
                            ✕
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : null}

            {/* Main Bottom Bar */}
            <div className="flex flex-wrap items-center justify-between gap-3">
              {/* Left: Summary & Toggle Drawer Button */}
              <div className="flex flex-wrap items-center gap-2.5">
                <Button
                  type="button"
                  size="sm"
                  variant={basketItems.length > 0 ? "secondary" : "outline"}
                  aria-label={`写本子素材篮，${basketItems.length} 项`}
                  className="h-8 px-2.5 text-[12px] font-medium"
                  onClick={() => setBasketPanelOpen((prev) => !prev)}
                >
                  🧺 {t.reuseBasketTitle} ({basketItems.length})
                  <span className="ml-1 text-[11px] opacity-80">
                    {basketPanelOpen ? "▾" : "▴"}
                  </span>
                </Button>
                <span className="text-[12px] font-medium text-foreground">
                  已选 {basketCounts.person} 人 · {basketCounts.project} 项项目
                  · {basketCounts.achievement} 篇成果 · {basketCounts.facility}{" "}
                  项条件
                </span>
              </div>

              {/* Center: Formatting Options */}
              <div className="flex flex-wrap items-center gap-3 text-[12px]">
                <label className="flex items-center gap-1 text-foreground">
                  <span className="text-muted-foreground">编号样式:</span>
                  <select
                    aria-label="编号样式"
                    value={basketOptions.numbering}
                    onChange={(e) =>
                      updateBasketOption(
                        "numbering",
                        e.target.value as BasketOptions["numbering"],
                      )
                    }
                    className="h-7 rounded border border-input bg-background px-1.5 text-[12px] text-foreground"
                  >
                    <option value="bracket">[1]</option>
                    <option value="arabic">1.</option>
                    <option value="none">无</option>
                  </select>
                </label>

                <label className="flex cursor-pointer items-center gap-1 text-foreground">
                  <input
                    type="checkbox"
                    checked={basketOptions.group_by_kind}
                    onChange={(e) =>
                      updateBasketOption("group_by_kind", e.target.checked)
                    }
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  按类别分组
                </label>

                <label className="flex cursor-pointer items-center gap-1 text-foreground">
                  <input
                    type="checkbox"
                    checked={
                      basketOptions.with_support || basketOptions.with_relation
                    }
                    onChange={(e) => {
                      const nextOpts: BasketOptions = {
                        ...basketOptions,
                        with_support: e.target.checked,
                        with_relation: e.target.checked,
                      };
                      setBasketOptions(nextOpts);
                      persistBasketToStorage(basketItems, nextOpts);
                    }}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  附带支撑/关系说明
                </label>

                <label className="flex cursor-pointer items-center gap-1 text-foreground">
                  <input
                    type="checkbox"
                    checked={basketOptions.include_signatures}
                    onChange={(e) =>
                      updateBasketOption("include_signatures", e.target.checked)
                    }
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  Word 附带成员签名表
                </label>
              </div>

              {/* Right: 3 Action Buttons */}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 px-3 text-[12px]"
                  disabled={
                    basketItems.length === 0 || basketActionLoading !== null
                  }
                  onClick={() => void handleBasketBatchCopy()}
                >
                  {basketActionLoading === "copy"
                    ? "正在复制…"
                    : t.batchCopyBasketBtn}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 px-3 text-[12px]"
                  disabled={
                    basketCounts.person === 0 || basketActionLoading !== null
                  }
                  title={
                    basketCounts.person === 0
                      ? "素材篮中没有成员"
                      : `打包下载所选 ${basketCounts.person} 名成员的电子签名 ZIP`
                  }
                  onClick={() => void handleBasketDownloadSignaturesZip()}
                >
                  {basketActionLoading === "zip"
                    ? "正在打包…"
                    : t.downloadSignaturesZipBtn}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  className="h-8 px-3 text-[12px]"
                  disabled={
                    basketItems.length === 0 || basketActionLoading !== null
                  }
                  onClick={() => void handleBasketExportSelectedDocx()}
                >
                  {basketActionLoading === "docx"
                    ? "正在导出…"
                    : t.exportSelectedDocxBtn}
                </Button>
              </div>
            </div>
          </div>
        </div>

        {/* Clipboard Fallback Dialog (§6.4) */}
        <Dialog
          open={clipboardFallbackText !== null}
          onOpenChange={(next) => {
            if (!next) setClipboardFallbackText(null);
          }}
        >
          <DialogContent className="max-w-xl">
            <DialogHeader>
              <DialogTitle>手动复制格式文本（请按 Ctrl/⌘+C）</DialogTitle>
              <DialogDescription>
                当前浏览器环境未授予剪贴板直接写入权限，已为您全选以下文本，请按
                Ctrl+C 或 ⌘+C 复制。
              </DialogDescription>
            </DialogHeader>
            <textarea
              readOnly
              rows={10}
              value={clipboardFallbackText || ""}
              onFocus={(e) => e.currentTarget.select()}
              className="w-full rounded-md border border-input bg-muted/30 p-3 font-mono text-[12px] text-foreground"
            />
            <DialogFooter>
              <Button
                type="button"
                onClick={() => setClipboardFallbackText(null)}
              >
                完成并关闭
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Phase 2 Online Edit & Field Provenance Restore Dialog */}
        <Dialog
          open={editModalOpen}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) {
              editSessionSeqRef.current += 1;
              setEditSaving(false);
              setRestoringFieldKey(null);
            }
            setEditModalOpen(nextOpen);
          }}
        >
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
              <form
                onSubmit={handleSaveEdit}
                className="space-y-3.5 text-[13px]"
              >
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
                              disabled={restoringFieldKey !== null || editSaving}
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
                    onClick={() => {
                      editSessionSeqRef.current += 1;
                      setEditSaving(false);
                      setRestoringFieldKey(null);
                      setEditModalOpen(false);
                    }}
                  >
                    {t.cancel}
                  </Button>
                  <Button
                    type="submit"
                    disabled={editSaving || restoringFieldKey !== null}
                  >
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
                  ? "上传既往项目申报书（仅支持 .docx）。系统会自动剥离全部身份证号与手机号，提取成员、电子签名、项目、成果与设备。"
                  : wizardStep === 2
                    ? "核对三级登录标识（T1 学工号 / T2 姓名全拼 / T3 姓名全拼+邮箱）与提取到的电子签名。所有新建账号角色固定为普通成员 (member)。"
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
                  项
                  {previewSignatures.length > 0 ? (
                    <>
                      、提取绑定电子签名{" "}
                      <span className="font-mono font-semibold">
                        {
                          previewSignatures.filter((s) => s.status === "bound")
                            .length
                        }
                      </span>{" "}
                      枚
                    </>
                  ) : null}
                  。
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

                {/* Spec §6.6 Extracted Signatures Preview Table */}
                {previewSignatures.length > 0 ? (
                  <div className="space-y-1.5">
                    <h4 className="font-medium text-[12px] text-foreground">
                      抽取到的成员电子签名（取消勾选将跳过入库）
                    </h4>
                    <div className="overflow-x-auto rounded-md border border-border">
                      <table className="w-full text-left text-[12px]">
                        <thead className="border-b border-border bg-muted/40 text-muted-foreground">
                          <tr>
                            <th className="px-2.5 py-2">入库</th>
                            <th className="px-2.5 py-2">姓名</th>
                            <th className="px-2.5 py-2">角色</th>
                            <th className="px-2.5 py-2">状态</th>
                            <th className="px-2.5 py-2">尺寸 / 格式</th>
                            <th className="px-2.5 py-2">告警</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {previewSignatures.map((sig, idx) => {
                            const sigKey =
                              sig.person_temp_id ||
                              sig.sig_id ||
                              `sig-${idx + 1}`;
                            const sigStatus = sig.status || "bound";
                            const isSkipped = Boolean(skipSignatureIds[sigKey]);
                            return (
                              <tr key={sigKey}>
                                <td className="px-2.5 py-1.5">
                                  <input
                                    type="checkbox"
                                    disabled={sigStatus !== "bound"}
                                    checked={
                                      sigStatus === "bound" && !isSkipped
                                    }
                                    onChange={(e) =>
                                      setSkipSignatureIds((prev) => ({
                                        ...prev,
                                        [sigKey]: !e.target.checked,
                                      }))
                                    }
                                  />
                                </td>
                                <td className="px-2.5 py-1.5 font-medium text-foreground">
                                  {sig.person_name || "—"}
                                </td>
                                <td className="px-2.5 py-1.5">
                                  {sig.role === "pi" ? "负责人" : "成员"}
                                </td>
                                <td className="px-2.5 py-1.5">
                                  <Badge
                                    variant={
                                      sigStatus === "bound"
                                        ? "secondary"
                                        : "outline"
                                    }
                                  >
                                    {sigStatus === "bound"
                                      ? "已绑定"
                                      : sigStatus === "ambiguous"
                                        ? "存在歧义"
                                        : "已拒绝"}
                                  </Badge>
                                </td>
                                <td className="px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground">
                                  {sig.width && sig.height
                                    ? `${sig.width}×${sig.height}`
                                    : "—"}{" "}
                                  {sig.mime ? `(${sig.mime})` : ""}
                                </td>
                                <td className="px-2.5 py-1.5 text-[11px] text-muted-foreground">
                                  {(sig.warnings || []).length > 0
                                    ? (sig.warnings || [])
                                        .map((w) => mapLabErrorCode(w))
                                        .join("；")
                                    : "无"}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ) : null}

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
