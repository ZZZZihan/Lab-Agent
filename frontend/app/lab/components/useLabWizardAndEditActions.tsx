"use client";


import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  api,
  reindexLabAssets,
  restoreImportedField,
   updateLabEntity,
  type FieldProvenanceSummary,
  type ImportJobResponse,
  type LabAchievementItem,
  type LabFacilityItem,
  type LabPersonItem,
  type LabProjectItem,
  type ProvisionResult,
} from "@/lib/api";

import {
  TabKey,
  EDITABLE_FIELDS,
  FIELD_LABEL_MAP,
  formatProvValue,
  PersonOverrideState,
  MAX_UPLOAD_BYTES,
  triggerBlobDownload,
} from "./labTypesAndHelpers";
import type { LabCoreAndFilterState } from "../page";

export function createWizardAndEditActions(ctx: LabCoreAndFilterState) {
  const {
    me,
    setError,
    setStatusBanner,
    expandedProvId,
    setExpandedProvId,
    setExportingFormat,
    setReindexing,
    editModalOpen,
    setEditModalOpen,
    editType,
    setEditType,
    editItem,
    setEditItem,
    editForm,
    setEditForm,
    editSaving,
    setEditSaving,
    setEditError,
    restoringFieldKey,
    setRestoringFieldKey,
    setWizardStep,
    uploadFile,
    setUploadFile,
    uploadAsOf,
    setParsing,
    setWizardError,
    importJob,
    setImportJob,
    provisionAsOf,
    setProvisionAsOf,
    initialPassword,
    includePassword,
    overrides,
    setOverrides,
    skipSignatureIds,
    setSkipSignatureIds,
    setProvisioning,
    provisionResult,
    setProvisionResult,
    setHasSavedCredentials,
    t,
    editSessionSeqRef,
    myAllowedNames,
    loadAll,
  } = ctx;

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


  return {
    handleExportAll,
    handleReindex,
    handleFileChange,
    handleParseUpload,
    handleProvision,
    handleDownloadCsv,
    canEditEntity,
    openEditModal,
    handleSaveEdit,
    handleRestoreImported,
    renderEntitySourceBadges,
    renderFieldProvenanceBadge,
    renderCardProvenanceSection,
  };
}
