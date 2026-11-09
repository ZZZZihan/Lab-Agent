"use client";

import Link from "next/link";

import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "cn";

import {
  basketKey,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function LabHeaderAndToolbar({ ctx }: { ctx: LabPageContext }) {
  const {
    me,
    overview,
    people,
    projects,
    achievements,
    facilities,
    error,
    setError,
    statusBanner,
    setStatusBanner,
    activeTab,
    setActiveTab,
    searchQuery,
    setSearchQuery,
    roleFilter,
    setRoleFilter,
    statusFilter,
    setStatusFilter,
    kindFilter,
    setKindFilter,
    autoNumber,
    setAutoNumber,
    onlyOngoingProjects,
    setOnlyOngoingProjects,
    onlyRecentProjects,
    setOnlyRecentProjects,
    includeProjectRel,
    setIncludeProjectRel,
    onlyRecentAchievements,
    setOnlyRecentAchievements,
    withSupportNote,
    setWithSupportNote,
    copiedId,
    exportingFormat,
    reindexing,
    basketMissingIds,
    setBasketMissingIds,
    openColdStartWizard,
    t,
    currentTabBasketCandidates,
    currentTabListLength,
    selectedCount,
    removeItemsFromBasket,
    handleBatchCopySelected,
    handleBatchCopyAllTab,
    toggleSelectAllCurrentList,
    handleQuickSelectOngoingProjects,
    handleQuickSelectRecentAchievements,
    handleExportAll,
    handleReindex,
  } = ctx;

  return (
    <>
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
                <Button size="sm" onClick={openColdStartWizard}>
                  {t.importProposalBtn}
                </Button>
              </>
            ) : null}
          </div>
        </div>

        {me?.role === "admin" && overview?.counts.people === 0 ? (
          <div className="flex flex-col justify-between gap-4 rounded-xl border border-[var(--accent-warm)]/50 bg-[var(--accent-warm)]/10 p-4 sm:flex-row sm:items-center">
            <div className="space-y-1">
              <h2 className="font-display text-[15px] font-semibold text-foreground">
                {t.coldStartCardTitle}
              </h2>
              <p className="text-[13px] leading-relaxed text-muted-foreground">
                {t.coldStartCardBody}
              </p>
            </div>
            <div className="shrink-0">
              <Button size="sm" onClick={openColdStartWizard}>
                {t.coldStartCardCta}
              </Button>
            </div>
          </div>
        ) : null}

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

    </>
  );
}
