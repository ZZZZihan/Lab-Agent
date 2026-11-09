"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { Shell } from "@/components/shell";
import {
  ApiError,
  api,
  copyTextToClipboard,
  currentUser,
  getPersonDossier,
  mapLabErrorCode,
  type BasketItem,
  type BasketOptions,
  type BasketState,
  type DossierAchievementItem,
  type DossierProjectItem,
  type LabAchievementItem,
  type LabFacilityItem,
  type LabOverview,
  type LabPersonItem,
  type LabProjectItem,
  type PersonDossierResponse,
  type User,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

import {
  TabKey,
  DEFAULT_BASKET_OPTIONS,
  basketKey,
  getBasketStorageKey,
  parseBasketStorage,
} from "./components/labTypesAndHelpers";
import { ColdStartWizardDialog } from "./components/ColdStartWizardDialog";
import { EntityEditDialog } from "./components/EntityEditDialog";
import { LabAssetTabLists } from "./components/LabAssetTabLists";
import { LabHeaderAndToolbar } from "./components/LabHeaderAndToolbar";
import { PersonDossierDialog } from "./components/PersonDossierDialog";
import { ReuseBasketBar } from "./components/ReuseBasketBar";
import { useColdStartBatchWizard } from "./components/useColdStartBatchWizard";
import { createDossierAndBasketActions } from "./components/useLabDossierAndBasketActions";
import { useLabFiltersAndSelection } from "./components/useLabFiltersAndSelection";
import { createWizardAndEditActions } from "./components/useLabWizardAndEditActions";

function useLabCoreState() {
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
  const dossierSessionRef = useRef(0);
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

  async function fetchDossierById(
    personId: string,
    options?: { preserveSigFeedback?: boolean },
  ) {
    if (dossierAbortRef.current) {
      dossierAbortRef.current.abort();
    }
    const controller = new AbortController();
    dossierAbortRef.current = controller;
    const reqSeq = ++dossierReqSeqRef.current;
    if (!options?.preserveSigFeedback) {
      dossierSessionRef.current += 1;
    }

    setDossierOpen(true);
    setDossierPersonId(personId);
    setDossier((prev) => (prev?.person.id === personId ? prev : null));
    setDossierLoading(true);
    setDossierError("");
    setDossierNotFound(false);
    if (!options?.preserveSigFeedback) {
      setSigUploading(false);
      setSigDeleting(false);
      setSigError("");
      setSigStatusMsg("");
      setSigConfirmDelete(false);
    }

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
    dossierSessionRef.current += 1;
    if (dossierAbortRef.current) {
      dossierAbortRef.current.abort();
      dossierAbortRef.current = null;
    }
    setDossierOpen(false);
    setDossierPersonId(null);
    setSigUploading(false);
    setSigDeleting(false);
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
      if (initialPerson && initialPerson.trim()) {
        const targetId = initialPerson.trim();
        void Promise.resolve().then(() => {
          if (!cancelled) {
            void fetchDossierById(targetId);
          }
        });
      }
    }
    return () => {
      cancelled = true;
      dossierReqSeqRef.current += 1;
      if (dossierAbortRef.current) {
        dossierAbortRef.current.abort();
        dossierAbortRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
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
    };
  }, [t.error]);

  const batchWizard = useColdStartBatchWizard({
    me,
    overview,
    t,
    loadAll,
  });

  async function copyToClipboard(text: string, tag: string) {
    setError("");
    try {
      await copyTextToClipboard(text);
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

  return {
    me, setMe, overview, setOverview,
    people, setPeople, projects, setProjects,
    achievements, setAchievements, facilities, setFacilities,
    loading, setLoading, error, setError,
    statusBanner, setStatusBanner, activeTab, setActiveTab,
    searchQuery, setSearchQuery, roleFilter, setRoleFilter,
    statusFilter, setStatusFilter, kindFilter, setKindFilter,
    autoNumber, setAutoNumber, onlyOngoingProjects, setOnlyOngoingProjects,
    onlyRecentProjects, setOnlyRecentProjects, includeProjectRel, setIncludeProjectRel,
    onlyRecentAchievements, setOnlyRecentAchievements, withSupportNote, setWithSupportNote,
    selectedIds, setSelectedIds, copiedId, setCopiedId,
    expandedBioId, setExpandedBioId, expandedProvId, setExpandedProvId,
    exportingFormat, setExportingFormat, reindexing, setReindexing,
    dossierOpen, setDossierOpen, dossierPersonId, setDossierPersonId,
    dossier, setDossier, dossierLoading, setDossierLoading,
    dossierError, setDossierError, dossierNotFound, setDossierNotFound,
    dossierCheckedProjects, setDossierCheckedProjects, dossierCheckedAchievements, setDossierCheckedAchievements,
    sigUploading, setSigUploading, sigDeleting, setSigDeleting,
    sigConfirmDelete, setSigConfirmDelete, sigError, setSigError,
    sigStatusMsg, setSigStatusMsg, basketItems, setBasketItems,
    basketOptions, setBasketOptions, basketHydratedForUser, setBasketHydratedForUser,
    basketPanelOpen, setBasketPanelOpen, basketConfirmClear, setBasketConfirmClear,
    basketActionLoading, setBasketActionLoading, basketMissingIds, setBasketMissingIds,
    clipboardFallbackText, setClipboardFallbackText, editModalOpen, setEditModalOpen,
    editType, setEditType, editItem, setEditItem,
    editForm, setEditForm, editSaving, setEditSaving,
    editError, setEditError, restoringFieldKey, setRestoringFieldKey,
    t, lastClickedIndexRef, dossierAbortRef, dossierReqSeqRef,
    dossierSessionRef, sigFileInputRef, editSessionSeqRef,
    basketKeySet, myAllowedNames, dossierHasSig, dossierSigSha,
    dossierSigMime, dossierSigSource, dossierSigUpdatedAt, dossierCanEditSig,
    dossierProjects, dossierAchievements, hydrateBasketFromStorage,
    persistBasketToStorage, fetchDossierById, closePersonDossier, loadAll,
    copyToClipboard,
    ...batchWizard,
  };
}

export type LabCoreState = ReturnType<typeof useLabCoreState>;

function useLabCoreAndFilters() {
  const core = useLabCoreState();
  const filters = useLabFiltersAndSelection(core);
  return { ...core, ...filters };
}

export type LabCoreAndFilterState = ReturnType<typeof useLabCoreAndFilters>;

function useLabAssetsController() {
  const base = useLabCoreAndFilters();
  const dossierAndBasket = createDossierAndBasketActions(base);
  const wizardAndEdit = createWizardAndEditActions(base);
  return { ...base, ...dossierAndBasket, ...wizardAndEdit };
}

export type LabPageContext = ReturnType<typeof useLabAssetsController>;

export default function LabAssetsPage() {
  const ctx = useLabAssetsController();
  return (
    <Shell>
      <div className="space-y-6 pb-36">
        <LabHeaderAndToolbar ctx={ctx} />
        <LabAssetTabLists ctx={ctx} />
        <PersonDossierDialog ctx={ctx} />
        <ReuseBasketBar ctx={ctx} />
        <EntityEditDialog ctx={ctx} />
        <ColdStartWizardDialog ctx={ctx} />
      </div>
    </Shell>
  );
}
