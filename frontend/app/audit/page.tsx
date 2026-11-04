"use client";

import { useEffect, useState } from "react";

import { Shell } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type EventRow = {
  id: number;
  created_at: string;
  actor_id: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  detail: Record<string, unknown> | null;
};

export default function AuditPage() {
  const { t } = useI18n();
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  async function load() {
    try {
      const body = await api<{ events: EventRow[] }>("audit?limit=80");
      setEvents(body.events);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
      setEvents([]);
    }
  }

  useEffect(() => {
    let cancelled = false;
    api<{ events: EventRow[] }>("audit?limit=80")
      .then((body) => {
        if (!cancelled) setEvents(body.events);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "这次请求没有完成。");
        setEvents([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Shell>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl">{t.audit}</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{t.auditHint}</p>
        </div>
        <Button
          variant="outline"
          onClick={async () => {
            try {
              const result = await api<{ deleted: number }>("audit/retention", {
                method: "POST",
                body: JSON.stringify({ days: 180 }),
              });
              setNote(`${result.deleted}`);
              await load();
            } catch (err) {
              setError(err instanceof ApiError ? err.message : t.error);
            }
          }}
        >
          {t.retention}
        </Button>
      </div>
      {error ? <p className="mb-3 text-sm text-destructive">{error}</p> : null}
      {note ? <p className="mb-3 text-sm text-muted-foreground">{note}</p> : null}
      {events && events.length === 0 ? <p className="text-sm">{t.auditEmpty}</p> : null}
      {events === null ? <p className="text-sm text-muted-foreground">{t.loading}</p> : null}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr>
              <th className="py-2 pr-3">{t.when}</th>
              <th className="py-2 pr-3">{t.action}</th>
              <th className="py-2 pr-3">{t.actor}</th>
              <th className="py-2">{t.detail}</th>
            </tr>
          </thead>
          <tbody>
            {(events || []).map((event) => (
              <tr key={event.id} className="border-t border-border align-top">
                <td className="py-2 pr-3 whitespace-nowrap">{event.created_at?.replace("T", " ").slice(0, 19)}</td>
                <td className="py-2 pr-3">
                  {event.action}
                  {event.resource_type ? (
                    <span className="mt-1 block text-xs text-muted-foreground">{event.resource_type}</span>
                  ) : null}
                </td>
                <td className="py-2 pr-3 font-mono text-xs">{event.actor_id ? event.actor_id.slice(0, 8) : "—"}</td>
                <td className="py-2 font-mono text-xs">{JSON.stringify(event.detail || {})}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Shell>
  );
}
