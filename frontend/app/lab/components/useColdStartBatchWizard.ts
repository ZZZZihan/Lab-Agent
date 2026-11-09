"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  api,
  copyTextToClipboard,
  executeImportMerge,
  mapLabErrorCode,
  previewImportMerge,
  provisionImportBatch,
  resolveImportBatch,
  type BatchIdentityCluster,
  type BatchIdentityQuality,
  type BatchProvisionResponse,
  type BatchResolveJobItem,
  type BatchResolveResponse,
  type BatchSelfCandidate,
  type ImportJobResponse,
  type ImportPreviewPerson,
  type ImportPreviewSignatureItem,
  type LabOverview,
  type MergeDecision,
  type MergeExecuteResponse,
  type MergePreviewResponse,
  type User,
} from "@/lib/api";
import type { Copy } from "@/lib/i18n";

import {
  DEFAULT_INITIAL_PASSWORD,
  MAX_UPLOAD_BYTES,
  type PersonOverrideState,
  triggerBlobDownload,
} from "./labTypesAndHelpers";

export const MAX_BATCH_FILES = 5;

export type BatchUploadItem = {
  id: string;
  file: File;
  asOfTime: string;
  status: "waiting" | "parsing" | "parsed" | "error";
  jobId?: string;
  jobResponse?: ImportJobResponse;
  error?: string;
};

export type Step4JobMergeState = {
  jobId: string;
  filename: string;
  asOfTime: string;
  status:
    | "pending"
    | "previewing"
    | "needs_decision"
    | "merging"
    | "merged"
    | "error";
  preview?: MergePreviewResponse;
  decisions: Record<string, "adopt_incoming" | "keep_current">;
  result?: MergeExecuteResponse;
  error?: string;
};

function normalizeSignaturesForJob(
  job?: BatchResolveJobItem | ImportJobResponse | null,
): ImportPreviewSignatureItem[] {
  const rawSigs = job?.preview?.signatures;
  if (!rawSigs) return [];
  const list = Array.isArray(rawSigs) ? rawSigs : rawSigs.items || [];
  const jobWarnings = job?.preview?.warnings || [];
  return list.map((sig, idx) => {
    const sigKey =
      sig.person_temp_id ||
      sig.person_key ||
      sig.sig_id ||
      sig.person_ref ||
      sig.person_name ||
      `sig-${idx + 1}`;
    const status = sig.status || "bound";
    const matchedWarnCodes = jobWarnings
      .filter(
        (w) =>
          (w.person_temp_id && w.person_temp_id === sigKey) ||
          (Array.isArray(w.person_temp_ids) &&
            w.person_temp_ids.includes(sigKey)) ||
          (sig.person_name &&
            ((w.person_name && w.person_name === sig.person_name) ||
              (Array.isArray(w.person_names) &&
                w.person_names.includes(sig.person_name)))),
      )
      .map((w) => w.code);
    const combinedWarnings = Array.from(
      new Set([...(sig.warnings || []), ...matchedWarnCodes]),
    );
    return {
      ...sig,
      sig_id: sigKey,
      person_temp_id: sig.person_temp_id || sigKey,
      status,
      warnings: combinedWarnings,
    };
  });
}

function mergeParsedAndResolvedJobs(
  parsedJobs: ImportJobResponse[],
  resolved: BatchResolveResponse,
): BatchResolveJobItem[] {
  const parsedById = new Map<string, ImportJobResponse>();
  for (const pj of parsedJobs) {
    parsedById.set(pj.job_id || pj.id, pj);
  }
  const clusters: BatchIdentityCluster[] =
    resolved.clusters || resolved.identity_clusters || [];

  const rawJobs: BatchResolveJobItem[] =
    Array.isArray(resolved.jobs) && resolved.jobs.length > 0
      ? resolved.jobs
      : parsedJobs.map((pj) => ({
          id: pj.id,
          job_id: pj.job_id || pj.id,
          original_filename: pj.original_filename,
          as_of_time: pj.as_of_time || pj.preview?.as_of_time || null,
          status: pj.status,
          stats: pj.stats,
          preview: pj.preview,
        }));

  return rawJobs.map((rj) => {
    const jid = rj.job_id || rj.id || "";
    const fallback = parsedById.get(jid);
    const mergedPreview: ImportJobResponse["preview"] = {
      ...(fallback?.preview || {}),
      ...(rj.preview || {}),
      people: rj.preview?.people || fallback?.preview?.people || [],
      projects: rj.preview?.projects || fallback?.preview?.projects || [],
      achievements:
        rj.preview?.achievements || fallback?.preview?.achievements || [],
      facilities: rj.preview?.facilities || fallback?.preview?.facilities || [],
      signatures: rj.preview?.signatures || fallback?.preview?.signatures,
      warnings: rj.preview?.warnings || fallback?.preview?.warnings || [],
    };

    const enrichedPeople: ImportPreviewPerson[] = (
      mergedPreview.people || []
    ).map((p) => {
      const matchedCluster = clusters.find(
        (c) =>
          (p.cluster_id && c.cluster_id === p.cluster_id) ||
          (c.members || c.occurrences)?.some(
            (occ) =>
              occ.job_id === jid &&
              (occ.key === p.key ||
                ("person_key" in occ && occ.person_key === p.key)),
          ),
      );
      const warnSet = new Set<string>(p.warnings || []);
      for (const w of matchedCluster?.warnings || []) {
        warnSet.add(w);
      }
      const injectedStudentId = Boolean(
        p.injected_student_id ||
          matchedCluster?.injected_student_id ||
          warnSet.has("STUDENT_ID_FROM_SIBLING"),
      );
      const injectedEmail = Boolean(
        p.injected_email ||
          matchedCluster?.injected_email ||
          warnSet.has("EMAIL_FROM_SIBLING"),
      );
      if (injectedStudentId) {
        warnSet.add("STUDENT_ID_FROM_SIBLING");
      }
      if (injectedEmail) {
        warnSet.add("EMAIL_FROM_SIBLING");
      }
      return {
        ...p,
        cluster_id: p.cluster_id || matchedCluster?.cluster_id,
        tier: p.tier || matchedCluster?.tier || "T2",
        login_name: p.login_name || matchedCluster?.login_name || "",
        email: p.email ?? matchedCluster?.email ?? null,
        injected_student_id: injectedStudentId,
        injected_email: injectedEmail,
        warnings: Array.from(warnSet),
      };
    });

    return {
      id: rj.id || fallback?.id || jid,
      job_id: jid,
      original_filename:
        rj.original_filename || fallback?.original_filename || jid,
      as_of_time:
        rj.as_of_time ||
        rj.preview?.as_of_time ||
        fallback?.as_of_time ||
        fallback?.preview?.as_of_time ||
        null,
      status: rj.status || fallback?.status || "parsed",
      role_in_batch: rj.role_in_batch,
      stats: rj.stats || fallback?.stats || {},
      preview: {
        ...mergedPreview,
        people: enrichedPeople,
      },
    };
  });
}

export function useColdStartBatchWizard({
  me,
  overview,
  t,
  loadAll,
}: {
  me: User | null;
  overview: LabOverview | null;
  t: Copy;
  loadAll: () => Promise<void>;
}) {
  const router = useRouter();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardStep, setWizardStep] = useState<1 | 2 | 3 | 4>(1);
  const [uploadItems, setUploadItems] = useState<BatchUploadItem[]>([]);
  const [uploadAsOf, setUploadAsOf] = useState("");
  const [parsing, setParsing] = useState(false);
  const [wizardError, setWizardError] = useState("");

  const [batchResolve, setBatchResolve] =
    useState<BatchResolveResponse | null>(null);
  const [batchJobs, setBatchJobs] = useState<BatchResolveJobItem[]>([]);
  const [activeJobId, setActiveJobId] = useState<string>("");
  const [jobAsOfMap, setJobAsOfMap] = useState<Record<string, string>>({});
  const [jobOverridesMap, setJobOverridesMap] = useState<
    Record<string, Record<string, PersonOverrideState>>
  >({});
  const [jobSkipSignaturesMap, setJobSkipSignaturesMap] = useState<
    Record<string, Record<string, boolean>>
  >({});
  const [linkSelfClusterId, setLinkSelfClusterId] = useState<string | null>(
    null,
  );

  const [initialPassword, setInitialPassword] = useState(
    DEFAULT_INITIAL_PASSWORD,
  );
  const [includePassword, setIncludePassword] = useState(true);
  const [provisioning, setProvisioning] = useState(false);
  const [provisionResult, setProvisionResult] =
    useState<BatchProvisionResponse | null>(null);
  const [rosterCopied, setRosterCopied] = useState(false);
  const [hasSavedCredentials, setHasSavedCredentials] = useState(false);

  const [step4Jobs, setStep4Jobs] = useState<Step4JobMergeState[]>([]);
  const [step4Running, setStep4Running] = useState(false);
  const [step4Done, setStep4Done] = useState(false);

  const parsingRef = useRef(false);
  const provisioningRef = useRef(false);
  const step4RunningRef = useRef(false);
  const autoPromptCheckedForUserRef = useRef<string | null>(null);

  function resetWizardState() {
    setWizardError("");
    setWizardStep(1);
    setUploadItems([]);
    setUploadAsOf("");
    setBatchResolve(null);
    setBatchJobs([]);
    setActiveJobId("");
    setJobAsOfMap({});
    setJobOverridesMap({});
    setJobSkipSignaturesMap({});
    setLinkSelfClusterId(null);
    setProvisionResult(null);
    setRosterCopied(false);
    setHasSavedCredentials(false);
    setStep4Jobs([]);
    setStep4Running(false);
    setStep4Done(false);
  }

  function openColdStartWizard() {
    resetWizardState();
    setWizardOpen(true);
  }

  // Auto-open wizard on ?coldstart=1 or empty lab for admin
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!me || me.role !== "admin") return;

    let cancelled = false;
    const searchParams = new URLSearchParams(window.location.search);
    if (searchParams.get("coldstart") === "1") {
      autoPromptCheckedForUserRef.current = me.id;
      router.replace("/lab");
      void Promise.resolve().then(() => {
        if (!cancelled) {
          setWizardError("");
          setWizardStep(1);
          setWizardOpen(true);
        }
      });
      return () => {
        cancelled = true;
      };
    }

    if (!overview) return;
    if (autoPromptCheckedForUserRef.current === me.id) return;
    autoPromptCheckedForUserRef.current = me.id;

    let dismissed = false;
    try {
      dismissed =
        window.localStorage.getItem(`coldstart-dismissed:${me.id}`) === "1";
    } catch {
      dismissed = false;
    }
    if (
      overview.counts.people === 0 &&
      !overview.latest_import &&
      !dismissed
    ) {
      void Promise.resolve().then(() => {
        if (!cancelled) {
          setWizardError("");
          setWizardStep(1);
          setWizardOpen(true);
        }
      });
    }
    return () => {
      cancelled = true;
    };
  }, [me, overview, router]);

  // Spec §8.3.3 beforeunload guard when Step 3 or Step 4 has plaintext credentials in memory
  const hasUnsavedPlaintextRoster = Boolean(
    wizardOpen &&
      (wizardStep === 3 || wizardStep === 4) &&
      provisionResult &&
      includePassword &&
      !hasSavedCredentials,
  );

  useEffect(() => {
    if (!hasUnsavedPlaintextRoster) return;
    function handleBeforeUnload(e: BeforeUnloadEvent) {
      e.preventDefault();
      e.returnValue = t.credentialLeaveConfirm;
      return t.credentialLeaveConfirm;
    }
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
    };
  }, [hasUnsavedPlaintextRoster, t.credentialLeaveConfirm]);

  function markColdStartDismissed() {
    if (typeof window === "undefined" || !me?.id) return;
    try {
      window.localStorage.setItem(`coldstart-dismissed:${me.id}`, "1");
    } catch {
      // Ignore storage errors
    }
  }

  function tryCloseWizard(): boolean {
    if (parsing || provisioning || step4Running) return false;
    if (
      (wizardStep === 3 || wizardStep === 4) &&
      provisionResult &&
      includePassword &&
      !hasSavedCredentials
    ) {
      const confirmed = window.confirm(t.credentialLeaveConfirm);
      if (!confirmed) return false;
    }
    markColdStartDismissed();
    setWizardOpen(false);
    setProvisionResult(null);
    return true;
  }

  function handleFilesSelected(fileList: FileList | File[] | null) {
    setWizardError("");
    if (!fileList) return;
    const incoming = Array.from(fileList);
    if (incoming.length === 0) return;

    for (const file of incoming) {
      const lower = file.name.toLowerCase();
      if (
        (!lower.endsWith(".docx") && !lower.endsWith(".doc")) ||
        lower.endsWith(".docm")
      ) {
        setWizardError(
          `文件「${file.name}」格式不支持：仅支持 .doc 或 .docx 格式的申报书文件（不支持含宏的 .docm）`,
        );
        return;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        setWizardError(`文件「${file.name}」大小超过 20 MB 上限`);
        return;
      }
    }

    setUploadItems((prev) => {
      const combined = [...prev];
      for (const file of incoming) {
        const dup = combined.some(
          (it) =>
            it.file.name === file.name &&
            it.file.size === file.size &&
            it.file.lastModified === file.lastModified,
        );
        if (!dup) {
          combined.push({
            id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 7)}`,
            file,
            asOfTime: uploadAsOf || "",
            status: "waiting",
          });
        }
      }
      if (combined.length > MAX_BATCH_FILES) {
        setWizardError(mapLabErrorCode("batch_size_invalid"));
        return combined.slice(0, MAX_BATCH_FILES);
      }
      return combined;
    });
  }

  function handleFileChange(file: File | null) {
    if (!file) {
      setUploadItems([]);
      return;
    }
    handleFilesSelected([file]);
  }

  function handleRemoveUploadItem(id: string) {
    if (parsing) return;
    setWizardError("");
    setUploadItems((prev) => prev.filter((it) => it.id !== id));
  }

  function handleUpdateUploadItemAsOf(id: string, asOfTime: string) {
    if (parsing) return;
    setUploadItems((prev) =>
      prev.map((it) => {
        if (it.id !== id) return it;
        const changed = it.asOfTime.trim() !== asOfTime.trim();
        return {
          ...it,
          asOfTime,
          status:
            it.status === "error" || (it.status === "parsed" && changed)
              ? "waiting"
              : it.status,
          jobId: changed ? undefined : it.jobId,
          jobResponse: changed ? undefined : it.jobResponse,
          error: undefined,
        };
      }),
    );
  }

  async function handleParseUpload(e: React.FormEvent) {
    e.preventDefault();
    if (parsingRef.current || parsing) return;
    if (uploadItems.length === 0) {
      setWizardError("请先选择 1~5 份 .doc 或 .docx 申报书文件");
      return;
    }
    if (uploadItems.length > MAX_BATCH_FILES) {
      setWizardError(mapLabErrorCode("batch_size_invalid"));
      return;
    }

    parsingRef.current = true;
    setWizardError("");
    setParsing(true);

    const itemsSnapshot = uploadItems.map((it) => ({
      ...it,
      asOfTime: it.asOfTime.trim() || uploadAsOf.trim(),
    }));
    const results: Array<{
      index: number;
      job?: ImportJobResponse;
      err?: string;
    }> = new Array(itemsSnapshot.length);

    let nextIdx = 0;
    const concurrency = Math.min(2, itemsSnapshot.length);

    async function worker() {
      while (nextIdx < itemsSnapshot.length) {
        const idx = nextIdx++;
        const item = itemsSnapshot[idx];
        const cachedAsOf = (
          item.jobResponse?.as_of_time ||
          item.jobResponse?.preview?.as_of_time ||
          ""
        ).trim();
        if (
          item.status === "parsed" &&
          item.jobResponse &&
          item.jobId &&
          (!item.asOfTime || item.asOfTime === cachedAsOf)
        ) {
          results[idx] = {
            index: idx,
            job: item.asOfTime
              ? {
                  ...item.jobResponse,
                  as_of_time: item.asOfTime,
                  preview: item.jobResponse.preview
                    ? { ...item.jobResponse.preview, as_of_time: item.asOfTime }
                    : item.jobResponse.preview,
                }
              : item.jobResponse,
          };
          continue;
        }
        setUploadItems((prev) =>
          prev.map((it) =>
            it.id === item.id
              ? { ...it, status: "parsing", error: undefined }
              : it,
          ),
        );
        try {
          const form = new FormData();
          form.append("file", item.file);
          if (item.asOfTime.trim()) {
            form.append("as_of_time", item.asOfTime.trim());
          }
          const parsed = await api<ImportJobResponse>("admin/imports/proposal", {
            method: "POST",
            body: form,
          });
          const jid = parsed.job_id || parsed.id;
          results[idx] = { index: idx, job: parsed };
          setUploadItems((prev) =>
            prev.map((it) =>
              it.id === item.id
                ? {
                    ...it,
                    status: "parsed",
                    jobId: jid,
                    jobResponse: parsed,
                    asOfTime:
                      it.asOfTime.trim() ||
                      parsed.as_of_time ||
                      parsed.preview?.as_of_time ||
                      "",
                  }
                : it,
            ),
          );
        } catch (err) {
          const msg =
            err instanceof ApiError
              ? mapLabErrorCode(err.code, err.message)
              : t.error;
          results[idx] = { index: idx, err: msg };
          setUploadItems((prev) =>
            prev.map((it) =>
              it.id === item.id ? { ...it, status: "error", error: msg } : it,
            ),
          );
        }
      }
    }

    try {
      await Promise.all(Array.from({ length: concurrency }, () => worker()));
      const failed = results.find((r) => !r || r.err || !r.job);
      if (failed) {
        setWizardError(failed.err || t.error);
        return;
      }

      const parsedJobs = results.map((r) => r.job!);
      const jobIds = parsedJobs.map((j) => j.job_id || j.id);
      const resolved = await resolveImportBatch(jobIds);
      const normalizedJobs = mergeParsedAndResolvedJobs(parsedJobs, resolved);

      const nextAsOfMap: Record<string, string> = {};
      const nextOverridesMap: Record<
        string,
        Record<string, PersonOverrideState>
      > = {};
      const nextSkipSigsMap: Record<string, Record<string, boolean>> = {};

      const editedAsOfByJobId = new Map<string, string>();
      for (let i = 0; i < itemsSnapshot.length; i++) {
        const snap = itemsSnapshot[i];
        const rJob = results[i]?.job;
        const jid = rJob?.job_id || rJob?.id || snap.jobId;
        if (jid && snap.asOfTime) {
          editedAsOfByJobId.set(jid, snap.asOfTime);
        }
      }

      for (const job of normalizedJobs) {
        const jid = job.job_id;
        nextAsOfMap[jid] =
          editedAsOfByJobId.get(jid) ||
          job.as_of_time ||
          job.preview?.as_of_time ||
          uploadAsOf.trim() ||
          "";
        const ovForJob: Record<string, PersonOverrideState> = {};
        for (const p of job.preview?.people || []) {
          ovForJob[p.key] = {
            exclude: Boolean(p.exclude),
            login_name: p.login_name || "",
          };
        }
        nextOverridesMap[jid] = ovForJob;
        nextSkipSigsMap[jid] = {};
      }

      setBatchResolve(resolved);
      setBatchJobs(normalizedJobs);
      setActiveJobId(normalizedJobs[0]?.job_id || "");
      setJobAsOfMap(nextAsOfMap);
      setJobOverridesMap(nextOverridesMap);
      setJobSkipSignaturesMap(nextSkipSigsMap);
      setLinkSelfClusterId(resolved.self_candidate?.cluster_id ?? null);
      setWizardStep(2);
    } catch (err) {
      setWizardError(
        err instanceof ApiError
          ? mapLabErrorCode(err.code, err.message)
          : t.error,
      );
    } finally {
      parsingRef.current = false;
      setParsing(false);
    }
  }

  const currentBatchJob = useMemo<BatchResolveJobItem | null>(() => {
    if (batchJobs.length === 0) return null;
    return (
      batchJobs.find((j) => j.job_id === activeJobId) || batchJobs[0] || null
    );
  }, [batchJobs, activeJobId]);

  const previewSignatures = useMemo<ImportPreviewSignatureItem[]>(() => {
    return normalizeSignaturesForJob(currentBatchJob);
  }, [currentBatchJob]);

  const qualitySummary = useMemo<BatchIdentityQuality>(() => {
    const raw = batchResolve?.quality || batchResolve?.identity_quality;
    if (raw) {
      return {
        people_total: raw.people_total ?? 0,
        t1_covered: raw.t1_covered ?? 0,
        t2_count: raw.t2_count,
        t3_count: raw.t3_count,
        injected_student_ids: raw.injected_student_ids ?? 0,
        injected_emails: raw.injected_emails ?? 0,
        name_collisions: raw.name_collisions || [],
        same_as_of_jobs: raw.same_as_of_jobs || [],
      };
    }
    const seenNames = new Set<string>();
    let t1 = 0;
    let injSid = 0;
    let injEmail = 0;
    for (const job of batchJobs) {
      for (const p of job.preview?.people || []) {
        const key = p.cluster_id || p.login_name || p.name;
        if (seenNames.has(key)) continue;
        seenNames.add(key);
        if (p.tier === "T1") t1 += 1;
        if (
          p.injected_student_id ||
          p.warnings?.includes("STUDENT_ID_FROM_SIBLING")
        ) {
          injSid += 1;
        }
        if (p.injected_email || p.warnings?.includes("EMAIL_FROM_SIBLING")) {
          injEmail += 1;
        }
      }
    }
    return {
      people_total: seenNames.size,
      t1_covered: t1,
      injected_student_ids: injSid,
      injected_emails: injEmail,
      name_collisions: [],
      same_as_of_jobs: [],
    };
  }, [batchResolve, batchJobs]);

  const selfCandidate = useMemo<BatchSelfCandidate | null>(() => {
    return batchResolve?.self_candidate ?? null;
  }, [batchResolve]);

  function updatePersonOverride(
    jobId: string,
    person: ImportPreviewPerson,
    patch: Partial<PersonOverrideState>,
  ) {
    setJobOverridesMap((prev) => {
      const next = { ...prev };
      for (const job of batchJobs) {
        const jid = job.job_id;
        const jobOv = { ...(next[jid] || {}) };
        for (const p of job.preview?.people || []) {
          const isTarget =
            (jid === jobId && p.key === person.key) ||
            (person.cluster_id && p.cluster_id === person.cluster_id);
          if (isTarget) {
            const cur = jobOv[p.key] || {
              exclude: false,
              login_name: p.login_name || "",
            };
            jobOv[p.key] = { ...cur, ...patch };
          }
        }
        next[jid] = jobOv;
      }
      return next;
    });
  }

  function updateJobAsOf(jobId: string, asOfTime: string) {
    setJobAsOfMap((prev) => ({
      ...prev,
      [jobId]: asOfTime,
    }));
  }

  function toggleJobSignatureSkip(
    jobId: string,
    sigKey: string,
    skip: boolean,
  ) {
    setJobSkipSignaturesMap((prev) => ({
      ...prev,
      [jobId]: {
        ...(prev[jobId] || {}),
        [sigKey]: skip,
      },
    }));
  }

  function getSkippedSignatureIdsForJob(jobId: string): string[] {
    const skipForJob = jobSkipSignaturesMap[jobId] || {};
    return Object.entries(skipForJob)
      .filter(([, skipped]) => skipped)
      .map(([sigId]) => sigId);
  }

  async function handleProvision() {
    if (provisioningRef.current || provisioning || batchJobs.length === 0) {
      return;
    }
    for (const job of batchJobs) {
      const asOf = (jobAsOfMap[job.job_id] || "").trim();
      if (!asOf) {
        setActiveJobId(job.job_id);
        setWizardError(
          `请为申报书「${job.original_filename}」填写本子基准日期 (as_of_time)`,
        );
        return;
      }
    }

    provisioningRef.current = true;
    setWizardError("");
    setProvisioning(true);
    try {
      const jobsPayload = batchJobs.map((job) => {
        const jid = job.job_id;
        const ovForJob = jobOverridesMap[jid] || {};
        const peoplePayload = (job.preview?.people || []).map((p) => {
          const ov = ovForJob[p.key];
          return {
            key: p.key,
            exclude: Boolean(ov?.exclude),
            ...(ov?.login_name && ov.login_name.trim() !== p.login_name
              ? { login_name: ov.login_name.trim() }
              : {}),
          };
        });
        const skippedSigs = getSkippedSignatureIdsForJob(jid);
        return {
          job_id: jid,
          as_of_time: (jobAsOfMap[jid] || "").trim() || undefined,
          people: peoplePayload,
          ...(skippedSigs.length > 0
            ? { skip_signature_ids: skippedSigs }
            : {}),
        };
      });

      const res = await provisionImportBatch({
        jobs: jobsPayload,
        initial_password: initialPassword.trim() || undefined,
        include_password: includePassword,
        ...(linkSelfClusterId
          ? { link_self_cluster_id: linkSelfClusterId }
          : {}),
      });

      const normalizedRes: BatchProvisionResponse = {
        ...res,
        rag_doc_key: res.rag_doc_key || "lab:assets",
        merge_queue: Array.isArray(res.merge_queue) ? res.merge_queue : [],
        stats: {
          users_created: res.stats?.users_created ?? 0,
          users_linked: res.stats?.users_linked ?? 0,
          people_inserted: res.stats?.people_inserted ?? 0,
          people_skipped: res.stats?.people_skipped ?? 0,
          achievements_inserted: res.stats?.achievements_inserted ?? 0,
          achievements_skipped: res.stats?.achievements_skipped ?? 0,
          projects_inserted: res.stats?.projects_inserted ?? 0,
          projects_skipped: res.stats?.projects_skipped ?? 0,
          facilities_inserted: res.stats?.facilities_inserted ?? 0,
          facilities_skipped: res.stats?.facilities_skipped ?? 0,
        },
      };

      setProvisionResult(normalizedRes);
      setHasSavedCredentials(false);
      setWizardStep(3);
      await loadAll();
    } catch (err) {
      setWizardError(
        err instanceof ApiError
          ? mapLabErrorCode(err.code, err.message)
          : t.error,
      );
    } finally {
      provisioningRef.current = false;
      setProvisioning(false);
    }
  }

  function handleDownloadCsv() {
    if (!provisionResult) return;
    const csvContent = provisionResult.roster_csv.startsWith("﻿")
      ? provisionResult.roster_csv
      : `﻿${provisionResult.roster_csv}`;
    const blob = new Blob([csvContent], {
      type: "text/csv;charset=utf-8;",
    });
    const dateTag =
      provisionResult.as_of_time ||
      new Date().toISOString().slice(0, 10);
    triggerBlobDownload(blob, `labagent-accounts-${dateTag}.csv`);
    setHasSavedCredentials(true);
  }

  async function processStep4QueueFromList(queueList: Step4JobMergeState[]) {
    if (step4RunningRef.current) return;
    step4RunningRef.current = true;
    setStep4Running(true);
    setWizardError("");

    const working = queueList.map((item) => ({ ...item }));

    try {
      for (let i = 0; i < working.length; i++) {
        const current = working[i];
        if (current.status === "merged") continue;

        if (current.status === "needs_decision" && current.preview) {
          // Wait for user to confirm decisions on this job
          setStep4Jobs([...working]);
          return;
        }

        working[i] = { ...current, status: "previewing", error: undefined };
        setStep4Jobs([...working]);

        try {
          const preview = await previewImportMerge(
            current.jobId,
            current.asOfTime || undefined,
          );
          const conflictItems = (preview.changes || []).filter(
            (c) => c.requires_decision || c.category === "same_time_conflict",
          );
          if (conflictItems.length > 0) {
            const initDecisions: Record<
              string,
              "adopt_incoming" | "keep_current"
            > = {};
            for (const c of conflictItems) {
              initDecisions[c.change_key] =
                c.default_action === "keep_current"
                  ? "keep_current"
                  : "adopt_incoming";
            }
            working[i] = {
              ...working[i],
              status: "needs_decision",
              asOfTime: preview.as_of_time || current.asOfTime,
              preview,
              decisions: initDecisions,
            };
            setStep4Jobs([...working]);
            return;
          }

          working[i] = {
            ...working[i],
            status: "merging",
            asOfTime: preview.as_of_time || current.asOfTime,
            preview,
          };
          setStep4Jobs([...working]);

          const skippedSigs = getSkippedSignatureIdsForJob(current.jobId);
          const execRes = await executeImportMerge(current.jobId, {
            as_of_time: preview.as_of_time || current.asOfTime || undefined,
            decisions: [],
            ...(skippedSigs.length > 0
              ? { skip_signature_ids: skippedSigs }
              : {}),
          });
          working[i] = {
            ...working[i],
            status: "merged",
            result: execRes,
          };
          setStep4Jobs([...working]);
        } catch (err) {
          const msg =
            err instanceof ApiError
              ? mapLabErrorCode(err.code, err.message)
              : t.error;
          working[i] = {
            ...working[i],
            status: "error",
            error: msg,
          };
          setStep4Jobs([...working]);
          setWizardError(msg);
          return;
        }
      }

      if (working.every((it) => it.status === "merged")) {
        setStep4Done(true);
        await loadAll();
      }
    } finally {
      step4RunningRef.current = false;
      setStep4Running(false);
    }
  }

  async function handleStartStep4Merge() {
    if (!provisionResult || step4RunningRef.current) return;
    const queueIds = provisionResult.merge_queue || [];
    if (queueIds.length === 0) return;

    const initialList: Step4JobMergeState[] = queueIds.map((jid) => {
      const matchedJob = batchJobs.find((j) => j.job_id === jid);
      return {
        jobId: jid,
        filename: matchedJob?.original_filename || jid,
        asOfTime:
          jobAsOfMap[jid] ||
          matchedJob?.as_of_time ||
          matchedJob?.preview?.as_of_time ||
          "",
        status: "pending",
        decisions: {},
      };
    });

    setStep4Done(false);
    setStep4Jobs(initialList);
    setWizardStep(4);
    await processStep4QueueFromList(initialList);
  }

  function handleUpdateStep4Decision(
    jobId: string,
    changeKey: string,
    action: "adopt_incoming" | "keep_current",
  ) {
    setStep4Jobs((prev) =>
      prev.map((it) =>
        it.jobId === jobId
          ? {
              ...it,
              decisions: {
                ...it.decisions,
                [changeKey]: action,
              },
            }
          : it,
      ),
    );
  }

  async function handleConfirmStep4JobDecision(jobId: string) {
    if (step4RunningRef.current) return;
    const targetIdx = step4Jobs.findIndex((it) => it.jobId === jobId);
    if (targetIdx < 0) return;
    const target = step4Jobs[targetIdx];
    if (!target.preview) return;

    const conflictItems = (target.preview.changes || []).filter(
      (c) => c.requires_decision || c.category === "same_time_conflict",
    );
    for (const c of conflictItems) {
      if (!target.decisions[c.change_key]) {
        setWizardError(t.unresolvedConflictsHint);
        return;
      }
    }

    step4RunningRef.current = true;
    setStep4Running(true);
    setWizardError("");

    const working = step4Jobs.map((it) => ({ ...it }));
    working[targetIdx] = {
      ...target,
      status: "merging",
      error: undefined,
    };
    setStep4Jobs([...working]);

    try {
      const decisionsList: MergeDecision[] = Object.entries(
        target.decisions,
      ).map(([change_key, action]) => ({
        change_key,
        action,
      }));
      const skippedSigs = getSkippedSignatureIdsForJob(target.jobId);
      const execRes = await executeImportMerge(target.jobId, {
        as_of_time:
          target.preview.as_of_time || target.asOfTime || undefined,
        decisions: decisionsList,
        ...(skippedSigs.length > 0
          ? { skip_signature_ids: skippedSigs }
          : {}),
      });
      working[targetIdx] = {
        ...working[targetIdx],
        status: "merged",
        result: execRes,
      };
      setStep4Jobs([...working]);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? mapLabErrorCode(err.code, err.message)
          : t.error;
      working[targetIdx] = {
        ...working[targetIdx],
        status: "error",
        error: msg,
      };
      setStep4Jobs([...working]);
      setWizardError(msg);
      step4RunningRef.current = false;
      setStep4Running(false);
      return;
    }

    step4RunningRef.current = false;
    setStep4Running(false);
    await processStep4QueueFromList(working);
  }

  const step4Totals = useMemo(() => {
    let added = 0;
    let updated = 0;
    let fillBlank = 0;
    for (const item of step4Jobs) {
      if (item.status !== "merged") continue;
      const st = item.result?.stats;
      const sum = item.result?.summary;
      const prevSum = item.preview?.summary;
      added +=
        st?.added_entities ??
        item.result?.added_count ??
        sum?.added ??
        prevSum?.added ??
        0;
      updated +=
        st?.updated_fields ??
        item.result?.updated_count ??
        sum?.updated ??
        prevSum?.updated ??
        0;
      fillBlank +=
        item.result?.fill_blank_count ??
        sum?.fill_blank ??
        prevSum?.fill_blank ??
        0;
    }
    return { added, updated, fillBlank };
  }, [step4Jobs]);

  // Backward-compatible single-job aliases so any existing consumer still works
  const uploadFile = uploadItems[0]?.file ?? null;
  const setUploadFile = (f: File | null) => {
    if (!f) {
      setUploadItems([]);
    } else {
      handleFilesSelected([f]);
    }
  };
  const importJob = useMemo<ImportJobResponse | null>(() => {
    if (!currentBatchJob) return null;
    return {
      id: currentBatchJob.id || currentBatchJob.job_id,
      job_id: currentBatchJob.job_id,
      original_filename: currentBatchJob.original_filename,
      as_of_time: currentBatchJob.as_of_time,
      status: currentBatchJob.status || "parsed",
      stats: currentBatchJob.stats || {},
      preview: currentBatchJob.preview,
    };
  }, [currentBatchJob]);
  const provisionAsOf = currentBatchJob
    ? jobAsOfMap[currentBatchJob.job_id] || ""
    : "";
  const setProvisionAsOf = (val: string) => {
    if (currentBatchJob) {
      updateJobAsOf(currentBatchJob.job_id, val);
    }
  };
  const overrides = useMemo<Record<string, PersonOverrideState>>(() => {
    if (!currentBatchJob) return {};
    return jobOverridesMap[currentBatchJob.job_id] || {};
  }, [currentBatchJob, jobOverridesMap]);
  const skipSignatureIds = useMemo<Record<string, boolean>>(() => {
    if (!currentBatchJob) return {};
    return jobSkipSignaturesMap[currentBatchJob.job_id] || {};
  }, [currentBatchJob, jobSkipSignaturesMap]);

  async function handleCopyRoster() {
    if (!provisionResult) return;
    setWizardError("");
    try {
      await copyTextToClipboard(provisionResult.roster_text);
      setRosterCopied(true);
      setHasSavedCredentials(true);
      setTimeout(() => setRosterCopied(false), 2000);
    } catch {
      setWizardError("复制到剪贴板失败，请手动全选复制或点击下载 CSV");
    }
  }

  return {
    wizardOpen,
    setWizardOpen,
    wizardStep,
    setWizardStep,
    uploadItems,
    setUploadItems,
    uploadFile,
    setUploadFile,
    uploadAsOf,
    setUploadAsOf,
    parsing,
    setParsing,
    wizardError,
    setWizardError,
    batchResolve,
    setBatchResolve,
    batchJobs,
    setBatchJobs,
    activeJobId,
    setActiveJobId,
    currentBatchJob,
    jobAsOfMap,
    setJobAsOfMap,
    jobOverridesMap,
    setJobOverridesMap,
    jobSkipSignaturesMap,
    setJobSkipSignaturesMap,
    linkSelfClusterId,
    setLinkSelfClusterId,
    qualitySummary,
    selfCandidate,
    importJob,
    provisionAsOf,
    setProvisionAsOf,
    initialPassword,
    setInitialPassword,
    includePassword,
    setIncludePassword,
    overrides,
    skipSignatureIds,
    provisioning,
    setProvisioning,
    provisionResult,
    setProvisionResult,
    rosterCopied,
    setRosterCopied,
    hasSavedCredentials,
    setHasSavedCredentials,
    hasUnsavedPlaintextRoster,
    previewSignatures,
    step4Jobs,
    step4Running,
    step4Done,
    step4Totals,
    openColdStartWizard,
    tryCloseWizard,
    handleFilesSelected,
    handleFileChange,
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
    processStep4QueueFromList,
  };
}
