"use client";

import { Badge } from "@/components/ui/badge";
import type { BatchIdentityQuality, BatchSelfCandidate } from "@/lib/api";
import type { Copy } from "@/lib/i18n";

function formatCollisionItem(
  item: BatchIdentityQuality["name_collisions"][number],
): string {
  if (typeof item === "string") return item;
  if (item.login_names && item.login_names.length > 0) {
    return `${item.name} (${item.login_names.join(", ")})`;
  }
  return item.count ? `${item.name} (${item.count}人)` : item.name;
}

function formatSameAsOfItem(
  item: BatchIdentityQuality["same_as_of_jobs"][number],
): string {
  if (typeof item === "string") return item;
  if (Array.isArray(item)) {
    return `${item.length} 份本子基准日期相同`;
  }
  const datePart = item.as_of_time || "未填日期";
  if (item.filenames && item.filenames.length > 0) {
    return `${datePart}：${item.filenames.join("、")}`;
  }
  if (item.job_ids && item.job_ids.length > 0) {
    return `${datePart} (${item.job_ids.length} 份本子)`;
  }
  return datePart;
}

export function ColdStartQualityPanel({
  quality,
  selfCandidate,
  linkSelfClusterId,
  onToggleLinkSelf,
  t,
}: {
  quality: BatchIdentityQuality;
  selfCandidate?: BatchSelfCandidate | null;
  linkSelfClusterId: string | null;
  onToggleLinkSelf: (clusterId: string | null) => void;
  t: Copy;
}) {
  const peopleTotal = quality.people_total ?? 0;
  const t1Covered = quality.t1_covered ?? 0;
  const injectedStudentIds = quality.injected_student_ids ?? 0;
  const injectedEmails = quality.injected_emails ?? 0;
  const collisions = Array.isArray(quality.name_collisions)
    ? quality.name_collisions
    : [];
  const sameAsOf = Array.isArray(quality.same_as_of_jobs)
    ? quality.same_as_of_jobs
    : [];
  const coveragePct =
    peopleTotal > 0 ? Math.round((t1Covered / peopleTotal) * 100) : 0;

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-3.5 text-[12px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-medium text-foreground">{t.qualityPanelTitle}</h4>
        <Badge variant="secondary" className="font-mono text-[11px]">
          T1 {t1Covered} / {peopleTotal} ({coveragePct}%)
        </Badge>
      </div>

      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
        <div className="rounded-md border border-border bg-card p-2.5">
          <p className="text-[11px] text-muted-foreground">
            {t.qualityPeopleTotal}
          </p>
          <p className="mt-1 font-display text-lg font-semibold text-foreground">
            {peopleTotal}
          </p>
        </div>
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2.5">
          <p className="text-[11px] text-emerald-800 dark:text-emerald-300">
            {t.qualityT1Coverage}
          </p>
          <p className="mt-1 font-display text-lg font-semibold text-emerald-800 dark:text-emerald-200">
            {t1Covered} / {peopleTotal}
          </p>
        </div>
        <div className="rounded-md border border-blue-500/30 bg-blue-500/10 p-2.5">
          <p className="text-[11px] text-blue-800 dark:text-blue-300">
            {t.qualityInjectedStudentIds}
          </p>
          <p className="mt-1 font-display text-lg font-semibold text-blue-800 dark:text-blue-200">
            +{injectedStudentIds}
          </p>
        </div>
        <div className="rounded-md border border-purple-500/30 bg-purple-500/10 p-2.5">
          <p className="text-[11px] text-purple-800 dark:text-purple-300">
            {t.qualityInjectedEmails}
          </p>
          <p className="mt-1 font-display text-lg font-semibold text-purple-800 dark:text-purple-200">
            +{injectedEmails}
          </p>
        </div>
      </div>

      {selfCandidate ? (
        <div className="rounded-md border border-emerald-500/50 bg-emerald-500/10 px-3 py-2.5">
          <label className="flex cursor-pointer items-start gap-2.5 text-[12px] font-medium text-foreground">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 rounded border-border"
              checked={linkSelfClusterId === selfCandidate.cluster_id}
              onChange={(e) =>
                onToggleLinkSelf(
                  e.target.checked ? selfCandidate.cluster_id : null,
                )
              }
            />
            <span className="leading-relaxed">
              <Badge
                variant="secondary"
                className="mr-1.5 border-emerald-500/40 bg-emerald-500/20 text-[11px] text-emerald-900 dark:text-emerald-200"
              >
                {t.selfCandidateLabel}
              </Badge>
              检测到申报书中的「{selfCandidate.name}」与您的管理员姓名一致，是否直接关联为您本人的科研档案与电子签名？
            </span>
          </label>
        </div>
      ) : null}

      {collisions.length > 0 ? (
        <div
          role="alert"
          className="rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-900 dark:text-amber-200"
        >
          <span className="font-semibold">⚠ {t.qualityHomonymWarning}：</span>
          {collisions.map(formatCollisionItem).join("、")}
          。请核对下方表格中的登录名与学工号分配。
        </div>
      ) : null}

      {sameAsOf.length > 0 ? (
        <div
          role="status"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-900 dark:text-amber-200"
        >
          <span className="font-semibold">提示（{t.qualitySameAsOfWarning}）：</span>
          检测到多份申报书的基准日期相同（
          {sameAsOf.map(formatSameAsOfItem).join("；")}
          ）。若属于不同年份或不同批次的申报书，建议在各本子标签页中分别核对基准日期 (as_of_time)，以便按时间先后自动互补合并。
        </div>
      ) : null}
    </div>
  );
}
