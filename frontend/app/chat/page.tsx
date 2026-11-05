"use client";

import Link from "next/link";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, type ChatEvent, type Citation } from "@/lib/api";
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

type ConversationSummary = {
  id: string;
  title: string;
  created_at: string;
};

type StoredMessage = {
  id: string;
  role: string;
  content: string;
  refused: boolean;
  citations: Citation[];
};

type ConversationDetail = {
  id: string;
  title: string;
  messages: StoredMessage[];
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

function mapToolPhase(name: string | undefined, t: Copy): string {
  if (name === "search_knowledge_base") return t.toolSearchKnowledgeBase;
  if (name === "search_papers") return t.toolSearchPapers;
  if (name === "fetch_document") return t.toolFetchDocument;
  return t.asking;
}

function reconstructCitationCheck(
  answer: string,
  citations: Citation[],
  refused: boolean,
): CitationCheckState | undefined {
  if (refused) return undefined;
  const validIndices = new Set(citations.map((c, idx) => c.index ?? idx + 1));
  const used: number[] = [];
  const invalid: number[] = [];
  for (const match of answer.matchAll(/\[([0-9]{1,2})\]/g)) {
    const num = Number(match[1]);
    if (validIndices.has(num)) {
      if (!used.includes(num)) {
        used.push(num);
      }
    } else if (!invalid.includes(num)) {
      invalid.push(num);
    }
  }
  if (citations.length === 0 && used.length === 0 && invalid.length === 0) {
    return undefined;
  }
  return {
    used,
    invalid,
    uncited: used.length === 0,
  };
}

function messagesToTurns(messages: StoredMessage[]): Turn[] {
  const result: Turn[] = [];
  for (const msg of messages) {
    if (msg.role === "user") {
      result.push({
        id: msg.id,
        question: msg.content,
        answer: "",
        pending: false,
        citations: [],
      });
    } else if (msg.role === "assistant") {
      const citations = Array.isArray(msg.citations) ? msg.citations : [];
      const citationCheck = reconstructCitationCheck(msg.content, citations, msg.refused);
      const last = result[result.length - 1];
      if (last && !last.answer && !last.refused) {
        last.answer = msg.content;
        last.refused = msg.refused;
        last.citations = citations;
        last.citationCheck = citationCheck;
      } else {
        result.push({
          id: msg.id,
          question: "",
          answer: msg.content,
          pending: false,
          refused: msg.refused,
          citations,
          citationCheck,
        });
      }
    }
  }
  return result;
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
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const activeSelectionRef = useRef<string | null>(null);
  const historyRequestIdRef = useRef(0);
  const uiLocked = busy || loadingHistory || deletingId !== null;

  const loadConversations = useCallback(async () => {
    try {
      const res = await api<{ conversations: ConversationSummary[] }>("conversations");
      setConversations(res.conversations || []);
    } catch {
      // Ignore unauthenticated or transient list errors on initial load
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    api<{ conversations: ConversationSummary[] }>("conversations")
      .then((res) => {
        if (!cancelled) {
          setConversations(res.conversations || []);
        }
      })
      .catch(() => {
        // Ignore unauthenticated or transient list errors on initial load
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function selectConversation(id: string) {
    if (busy || loadingHistory || deletingId !== null) return;
    const reqId = ++historyRequestIdRef.current;
    setLoadingHistory(true);
    try {
      const detail = await api<ConversationDetail>(`conversations/${id}`);
      if (historyRequestIdRef.current !== reqId) return;
      activeSelectionRef.current = detail.id;
      setConversationId(detail.id);
      setTurns(messagesToTurns(detail.messages || []));
    } catch {
      // Keep current state if loading fails
    } finally {
      if (historyRequestIdRef.current === reqId) {
        setLoadingHistory(false);
      }
    }
  }

  function startNewConversation() {
    if (busy || loadingHistory || deletingId !== null) return;
    historyRequestIdRef.current += 1;
    activeSelectionRef.current = null;
    setConversationId(null);
    setTurns([]);
    setDraft("");
  }

  async function handleDeleteConversation(id: string) {
    if (busy || loadingHistory || deletingId !== null) return;
    setDeletingId(id);
    try {
      await api(`conversations/${id}`, { method: "DELETE" });
      setConversations((current) => current.filter((c) => c.id !== id));
      if (activeSelectionRef.current === id) {
        historyRequestIdRef.current += 1;
        activeSelectionRef.current = null;
        setConversationId(null);
        setTurns([]);
      }
    } catch {
      // Ignore delete error
    } finally {
      setDeletingId(null);
    }
  }

  async function ask(question: string) {
    const text = question.trim();
    if (!text || busy || loadingHistory || deletingId !== null) return;
    historyRequestIdRef.current += 1;
    const id = crypto.randomUUID();
    const targetConversationId = activeSelectionRef.current;
    setDraft("");
    setBusy(true);
    setTurns((current) => [
      ...current,
      { id, question: text, answer: "", pending: true, citations: [], phase: t.asking },
    ]);
    try {
      const payload: { message: string; conversation_id?: string } = { message: text };
      if (targetConversationId) {
        payload.conversation_id = targetConversationId;
      }
      const response = await fetch("/api/backend/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
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
      let resolvedConvId: string | undefined;
      const apply = (event: ChatEvent) => {
        if (event.type === "done" && event.conversation_id) {
          resolvedConvId = event.conversation_id;
          activeSelectionRef.current = event.conversation_id;
          setConversationId(event.conversation_id);
        }
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
            if (event.type === "status") return { ...turn, phase: mapToolPhase(event.phase, t) };
            if (event.type === "tool") return { ...turn, phase: mapToolPhase(event.name, t) };
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
      if (resolvedConvId) {
        void loadConversations();
      }
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
      <div className="mx-auto grid min-h-[70vh] w-full max-w-5xl gap-6 md:grid-cols-[240px_1fr]">
        <aside className="flex flex-col gap-3 rounded-xl border border-border bg-card p-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-xs font-medium tracking-wide text-muted-foreground">
              {t.conversationsTitle}
            </h2>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={uiLocked}
              onClick={startNewConversation}
            >
              {t.newConversation}
            </Button>
          </div>
          {conversations.length === 0 ? (
            <p className="py-4 text-xs text-muted-foreground">{t.noConversations}</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {conversations.map((conv) => {
                const active = conv.id === conversationId;
                return (
                  <li
                    key={conv.id}
                    className={`flex items-center justify-between gap-1 rounded-lg px-2 py-1.5 text-sm ${
                      active ? "bg-muted font-medium" : "hover:bg-muted/60"
                    }`}
                  >
                    <button
                      type="button"
                      className="min-w-0 flex-1 truncate text-left"
                      disabled={uiLocked}
                      onClick={() => void selectConversation(conv.id)}
                      title={conv.title}
                    >
                      {conv.title}
                    </button>
                    <button
                      type="button"
                      className="shrink-0 px-1.5 py-0.5 text-xs text-muted-foreground hover:text-destructive"
                      disabled={uiLocked}
                      onClick={() => void handleDeleteConversation(conv.id)}
                      aria-label={t.deleteConversation}
                    >
                      {t.deleteConversation}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <div className="flex min-w-0 flex-col">
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
                    disabled={uiLocked}
                    className="rounded-lg border border-border px-3 py-2 text-left text-sm hover:bg-muted disabled:opacity-50"
                    onClick={() => void ask(item)}
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
                    ) : turn.refused ? (
                      <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3">
                        <p className="font-medium text-destructive">{t.refusalTitle}</p>
                        <p className="mt-1 text-sm text-muted-foreground">{turn.answer}</p>
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
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">{t.independentTurnNote}</p>
              <Button type="submit" disabled={uiLocked || !draft.trim()}>
                {busy ? t.asking : t.send}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </Shell>
  );
}
