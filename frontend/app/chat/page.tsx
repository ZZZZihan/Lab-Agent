"use client";

import Link from "next/link";
import { type ReactNode, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { type ChatEvent, type Citation } from "@/lib/api";
import { type Copy, useI18n } from "@/lib/i18n";

type CitationCheckState = {
  used: number[];
  invalid: number[];
  uncited: boolean;
};

type Turn = {
  id: string;
  question: string;
  answer: string;
  pending: boolean;
  error?: string;
  refused?: boolean;
  refusal?: ChatEvent;
  citations: Citation[];
  citationCheck?: CitationCheckState;
  paperStatus?: string | null;
  phase?: string;
};

const SUGGESTIONS = [
  "清衡-A100 一共有多少台节点？",
  "周三学术例会在哪间实体会议室？",
  "赫利俄斯的内部代号是什么？",
  "实验室本科生宿舍在哪一栋？",
];

function mapErrorCode(code: string | undefined, t: Copy): string {
  if (code === "llm_unavailable") return t.llmUnavailable;
  if (code === "embedding_unavailable") return t.embeddingUnavailable;
  if (code === "unauthorized") return t.noSession;
  return t.error;
}

function renderAnswerWithCitations(answer: string, turnId: string): ReactNode[] {
  const parts = answer.split(/(\[[0-9]{1,2}\])/g);
  return parts.map((part, idx) => {
    const match = /^\[([0-9]{1,2})\]$/.exec(part);
    if (!match) return <span key={idx}>{part}</span>;
    const num = Number(match[1]);
    const targetId = `citation-${turnId}-${num}`;
    return (
      <sup key={idx} className="mx-0.5">
        <button
          type="button"
          className="cursor-pointer text-xs font-medium text-primary underline hover:opacity-80"
          onClick={() => {
            const el = document.getElementById(targetId);
            el?.scrollIntoView({ behavior: "smooth", block: "center" });
          }}
        >
          [{num}]
        </button>
      </sup>
    );
  });
}

function Citations({
  items,
  label,
  turnId,
}: {
  items: Citation[];
  label: string;
  turnId: string;
}) {
  const { t } = useI18n();
  if (!items.length) return null;
  return (
    <div className="mt-3 grid gap-2">
      <p className="text-xs tracking-wide text-muted-foreground">{label}</p>
      {items.map((item, index) => {
        const citeIndex = item.index ?? index + 1;
        return (
          <div
            key={`${item.chunk_id || index}`}
            id={`citation-${turnId}-${citeIndex}`}
            className="rounded-lg border border-border bg-background px-3 py-2"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">[{citeIndex}]</Badge>
              <Badge variant="secondary">{item.heading || "—"}</Badge>
              <span className="text-sm">{item.title}</span>
              {typeof item.score === "number" ? (
                <span className="text-xs text-muted-foreground">{item.score.toFixed(2)}</span>
              ) : null}
            </div>
            <p className="mt-2 text-sm leading-6">{item.excerpt}</p>
            {item.document_id ? (
              <Link
                className="mt-2 inline-block text-xs text-primary underline"
                href={`/documents/${item.document_id}`}
              >
                {t.openDoc}
              </Link>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export default function ChatPage() {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);

  async function ask(question: string) {
    const text = question.trim();
    if (!text || busy) return;
    const id = crypto.randomUUID();
    setDraft("");
    setBusy(true);
    setTurns((current) => [
      ...current,
      { id, question: text, answer: "", pending: true, citations: [], phase: t.asking },
    ]);
    try {
      const response = await fetch("/api/backend/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (!response.ok || !response.body) {
        const message = response.status === 401 ? t.noSession : t.error;
        setTurns((current) =>
          current.map((turn) => (turn.id === id ? { ...turn, pending: false, error: message } : turn)),
        );
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const apply = (event: ChatEvent) => {
        setTurns((current) =>
          current.map((turn) => {
            if (turn.id !== id) return turn;
            if (event.type === "token") return { ...turn, answer: turn.answer + (event.text || "") };
            if (event.type === "citations") {
              return { ...turn, citations: event.items || [], paperStatus: event.paper_status };
            }
            if (event.type === "citation_check") {
              return {
                ...turn,
                citationCheck: {
                  used: event.used || [],
                  invalid: event.invalid || [],
                  uncited: Boolean(event.uncited),
                },
              };
            }
            if (event.type === "refusal") {
              return { ...turn, refused: true, refusal: event, paperStatus: event.paper_status };
            }
            if (event.type === "tool") return { ...turn, phase: event.name };
            if (event.type === "error") {
              return { ...turn, error: mapErrorCode(event.code, t), pending: false };
            }
            if (event.type === "done") {
              return {
                ...turn,
                pending: false,
                refused: event.refused ?? turn.refused,
                error: event.error ? mapErrorCode(event.error, t) : turn.error,
              };
            }
            return turn;
          }),
        );
      };
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) {
          const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
          if (!dataLine) continue;
          apply(JSON.parse(dataLine.slice(5).trim()) as ChatEvent);
        }
      }
      setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, pending: false } : turn)));
    } catch {
      setTurns((current) =>
        current.map((turn) => (turn.id === id ? { ...turn, pending: false, error: t.error } : turn)),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <div className="mx-auto flex min-h-[70vh] w-full max-w-3xl flex-col">
        {turns.length === 0 ? (
          <div className="rounded-xl border border-border bg-card px-5 py-8">
            <h1 className="font-display text-3xl">{t.emptyTitle}</h1>
            <p className="mt-3 max-w-xl text-sm leading-6 text-muted-foreground">{t.emptyBody}</p>
            <p className="mt-6 text-xs tracking-wide text-muted-foreground">{t.suggestions}</p>
            <div className="mt-2 flex flex-col gap-2">
              {SUGGESTIONS.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="rounded-lg border border-border px-3 py-2 text-left text-sm hover:bg-muted"
                  onClick={() => ask(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="grid gap-6">
            {turns.map((turn) => {
              const isModelDeclined = turn.refusal?.reason === "model_declined";
              const usedSet =
                turn.citationCheck && turn.citationCheck.used.length > 0
                  ? new Set(turn.citationCheck.used)
                  : null;
              const displayedCitations = usedSet
                ? turn.citations.filter((c, i) => usedSet.has(c.index ?? i + 1))
                : turn.citations;

              return (
                <article key={turn.id} className="grid gap-3">
                  <p className="text-sm font-medium">{turn.question}</p>
                  {turn.refused && turn.refusal ? (
                    <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3">
                      <p className="font-medium text-destructive">
                        {isModelDeclined ? t.modelDeclinedTitle : t.refusalTitle}
                      </p>
                      <p className="mt-1 text-sm">
                        {t.score} {Number(turn.refusal.score || 0).toFixed(2)} · {t.threshold}{" "}
                        {Number(turn.refusal.threshold || 0).toFixed(2)} · {t.coverage}{" "}
                        {Number(turn.refusal.coverage || 0).toFixed(2)}
                      </p>
                      <p className="mt-2 text-sm text-muted-foreground">
                        {isModelDeclined ? t.modelDeclinedBody : t.refusalBody}
                      </p>
                      <Citations
                        items={turn.refusal.neighbors || []}
                        label={isModelDeclined ? t.modelDeclinedBody : t.refusalBody}
                        turnId={turn.id}
                      />
                    </div>
                  ) : (
                    <div className="rounded-xl bg-card px-4 py-3 ring-1 ring-foreground/10">
                      {turn.pending && !turn.answer ? (
                        <p className="text-sm text-muted-foreground">{turn.phase || t.asking}</p>
                      ) : (
                        <p className="whitespace-pre-wrap text-sm leading-7">
                          {renderAnswerWithCitations(turn.answer, turn.id)}
                        </p>
                      )}
                      {turn.citationCheck?.invalid && turn.citationCheck.invalid.length > 0 ? (
                        <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
                          {t.invalidCitationsNote}
                          {turn.citationCheck.invalid.map((n) => `[${n}]`).join(", ")}
                        </p>
                      ) : null}
                      {turn.citationCheck?.uncited ? (
                        <p className="mt-2 text-xs text-muted-foreground">{t.uncitedNote}</p>
                      ) : null}
                      <Citations
                        items={displayedCitations}
                        label={turn.citationCheck?.uncited ? t.uncitedNote : t.citations}
                        turnId={turn.id}
                      />
                    </div>
                  )}
                  {turn.paperStatus === "not_configured" ? (
                    <p className="text-xs text-muted-foreground">{t.paperNote}</p>
                  ) : null}
                  {turn.error ? <p className="text-sm text-destructive">{turn.error}</p> : null}
                </article>
              );
            })}
          </div>
        )}
        <form
          className="sticky bottom-0 mt-6 grid gap-2 bg-background/95 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            void ask(draft);
          }}
        >
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t.placeholder}
            rows={3}
          />
          <div className="flex justify-end">
            <Button type="submit" disabled={busy || !draft.trim()}>
              {busy ? t.asking : t.send}
            </Button>
          </div>
        </form>
      </div>
    </Shell>
  );
}
