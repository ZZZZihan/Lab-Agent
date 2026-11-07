"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
  deleteImportJob,
  executeImportMerge,
  getImportJob,
  getImportRoster,
  listImportJobs,
  previewImportMerge,
  reparseImportJob,
  revertImportMerge,
  updateImportPreview,
  type ImportJobResponse,
  type ImportJobSummary,
  type MergeChangeCategory,
  type MergeChangeItem,
  type MergeDecision,
  type MergePreviewResponse,
  type ProvisionResult,
  type RosterRegenResponse,
  type User,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const DEFAULT_INITIAL_PASSWORD = "LabAgent@2026";
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const LOGIN_NAME_RE = /^[a-z0-9][a-z0-9._-]{2,63}$/;
const RESERVED_LOGIN_NAMES = new Set([
  "admin",
  "root",
  "system",
  "api",
  "null",
  "test",
  "labagent",
]);

function isValidLoginName(login: string): boolean {
  const normalized = login.trim().toLowerCase();
  return (
    normalized.length > 0 &&
    !RESERVED_LOGIN_NAMES.has(normalized) &&
    LOGIN_NAME_RE.test(normalized)
  );
}

type PersonMode = "new" | "existing" | "exclude";

type PersonOverrideRow = {
  mode: PersonMode;
  login_name: string;
  link_user_id: string;
};

type AdminUserItem = {
  id: string;
  email: string | null;
  login_name?: string | null;
  display_name: string;
  role: string;
};

const FIELD_LABELS: Record<string, string> = {
  title: "职称/名称 (title)",
  admin_duty: "行政职务 (admin_duty)",
  tutor_role: "博硕导 (tutor_role)",
  research_specialty: "研究专长 (research_specialty)",
  degree: "学位 (degree)",
  affiliation: "所在单位 (affiliation)",
  email: "邮箱 (email)",
  project_role: "项目角色 (project_role)",
  bio: "学术简历 (bio)",
  funder: "资助来源 (funder)",
  grant_no: "批准号 (grant_no)",
  pi_name: "负责人 (pi_name)",
  lab_role: "承担角色 (lab_role)",
  start_date: "起始日期 (start_date)",
  end_date: "截止日期 (end_date)",
  amount_wan: "经费万元 (amount_wan)",
  status: "项目状态 (status)",
  status_source: "状态来源 (status_source)",
  relationship_note: "关系说明 (relationship_note)",
  raw_text: "原始描述 (raw_text)",
  kind: "成果类型 (kind)",
  first_author: "第一作者 (first_author)",
  authors_text: "作者列表 (authors_text)",
  venue: "发表刊物/采纳 (venue)",
  year: "年份 (year)",
  support_note: "支撑价值说明 (support_note)",
  parse_quality: "解析质量 (parse_quality)",
  category: "设备类别 (category)",
  name: "名称 (name)",
  spec: "型号规格 (spec)",
  value_wan: "原值万元 (value_wan)",
  usage_scenario: "用途说明 (usage_scenario)",
};

const ENTITY_TYPE_LABELS: Record<string, string> = {
  person: "成员",
  project: "项目",
  achievement: "成果",
  facility: "设备",
};

function formatMergeVal(val: unknown): string {
  if (val === null || val === undefined || val === "") return "（空）";
  if (typeof val === "string") return val;
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    return JSON.stringify(val);
  } catch {
    return String(val);
  }
}

function buildInitialOverrides(
  job: ImportJobResponse,
): Record<string, PersonOverrideRow> {
  const init: Record<string, PersonOverrideRow> = {};
  for (const p of job.preview?.people || []) {
    const matchStatus = p.match?.status || "new";
    const mode: PersonMode =
      p.exclude || p.override_mode === "exclude"
        ? "exclude"
        : matchStatus === "existing"
          ? "existing"
          : p.override_mode === "existing" || p.override_mode === "new"
            ? p.override_mode
            : "new";
    init[p.key] = {
      mode,
      login_name: p.override_login_name || p.login_name || "",
      link_user_id:
        p.override_link_user_id !== undefined
          ? p.override_link_user_id
          : p.match?.user_id || "",
    };
  }
  return init;
}

let retainedUnsavedCredentialState: {
  ownerUserId: string;
  importJob: ImportJobResponse | null;
  provisionResult: ProvisionResult;
  includePassword: boolean;
} | null = null;

if (typeof window !== "undefined") {
  window.addEventListener("lab-auth-logout", () => {
    retainedUnsavedCredentialState = null;
  });
}

export default function AdminImportsPage() {
  const { t } = useI18n();
  const [me, setMe] = useState<User | null>(null);
  const [jobs, setJobs] = useState<ImportJobSummary[] | null>(null);
  const [users, setUsers] = useState<AdminUserItem[]>([]);
  const [error, setError] = useState("");
  const [infoMessage, setInfoMessage] = useState("");

  // Upload & Provision Wizard state
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardStep, setWizardStep] = useState<1 | 2 | 3>(1);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadAsOf, setUploadAsOf] = useState("");
  const [parsing, setParsing] = useState(false);
  const [wizardError, setWizardError] = useState("");
  const [wizardNotice, setWizardNotice] = useState("");

  const [importJob, setImportJob] = useState<ImportJobResponse | null>(null);
  const [provisionAsOf, setProvisionAsOf] = useState("");
  const [initialPassword, setInitialPassword] = useState(DEFAULT_INITIAL_PASSWORD);
  const [includePassword, setIncludePassword] = useState(true);
  const [overrides, setOverrides] = useState<Record<string, PersonOverrideRow>>({});
  const [savingPreview, setSavingPreview] = useState(false);
  const [reparsing, setReparsing] = useState(false);
  const [reparseMode, setReparseMode] = useState<"fsm" | "llm">("fsm");
  const [provisioning, setProvisioning] = useState(false);
  const [provisionResult, setProvisionResult] = useState<ProvisionResult | null>(null);
  const [rosterCopied, setRosterCopied] = useState(false);
  const [hasSavedCredentials, setHasSavedCredentials] = useState(false);
  const [showAssetDetails, setShowAssetDetails] = useState(false);

  // Passwordless Roster Modal state (for provisioned / merged jobs)
  const [rosterModalOpen, setRosterModalOpen] = useState(false);
  const [rosterJobTitle, setRosterJobTitle] = useState("");
  const [rosterData, setRosterData] = useState<RosterRegenResponse | null>(null);
  const [rosterModalCopied, setRosterModalCopied] = useState(false);

  // Phase 2 Merge Preview & Conflict Arbitration Modal state
  const [mergeModalOpen, setMergeModalOpen] = useState(false);
  const [mergeTargetJob, setMergeTargetJob] = useState<ImportJobSummary | null>(null);
  const [mergeAsOfInput, setMergeAsOfInput] = useState("");
  const [mergePreview, setMergePreview] = useState<MergePreviewResponse | null>(null);
  const [mergeLoading, setMergeLoading] = useState(false);
  const [mergeError, setMergeError] = useState("");
  const [mergeCategoryFilter, setMergeCategoryFilter] = useState<string>("all");
  const [mergeDecisions, setMergeDecisions] = useState<
    Record<string, "adopt_incoming" | "keep_current" | "force_adopt">
  >({});
  const [executingMerge, setExecutingMerge] = useState(false);
  const [revertingJobId, setRevertingJobId] = useState<string | null>(null);
  const mergePreviewReqSeqRef = useRef(0);

  const loadJobs = useCallback(async () => {
    setError("");
    try {
      const [actor, listedJobs, listedUsers] = await Promise.all([
        currentUser(),
        listImportJobs(),
        api<{ users: AdminUserItem[] }>("admin/users").catch(() => ({ users: [] })),
      ]);
      setMe(actor);
      if (!actor || actor.role !== "admin") {
        retainedUnsavedCredentialState = null;
        setWizardOpen(false);
        setProvisionResult(null);
      } else if (
        retainedUnsavedCredentialState &&
        retainedUnsavedCredentialState.ownerUserId !== actor.id
      ) {
        retainedUnsavedCredentialState = null;
        setWizardOpen(false);
        setProvisionResult(null);
      }
      setJobs(listedJobs);
      setUsers(listedUsers.users);
    } catch (err) {
      retainedUnsavedCredentialState = null;
      setError(err instanceof ApiError ? err.message : t.error);
      setJobs([]);
    }
  }, [t.error]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      currentUser(),
      listImportJobs().catch((err) => {
        throw err;
      }),
      api<{ users: AdminUserItem[] }>("admin/users").catch(() => ({ users: [] })),
    ])
      .then(([actor, listedJobs, listedUsers]) => {
        if (cancelled) return;
        setMe(actor);
        if (!actor || actor.role !== "admin") {
          retainedUnsavedCredentialState = null;
          setWizardOpen(false);
          setProvisionResult(null);
        } else if (retainedUnsavedCredentialState) {
          if (retainedUnsavedCredentialState.ownerUserId === actor.id) {
            setImportJob(retainedUnsavedCredentialState.importJob);
            setProvisionResult(retainedUnsavedCredentialState.provisionResult);
            setIncludePassword(retainedUnsavedCredentialState.includePassword);
            setHasSavedCredentials(false);
            setWizardStep(3);
            setWizardOpen(true);
          } else {
            retainedUnsavedCredentialState = null;
            setWizardOpen(false);
            setProvisionResult(null);
          }
        }
        setJobs(listedJobs);
        setUsers(listedUsers.users);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        retainedUnsavedCredentialState = null;
        setWizardOpen(false);
        setProvisionResult(null);
        setError(err instanceof ApiError ? err.message : t.error);
        setJobs([]);
      });
    return () => {
      cancelled = true;
    };
  }, [t.error]);

  const wizardBusy = savingPreview || reparsing || provisioning;

  // Spec §8.3.3 beforeunload guard when Step 3 has plaintext credentials in memory
  const hasUnsavedPlaintextRoster = Boolean(
    wizardOpen &&
      wizardStep === 3 &&
      provisionResult &&
      Boolean(provisionResult.initial_password || includePassword) &&
      !hasSavedCredentials,
  );

  useEffect(() => {
    if (
      hasUnsavedPlaintextRoster &&
      provisionResult &&
      me?.id &&
      me.role === "admin"
    ) {
      retainedUnsavedCredentialState = {
        ownerUserId: me.id,
        importJob,
        provisionResult,
        includePassword,
      };
    } else if (hasSavedCredentials || (me && me.role !== "admin")) {
      retainedUnsavedCredentialState = null;
    }
  }, [
    hasUnsavedPlaintextRoster,
    hasSavedCredentials,
    importJob,
    provisionResult,
    includePassword,
    me,
  ]);

  useEffect(() => {
    if (!hasUnsavedPlaintextRoster) return;
    function handleBeforeUnload(e: BeforeUnloadEvent) {
      e.preventDefault();
      e.returnValue = t.credentialLeaveConfirm;
      return t.credentialLeaveConfirm;
    }
    function handleDocClick(e: MouseEvent) {
      const target = e.target as HTMLElement | null;
      const anchor = target?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!anchor || anchor.hasAttribute("download")) return;
      const href = anchor.getAttribute("href");
      if (!href || href.startsWith("#") || href.startsWith("blob:")) return;
      if (!window.confirm(t.credentialLeaveConfirm)) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      retainedUnsavedCredentialState = null;
    }
    function handlePopState() {
      if (!window.confirm(t.credentialLeaveConfirm)) {
        window.history.pushState(null, "", window.location.href);
        return;
      }
      retainedUnsavedCredentialState = null;
    }
    window.addEventListener("beforeunload", handleBeforeUnload);
    document.addEventListener("click", handleDocClick, true);
    window.addEventListener("popstate", handlePopState);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      document.removeEventListener("click", handleDocClick, true);
      window.removeEventListener("popstate", handlePopState);
    };
  }, [hasUnsavedPlaintextRoster, t.credentialLeaveConfirm]);

  function tryCloseWizard() {
    if (wizardBusy) return;
    if (
      wizardStep === 3 &&
      provisionResult &&
      Boolean(provisionResult.initial_password || includePassword) &&
      !hasSavedCredentials
    ) {
      const confirmed = window.confirm(t.credentialLeaveConfirm);
      if (!confirmed) return;
    }
    retainedUnsavedCredentialState = null;
    setWizardOpen(false);
    setProvisionResult(null);
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
      setWizardError("文件大小超过 20 MB 上限，请检查文件后再上传");
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
    const lower = uploadFile.name.toLowerCase();
    if (!lower.endsWith(".docx")) {
      setWizardError("仅支持上传 .docx 格式的申报书文件");
      return;
    }
    if (uploadFile.size > MAX_UPLOAD_BYTES) {
      setWizardError("文件大小超过 20 MB 上限");
      return;
    }

    setWizardError("");
    setWizardNotice("");
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
      setOverrides(buildInitialOverrides(parsed));
      if (parsed.deduplicated) {
        setWizardNotice("该文件已上传过且尚未开号，已打开原任务预览。");
      }
      setWizardStep(2);
      await loadJobs();
    } catch (err) {
      setWizardError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setParsing(false);
    }
  }

  async function handleOpenJobPreview(jobId: string) {
    setWizardError("");
    setWizardNotice("");
    setProvisionResult(null);
    setHasSavedCredentials(false);
    try {
      const detail = await getImportJob(jobId);
      setImportJob(detail);
      setProvisionAsOf(detail.as_of_time || detail.preview?.as_of_time || "");
      setOverrides(buildInitialOverrides(detail));
      setWizardStep(2);
      setWizardOpen(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    }
  }

  async function handleDeleteJob(job: ImportJobSummary) {
    setError("");
    setInfoMessage("");
    try {
      await deleteImportJob(job.job_id || job.id);
      setInfoMessage(`已删除导入任务：${job.original_filename}`);
      await loadJobs();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    }
  }

  async function handleOpenRosterModal(job: ImportJobSummary) {
    setError("");
    try {
      const data = await getImportRoster(job.job_id || job.id, "text");
      setRosterJobTitle(job.original_filename);
      setRosterData(data);
      setRosterModalCopied(false);
      setRosterModalOpen(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    }
  }

  async function handleSavePreview() {
    if (!importJob || savingPreview || reparsing || provisioning) return;
    setWizardError("");
    setWizardNotice("");
    setSavingPreview(true);
    try {
      const updatedPeople = (importJob.preview?.people || []).map((p) => {
        const ov = overrides[p.key];
        if (!ov) return p;
        const trimmedLogin = ov.login_name.trim().toLowerCase();
        return {
          ...p,
          exclude: ov.mode === "exclude",
          override_mode: ov.mode,
          override_login_name:
            ov.mode === "new" ? trimmedLogin || p.login_name : p.login_name,
          override_link_user_id: ov.mode === "existing" ? ov.link_user_id : "",
          login_name:
            ov.mode === "new" ? trimmedLogin || p.login_name : p.login_name,
          match:
            ov.mode === "existing" && ov.link_user_id
              ? { status: "existing", user_id: ov.link_user_id }
              : ov.mode === "new"
                ? { status: "new" }
                : p.match,
        };
      });
      const updated = await updateImportPreview(importJob.job_id, {
        as_of_time: provisionAsOf.trim() || null,
        people: updatedPeople,
      });
      setImportJob(updated);
      setProvisionAsOf(updated.as_of_time || updated.preview?.as_of_time || "");
      setOverrides(buildInitialOverrides(updated));
      setWizardNotice("校对草稿已保存。");
      await loadJobs();
    } catch (err) {
      setWizardError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setSavingPreview(false);
    }
  }

  async function handleReparse() {
    if (!importJob || savingPreview || reparsing || provisioning) return;
    setWizardError("");
    setWizardNotice("");
    setReparsing(true);
    try {
      const reparsed = await reparseImportJob(importJob.job_id, reparseMode);
      setImportJob(reparsed);
      setProvisionAsOf(reparsed.as_of_time || reparsed.preview?.as_of_time || "");
      setOverrides(buildInitialOverrides(reparsed));
      setWizardNotice(
        `已完成重新解析（模式：${reparseMode === "llm" ? "rule+llm" : "rule+regex_fsm"}）。`,
      );
      await loadJobs();
    } catch (err) {
      setWizardError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setReparsing(false);
    }
  }

  async function handleProvision() {
    if (!importJob || savingPreview || reparsing || provisioning) return;
    if (!provisionAsOf.trim()) {
      setWizardError("请填写本子基准日期 (as_of_time)");
      return;
    }
    setWizardError("");
    setWizardNotice("");
    setProvisioning(true);
    try {
      const peoplePayload = (importJob.preview?.people || []).map((p) => {
        const ov = overrides[p.key];
        const trimmedLogin = (ov?.login_name || "").trim().toLowerCase();
        return {
          key: p.key,
          exclude: ov?.mode === "exclude",
          ...(ov?.mode === "new" &&
          trimmedLogin &&
          trimmedLogin !== p.login_name
            ? { login_name: trimmedLogin }
            : {}),
          ...(ov?.mode === "existing" && ov.link_user_id
            ? { link_user_id: ov.link_user_id }
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
      if (
        me?.id &&
        me.role === "admin" &&
        Boolean(res.initial_password || includePassword)
      ) {
        retainedUnsavedCredentialState = {
          ownerUserId: me.id,
          importJob,
          provisionResult: res,
          includePassword,
        };
      }
      setProvisionResult(res);
      setHasSavedCredentials(false);
      setWizardStep(3);
      await loadJobs();
    } catch (err) {
      setWizardError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setProvisioning(false);
    }
  }

  function handleDownloadProvisionCsv() {
    if (!provisionResult) return;
    const blob = new Blob([provisionResult.roster_csv], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `roster-${provisionResult.job_id}-${provisionResult.as_of_time.replace(/-/g, "")}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    retainedUnsavedCredentialState = null;
    setHasSavedCredentials(true);
  }

  function handleDownloadRegenCsv() {
    if (!rosterData) return;
    const blob = new Blob([rosterData.roster_csv], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `roster-passwordless-${rosterData.job_id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handleOpenMergePreview(job: ImportJobSummary) {
    const reqSeq = ++mergePreviewReqSeqRef.current;
    setError("");
    setInfoMessage("");
    setMergeError("");
    setMergeTargetJob(job);
    setMergeAsOfInput(job.as_of_time || "");
    setMergePreview(null);
    setMergeDecisions({});
    setMergeCategoryFilter("all");
    setMergeModalOpen(true);
    setMergeLoading(true);
    try {
      const preview = await previewImportMerge(
        job.job_id || job.id,
        job.as_of_time || undefined,
      );
      if (mergePreviewReqSeqRef.current !== reqSeq) return;
      setMergePreview(preview);
      if (preview.as_of_time) {
        setMergeAsOfInput(preview.as_of_time);
      }
    } catch (err) {
      if (mergePreviewReqSeqRef.current !== reqSeq) return;
      setMergeError(err instanceof ApiError ? err.message : t.error);
    } finally {
      if (mergePreviewReqSeqRef.current === reqSeq) {
        setMergeLoading(false);
      }
    }
  }

  async function handleRefreshMergePreview() {
    if (!mergeTargetJob) return;
    const reqSeq = ++mergePreviewReqSeqRef.current;
    setMergeError("");
    setMergePreview(null);
    setMergeDecisions({});
    setMergeLoading(true);
    try {
      const preview = await previewImportMerge(
        mergeTargetJob.job_id || mergeTargetJob.id,
        mergeAsOfInput.trim() || undefined,
      );
      if (mergePreviewReqSeqRef.current !== reqSeq) return;
      setMergePreview(preview);
      if (preview.as_of_time) {
        setMergeAsOfInput(preview.as_of_time);
      }
    } catch (err) {
      if (mergePreviewReqSeqRef.current !== reqSeq) return;
      setMergeError(err instanceof ApiError ? err.message : t.error);
    } finally {
      if (mergePreviewReqSeqRef.current === reqSeq) {
        setMergeLoading(false);
      }
    }
  }

  const unresolvedConflictCount = useMemo(() => {
    if (!mergePreview) return 0;
    return mergePreview.changes.filter(
      (c) =>
        (c.category === "same_time_conflict" || c.requires_decision) &&
        !mergeDecisions[c.change_key],
    ).length;
  }, [mergePreview, mergeDecisions]);

  const filteredMergeChanges = useMemo(() => {
    if (!mergePreview) return [];
    if (mergeCategoryFilter === "all") return mergePreview.changes;
    return mergePreview.changes.filter((c) => c.category === mergeCategoryFilter);
  }, [mergePreview, mergeCategoryFilter]);

  async function handleExecuteMerge() {
    if (!mergeTargetJob || !mergePreview) return;
    const targetJobId = mergeTargetJob.job_id || mergeTargetJob.id;
    if (mergePreview.job_id && mergePreview.job_id !== targetJobId) {
      setMergeError(
        "当前预览与目标导入任务不匹配，请点击「重新比对预览」后再确认合并。",
      );
      return;
    }
    if (
      mergeAsOfInput.trim() &&
      mergePreview.as_of_time &&
      mergeAsOfInput.trim() !== mergePreview.as_of_time
    ) {
      setMergeError(
        "已修改基准日期，请点击「重新比对预览」加载最新差异后再确认合并。",
      );
      return;
    }
    if (unresolvedConflictCount > 0) {
      setMergeError(`${t.unresolvedConflictsHint}（剩余 ${unresolvedConflictCount} 项）`);
      return;
    }
    setMergeError("");
    setExecutingMerge(true);
    try {
      const decisionsList: MergeDecision[] = Object.entries(mergeDecisions).map(
        ([change_key, action]) => ({ change_key, action }),
      );
      const res = await executeImportMerge(targetJobId, {
        as_of_time: mergePreview.as_of_time || undefined,
        decisions: decisionsList,
      });
      setMergeModalOpen(false);
      const st = res.stats || {};
      const sum = res.summary || {};
      const addedN = st.added_entities ?? res.added_count ?? sum.added ?? 0;
      const updatedN =
        st.updated_fields ?? res.updated_count ?? sum.updated ?? 0;
      const keptN = st.kept_fields ?? 0;
      const forcedN = st.forced_fields ?? res.forced_count ?? sum.forced ?? 0;
      setInfoMessage(
        `已完成版本合并（${mergeTargetJob.original_filename}）：新增实体 ${addedN} 项、更新字段 ${updatedN} 处（含人工强制采用 ${forcedN} 处）、保留原值 ${keptN} 处。`,
      );
      await loadJobs();
    } catch (err) {
      setMergeError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setExecutingMerge(false);
    }
  }

  async function handleRevertMerge(job: ImportJobSummary) {
    const confirmed = window.confirm(t.revertMergeConfirm);
    if (!confirmed) return;
    const targetId = job.job_id || job.id;
    setError("");
    setInfoMessage("");
    setRevertingJobId(targetId);
    try {
      const res = await revertImportMerge(targetId);
      setInfoMessage(
        `已撤销本子合并（${job.original_filename}）：回退字段 ${res.reverted_fields} 处，清理新增独立实体 ${res.deleted_entities} 项（状态恢复为 ${res.job_status}）。`,
      );
      await loadJobs();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setRevertingJobId(null);
    }
  }

  function renderMergeCategoryBadge(category: MergeChangeCategory) {
    if (category === "updated") {
      return (
        <Badge
          variant="secondary"
          className="border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
        >
          更新
        </Badge>
      );
    }
    if (category === "fill_blank") {
      return (
        <Badge
          variant="secondary"
          className="border-blue-500/30 bg-blue-500/15 text-blue-700 dark:text-blue-300"
        >
          填空
        </Badge>
      );
    }
    if (category === "time_regression") {
      return (
        <Badge
          variant="secondary"
          className="border-amber-500/40 bg-amber-500/15 text-amber-800 dark:text-amber-300"
        >
          时间倒退已忽略
        </Badge>
      );
    }
    if (category === "manual_protected") {
      return (
        <Badge
          variant="secondary"
          className="border-purple-500/40 bg-purple-500/15 text-purple-800 dark:text-purple-300"
        >
          人工保护已忽略
        </Badge>
      );
    }
    if (category === "same_time_conflict") {
      return (
        <Badge
          variant="outline"
          className="border-destructive/50 bg-destructive/15 font-semibold text-destructive"
        >
          同时间冲突
        </Badge>
      );
    }
    return <Badge variant="outline">{category}</Badge>;
  }

  function renderComparisonDetail(item: MergeChangeItem) {
    const incomingStr = formatMergeVal(item.incoming_value);
    const currentStr = formatMergeVal(item.current_value);
    const inTime = item.incoming_as_of_time || "—";
    const curTime = item.current_as_of_time || "—";

    if (item.category === "time_regression") {
      return (
        <span>
          <span className="font-medium text-amber-800 dark:text-amber-300">
            {incomingStr} ({inTime} 本子)
          </span>{" "}
          → 当前保留：
          <span className="font-semibold text-foreground">
            {currentStr} ({curTime} 本子)
          </span>
        </span>
      );
    }
    if (item.category === "manual_protected") {
      return (
        <span>
          <span className="text-muted-foreground">
            {incomingStr} ({inTime} 本子)
          </span>{" "}
          → 当前保留人工修改：
          <span className="font-semibold text-purple-800 dark:text-purple-300">
            {currentStr}
          </span>
        </span>
      );
    }
    if (item.category === "same_time_conflict") {
      return (
        <span>
          本次导入：
          <span className="font-semibold text-destructive">
            {incomingStr} ({inTime} 本子)
          </span>{" "}
          vs 当前值：
          <span className="font-semibold text-foreground">
            {currentStr} ({curTime} 本子)
          </span>
        </span>
      );
    }
    if (item.category === "fill_blank") {
      return (
        <span>
          当前：<span className="text-muted-foreground">（空）</span> → 补齐为：
          <span className="font-semibold text-blue-700 dark:text-blue-300">
            {incomingStr} ({inTime} 本子)
          </span>
        </span>
      );
    }
    return (
      <span>
        原值：
        <span className="text-muted-foreground">
          {currentStr} ({curTime} 本子)
        </span>{" "}
        → 更新为：
        <span className="font-semibold text-emerald-700 dark:text-emerald-300">
          {incomingStr} ({inTime} 本子)
        </span>
      </span>
    );
  }

  const previewSummary = useMemo(() => {
    const list = importJob?.preview?.people || [];
    let newCount = 0;
    let existingCount = 0;
    let excludeCount = 0;
    let unresolvedCount = 0;
    for (const p of list) {
      const ov = overrides[p.key] || {
        mode: p.match?.status === "existing" ? "existing" : "new",
        login_name: p.login_name || "",
        link_user_id: p.match?.user_id || "",
      };
      if (ov.mode === "exclude") {
        excludeCount += 1;
        continue;
      }
      if (ov.mode === "existing") {
        existingCount += 1;
        if (!ov.link_user_id && p.match?.status !== "existing") {
          unresolvedCount += 1;
        }
        continue;
      }
      newCount += 1;
      const loginClean = ov.login_name.trim().toLowerCase();
      const stillConflict =
        p.match?.status === "conflict" && loginClean === (p.login_name || "");
      if (!isValidLoginName(ov.login_name) || stillConflict) {
        unresolvedCount += 1;
      }
    }
    return {
      total: list.length,
      newCount,
      existingCount,
      excludeCount,
      unresolvedCount,
    };
  }, [importJob?.preview?.people, overrides]);

  function renderStatusBadge(job: ImportJobSummary) {
    if (job.status === "provisioned") {
      return (
        <Badge
          variant="secondary"
          className="border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
        >
          已开号 (provisioned)
        </Badge>
      );
    }
    if (job.status === "merged") {
      return (
        <Badge
          variant="secondary"
          className="border-blue-500/30 bg-blue-500/15 text-blue-700 dark:text-blue-300"
        >
          merged（已合并）
        </Badge>
      );
    }
    if (job.status === "failed") {
      return (
        <Badge
          variant="outline"
          title={job.error_code || "PARSE_FAILED"}
          className="border-destructive/40 bg-destructive/10 text-destructive"
        >
          失败{job.error_code ? ` (${job.error_code})` : ""}
        </Badge>
      );
    }
    return (
      <Badge variant="outline" className="text-muted-foreground">
        待开号 (parsed)
      </Badge>
    );
  }

  return (
    <Shell>
      <div className="space-y-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="font-display text-2xl">{t.importsTitle}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {t.importsSubtitle}
            </p>
          </div>
          {me?.role === "admin" ? (
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href="/lab"
                className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
              >
                {t.labAssets}
              </Link>
              <Button
                size="sm"
                onClick={() => {
                  setWizardError("");
                  setWizardNotice("");
                  setWizardStep(1);
                  setUploadFile(null);
                  setUploadAsOf("");
                  setImportJob(null);
                  setProvisionResult(null);
                  setHasSavedCredentials(false);
                  setWizardOpen(true);
                }}
              >
                {t.uploadProposalBtn}
              </Button>
            </div>
          ) : null}
        </div>

        {me && me.role !== "admin" ? (
          <p role="alert" className="text-sm text-destructive">
            {t.adminOnly}
          </p>
        ) : null}

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {infoMessage ? (
          <p role="status" className="text-sm font-medium text-emerald-600">
            {infoMessage}
          </p>
        ) : null}

        {jobs === null ? (
          <p className="text-sm text-muted-foreground">{t.loading}</p>
        ) : jobs.length === 0 ? (
          <div className="rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            暂无申报书导入任务记录。点击右上角「{t.uploadProposalBtn}」开始全组冷启动导入。
          </div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border bg-card">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-border bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">文件名</th>
                  <th className="px-4 py-3 font-medium">状态</th>
                  <th className="px-4 py-3 font-medium">基准时间 (as_of_time)</th>
                  <th className="px-4 py-3 font-medium">识别统计</th>
                  <th className="px-4 py-3 font-medium">新建账号数</th>
                  <th className="px-4 py-3 font-medium">上传时间</th>
                  <th className="px-4 py-3 font-medium">{t.action}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {jobs.map((job) => {
                  const st = job.stats || {};
                  const peopleCount =
                    st.people_parsed ??
                    (st.people_inserted ?? 0) + (st.people_skipped ?? 0);
                  const projCount =
                    st.projects_parsed ??
                    (st.projects_inserted ?? 0) + (st.projects_skipped ?? 0);
                  const achCount =
                    st.achievements_parsed ??
                    (st.achievements_inserted ?? 0) +
                      (st.achievements_skipped ?? 0);
                  const facCount =
                    st.facilities_parsed ??
                    (st.facilities_inserted ?? 0) + (st.facilities_skipped ?? 0);
                  return (
                    <tr key={job.id}>
                      <td className="px-4 py-3 font-medium">
                        {job.original_filename}
                      </td>
                      <td className="px-4 py-3">{renderStatusBadge(job)}</td>
                      <td className="px-4 py-3 font-mono text-xs">
                        {job.as_of_time || "—"}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        成员 {peopleCount} · 项目 {projCount} · 成果 {achCount} · 设备{" "}
                        {facCount}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs">
                        {st.users_created !== undefined ? st.users_created : "—"}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                        {job.created_at
                          ? job.created_at.replace("T", " ").slice(0, 16)
                          : "—"}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-2">
                          {job.status === "parsed" ? (
                            <>
                              <Button
                                size="sm"
                                variant="default"
                                onClick={() =>
                                  handleOpenJobPreview(job.job_id || job.id)
                                }
                              >
                                继续开号 / 校对预览
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => handleOpenMergePreview(job)}
                              >
                                {t.mergePreviewBtn}
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => handleDeleteJob(job)}
                              >
                                {t.delete}
                              </Button>
                            </>
                          ) : null}
                          {job.status === "provisioned" ? (
                            <>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => handleOpenMergePreview(job)}
                              >
                                {t.mergePreviewBtn}
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => handleOpenRosterModal(job)}
                              >
                                查看清单（不含口令）
                              </Button>
                            </>
                          ) : null}
                          {job.status === "merged" ? (
                            <>
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={
                                  revertingJobId === (job.job_id || job.id)
                                }
                                onClick={() => handleRevertMerge(job)}
                              >
                                {revertingJobId === (job.job_id || job.id)
                                  ? t.loading
                                  : t.revertMergeBtn}
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => handleOpenRosterModal(job)}
                              >
                                查看清单（不含口令）
                              </Button>
                            </>
                          ) : null}
                          {job.status === "failed" ? (
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => handleDeleteJob(job)}
                            >
                              {t.delete}
                            </Button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Phase 2 Multi-Proposal Merge Preview & Conflict Arbitration Modal */}
        <Dialog
          open={mergeModalOpen}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) {
              mergePreviewReqSeqRef.current += 1;
              setMergeLoading(false);
            }
            setMergeModalOpen(nextOpen);
          }}
        >
          <DialogContent className="max-w-5xl max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {t.mergeDialogTitle} — {mergeTargetJob?.original_filename || ""}
              </DialogTitle>
              <DialogDescription>{t.mergeDialogDesc}</DialogDescription>
            </DialogHeader>

            <div className="space-y-4 text-xs">
              <div className="flex flex-wrap items-end justify-between gap-3 rounded-md border border-border bg-muted/30 p-3">
                <div className="flex flex-wrap items-end gap-2">
                  <div className="space-y-1">
                    <Label htmlFor="merge-as-of" className="text-xs">
                      本次合并基准日期 (as_of_time)
                    </Label>
                    <Input
                      id="merge-as-of"
                      type="date"
                      value={mergeAsOfInput}
                      onChange={(e) => {
                        mergePreviewReqSeqRef.current += 1;
                        setMergeLoading(false);
                        setMergeError("");
                        setMergeAsOfInput(e.target.value);
                        setMergePreview(null);
                        setMergeDecisions({});
                      }}
                      className="h-8 w-44 font-mono text-xs"
                    />
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={mergeLoading}
                    onClick={handleRefreshMergePreview}
                  >
                    {mergeLoading ? t.loading : "重新比对预览"}
                  </Button>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  合并仅增量演进课题组资料库与字段级溯源，绝不新建或改动已有用户账号与口令。
                </p>
              </div>

              {mergeError ? (
                <p role="alert" className="text-sm text-destructive">
                  {mergeError}
                </p>
              ) : null}

              {mergeLoading ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  {t.loading}
                </p>
              ) : mergePreview ? (
                <div className="space-y-4">
                  {/* Top 6-item Summary Cards (Spec §10.5) */}
                  <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-6">
                    <div className="rounded-md border border-border bg-card p-2.5">
                      <p className="text-[11px] text-muted-foreground">
                        {t.mergeSummaryAdded}
                      </p>
                      <p className="mt-1 font-display text-lg font-semibold text-foreground">
                        新增 {mergePreview.summary.added}
                      </p>
                    </div>
                    <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2.5">
                      <p className="text-[11px] text-emerald-800 dark:text-emerald-300">
                        {t.mergeSummaryUpdated}
                      </p>
                      <p className="mt-1 font-display text-lg font-semibold text-emerald-800 dark:text-emerald-200">
                        更新 {mergePreview.summary.updated}
                      </p>
                    </div>
                    <div className="rounded-md border border-blue-500/30 bg-blue-500/10 p-2.5">
                      <p className="text-[11px] text-blue-800 dark:text-blue-300">
                        {t.mergeSummaryFillBlank}
                      </p>
                      <p className="mt-1 font-display text-lg font-semibold text-blue-800 dark:text-blue-200">
                        填空 {mergePreview.summary.fill_blank}
                      </p>
                    </div>
                    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5">
                      <p className="text-[11px] text-amber-800 dark:text-amber-300">
                        {t.mergeSummaryTimeRegression}
                      </p>
                      <p className="mt-1 font-display text-lg font-semibold text-amber-800 dark:text-amber-200">
                        倒退 {mergePreview.summary.time_regression}（已自动忽略）
                      </p>
                    </div>
                    <div className="rounded-md border border-purple-500/40 bg-purple-500/10 p-2.5">
                      <p className="text-[11px] text-purple-800 dark:text-purple-300">
                        {t.mergeSummaryManualProtected}
                      </p>
                      <p className="mt-1 font-display text-lg font-semibold text-purple-800 dark:text-purple-200">
                        人工保护 {mergePreview.summary.manual_protected}（已自动忽略）
                      </p>
                    </div>
                    <div
                      className={cn(
                        "rounded-md border p-2.5",
                        mergePreview.summary.same_time_conflict > 0
                          ? "border-destructive bg-destructive/15"
                          : "border-border bg-card",
                      )}
                    >
                      <p className="text-[11px] text-muted-foreground">
                        {t.mergeSummaryConflict}
                      </p>
                      <p
                        className={cn(
                          "mt-1 font-display text-lg font-semibold",
                          mergePreview.summary.same_time_conflict > 0
                            ? "text-destructive"
                            : "text-foreground",
                        )}
                      >
                        待裁决冲突 {mergePreview.summary.same_time_conflict}
                      </p>
                    </div>
                  </div>

                  {/* Added Entities Summary */}
                  {mergePreview.added_entities &&
                  mergePreview.added_entities.length > 0 ? (
                    <div className="rounded-md border border-border bg-muted/20 p-3">
                      <p className="mb-1.5 font-medium text-foreground">
                        本次将直接并入的新增条目（共{" "}
                        {mergePreview.added_entities.length} 项）：
                      </p>
                      <div className="flex flex-wrap gap-1.5">
                        {mergePreview.added_entities.map((ent, idx) => (
                          <Badge
                            key={`${ent.entity_type}-${ent.name}-${idx}`}
                            variant="outline"
                            className="text-xs"
                          >
                            [
                            {ENTITY_TYPE_LABELS[ent.entity_type] ||
                              ent.entity_type}
                            ] {ent.name}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {/* Category filter buttons */}
                  <div className="flex flex-wrap items-center gap-1.5">
                    {(
                      [
                        ["all", `全部字段变更 (${mergePreview.changes.length})`],
                        ["updated", `更新 (${mergePreview.summary.updated})`],
                        ["fill_blank", `填空 (${mergePreview.summary.fill_blank})`],
                        [
                          "time_regression",
                          `时间倒退已忽略 (${mergePreview.summary.time_regression})`,
                        ],
                        [
                          "manual_protected",
                          `人工保护已忽略 (${mergePreview.summary.manual_protected})`,
                        ],
                        [
                          "same_time_conflict",
                          `同时间冲突 (${mergePreview.summary.same_time_conflict})`,
                        ],
                      ] as const
                    ).map(([catKey, catLabel]) => (
                      <Button
                        key={catKey}
                        type="button"
                        size="sm"
                        variant={
                          mergeCategoryFilter === catKey ? "default" : "outline"
                        }
                        className="h-7 px-2.5 text-xs"
                        onClick={() => setMergeCategoryFilter(catKey)}
                      >
                        {catLabel}
                      </Button>
                    ))}
                  </div>

                  {/* Changes Detail Table */}
                  {filteredMergeChanges.length === 0 ? (
                    <div className="rounded-md border border-border bg-card p-6 text-center text-muted-foreground">
                      当前筛选分类下暂无字段级差异记录。
                    </div>
                  ) : (
                    <div className="overflow-x-auto rounded-md border border-border">
                      <table className="w-full text-left text-xs">
                        <thead className="border-b border-border bg-muted/40 text-muted-foreground">
                          <tr>
                            <th className="px-3 py-2">实体名称</th>
                            <th className="px-3 py-2">字段名</th>
                            <th className="px-3 py-2">变化类别</th>
                            <th className="px-3 py-2">对比详情</th>
                            <th className="px-3 py-2">裁决操作</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {filteredMergeChanges.map((item) => {
                            const decision = mergeDecisions[item.change_key];
                            const isConflict =
                              item.category === "same_time_conflict" ||
                              item.requires_decision;
                            const isForceable =
                              item.category === "time_regression" ||
                              item.category === "manual_protected";
                            return (
                              <tr
                                key={item.change_key}
                                className={
                                  isConflict
                                    ? "bg-destructive/10"
                                    : undefined
                                }
                              >
                                <td className="px-3 py-2.5 font-medium text-foreground">
                                  <span className="mr-1 text-muted-foreground">
                                    [
                                    {ENTITY_TYPE_LABELS[item.entity_type] ||
                                      item.entity_type}
                                    ]
                                  </span>
                                  {item.entity_name}
                                </td>
                                <td className="px-3 py-2.5 font-mono">
                                  {FIELD_LABELS[item.field] || item.field}
                                </td>
                                <td className="px-3 py-2.5">
                                  {renderMergeCategoryBadge(item.category)}
                                </td>
                                <td className="px-3 py-2.5 leading-relaxed">
                                  {renderComparisonDetail(item)}
                                </td>
                                <td className="px-3 py-2.5">
                                  {isForceable ? (
                                    <div className="space-y-0.5">
                                      <label className="flex cursor-pointer items-center gap-1.5 font-medium text-foreground">
                                        <input
                                          type="checkbox"
                                          checked={decision === "force_adopt"}
                                          onChange={(e) => {
                                            const checked = e.target.checked;
                                            setMergeDecisions((prev) => {
                                              const next = { ...prev };
                                              if (checked) {
                                                next[item.change_key] =
                                                  "force_adopt";
                                              } else {
                                                delete next[item.change_key];
                                              }
                                              return next;
                                            });
                                          }}
                                          className="h-3.5 w-3.5 rounded border-border"
                                        />
                                        {t.forceAdoptLabel}
                                      </label>
                                      <p className="text-[11px] text-muted-foreground">
                                        {t.forceAdoptHint}
                                      </p>
                                    </div>
                                  ) : isConflict ? (
                                    <div className="flex flex-col gap-1">
                                      <label className="flex cursor-pointer items-center gap-1.5 font-medium text-foreground">
                                        <input
                                          type="radio"
                                          name={`conflict-${item.change_key}`}
                                          checked={
                                            decision === "adopt_incoming"
                                          }
                                          onChange={() =>
                                            setMergeDecisions((prev) => ({
                                              ...prev,
                                              [item.change_key]:
                                                "adopt_incoming",
                                            }))
                                          }
                                          className="h-3.5 w-3.5"
                                        />
                                        {t.adoptIncomingLabel}
                                      </label>
                                      <label className="flex cursor-pointer items-center gap-1.5 font-medium text-foreground">
                                        <input
                                          type="radio"
                                          name={`conflict-${item.change_key}`}
                                          checked={decision === "keep_current"}
                                          onChange={() =>
                                            setMergeDecisions((prev) => ({
                                              ...prev,
                                              [item.change_key]: "keep_current",
                                            }))
                                          }
                                          className="h-3.5 w-3.5"
                                        />
                                        {t.keepCurrentLabel}
                                      </label>
                                    </div>
                                  ) : (
                                    <span className="text-muted-foreground">
                                      默认自动写入
                                    </span>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {unresolvedConflictCount > 0 ? (
                    <p
                      role="alert"
                      className="text-xs font-medium text-destructive"
                    >
                      ⚠ {t.unresolvedConflictsHint}（剩余待裁决冲突：
                      {unresolvedConflictCount} 项）
                    </p>
                  ) : null}

                  <DialogFooter className="flex flex-wrap items-center justify-between gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => {
                        mergePreviewReqSeqRef.current += 1;
                        setMergeLoading(false);
                        setMergeModalOpen(false);
                      }}
                    >
                      {t.cancel}
                    </Button>
                    <Button
                      type="button"
                      disabled={
                        executingMerge ||
                        mergeLoading ||
                        unresolvedConflictCount > 0 ||
                        Boolean(
                          mergePreview?.job_id &&
                            mergePreview.job_id !==
                              (mergeTargetJob?.job_id || mergeTargetJob?.id),
                        )
                      }
                      onClick={handleExecuteMerge}
                    >
                      {executingMerge
                        ? "正在执行版本合并…"
                        : unresolvedConflictCount > 0
                          ? `${t.confirmExecuteMerge}（待裁决 ${unresolvedConflictCount} 项）`
                          : t.confirmExecuteMerge}
                    </Button>
                  </DialogFooter>
                </div>
              ) : !mergeError ? (
                <div className="rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground">
                  已修改基准日期，请点击「重新比对预览」加载最新差异后再确认合并。
                </div>
              ) : null}
            </div>
          </DialogContent>
        </Dialog>

        {/* Passwordless Roster Modal */}
        <Dialog open={rosterModalOpen} onOpenChange={setRosterModalOpen}>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle>分发清单（不含口令） — {rosterJobTitle}</DialogTitle>
              <DialogDescription>
                初始口令明文仅在开号成功当次返回，系统不落盘存储。此处重新生成的清单口令固定显示为「（见开号时的通知）」。
              </DialogDescription>
            </DialogHeader>
            {rosterData ? (
              <div className="space-y-4">
                <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground">
                  {rosterData.roster_text}
                </pre>
                <DialogFooter className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleDownloadRegenCsv}
                  >
                    下载无口令 CSV
                  </Button>
                  <Button
                    type="button"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(
                          rosterData.roster_text,
                        );
                        setRosterModalCopied(true);
                        setTimeout(() => setRosterModalCopied(false), 2000);
                      } catch {
                        setError("复制到剪贴板失败，请手动全选复制或点击下载 CSV");
                      }
                    }}
                  >
                    {rosterModalCopied ? "已复制文本" : "复制无口令通知文本"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => setRosterModalOpen(false)}
                  >
                    关闭
                  </Button>
                </DialogFooter>
              </div>
            ) : null}
          </DialogContent>
        </Dialog>

        {/* Upload / Preview / Provision 3-Step Wizard Dialog */}
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
          <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {wizardStep === 1
                  ? "步骤 1 / 3：上传申报书 (.docx) 解析课题组资产"
                  : wizardStep === 2
                    ? `步骤 2 / 3：预览与校对开号 — ${importJob?.original_filename || ""}`
                    : "步骤 3 / 3：开号完成 · 复制群发名单"}
              </DialogTitle>
              <DialogDescription>
                {wizardStep === 1
                  ? "上传项目申报书（仅支持 .docx，文件上限 20 MB）。身份证号与手机号将在内存中物理剥离，零入库。"
                  : wizardStep === 2
                    ? "核对基准时间、人员登录名与处理方式，确认无误后一键开通全组账号并同步入库。"
                    : "全组账号与课题组资料库已落盘，并已同步写入 RAG 知识库。"}
              </DialogDescription>
            </DialogHeader>

            {wizardError ? (
              <p role="alert" className="text-sm text-destructive">
                {wizardError}
              </p>
            ) : null}

            {wizardNotice ? (
              <p role="status" className="text-sm font-medium text-emerald-600">
                {wizardNotice}
              </p>
            ) : null}

            {wizardStep === 1 ? (
              <form onSubmit={handleParseUpload} className="grid gap-4">
                <div className="space-y-1.5">
                  <Label htmlFor="admin-proposal-docx">
                    申报书文件 (.docx，上限 20 MB)
                  </Label>
                  <Input
                    id="admin-proposal-docx"
                    type="file"
                    accept=".docx"
                    onChange={(e) => handleFileChange(e.target.files?.[0] || null)}
                    required
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="admin-upload-as-of">
                    本子基准日期（选填，留空则从封面自动提取）
                  </Label>
                  <Input
                    id="admin-upload-as-of"
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
              <div className="grid gap-4 text-xs">
                <div className="rounded-md border border-[var(--accent-warm)]/40 bg-[var(--accent-warm)]/10 px-3 py-2 text-foreground">
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

                {/* Default password warning bar (Spec §2.4 & §8.3.2) */}
                {initialPassword.trim() === DEFAULT_INITIAL_PASSWORD ? (
                  <div
                    role="alert"
                    className="rounded-md border border-destructive bg-destructive/10 px-3.5 py-2.5 text-xs font-semibold text-destructive"
                  >
                    ⚠ {t.defaultPasswordWarning}
                  </div>
                ) : null}

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <div className="space-y-1">
                    <Label htmlFor="imp-as-of">
                      本子基准日期 (as_of_time)
                      {importJob.preview.cover?.as_of_time
                        ? "（取自封面填表日期）"
                        : ""}
                    </Label>
                    <Input
                      id="imp-as-of"
                      type="date"
                      value={provisionAsOf}
                      disabled={wizardBusy}
                      onChange={(e) => setProvisionAsOf(e.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="imp-pw">全组统一初始口令（7天内有效）</Label>
                    <Input
                      id="imp-pw"
                      type="text"
                      value={initialPassword}
                      disabled={wizardBusy}
                      onChange={(e) => setInitialPassword(e.target.value)}
                      required
                    />
                  </div>
                  <div className="flex items-end pb-2">
                    <label className="flex cursor-pointer items-center gap-2 text-foreground">
                      <input
                        type="checkbox"
                        checked={includePassword}
                        disabled={wizardBusy}
                        onChange={(e) => setIncludePassword(e.target.checked)}
                        className="h-4 w-4 rounded border-border"
                      />
                      群发名单中包含初始口令明文
                    </label>
                  </div>
                </div>

                <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-muted/30 px-3 py-2">
                  <span>
                    人员汇总：共{" "}
                    <strong className="font-mono">{previewSummary.total}</strong> 人
                    ，新建{" "}
                    <strong className="font-mono">{previewSummary.newCount}</strong>
                    ，关联{" "}
                    <strong className="font-mono">
                      {previewSummary.existingCount}
                    </strong>
                    ，排除{" "}
                    <strong className="font-mono">
                      {previewSummary.excludeCount}
                    </strong>
                    ，待处理{" "}
                    <strong
                      className={
                        previewSummary.unresolvedCount > 0
                          ? "font-mono text-destructive"
                          : "font-mono"
                      }
                    >
                      {previewSummary.unresolvedCount}
                    </strong>
                  </span>
                  <div className="flex flex-wrap items-center gap-2">
                    <select
                      aria-label="解析模式"
                      value={reparseMode}
                      disabled={savingPreview || reparsing || provisioning}
                      onChange={(e) =>
                        setReparseMode(e.target.value as "fsm" | "llm")
                      }
                      className="h-7 rounded border border-input bg-background px-2 text-xs"
                    >
                      <option value="fsm">规则+正则状态机 (fsm)</option>
                      <option value="llm">规则+大模型抽取 (llm)</option>
                    </select>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={savingPreview || reparsing || provisioning}
                      onClick={handleReparse}
                    >
                      {reparsing ? "正在重新解析…" : "重新解析"}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={savingPreview || reparsing || provisioning}
                      onClick={handleSavePreview}
                    >
                      {savingPreview ? "保存中…" : "保存校对草稿"}
                    </Button>
                  </div>
                </div>

                <div className="overflow-x-auto rounded-md border border-border">
                  <table className="w-full text-left text-xs">
                    <thead className="border-b border-border bg-muted/40 text-muted-foreground">
                      <tr>
                        <th className="px-2.5 py-2">姓名</th>
                        <th className="px-2.5 py-2">单位 / 职称</th>
                        <th className="px-2.5 py-2">分级</th>
                        <th className="px-2.5 py-2">拟用登录名 (login_name)</th>
                        <th className="px-2.5 py-2">处理方式</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {(importJob.preview.people || []).map((p) => {
                        const ov = overrides[p.key] || {
                          mode:
                            p.match?.status === "existing" ? "existing" : "new",
                          login_name: p.login_name || "",
                          link_user_id: p.match?.user_id || "",
                        };
                        const loginValid =
                          ov.mode !== "new" || isValidLoginName(ov.login_name);
                        const isConflict =
                          ov.mode === "new" &&
                          p.match?.status === "conflict" &&
                          ov.login_name.trim().toLowerCase() ===
                            (p.login_name || "");
                        return (
                          <tr
                            key={p.key}
                            className={
                              !loginValid || isConflict
                                ? "bg-destructive/10"
                                : undefined
                            }
                          >
                            <td className="px-2.5 py-2 font-medium text-foreground">
                              {p.name}
                              {p.project_role === "applicant" ? (
                                <span className="ml-1 text-[11px] text-muted-foreground">
                                  (申请人)
                                </span>
                              ) : null}
                            </td>
                            <td className="px-2.5 py-2 text-muted-foreground">
                              {[p.affiliation, p.title]
                                .filter(Boolean)
                                .join(" · ") || "—"}
                            </td>
                            <td className="px-2.5 py-2 font-mono">{p.tier}</td>
                            <td className="px-2.5 py-2">
                              <Input
                                value={ov.login_name}
                                disabled={wizardBusy || ov.mode !== "new"}
                                onChange={(e) =>
                                  setOverrides((prev) => ({
                                    ...prev,
                                    [p.key]: {
                                      ...ov,
                                      login_name: e.target.value,
                                    },
                                  }))
                                }
                                className="h-7 font-mono text-xs"
                              />
                            </td>
                            <td className="px-2.5 py-2">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <select
                                  aria-label={`${p.name} 处理方式`}
                                  value={ov.mode}
                                  disabled={wizardBusy}
                                  onChange={(e) =>
                                    setOverrides((prev) => ({
                                      ...prev,
                                      [p.key]: {
                                        ...ov,
                                        mode: e.target.value as PersonMode,
                                      },
                                    }))
                                  }
                                  className="h-7 rounded border border-input bg-background px-2 text-xs"
                                >
                                  {p.match?.status !== "existing" ? (
                                    <option value="new">新建账号 (member)</option>
                                  ) : null}
                                  <option value="existing">关联已有用户</option>
                                  <option value="exclude">排除</option>
                                </select>
                                {ov.mode === "existing" && users.length > 0 ? (
                                  <select
                                    aria-label={`${p.name} 关联目标用户`}
                                    value={ov.link_user_id}
                                    disabled={wizardBusy}
                                    onChange={(e) =>
                                      setOverrides((prev) => ({
                                        ...prev,
                                        [p.key]: {
                                          ...ov,
                                          link_user_id: e.target.value,
                                        },
                                      }))
                                    }
                                    className="h-7 rounded border border-input bg-background px-2 text-xs"
                                  >
                                    <option value="">选择已有用户…</option>
                                    {users.map((u) => (
                                      <option key={u.id} value={u.id}>
                                        {u.display_name} (
                                        {u.login_name || u.email || u.role})
                                      </option>
                                    ))}
                                  </select>
                                ) : null}
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Collapsible Projects & Achievements Preview */}
                <div className="rounded-md border border-border p-3">
                  <button
                    type="button"
                    className="flex w-full items-center justify-between text-left text-xs font-medium text-foreground"
                    onClick={() => setShowAssetDetails((prev) => !prev)}
                  >
                    <span>
                      承担项目（{importJob.preview.projects?.length ?? 0} 项）·
                      前期成果（{importJob.preview.achievements?.length ?? 0} 项）·
                      设备条件（{importJob.preview.facilities?.length ?? 0} 项）
                    </span>
                    <span className="text-muted-foreground underline">
                      {showAssetDetails ? "收起预览" : "展开只读预览"}
                    </span>
                  </button>
                  {showAssetDetails ? (
                    <div className="mt-3 space-y-2 border-t border-border pt-2 text-xs text-muted-foreground">
                      {(importJob.preview.projects || []).map((proj) => (
                        <div key={proj.key}>
                          [项目] 《{proj.title}》 · {proj.grant_no || "无编号"} ·{" "}
                          {proj.pi_name || "—"} ({proj.status})
                        </div>
                      ))}
                      {(importJob.preview.achievements || []).map((ach) => (
                        <div key={ach.key}>
                          [成果] 《{ach.title}》 · {ach.first_author || "—"} ·{" "}
                          {ach.year || "—"}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>

                <DialogFooter>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={provisioning || savingPreview || reparsing}
                    onClick={() => setWizardOpen(false)}
                  >
                    稍后继续
                  </Button>
                  <Button
                    type="button"
                    disabled={
                      savingPreview ||
                      reparsing ||
                      provisioning ||
                      !provisionAsOf.trim() ||
                      previewSummary.unresolvedCount > 0
                    }
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
              <div className="grid gap-4 text-xs">
                {/* Spec §8.3.3 Yellow Warning Banner */}
                <div
                  role="alert"
                  className="rounded-md border border-amber-500/50 bg-amber-500/15 px-3.5 py-2.5 text-xs font-semibold text-amber-900 dark:text-amber-200"
                >
                  ⚠ {t.credentialOnceBanner}
                </div>

                <div className="rounded-md border border-border bg-muted/30 p-3">
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
                  人（跳过已存在 {provisionResult.stats.people_skipped} 人）、项目{" "}
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
                  <Label className="text-xs text-muted-foreground">
                    微信群/飞书群一键通知文本
                  </Label>
                  <pre className="mt-1.5 max-h-60 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground">
                    {provisionResult.roster_text}
                  </pre>
                </div>

                <DialogFooter className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleDownloadProvisionCsv}
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
                        retainedUnsavedCredentialState = null;
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
