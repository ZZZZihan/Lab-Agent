"use client";


import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import {
  mapLabErrorCode,
} from "@/lib/api";

import {
  DEFAULT_INITIAL_PASSWORD,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function ColdStartWizardDialog({ ctx }: { ctx: LabPageContext }) {
  const {
    wizardOpen,
    setWizardOpen,
    wizardStep,
    setWizardStep,
    uploadFile,
    uploadAsOf,
    setUploadAsOf,
    parsing,
    wizardError,
    setWizardError,
    importJob,
    provisionAsOf,
    setProvisionAsOf,
    initialPassword,
    setInitialPassword,
    includePassword,
    setIncludePassword,
    overrides,
    setOverrides,
    skipSignatureIds,
    setSkipSignatureIds,
    provisioning,
    provisionResult,
    rosterCopied,
    setRosterCopied,
    setHasSavedCredentials,
    t,
    previewSignatures,
    tryCloseWizard,
    handleFileChange,
    handleParseUpload,
    handleProvision,
    handleDownloadCsv,
  } = ctx;

  return (
    <>
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
    </>
  );
}
