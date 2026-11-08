"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { type Citation } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

import {
  COMPACT_RAIL_SUGGESTIONS,
  type ActiveEvidenceSelection,
  type ConversationSummary,
  type Turn,
  formatSourcePillTitle,
} from "./chatTypesAndHelpers";

export function CopilotInspectorRail({
  conversations,
  conversationId,
  turns,
  draft,
  busy,
  uiLocked,
  activeEvidence,
  onDraftChange,
  onAsk,
  onSelectConversation,
  onStartNewConversation,
  onDeleteConversation,
  onSelectEvidence,
}: {
  conversations: ConversationSummary[];
  conversationId: string | null;
  turns: Turn[];
  draft: string;
  busy: boolean;
  uiLocked: boolean;
  activeEvidence: ActiveEvidenceSelection | null;
  onDraftChange: (value: string) => void;
  onAsk: (question: string) => void;
  onSelectConversation: (id: string) => void;
  onStartNewConversation: () => void;
  onDeleteConversation: (id: string) => void;
  onSelectEvidence: (selection: ActiveEvidenceSelection | null) => void;
}) {
  const { t } = useI18n();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [menuOpenConvId, setMenuOpenConvId] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-resize textarea height based on content
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const nextHeight = Math.min(Math.max(el.scrollHeight, 56), 160);
    el.style.height = `${nextHeight}px`;
  }, [draft]);

  // Latest turn for live retrieval telemetry
  const latestTurn = turns.length > 0 ? turns[turns.length - 1] : null;

  // Gather all citations across turns (prioritizing the latest turn, or all turns in canvas)
  const evidenceEntries: Array<{
    turnId: string;
    citeIndex: number;
    citation: Citation;
  }> = [];

  if (turns.length > 0) {
    // Show citations from all turns in reverse chronological order so latest turn's pills are at top
    for (let tIdx = turns.length - 1; tIdx >= 0; tIdx -= 1) {
      const turn = turns[tIdx];
      const usedSet =
        turn.citationCheck && turn.citationCheck.used.length > 0
          ? new Set(turn.citationCheck.used)
          : null;
      const sourceCitations =
        turn.refused && turn.refusal?.neighbors
          ? turn.refusal.neighbors
          : usedSet
            ? turn.citations.filter((c, i) => usedSet.has(c.index ?? i + 1))
            : turn.citations;

      sourceCitations.forEach((cit, idx) => {
        const citeIndex = cit.index ?? idx + 1;
        evidenceEntries.push({
          turnId: turn.id,
          citeIndex,
          citation: cit,
        });
      });
    }
  }

  // Build hit summary text for Live Retrieval Inspector (cap to top 2 whole segments so it never truncates mid-word)
  const hitSummaryItems: string[] = [];
  if (latestTurn?.retrieval?.hits && latestTurn.retrieval.hits.length > 0) {
    for (const h of latestTurn.retrieval.hits.slice(0, 2)) {
      const shortName = formatSourcePillTitle({
        title: h.title,
        heading: h.heading,
      });
      hitSummaryItems.push(
        typeof h.score === "number"
          ? `${shortName} (${h.score.toFixed(2)})`
          : shortName,
      );
    }
  } else if (evidenceEntries.length > 0) {
    for (const entry of evidenceEntries.slice(0, 2)) {
      const shortName = formatSourcePillTitle(entry.citation);
      hitSummaryItems.push(
        typeof entry.citation.score === "number"
          ? `${shortName} (${entry.citation.score.toFixed(2)})`
          : shortName,
      );
    }
  }

  const modeLabel =
    latestTurn?.retrieval?.mode === "hybrid_rrf_rerank"
      ? "Hybrid RRF + Rerank (4096维)"
      : "Hybrid RRF (4096维)";

  return (
    <aside
      aria-label="智能探针与证据检索流"
      className="flex flex-col justify-between gap-4 rounded-[16px] border border-stone-200/85 bg-[#F3F1EC]/90 p-4 dark:border-stone-800 dark:bg-[#1E1E1B] lg:sticky lg:top-5 lg:max-h-[calc(100vh-6.5rem)] lg:overflow-y-auto"
    >
      <div className="space-y-4">
        {/* Rail Header */}
        <div className="flex items-center justify-between gap-2 border-b border-stone-200/80 pb-3 dark:border-stone-800">
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-primary dark:text-emerald-400">✦</span>
            <h2 className="text-xs font-semibold tracking-wide text-stone-700 dark:text-stone-200">
              智能探针与证据检索流
            </h2>
          </div>
          <span className="font-mono text-[11px] text-stone-400 dark:text-stone-500">
            Inspector
          </span>
        </div>

        {/* 1. Compact Session Switcher */}
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <button
              type="button"
              onClick={() => setHistoryOpen((prev) => !prev)}
              className="inline-flex min-h-[34px] cursor-pointer items-center gap-1.5 rounded-[10px] border border-stone-200/90 bg-[#FAF9F6] px-2.5 py-1 text-xs font-medium text-stone-700 transition-colors hover:bg-stone-200/50 dark:border-stone-700/80 dark:bg-stone-800/80 dark:text-stone-200 dark:hover:bg-stone-800"
              aria-expanded={historyOpen}
            >
              <span>💬 {t.conversationsTitle}</span>
              <span className="rounded bg-stone-200/80 px-1.5 py-0.2 font-mono text-[10px] tabular-nums text-stone-600 dark:bg-stone-700 dark:text-stone-300">
                {conversations.length}
              </span>
              <span className="text-[10px] text-stone-400">
                {historyOpen ? "▴" : "▾"}
              </span>
            </button>

            <button
              type="button"
              disabled={uiLocked}
              onClick={onStartNewConversation}
              className="inline-flex min-h-[34px] cursor-pointer items-center gap-1 rounded-[10px] border border-stone-200/90 bg-[#FAF9F6] px-2.5 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/8 disabled:opacity-50 dark:border-stone-700/80 dark:bg-stone-800/80 dark:text-emerald-300"
            >
              <span>+</span>
              <span>新建探索</span>
            </button>
          </div>

          {historyOpen ? (
            <div className="max-h-48 overflow-y-auto rounded-[12px] border border-stone-200/80 bg-[#FAF9F6] p-1.5 shadow-xs dark:border-stone-800 dark:bg-[#181816]">
              {conversations.length === 0 ? (
                <p className="px-2.5 py-3 text-center text-xs text-stone-400">
                  {t.noConversations}
                </p>
              ) : (
                <ul className="space-y-0.5">
                  {conversations.map((conv) => {
                    const active = conv.id === conversationId;
                    const menuOpen = menuOpenConvId === conv.id;
                    return (
                      <li
                        key={conv.id}
                        className={`group relative flex items-center justify-between gap-1 rounded-[8px] px-2.5 py-1.5 text-xs transition-colors ${
                          active
                            ? "bg-stone-200/80 font-medium text-stone-900 dark:bg-stone-800 dark:text-stone-100"
                            : "text-stone-600 hover:bg-stone-200/45 dark:text-stone-300 dark:hover:bg-stone-800/50"
                        }`}
                      >
                        <button
                          type="button"
                          disabled={uiLocked}
                          onClick={() => {
                            onSelectConversation(conv.id);
                            setMenuOpenConvId(null);
                          }}
                          title={conv.title}
                          className="min-w-0 flex-1 cursor-pointer truncate text-left"
                        >
                          {conv.title}
                        </button>

                        <div className="flex shrink-0 items-center gap-1">
                          {menuOpen ? (
                            <button
                              type="button"
                              disabled={uiLocked}
                              onClick={() => {
                                setMenuOpenConvId(null);
                                onDeleteConversation(conv.id);
                              }}
                              aria-label={t.deleteConversation}
                              className="cursor-pointer rounded px-1.5 py-0.5 text-[11px] font-medium text-stone-600 hover:bg-stone-300/60 hover:text-destructive dark:text-stone-300 dark:hover:bg-stone-700"
                            >
                              {t.deleteConversation}
                            </button>
                          ) : null}
                          <button
                            type="button"
                            disabled={uiLocked}
                            onClick={() =>
                              setMenuOpenConvId((prev) =>
                                prev === conv.id ? null : conv.id,
                              )
                            }
                            aria-label={`会话操作 ${conv.title}`}
                            className="cursor-pointer rounded px-1.5 py-0.5 font-mono text-xs text-stone-400 opacity-75 transition-opacity hover:bg-stone-200/70 hover:text-stone-700 group-hover:opacity-100 dark:hover:bg-stone-700 dark:hover:text-stone-200"
                          >
                            ···
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          ) : null}
        </div>

        {/* 2. Live Retrieval Inspector */}
        <section
          aria-label="实时检索穿透监视"
          className="rounded-[12px] border border-stone-200/80 bg-[#FAF9F6] p-3 dark:border-stone-800 dark:bg-[#181816]"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5">
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  busy
                    ? "animate-ping bg-primary"
                    : "bg-primary dark:bg-emerald-400"
                }`}
              />
              <h3 className="text-xs font-semibold text-stone-700 dark:text-stone-200">
                ⚡ 实时检索穿透监视
              </h3>
            </div>
            <span className="font-mono text-[10px] tabular-nums text-stone-400">
              {latestTurn?.retrieval?.elapsedMs
                ? `${latestTurn.retrieval.elapsedMs.toFixed(0)} ms`
                : "Live Retrieval"}
            </span>
          </div>

          {busy ? (
            <div className="mt-2.5 flex items-center gap-2 rounded-lg bg-primary/8 px-2.5 py-1.5 text-xs text-primary dark:bg-emerald-500/10 dark:text-emerald-300">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
              <span>{latestTurn?.phase || "正在检索课题组资料库..."}</span>
            </div>
          ) : null}

          <ul className="mt-2.5 space-y-1.5 text-xs text-stone-600 dark:text-stone-400">
            <li className="flex items-baseline justify-between gap-2">
              <span className="text-stone-500 dark:text-stone-400">• 模式:</span>
              <span className="font-mono text-[11px] font-medium text-stone-800 dark:text-stone-200">
                {modeLabel}
              </span>
            </li>
            <li className="flex items-baseline justify-between gap-2">
              <span className="shrink-0 text-stone-500 dark:text-stone-400">
                • 命中:
              </span>
              <span className="text-right font-mono text-[11px] tabular-nums text-stone-700 dark:text-stone-300">
                {hitSummaryItems.length > 0
                  ? hitSummaryItems.join(", ")
                  : "等待提问检索"}
              </span>
            </li>
          </ul>
        </section>

        {/* 3. Source Evidence Pills & In-Rail Evidence Highlight Drawer */}
        <section aria-label="来源证据胶囊" className="space-y-2.5">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold text-stone-700 dark:text-stone-200">
              📄 来源证据胶囊
            </h3>
            <span className="font-mono text-[11px] tabular-nums text-stone-400">
              {evidenceEntries.length > 0
                ? `${evidenceEntries.length} 条命中依据`
                : "点击胶囊透视原文"}
            </span>
          </div>

          {evidenceEntries.length === 0 ? (
            <div className="rounded-[12px] border border-dashed border-stone-200/90 bg-[#FAF9F6]/60 px-3 py-4 text-center text-xs text-stone-400 dark:border-stone-800 dark:bg-stone-900/30">
              提问后此处将实时生成可溯源的证据胶囊，点击可在栏内直接核对原文段落。
            </div>
          ) : (
            <div className="flex flex-col gap-1.5">
              {evidenceEntries.map(({ turnId, citeIndex, citation }, idx) => {
                const pillTitle = formatSourcePillTitle(citation);
                const scoreText =
                  typeof citation.score === "number"
                    ? citation.score.toFixed(2)
                    : null;
                const isSelected =
                  activeEvidence?.turnId === turnId &&
                  activeEvidence?.citeIndex === citeIndex;

                return (
                  <div
                    key={`${turnId}-${citation.chunk_id || citeIndex}-${idx}`}
                    id={`citation-${turnId}-${citeIndex}`}
                    className="space-y-1.5"
                  >
                    <button
                      type="button"
                      onClick={() =>
                        onSelectEvidence(
                          isSelected
                            ? null
                            : { turnId, citeIndex, citation },
                        )
                      }
                      className={`flex w-full cursor-pointer items-center justify-between gap-2 rounded-[10px] border px-3 py-2 text-left text-xs transition-colors ${
                        isSelected
                          ? "border-primary/50 bg-primary/10 text-stone-900 shadow-xs dark:border-emerald-400/50 dark:bg-emerald-500/15 dark:text-stone-100"
                          : "border-stone-200/90 bg-[#FAF9F6] text-stone-700 hover:border-stone-300 hover:bg-white dark:border-stone-800 dark:bg-[#181816] dark:text-stone-200 dark:hover:bg-stone-900"
                      }`}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="inline-flex h-5 min-w-[22px] shrink-0 items-center justify-center rounded bg-stone-200/80 px-1 font-mono text-[11px] font-semibold tabular-nums text-stone-700 dark:bg-stone-800 dark:text-stone-300">
                          {idx + 1}.
                        </span>
                        <span className="truncate font-medium">
                          {pillTitle}
                        </span>
                      </div>

                      {scoreText ? (
                        <span className="shrink-0 rounded bg-emerald-950/6 px-1.5 py-0.5 font-mono text-[11px] font-medium tabular-nums text-primary dark:bg-emerald-500/10 dark:text-emerald-300">
                          [{scoreText}]
                        </span>
                      ) : null}
                    </button>

                    {/* In-Rail Slide-out Evidence Excerpt Drawer */}
                    {isSelected ? (
                      <div className="rounded-[12px] border border-stone-200/90 bg-white p-3 text-xs shadow-xs dark:border-stone-800 dark:bg-[#22221F]">
                        <div className="flex items-start justify-between gap-2 border-b border-stone-100 pb-2 dark:border-stone-800">
                          <div className="space-y-0.5">
                            <p className="font-semibold text-stone-800 dark:text-stone-100">
                              {citation.title || pillTitle}
                            </p>
                            {citation.heading ? (
                              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                                章节: {citation.heading}
                              </p>
                            ) : null}
                          </div>
                          <button
                            type="button"
                            onClick={() => onSelectEvidence(null)}
                            className="cursor-pointer rounded px-1.5 py-0.5 text-[11px] text-stone-400 hover:bg-stone-100 hover:text-stone-700 dark:hover:bg-stone-800"
                          >
                            收起
                          </button>
                        </div>

                        <p className="mt-2 whitespace-pre-wrap leading-[1.7] text-stone-700 dark:text-stone-300">
                          {citation.excerpt || "（无摘要片段）"}
                        </p>

                        {citation.document_id ? (
                          <div className="mt-2.5 flex items-center justify-end border-t border-stone-100 pt-2 dark:border-stone-800">
                            <Link
                              href={`/documents/${citation.document_id}`}
                              className="inline-flex items-center gap-1 font-medium text-primary underline hover:opacity-80 dark:text-emerald-300"
                            >
                              <span>{t.openDoc}</span>
                              <span>↗</span>
                            </Link>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>

      {/* 4. Floating Composer Island */}
      <div className="space-y-2.5 pt-2">
        {/* Quick Suggestion Chips */}
        <div className="space-y-1.5">
          <p className="text-[11px] font-medium text-stone-500 dark:text-stone-400">
            快捷探索建议
          </p>
          <div className="flex flex-wrap gap-1.5">
            {COMPACT_RAIL_SUGGESTIONS.map((item) => (
              <button
                key={item.label}
                type="button"
                disabled={uiLocked}
                onClick={() => onAsk(item.prompt)}
                className="cursor-pointer rounded-full border border-stone-200/90 bg-[#FAF9F6] px-2.5 py-1 text-left text-[11px] text-stone-600 transition-colors hover:border-primary/40 hover:bg-white hover:text-stone-900 disabled:opacity-50 dark:border-stone-800 dark:bg-[#181816] dark:text-stone-300 dark:hover:border-emerald-500/40"
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        {/* Floating Composer Form */}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onAsk(draft);
          }}
          className="rounded-2xl border border-stone-300/80 bg-white p-3 shadow-[0_4px_20px_rgba(28,25,23,0.06)] transition-colors focus-within:border-primary/60 dark:border-stone-700/80 dark:bg-[#22221F]"
        >
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.nativeEvent.isComposing ||
                event.keyCode === 229
              ) {
                return;
              }
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                if (!uiLocked && draft.trim()) {
                  onAsk(draft);
                }
              }
            }}
            placeholder="向课题组知识库提问…"
            rows={2}
            disabled={uiLocked}
            className="w-full resize-none bg-transparent text-sm leading-6 text-stone-900 placeholder:text-stone-400 focus:outline-none disabled:opacity-50 dark:text-stone-100"
          />

          <div className="mt-2 flex items-center justify-between gap-2 border-t border-stone-100 pt-2 dark:border-stone-800/80">
            <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-stone-500 dark:text-stone-400">
              <span className="text-primary dark:text-emerald-400">✦</span>
              <span>{busy ? t.asking : "4096维混合检索就绪"}</span>
            </span>

            <button
              type="submit"
              disabled={uiLocked || !draft.trim()}
              aria-label={busy ? t.asking : t.send}
              className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground shadow-xs transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-35"
            >
              ↑
            </button>
          </div>
        </form>

        <div className="flex items-center justify-between px-1 text-[11px] text-stone-400 dark:text-stone-500">
          <span>Enter 发送 · Shift+Enter 换行</span>
          <span>独立核验每轮证据</span>
        </div>
      </div>
    </aside>
  );
}
