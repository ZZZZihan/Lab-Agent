"use client";


import {
  ApiError,
  copyTextToClipboard,
  deletePersonSignature,
  downloadSignaturesZip,
  exportSelectedLabAssets,
  formatLabBatch,
  getPersonSignatureUrl,
  mapLabErrorCode,
  parseFilenameFromDisposition,
  uploadPersonSignature,
  type BasketEntityKind,
  type BasketItem,
} from "@/lib/api";

import {
  BASKET_KIND_LABELS,
  MAX_SIGNATURE_BYTES,
  ALLOWED_SIGNATURE_MIMES,
  normalizeBasketEntityKind,
  extFromMime,
  basketKey,
  triggerBlobDownload,
} from "./labTypesAndHelpers";
import type { LabCoreAndFilterState } from "../page";

export function createDossierAndBasketActions(ctx: LabCoreAndFilterState) {
  const {
    people,
    projects,
    achievements,
    facilities,
    setError,
    setStatusBanner,
    dossierOpen,
    dossierPersonId,
    dossier,
    dossierCheckedProjects,
    setDossierCheckedProjects,
    dossierCheckedAchievements,
    setDossierCheckedAchievements,
    setSigUploading,
    setSigDeleting,
    setSigConfirmDelete,
    setSigError,
    setSigStatusMsg,
    basketItems,
    basketOptions,
    setBasketActionLoading,
    setBasketMissingIds,
    setClipboardFallbackText,
    t,
    dossierSessionRef,
    fetchDossierById,
    loadAll,
    addBatchToBasket,
  } = ctx;

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
    const startSessionSeq = dossierSessionRef.current;
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
      if (
        dossierOpen &&
        dossierSessionRef.current === startSessionSeq &&
        dossierPersonId === personId
      ) {
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

    const targetPersonId = dossier.person.id;
    const targetPersonName = dossier.person.name;
    const startSessionSeq = dossierSessionRef.current;
    const currentSha =
      dossier.signature?.signature_sha256 ??
      dossier.signature?.sha256 ??
      dossier.person.signature_sha256 ??
      null;

    setSigUploading(true);
    try {
      await uploadPersonSignature(targetPersonId, file, currentSha);
      setStatusBanner(`已更新 ${targetPersonName} 的电子签名`);
      const loadAllPromise = loadAll();
      if (dossierSessionRef.current === startSessionSeq) {
        setSigStatusMsg("电子签名已上传更新");
        await Promise.all([
          fetchDossierById(targetPersonId, { preserveSigFeedback: true }),
          loadAllPromise,
        ]);
      } else {
        await loadAllPromise;
      }
    } catch (err) {
      if (dossierSessionRef.current !== startSessionSeq) {
        return;
      }
      if (
        err instanceof ApiError &&
        (err.status === 412 ||
          err.code.toUpperCase() === "PRECONDITION_FAILED" ||
          err.code.toUpperCase() === "SIGNATURE_PRECONDITION_FAILED")
      ) {
        const conflictMsg = mapLabErrorCode("SIGNATURE_PRECONDITION_FAILED");
        setSigError(conflictMsg);
        await fetchDossierById(targetPersonId, { preserveSigFeedback: true });
        if (dossierSessionRef.current === startSessionSeq) {
          setSigError(conflictMsg);
        }
      } else {
        setSigError(
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : "上传电子签名失败，请稍后重试",
        );
      }
    } finally {
      if (dossierSessionRef.current === startSessionSeq) {
        setSigUploading(false);
      }
    }
  }

  async function handleConfirmDeleteSignature() {
    if (!dossier) return;
    setSigError("");
    setSigStatusMsg("");
    setSigDeleting(true);
    const targetPersonId = dossier.person.id;
    const targetPersonName = dossier.person.name;
    const startSessionSeq = dossierSessionRef.current;
    const currentSha =
      dossier.signature?.signature_sha256 ??
      dossier.signature?.sha256 ??
      dossier.person.signature_sha256 ??
      null;
    try {
      await deletePersonSignature(targetPersonId, currentSha);
      setStatusBanner(`已删除 ${targetPersonName} 的电子签名`);
      const loadAllPromise = loadAll();
      if (dossierSessionRef.current === startSessionSeq) {
        setSigConfirmDelete(false);
        setSigStatusMsg("电子签名已删除");
        await Promise.all([
          fetchDossierById(targetPersonId, { preserveSigFeedback: true }),
          loadAllPromise,
        ]);
      } else {
        await loadAllPromise;
      }
    } catch (err) {
      if (dossierSessionRef.current !== startSessionSeq) {
        return;
      }
      if (
        err instanceof ApiError &&
        (err.status === 412 ||
          err.code.toUpperCase() === "PRECONDITION_FAILED" ||
          err.code.toUpperCase() === "SIGNATURE_PRECONDITION_FAILED")
      ) {
        const conflictMsg = mapLabErrorCode("SIGNATURE_PRECONDITION_FAILED");
        setSigError(conflictMsg);
        await fetchDossierById(targetPersonId, { preserveSigFeedback: true });
        if (dossierSessionRef.current === startSessionSeq) {
          setSigError(conflictMsg);
        }
      } else {
        setSigError(
          err instanceof ApiError
            ? mapLabErrorCode(err.code, err.message)
            : "删除电子签名失败，请稍后重试",
        );
      }
    } finally {
      if (dossierSessionRef.current === startSessionSeq) {
        setSigDeleting(false);
      }
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
        await copyTextToClipboard(finalText);
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
              await copyTextToClipboard(retryText);
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


  return {
    handleDownloadPersonSignature,
    handleSignatureFileSelect,
    handleConfirmDeleteSignature,
    handleAddPersonOnlyFromDossier,
    handleAddCheckedDossierToBasket,
    buildLocalFallbackBatchText,
    handleBasketBatchCopy,
    handleBasketDownloadSignaturesZip,
    handleBasketExportSelectedDocx,
  };
}
