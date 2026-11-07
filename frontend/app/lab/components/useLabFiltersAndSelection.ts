"use client";

import { useMemo } from "react";

import {
  type BasketEntityKind,
  type BasketItem,
  type BasketOptions,
  type LabAchievementItem,
  type LabPersonItem,
  type LabProjectItem,
} from "@/lib/api";

import {
  TabKey,
  TAB_TO_BASKET_KIND,
  BASKET_MAX_ITEMS,
  normalizeNameKey,
  basketKey,
  extractProjectYear,
} from "./labTypesAndHelpers";
import type { LabCoreState } from "../page";

export function useLabFiltersAndSelection(core: LabCoreState) {
  const {
    overview,
    people,
    projects,
    achievements,
    facilities,
    setError,
    setStatusBanner,
    activeTab,
    searchQuery,
    roleFilter,
    statusFilter,
    kindFilter,
    autoNumber,
    onlyOngoingProjects,
    onlyRecentProjects,
    includeProjectRel,
    onlyRecentAchievements,
    withSupportNote,
    selectedIds,
    setSelectedIds,
    basketItems,
    setBasketItems,
    basketOptions,
    setBasketOptions,
    lastClickedIndexRef,
    basketKeySet,
    persistBasketToStorage,
    copyToClipboard,
  } = core;

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


  return {
    basketCounts,
    personByNormalizedName,
    baseYear,
    filteredPeople,
    filteredProjects,
    filteredAchievements,
    filteredFacilities,
    currentTabBasketCandidates,
    currentTabListLength,
    selectedCount,
    isItemSelected,
    updateBasketItems,
    updateBasketOption,
    addBatchToBasket,
    removeItemsFromBasket,
    moveBasketItem,
    handleRowCheckboxClick,
    getProjectText,
    getAchievementText,
    formatBatchLines,
    collectTabLines,
    handleBatchCopySelected,
    handleBatchCopyAllTab,
    toggleSelectAllCurrentList,
    handleQuickSelectOngoingProjects,
    handleQuickSelectRecentAchievements,
  };
}
