"use client";

import Link from "next/link";

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
import { mapLabErrorCode } from "@/lib/api";
import {
  KNOWN_PUBLIC_DEFAULT_PASSWORD,
  canSubmitProvisioning,
  isInitialPasswordRequired,
} from "@/lib/security";
import { cn } from "cn";

import { ColdStartQualityPanel } from "./ColdStartQualityPanel";
import {
  FIELD_LABEL_MAP,
  formatProvValue,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function ColdStartWizardDialog({ ctx }: { ctx: LabPageContext }) {
  const {
    wizardOpen,
    setWizardOpen,
    wizardStep,
    setWizardStep,
    uploadItems,
    uploadAsOf,
    setUploadAsOf,
    parsing,
    wizardError,
    batchJobs,
    setActiveJobId,
    currentBatchJob,
    jobAsOfMap,
    jobOverridesMap,
    jobSkipSignaturesMap,
    linkSelfClusterId,
    setLinkSelfClusterId,
    qualitySummary,
    selfCandidate,
    needsNewAccounts,
    oidcMode,
    initialPassword,
    setInitialPassword,
    includePassword,
    setIncludePassword,
    provisioning,
    provisionResult,
    rosterCopied,
    t,
    previewSignatures,
    step4Jobs,
    step4Running,
    step4Done,
    step4Totals,
    tryCloseWizard,
    handleFilesSelected,
    handleRemoveUploadItem,
    handleUpdateUploadItemAsOf,
    handleParseUpload,
    updatePersonOverride,
    updateJobAsOf,
    toggleJobSignatureSkip,
    handleProvision,
    handleDownloadCsv,
    handleCopyRoster,
    handleStartStep4Merge,
    handleUpdateStep4Decision,
    handleConfirmStep4JobDecision,
  } = ctx;

  const activeOverrides = currentBatchJob
    ? jobOverridesMap[currentBatchJob.job_id] || {}
    : {};
  const activeSkipSigs = currentBatchJob
    ? jobSkipSignaturesMap[currentBatchJob.job_id] || {}
    : {};
  const activeAsOf = currentBatchJob
    ? jobAsOfMap[currentBatchJob.job_id] || ""
    : "";

  return (
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
              ? t.wizardStep1Title
              : wizardStep === 2
                ? t.wizardStep2Title
                : wizardStep === 3
                  ? t.wizardStep3Title
                  : t.wizardStep4Title}
          </DialogTitle>
          <DialogDescription>
            {wizardStep === 1
              ? t.wizardStep1Desc
              : wizardStep === 2
                ? t.wizardStep2Desc
                : wizardStep === 3
                  ? t.wizardStep3Desc
                  : t.wizardStep4Desc}
          </DialogDescription>
        </DialogHeader>

        {wizardError ? (
          <p role="alert" className="text-[13px] text-destructive">
            {wizardError}
          </p>
        ) : null}

        {/* ================================================================ */}
        {/* Step 1: Upload 2~3 Proposals (1..5 supported)                    */}
        {/* ================================================================ */}
        {wizardStep === 1 ? (
          <form onSubmit={handleParseUpload} className="grid gap-4">
            <div className="rounded-lg border border-[var(--accent-warm)]/40 bg-[var(--accent-warm)]/10 px-3.5 py-2.5 text-[12px] leading-relaxed text-foreground">
              {t.wizardStep1Guidance}
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <Label
                  htmlFor="proposal-docx"
                  className="text-[12px] font-medium text-muted-foreground"
                >
                  选择申报书文件（支持多选 1~5 份 .doc / .docx，单文件上限 20 MB）
                </Label>
                <Input
                  id="proposal-docx"
                  type="file"
                  multiple
                  disabled={parsing}
                  accept=".doc,.docx,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  onChange={(e) => {
                    handleFilesSelected(e.target.files);
                    e.target.value = "";
                  }}
                />
              </div>
              <div>
                <Label
                  htmlFor="upload-as-of"
                  className="text-[12px] font-medium text-muted-foreground"
                >
                  默认基准日期（选填，留空则从各本子封面自动提取）
                </Label>
                <Input
                  id="upload-as-of"
                  type="date"
                  disabled={parsing}
                  value={uploadAsOf}
                  onChange={(e) => setUploadAsOf(e.target.value)}
                />
              </div>
            </div>

            {uploadItems.length === 1 ? (
              <div
                role="status"
                className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-900 dark:text-amber-200"
              >
                {t.wizardStep1SingleFileTip}
              </div>
            ) : null}

            {uploadItems.length > 0 ? (
              <div className="space-y-2 rounded-md border border-border bg-muted/20 p-3">
                <div className="flex items-center justify-between text-[12px] text-muted-foreground">
                  <span>已选择 {uploadItems.length} / 5 份申报书</span>
                  <span>可为单份申报书单独指定基准日期 (as_of_time)</span>
                </div>
                <div className="divide-y divide-border rounded-md border border-border bg-card">
                  {uploadItems.map((item, idx) => (
                    <div
                      key={item.id}
                      className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-[12px]"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-muted-foreground">
                            #{idx + 1}
                          </span>
                          <span className="truncate font-medium text-foreground">
                            {item.file.name}
                          </span>
                          <span className="font-mono text-[11px] text-muted-foreground">
                            ({(item.file.size / (1024 * 1024)).toFixed(2)} MB)
                          </span>
                          <Badge
                            variant={
                              item.status === "parsed"
                                ? "secondary"
                                : item.status === "error"
                                  ? "outline"
                                  : "outline"
                            }
                            className={
                              item.status === "parsed"
                                ? "border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                                : item.status === "error"
                                  ? "border-destructive/40 bg-destructive/10 text-destructive"
                                  : item.status === "parsing"
                                    ? "border-blue-500/30 bg-blue-500/15 text-blue-700 dark:text-blue-300"
                                    : ""
                            }
                          >
                            {item.status === "waiting"
                              ? "待上传"
                              : item.status === "parsing"
                                ? "正在解析…"
                                : item.status === "parsed"
                                  ? "已解析"
                                  : "解析失败"}
                          </Badge>
                        </div>
                        {item.error ? (
                          <p className="mt-1 text-[11px] text-destructive">
                            {item.error}
                          </p>
                        ) : null}
                      </div>

                      <div className="flex items-center gap-2">
                        <Input
                          type="date"
                          aria-label={`${item.file.name} 基准日期`}
                          value={item.asOfTime}
                          disabled={parsing}
                          onChange={(e) =>
                            handleUpdateUploadItemAsOf(item.id, e.target.value)
                          }
                          className="h-7 w-36 font-mono text-[12px]"
                        />
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          disabled={parsing}
                          className="h-7 px-2 text-[12px] text-muted-foreground hover:text-destructive"
                          onClick={() => handleRemoveUploadItem(item.id)}
                        >
                          移除
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={parsing}
                onClick={tryCloseWizard}
              >
                {t.cancel}
              </Button>
              <Button
                type="submit"
                disabled={parsing || uploadItems.length === 0}
              >
                {parsing
                  ? `正在并发解析与跨本子对齐 (${uploadItems.filter((i) => i.status === "parsed").length}/${uploadItems.length})…`
                  : `上传并联合解析 (${uploadItems.length} 份)`}
              </Button>
            </DialogFooter>
          </form>
        ) : null}

        {/* ================================================================ */}
        {/* Step 2: Cross-Proposal Identity Quality & Account Preview        */}
        {/* ================================================================ */}
        {wizardStep === 2 && currentBatchJob ? (
          <div className="grid gap-4 text-[13px]">
            <ColdStartQualityPanel
              quality={qualitySummary}
              selfCandidate={selfCandidate}
              linkSelfClusterId={linkSelfClusterId}
              onToggleLinkSelf={setLinkSelfClusterId}
              t={t}
            />

            {/* Spec §2.4 & §8.3.2 Default password non-dismissible warning */}
            {initialPassword.trim() === KNOWN_PUBLIC_DEFAULT_PASSWORD ? (
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
                  当前本子基准日期 (as_of_time)
                </Label>
                <Input
                  id="prov-as-of"
                  type="date"
                  value={activeAsOf}
                  onChange={(e) =>
                    updateJobAsOf(currentBatchJob.job_id, e.target.value)
                  }
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
                  disabled={Boolean(oidcMode)}
                  placeholder={
                    oidcMode
                      ? "已启用 OIDC 统一身份认证，无需初始口令"
                      : "请设置自定义口令（至少12位，含3类字符）"
                  }
                  onChange={(e) => setInitialPassword(e.target.value)}
                  required={isInitialPasswordRequired(needsNewAccounts, oidcMode)}
                />
              </div>
              <div className="flex items-end pb-2">
                <label className="flex cursor-pointer items-center gap-2 text-[12px] text-foreground">
                  <input
                    type="checkbox"
                    checked={!oidcMode && includePassword}
                    disabled={Boolean(oidcMode)}
                    onChange={(e) => setIncludePassword(e.target.checked)}
                    className="h-4 w-4 rounded border-border"
                  />
                  群发名单中包含初始口令明文
                </label>
              </div>
            </div>

            {batchJobs.length > 1 ? (
              <div className="flex flex-wrap items-center gap-1.5 border-b border-border pb-2">
                {batchJobs.map((job, idx) => {
                  const isActive = job.job_id === currentBatchJob.job_id;
                  const jobAsOf = jobAsOfMap[job.job_id] || "未设日期";
                  return (
                    <Button
                      key={job.job_id}
                      type="button"
                      size="sm"
                      variant={isActive ? "default" : "outline"}
                      className="h-8 px-3 text-[12px]"
                      onClick={() => setActiveJobId(job.job_id)}
                    >
                      <span>
                        本子 #{idx + 1}：{job.original_filename}
                      </span>
                      <span className="ml-1.5 font-mono text-[11px] opacity-80">
                        ({jobAsOf})
                      </span>
                    </Button>
                  );
                })}
              </div>
            ) : null}

            <div className="rounded-md border border-[var(--accent-warm)]/40 bg-[var(--accent-warm)]/10 px-3 py-2 text-[12px] text-foreground">
              当前本子「{currentBatchJob.original_filename}」已物理剥离并丢弃{" "}
              <span className="font-mono font-semibold">
                {currentBatchJob.stats?.pii_dropped ?? 0}
              </span>{" "}
              处身份证号/手机号敏感信息（零入库）。识别出成员{" "}
              <span className="font-mono font-semibold">
                {currentBatchJob.preview.people?.length ?? 0}
              </span>{" "}
              人、承担项目{" "}
              <span className="font-mono font-semibold">
                {currentBatchJob.preview.projects?.length ?? 0}
              </span>{" "}
              项、前期成果{" "}
              <span className="font-mono font-semibold">
                {currentBatchJob.preview.achievements?.length ?? 0}
              </span>{" "}
              项、设备{" "}
              <span className="font-mono font-semibold">
                {currentBatchJob.preview.facilities?.length ?? 0}
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

            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full text-left text-[12px]">
                <thead className="border-b border-border bg-muted/40 text-muted-foreground">
                  <tr>
                    <th className="px-2.5 py-2">开通</th>
                    <th className="px-2.5 py-2">姓名</th>
                    <th className="px-2.5 py-2">挡位 / 跨本子补全</th>
                    <th className="px-2.5 py-2">登录名 (login_name)</th>
                    <th className="px-2.5 py-2">邮箱</th>
                    <th className="px-2.5 py-2">状态</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(currentBatchJob.preview.people || []).map((p) => {
                    const ov = activeOverrides[p.key] || {
                      exclude: false,
                      login_name: p.login_name,
                    };
                    const warnings = p.warnings || [];
                    const hasSiblingStudentId =
                      Boolean(p.injected_student_id) ||
                      warnings.includes("STUDENT_ID_FROM_SIBLING");
                    const hasSiblingEmail =
                      Boolean(p.injected_email) ||
                      warnings.includes("EMAIL_FROM_SIBLING");
                    const hasNameCollision = warnings.includes(
                      "BATCH_NAME_COLLISION",
                    );
                    const isSelfLinked = Boolean(
                      selfCandidate &&
                        linkSelfClusterId &&
                        selfCandidate.cluster_id === linkSelfClusterId &&
                        ((p.cluster_id &&
                          p.cluster_id === selfCandidate.cluster_id) ||
                          p.name === selfCandidate.name),
                    );
                    return (
                      <tr key={p.key}>
                        <td className="px-2.5 py-2">
                          <input
                            type="checkbox"
                            checked={!ov.exclude}
                            onChange={(e) =>
                              updatePersonOverride(currentBatchJob.job_id, p, {
                                exclude: !e.target.checked,
                              })
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
                          {isSelfLinked ? (
                            <Badge
                              variant="secondary"
                              className="ml-1.5 border-emerald-500/40 bg-emerald-500/15 text-[10px] text-emerald-800 dark:text-emerald-200"
                            >
                              本人 (admin)
                            </Badge>
                          ) : null}
                        </td>
                        <td className="px-2.5 py-2">
                          <div className="flex flex-wrap items-center gap-1">
                            <span className="font-mono font-semibold">
                              {p.tier}
                            </span>
                            {hasSiblingStudentId ? (
                              <Badge
                                variant="secondary"
                                title={mapLabErrorCode(
                                  "STUDENT_ID_FROM_SIBLING",
                                )}
                                className="border-blue-500/40 bg-blue-500/15 px-1.5 py-0 text-[10px] text-blue-800 dark:text-blue-200"
                              >
                                {t.siblingStudentIdBadge}
                              </Badge>
                            ) : null}
                            {hasSiblingEmail ? (
                              <Badge
                                variant="secondary"
                                title={mapLabErrorCode("EMAIL_FROM_SIBLING")}
                                className="border-purple-500/40 bg-purple-500/15 px-1.5 py-0 text-[10px] text-purple-800 dark:text-purple-200"
                              >
                                {t.siblingEmailBadge}
                              </Badge>
                            ) : null}
                            {hasNameCollision ? (
                              <Badge
                                variant="outline"
                                className="border-amber-500/50 bg-amber-500/15 px-1.5 py-0 text-[10px] text-amber-800 dark:text-amber-200"
                              >
                                同名碰撞
                              </Badge>
                            ) : null}
                          </div>
                        </td>
                        <td className="px-2.5 py-2">
                          <Input
                            value={ov.login_name}
                            disabled={ov.exclude}
                            onChange={(e) =>
                              updatePersonOverride(currentBatchJob.job_id, p, {
                                login_name: e.target.value,
                              })
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
                            {isSelfLinked || p.match?.status === "existing"
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
                          sig.person_temp_id || sig.sig_id || `sig-${idx + 1}`;
                        const sigStatus = sig.status || "bound";
                        const isSkipped = Boolean(activeSkipSigs[sigKey]);
                        return (
                          <tr key={sigKey}>
                            <td className="px-2.5 py-1.5">
                              <input
                                type="checkbox"
                                disabled={sigStatus !== "bound"}
                                checked={sigStatus === "bound" && !isSkipped}
                                onChange={(e) =>
                                  toggleJobSignatureSkip(
                                    currentBatchJob.job_id,
                                    sigKey,
                                    !e.target.checked,
                                  )
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
                disabled={provisioning}
                onClick={() => setWizardStep(1)}
              >
                返回上一步
              </Button>
              <Button
                type="button"
                disabled={
                  !canSubmitProvisioning({
                    busy: provisioning,
                    hasAsOfTime: Boolean(activeAsOf.trim()),
                    hasNewAccounts: needsNewAccounts,
                    oidcMode,
                    initialPassword,
                  })
                }
                onClick={handleProvision}
              >
                {provisioning
                  ? "正在批量开号并写入知识库…"
                  : "确认一键开号并入库"}
              </Button>
            </DialogFooter>
          </div>
        ) : null}

        {/* ================================================================ */}
        {/* Step 3: Batch Provision Complete & Combined Roster               */}
        {/* ================================================================ */}
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
              <span className="font-mono">{provisionResult.rag_doc_key}</span>。
            </div>

            <div>
              <Label className="text-[12px] text-muted-foreground">
                微信群/飞书群一键通知文本（跨本子去重汇总）
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
                variant={
                  (provisionResult.merge_queue?.length || 0) > 0
                    ? "outline"
                    : "default"
                }
                onClick={handleCopyRoster}
              >
                {rosterCopied ? "已复制群发文本" : "一键复制群发文本"}
              </Button>
              {(provisionResult.merge_queue?.length || 0) > 0 ? (
                <Button type="button" onClick={handleStartStep4Merge}>
                  {t.continueToStep4MergeBtn} (
                  {provisionResult.merge_queue.length} 份)
                </Button>
              ) : (
                <Button type="button" variant="ghost" onClick={tryCloseWizard}>
                  完成并关闭
                </Button>
              )}
            </DialogFooter>
          </div>
        ) : null}

        {/* ================================================================ */}
        {/* Step 4: Cross-Proposal Merge Progress & Conflict Resolution      */}
        {/* ================================================================ */}
        {wizardStep === 4 ? (
          <div className="grid gap-4 text-[13px]">
            <div className="space-y-2.5">
              <div className="flex items-center justify-between text-[12px] text-muted-foreground">
                <span>
                  {t.step4MergeProgressLabel}：已完成{" "}
                  {step4Jobs.filter((j) => j.status === "merged").length} /{" "}
                  {step4Jobs.length} 份申报书
                </span>
                <Link
                  href="/admin/imports"
                  className="underline hover:text-foreground"
                  onClick={(e) => {
                    if (!tryCloseWizard()) {
                      e.preventDefault();
                    }
                  }}
                >
                  前往导入管理查看详情
                </Link>
              </div>

              <div className="space-y-3">
                {step4Jobs.map((job, idx) => {
                  const conflictItems = (job.preview?.changes || []).filter(
                    (c) =>
                      c.requires_decision ||
                      c.category === "same_time_conflict",
                  );
                  return (
                    <div
                      key={job.jobId}
                      className="rounded-lg border border-border bg-card p-3.5 text-[12px]"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-muted-foreground">
                            #{idx + 1}
                          </span>
                          <span className="font-medium text-foreground">
                            {job.filename}
                          </span>
                          <span className="font-mono text-[11px] text-muted-foreground">
                            ({job.asOfTime || "—"})
                          </span>
                        </div>
                        <Badge
                          variant={
                            job.status === "merged" ? "secondary" : "outline"
                          }
                          className={
                            job.status === "merged"
                              ? "border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                              : job.status === "needs_decision"
                                ? "border-amber-500/50 bg-amber-500/15 text-amber-800 dark:text-amber-200"
                                : job.status === "error"
                                  ? "border-destructive/40 bg-destructive/10 text-destructive"
                                  : ""
                          }
                        >
                          {job.status === "pending"
                            ? "等待合并"
                            : job.status === "previewing"
                              ? "正在比对差异…"
                              : job.status === "needs_decision"
                                ? `待裁决同时间冲突 (${conflictItems.length})`
                                : job.status === "merging"
                                  ? "正在执行合并…"
                                  : job.status === "merged"
                                    ? "已完成互补合并"
                                    : "合并失败"}
                        </Badge>
                      </div>

                      {job.error ? (
                        <p className="mt-2 text-destructive">{job.error}</p>
                      ) : null}

                      {job.status === "needs_decision" &&
                      conflictItems.length > 0 ? (
                        <div className="mt-3 space-y-2.5 border-t border-border pt-3">
                          <p className="font-medium text-amber-900 dark:text-amber-200">
                            检测到以下字段与当前库具有相同基准日期 (as_of_time)，请选择保留方式：
                          </p>
                          <div className="overflow-x-auto rounded-md border border-border">
                            <table className="w-full text-left text-[12px]">
                              <thead className="border-b border-border bg-muted/40 text-muted-foreground">
                                <tr>
                                  <th className="px-2.5 py-1.5">实体与字段</th>
                                  <th className="px-2.5 py-1.5">当前库值</th>
                                  <th className="px-2.5 py-1.5">本子导入值</th>
                                  <th className="px-2.5 py-1.5">裁决选择</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border">
                                {conflictItems.map((item) => {
                                  const selected =
                                    job.decisions[item.change_key] ||
                                    "adopt_incoming";
                                  return (
                                    <tr key={item.change_key}>
                                      <td className="px-2.5 py-2 font-medium text-foreground">
                                        {item.entity_name} ·{" "}
                                        <span className="text-muted-foreground">
                                          {FIELD_LABEL_MAP[item.field] ||
                                            item.field}
                                        </span>
                                      </td>
                                      <td className="px-2.5 py-2 text-muted-foreground">
                                        {formatProvValue(item.current_value)}
                                      </td>
                                      <td className="px-2.5 py-2 text-foreground">
                                        {formatProvValue(item.incoming_value)}
                                      </td>
                                      <td className="px-2.5 py-2">
                                        <div className="flex flex-wrap items-center gap-3">
                                          <label className="flex cursor-pointer items-center gap-1">
                                            <input
                                              type="radio"
                                              name={`step4-${job.jobId}-${item.change_key}`}
                                              checked={
                                                selected === "adopt_incoming"
                                              }
                                              onChange={() =>
                                                handleUpdateStep4Decision(
                                                  job.jobId,
                                                  item.change_key,
                                                  "adopt_incoming",
                                                )
                                              }
                                            />
                                            <span>{t.adoptIncomingLabel}</span>
                                          </label>
                                          <label className="flex cursor-pointer items-center gap-1">
                                            <input
                                              type="radio"
                                              name={`step4-${job.jobId}-${item.change_key}`}
                                              checked={
                                                selected === "keep_current"
                                              }
                                              onChange={() =>
                                                handleUpdateStep4Decision(
                                                  job.jobId,
                                                  item.change_key,
                                                  "keep_current",
                                                )
                                              }
                                            />
                                            <span>{t.keepCurrentLabel}</span>
                                          </label>
                                        </div>
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                          <div className="flex justify-end">
                            <Button
                              type="button"
                              size="sm"
                              disabled={step4Running}
                              onClick={() =>
                                handleConfirmStep4JobDecision(job.jobId)
                              }
                            >
                              确认裁决并继续合并
                            </Button>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>

            {step4Done ? (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3.5 text-[12px] text-emerald-900 dark:text-emerald-200">
                <p className="font-semibold">{t.step4MergeDoneSummary}</p>
                <p className="mt-1">
                  跨本子累计新增条目{" "}
                  <span className="font-mono font-semibold">
                    {step4Totals.added}
                  </span>{" "}
                  项、按时间演进更新字段{" "}
                  <span className="font-mono font-semibold">
                    {step4Totals.updated}
                  </span>{" "}
                  处、自动补齐空白简历与字段{" "}
                  <span className="font-mono font-semibold">
                    {step4Totals.fillBlank}
                  </span>{" "}
                  处。
                </p>
              </div>
            ) : null}

            <DialogFooter className="flex flex-wrap gap-2">
              {provisionResult ? (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleDownloadCsv}
                  >
                    下载账号清单 CSV
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleCopyRoster}
                  >
                    {rosterCopied ? "已复制群发文本" : "复制群发文本"}
                  </Button>
                </>
              ) : null}
              <Link
                href="/admin/imports"
                className={cn(
                  buttonVariants({ variant: "outline", size: "default" }),
                )}
                onClick={(e) => {
                  if (!tryCloseWizard()) {
                    e.preventDefault();
                  }
                }}
              >
                {t.importsNav}
              </Link>
              <Button
                type="button"
                disabled={step4Running}
                onClick={tryCloseWizard}
              >
                完成并关闭
              </Button>
            </DialogFooter>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
