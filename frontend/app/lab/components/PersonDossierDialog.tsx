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
import {
  getPersonSignatureUrl,
} from "@/lib/api";

import {
  extFromMime,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function PersonDossierDialog({ ctx }: { ctx: LabPageContext }) {
  const {
    people,
    copiedId,
    dossierOpen,
    dossierPersonId,
    dossier,
    dossierLoading,
    dossierError,
    dossierNotFound,
    dossierCheckedProjects,
    setDossierCheckedProjects,
    dossierCheckedAchievements,
    setDossierCheckedAchievements,
    sigUploading,
    sigDeleting,
    sigConfirmDelete,
    setSigConfirmDelete,
    sigError,
    sigStatusMsg,
    t,
    sigFileInputRef,
    dossierHasSig,
    dossierSigSha,
    dossierSigMime,
    dossierSigSource,
    dossierSigUpdatedAt,
    dossierCanEditSig,
    dossierProjects,
    dossierAchievements,
    fetchDossierById,
    closePersonDossier,
    copyToClipboard,
    handleDownloadPersonSignature,
    handleSignatureFileSelect,
    handleConfirmDeleteSignature,
    handleAddPersonOnlyFromDossier,
    handleAddCheckedDossierToBasket,
  } = ctx;

  return (
    <>
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
    </>
  );
}
