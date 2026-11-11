"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Shell } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  api,
  exportAuditCsv,
  type AuditFilterParams,
} from "@/lib/api";
import { type Copy, useI18n } from "@/lib/i18n";
import { triggerBlobDownload } from "../lab/components/labTypesAndHelpers";

export const AUDIT_ACTIONS = [
  "login",
  "login_failed",
  "login_rate_limited",
  "logout",
  "bootstrap_admin",
  "user_created",
  "user_updated",
  "user_password_reset",
  "user_change_self_password",
  "group_created",
  "group_members_changed",
  "token_create",
  "token_revoke",
  "import_proposal_parse",
  "import_proposal_provision",
  "import_batch_resolve",
  "import_batch_provision",
  "import_proposal_merged",
  "import_merge_reverted",
  "import_preview_updated",
  "import_job_reparsed",
  "import_job_deleted",
  "import_provenance_backfilled",
  "lab_profile_reindexed",
  "lab_person_updated",
  "lab_project_created",
  "lab_project_updated",
  "lab_project_deleted",
  "lab_achievement_created",
  "lab_achievement_updated",
  "lab_achievement_deleted",
  "lab_facility_created",
  "lab_facility_updated",
  "lab_facility_deleted",
  "lab_field_restored_imported",
  "lab.signature.read",
  "lab.signature.upload",
  "lab.signature.delete",
  "invoice_uploaded",
  "invoice_updated",
  "invoice_deleted",
  "invoice_downloaded",
  "invoice_exported",
  "document_upload",
  "document_indexed",
  "document_index_failed",
  "document_delete",
  "document_purged",
  "acl_changed",
  "retrieval",
  "rerank_failed",
  "tool_call",
  "llm_call",
  "answer_refused",
  "permission_denied",
  "audit_export",
  "audit_retention",
  "maintenance_run",
  "index_rebuilt",
] as const;

export const AUDIT_RESOURCE_TYPES = [
  "user",
  "group",
  "document",
  "audit",
  "api_token",
  "import_job",
  "lab_person",
  "lab_project",
  "lab_achievement",
  "lab_facility",
  "invoice",
  "chat",
  "chunk",
  "embedding_index",
  "mcp_tool",
  "reranker",
  "system",
  "tool",
] as const;

export type AuditAction = (typeof AUDIT_ACTIONS)[number];

type EventRow = {
  id: number;
  created_at: string;
  actor_id: string | null;
  actor_name?: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  detail: Record<string, unknown> | null;
};

type AuditResponse = {
  events: EventRow[];
  next_before_id: number | null;
};

type ActorOption = {
  id: string;
  display_name: string;
  email: string | null;
  login_name?: string | null;
};

const PAGE_LIMIT = 50;

function actionLabel(action: string, t: Copy): string {
  const map: Record<AuditAction, string> = {
    login: t.action_login,
    login_failed: t.action_login_failed,
    login_rate_limited: t.action_login_rate_limited,
    logout: t.action_logout,
    bootstrap_admin: t.action_bootstrap_admin,
    user_created: t.action_user_created,
    user_updated: t.action_user_updated,
    user_password_reset: t.action_user_password_reset,
    user_change_self_password: t.action_user_change_self_password,
    group_created: t.action_group_created,
    group_members_changed: t.action_group_members_changed,
    token_create: t.action_token_create,
    token_revoke: t.action_token_revoke,
    import_proposal_parse: t.action_import_proposal_parse,
    import_proposal_provision: t.action_import_proposal_provision,
    import_batch_resolve: t.action_import_batch_resolve,
    import_batch_provision: t.action_import_batch_provision,
    import_proposal_merged: t.action_import_proposal_merged,
    import_merge_reverted: t.action_import_merge_reverted,
    import_preview_updated: t.action_import_preview_updated,
    import_job_reparsed: t.action_import_job_reparsed,
    import_job_deleted: t.action_import_job_deleted,
    import_provenance_backfilled: t.action_import_provenance_backfilled,
    lab_profile_reindexed: t.action_lab_profile_reindexed,
    lab_person_updated: t.action_lab_person_updated,
    lab_project_created: t.action_lab_project_created,
    lab_project_updated: t.action_lab_project_updated,
    lab_project_deleted: t.action_lab_project_deleted,
    lab_achievement_created: t.action_lab_achievement_created,
    lab_achievement_updated: t.action_lab_achievement_updated,
    lab_achievement_deleted: t.action_lab_achievement_deleted,
    lab_facility_created: t.action_lab_facility_created,
    lab_facility_updated: t.action_lab_facility_updated,
    lab_facility_deleted: t.action_lab_facility_deleted,
    lab_field_restored_imported: t.action_lab_field_restored_imported,
    "lab.signature.read": t.action_lab_signature_read,
    "lab.signature.upload": t.action_lab_signature_upload,
    "lab.signature.delete": t.action_lab_signature_delete,
    invoice_uploaded: t.action_invoice_uploaded,
    invoice_updated: t.action_invoice_updated,
    invoice_deleted: t.action_invoice_deleted,
    invoice_downloaded: t.action_invoice_downloaded,
    invoice_exported: t.action_invoice_exported,
    document_upload: t.action_document_upload,
    document_indexed: t.action_document_indexed,
    document_index_failed: t.action_document_index_failed,
    document_delete: t.action_document_delete,
    document_purged: t.action_document_purged,
    acl_changed: t.action_acl_changed,
    retrieval: t.action_retrieval,
    rerank_failed: t.action_rerank_failed,
    tool_call: t.action_tool_call,
    llm_call: t.action_llm_call,
    answer_refused: t.action_answer_refused,
    permission_denied: t.action_permission_denied,
    audit_export: t.action_audit_export,
    audit_retention: t.action_audit_retention,
    maintenance_run: t.action_maintenance_run,
    index_rebuilt: t.action_index_rebuilt,
  };
  return (map as Record<string, string>)[action] || action;
}

function toSinceIso(dateVal: string): string | undefined {
  const trimmed = dateVal.trim();
  if (!trimmed) return undefined;
  const parsed = new Date(`${trimmed}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return parsed.toISOString();
}

function toUntilIso(dateVal: string): string | undefined {
  const trimmed = dateVal.trim();
  if (!trimmed) return undefined;
  const parsed = new Date(`${trimmed}T23:59:59.999`);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return parsed.toISOString();
}

function buildAuditPath(
  filters: AuditFilterParams,
  beforeId?: number | null,
): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (filters.action) params.set("action", filters.action);
  if (filters.actor_id) params.set("actor_id", filters.actor_id);
  if (filters.resource_type) params.set("resource_type", filters.resource_type);
  if (filters.resource_id?.trim()) {
    params.set("resource_id", filters.resource_id.trim());
  }
  if (filters.since) params.set("since", filters.since);
  if (filters.until) params.set("until", filters.until);
  if (beforeId !== undefined && beforeId !== null) {
    params.set("before_id", String(beforeId));
  }
  return `audit?${params.toString()}`;
}

function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function AuditPage() {
  const { t } = useI18n();
  const [selectedAction, setSelectedAction] = useState<string>("");
  const [actorId, setActorId] = useState<string>("");
  const [resourceType, setResourceType] = useState<string>("");
  const [resourceId, setResourceId] = useState<string>("");
  const [sinceDate, setSinceDate] = useState<string>("");
  const [untilDate, setUntilDate] = useState<string>("");

  const [actors, setActors] = useState<ActorOption[]>([]);
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [exportingCsv, setExportingCsv] = useState(false);
  const [runningRetention, setRunningRetention] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const activeFilters = useMemo<AuditFilterParams>(
    () => ({
      action: selectedAction || undefined,
      actor_id: actorId || undefined,
      resource_type: resourceType || undefined,
      resource_id: resourceId.trim() || undefined,
      since: toSinceIso(sinceDate),
      until: toUntilIso(untilDate),
    }),
    [selectedAction, actorId, resourceType, resourceId, sinceDate, untilDate],
  );

  const hasAnyFilter = Boolean(
    selectedAction ||
      actorId ||
      resourceType ||
      resourceId.trim() ||
      sinceDate ||
      untilDate,
  );

  useEffect(() => {
    let cancelled = false;
    api<{ users: ActorOption[] }>("admin/users")
      .then((res) => {
        if (!cancelled && Array.isArray(res.users)) {
          setActors(res.users);
        }
      })
      .catch(() => {
        // Non-admin or transient error: leave actor dropdown empty
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(
    async (filters: AuditFilterParams) => {
      setError("");
      try {
        const body = await api<AuditResponse>(buildAuditPath(filters));
        setEvents(body.events);
        setNextBeforeId(body.next_before_id ?? null);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : t.error);
        setEvents([]);
        setNextBeforeId(null);
      }
    },
    [t.error],
  );

  useEffect(() => {
    let cancelled = false;
    api<AuditResponse>(buildAuditPath(activeFilters))
      .then((body) => {
        if (cancelled) return;
        setError("");
        setEvents(body.events);
        setNextBeforeId(body.next_before_id ?? null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "这次请求没有完成。");
        setEvents([]);
        setNextBeforeId(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeFilters]);

  async function handleLoadEarlier() {
    if (nextBeforeId === null || loadingMore) return;
    setLoadingMore(true);
    setError("");
    try {
      const body = await api<AuditResponse>(
        buildAuditPath(activeFilters, nextBeforeId),
      );
      setEvents((prev) => [...(prev || []), ...body.events]);
      setNextBeforeId(body.next_before_id ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setLoadingMore(false);
    }
  }

  async function handleExportCsv() {
    if (exportingCsv) return;
    setExportingCsv(true);
    setError("");
    try {
      const { blob, filename } = await exportAuditCsv(activeFilters);
      triggerBlobDownload(blob, filename);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setExportingCsv(false);
    }
  }

  async function handleRetention() {
    if (runningRetention) return;
    setRunningRetention(true);
    setError("");
    try {
      const result = await api<{ deleted: number }>("audit/retention", {
        method: "POST",
        body: JSON.stringify({ days: 180 }),
      });
      setNote(`已清理 ${result.deleted} 条超期记录`);
      await load(activeFilters);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setRunningRetention(false);
    }
  }

  function handleResetFilters() {
    setEvents(null);
    setNextBeforeId(null);
    setSelectedAction("");
    setActorId("");
    setResourceType("");
    setResourceId("");
    setSinceDate("");
    setUntilDate("");
  }

  return (
    <Shell>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl">{t.audit}</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{t.auditHint}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            disabled={exportingCsv}
            onClick={() => void handleExportCsv()}
          >
            {exportingCsv ? t.loading : t.auditExportCsv}
          </Button>
          <Button
            variant="outline"
            disabled={runningRetention}
            onClick={() => void handleRetention()}
          >
            {t.retention}
          </Button>
        </div>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-3">
        <select
          aria-label={t.action}
          className="h-8 rounded-md border border-input bg-background px-2 text-xs"
          value={selectedAction}
          onChange={(event) => {
            setEvents(null);
            setNextBeforeId(null);
            setSelectedAction(event.target.value);
          }}
        >
          <option value="">{t.actionAll}</option>
          {AUDIT_ACTIONS.map((act) => (
            <option key={act} value={act}>
              {actionLabel(act, t)} ({act})
            </option>
          ))}
        </select>

        <select
          aria-label={t.actor}
          className="h-8 rounded-md border border-input bg-background px-2 text-xs"
          value={actorId}
          onChange={(event) => {
            setEvents(null);
            setNextBeforeId(null);
            setActorId(event.target.value);
          }}
        >
          <option value="">{t.auditFilterActor}</option>
          {actors.map((u) => (
            <option key={u.id} value={u.id}>
              {u.display_name} ({u.login_name || u.email || u.id.slice(0, 8)})
            </option>
          ))}
        </select>

        <select
          aria-label={t.auditFilterResourceType}
          className="h-8 rounded-md border border-input bg-background px-2 text-xs"
          value={resourceType}
          onChange={(event) => {
            setEvents(null);
            setNextBeforeId(null);
            setResourceType(event.target.value);
          }}
        >
          <option value="">{t.auditFilterResourceAll}</option>
          {AUDIT_RESOURCE_TYPES.map((rt) => (
            <option key={rt} value={rt}>
              {rt}
            </option>
          ))}
        </select>

        <Input
          aria-label={t.auditFilterResourceId}
          placeholder={t.auditFilterResourceId}
          value={resourceId}
          onChange={(event) => {
            setEvents(null);
            setNextBeforeId(null);
            setResourceId(event.target.value);
          }}
          className="h-8 w-40 font-mono text-xs"
        />

        <div className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <span>{t.auditFilterSince}</span>
          <Input
            type="date"
            aria-label={t.auditFilterSince}
            value={sinceDate}
            onChange={(event) => {
              setEvents(null);
              setNextBeforeId(null);
              setSinceDate(event.target.value);
            }}
            className="h-8 w-36 text-xs"
          />
        </div>

        <div className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <span>{t.auditFilterUntil}</span>
          <Input
            type="date"
            aria-label={t.auditFilterUntil}
            value={untilDate}
            onChange={(event) => {
              setEvents(null);
              setNextBeforeId(null);
              setUntilDate(event.target.value);
            }}
            className="h-8 w-36 text-xs"
          />
        </div>

        {hasAnyFilter ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 text-xs"
            onClick={handleResetFilters}
          >
            {t.auditFilterReset}
          </Button>
        ) : null}
      </div>

      {error ? <p className="mb-3 text-sm text-destructive">{error}</p> : null}
      {note ? <p className="mb-3 text-sm text-muted-foreground">{note}</p> : null}
      {events && events.length === 0 ? <p className="text-sm">{t.auditEmpty}</p> : null}
      {events === null ? <p className="text-sm text-muted-foreground">{t.loading}</p> : null}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr>
              <th className="py-2 pr-3">{t.when}</th>
              <th className="py-2 pr-3">{t.actor}</th>
              <th className="py-2 pr-3">{t.action}</th>
              <th className="py-2 pr-3">{t.resource}</th>
              <th className="py-2">{t.detail}</th>
            </tr>
          </thead>
          <tbody>
            {(events || []).map((event) => {
              const detailEntries = Object.entries(event.detail || {});
              return (
                <tr key={event.id} className="border-t border-border align-top">
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {event.created_at?.replace("T", " ").slice(0, 19)}
                  </td>
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {event.actor_name ? (
                      <span>{event.actor_name}</span>
                    ) : event.actor_id ? (
                      <span className="font-mono text-xs text-muted-foreground">
                        {event.actor_id.slice(0, 8)}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    <div>{actionLabel(event.action, t)}</div>
                    <span className="block font-mono text-xs text-muted-foreground">
                      {event.action}
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    {event.resource_type || event.resource_id ? (
                      <div>
                        {event.resource_type ? (
                          <div className="text-xs font-medium">{event.resource_type}</div>
                        ) : null}
                        {event.resource_id ? (
                          <div className="font-mono text-xs text-muted-foreground">
                            {event.resource_id}
                          </div>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="py-2">
                    {detailEntries.length > 0 ? (
                      <table className="border-collapse text-xs">
                        <tbody>
                          {detailEntries.map(([key, val]) => (
                            <tr key={key}>
                              <td className="py-0.5 pr-2 align-top font-mono text-muted-foreground">
                                {key}
                              </td>
                              <td className="py-0.5 font-mono break-all">
                                {formatDetailValue(val)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {nextBeforeId !== null ? (
        <div className="mt-4 flex justify-center">
          <Button variant="outline" onClick={handleLoadEarlier} disabled={loadingMore}>
            {loadingMore ? t.loadingMore : t.loadEarlier}
          </Button>
        </div>
      ) : null}
    </Shell>
  );
}
