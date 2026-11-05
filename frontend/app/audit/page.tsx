"use client";

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { type Copy, useI18n } from "@/lib/i18n";

export const AUDIT_ACTIONS = [
  "login",
  "login_failed",
  "login_rate_limited",
  "logout",
  "document_upload",
  "document_indexed",
  "document_index_failed",
  "document_delete",
  "retrieval",
  "tool_call",
  "llm_call",
  "answer_refused",
  "permission_denied",
  "audit_retention",
  "index_rebuilt",
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

const PAGE_LIMIT = 50;

function actionLabel(action: string, t: Copy): string {
  const map: Record<AuditAction, string> = {
    login: t.action_login,
    login_failed: t.action_login_failed,
    login_rate_limited: t.action_login_rate_limited,
    logout: t.action_logout,
    document_upload: t.action_document_upload,
    document_indexed: t.action_document_indexed,
    document_index_failed: t.action_document_index_failed,
    document_delete: t.action_document_delete,
    retrieval: t.action_retrieval,
    tool_call: t.action_tool_call,
    llm_call: t.action_llm_call,
    answer_refused: t.action_answer_refused,
    permission_denied: t.action_permission_denied,
    audit_retention: t.action_audit_retention,
    index_rebuilt: t.action_index_rebuilt,
  };
  return (map as Record<string, string>)[action] || action;
}

function buildAuditPath(actionFilter: string, beforeId?: number | null): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (actionFilter) {
    params.set("action", actionFilter);
  }
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
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(
    async (actionFilter: string) => {
      setError("");
      try {
        const body = await api<AuditResponse>(buildAuditPath(actionFilter));
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
    api<AuditResponse>(buildAuditPath(selectedAction))
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
  }, [selectedAction]);

  async function handleLoadEarlier() {
    if (nextBeforeId === null || loadingMore) return;
    setLoadingMore(true);
    setError("");
    try {
      const body = await api<AuditResponse>(buildAuditPath(selectedAction, nextBeforeId));
      setEvents((prev) => [...(prev || []), ...body.events]);
      setNextBeforeId(body.next_before_id ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <Shell>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl">{t.audit}</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{t.auditHint}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label={t.action}
            className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm"
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
          <Button
            variant="outline"
            onClick={async () => {
              try {
                const result = await api<{ deleted: number }>("audit/retention", {
                  method: "POST",
                  body: JSON.stringify({ days: 180 }),
                });
                setNote(`${result.deleted}`);
                await load(selectedAction);
              } catch (err) {
                setError(err instanceof ApiError ? err.message : t.error);
              }
            }}
          >
            {t.retention}
          </Button>
        </div>
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
