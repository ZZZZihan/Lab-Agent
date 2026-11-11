"use client";

import Link from "next/link";
import { Children, type ReactNode, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "cn";
import { type Citation, type LabPersonItem } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

import {
  type ActiveEvidenceSelection,
  type MatchedPersonCardData,
  type Turn,
  extractMatchedPeopleForTurn,
} from "./chatTypesAndHelpers";

function renderInlineCitations(
  text: string,
  turnId: string,
  citations: Citation[],
  activeEvidence: ActiveEvidenceSelection | null,
  onSelectCitation: (turnId: string, citeIndex: number, citation?: Citation) => void,
): ReactNode[] {
  const parts = text.split(/(\[[0-9]{1,2}\])/g);
  return parts.map((part, idx) => {
    const match = /^\[([0-9]{1,2})\]$/.exec(part);
    if (!match) return <span key={idx}>{part}</span>;
    const num = Number(match[1]);
    const targetId = `citation-${turnId}-${num}`;
    const matchedCitation = citations.find((c, i) => (c.index ?? i + 1) === num);
    const isSelected =
      activeEvidence?.turnId === turnId && activeEvidence?.citeIndex === num;

    return (
      <sup key={idx} className="mx-0.5 inline-flex align-baseline">
        <button
          type="button"
          onClick={() => {
            onSelectCitation(turnId, num, matchedCitation);
            const el = document.getElementById(targetId);
            el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }}
          className={`inline-flex h-[18px] min-w-[22px] cursor-pointer items-center justify-center rounded-md px-1 font-mono text-[11px] font-medium tabular-nums transition-colors ${
            isSelected
              ? "bg-primary text-primary-foreground shadow-xs"
              : "bg-stone-200/80 text-primary hover:bg-primary/15 dark:bg-stone-800 dark:text-emerald-300"
          }`}
          aria-label={`查看引用依据 [${num}]`}
          title={
            matchedCitation
              ? `${matchedCitation.title || "依据"}${matchedCitation.heading ? ` · ${matchedCitation.heading}` : ""}`
              : `引用 [${num}]`
          }
        >
          [{num}]
        </button>
      </sup>
    );
  });
}

const REMARK_PLUGINS = [remarkGfm];

function EditorialProse({
  answer,
  turnId,
  citations,
  activeEvidence,
  onSelectCitation,
}: {
  answer: string;
  turnId: string;
  citations: Citation[];
  activeEvidence: ActiveEvidenceSelection | null;
  onSelectCitation: (turnId: string, citeIndex: number, citation?: Citation) => void;
}) {
  const trimmed = answer.trim();

  const components = useMemo<Components>(() => {
    const withCitations = (children: ReactNode): ReactNode =>
      Children.map(children, (child) =>
        typeof child === "string"
          ? renderInlineCitations(
              child,
              turnId,
              citations,
              activeEvidence,
              onSelectCitation,
            )
          : child,
      );

    return {
      p: ({ children }) => (
        <p className="whitespace-pre-wrap leading-[1.75]">
          {withCitations(children)}
        </p>
      ),
      h1: ({ children }) => (
        <h3 className="mt-4 font-display text-lg font-semibold text-stone-900 first:mt-0 dark:text-stone-100">
          {withCitations(children)}
        </h3>
      ),
      h2: ({ children }) => (
        <h3 className="mt-4 font-display text-base font-semibold text-stone-900 first:mt-0 dark:text-stone-100">
          {withCitations(children)}
        </h3>
      ),
      h3: ({ children }) => (
        <h4 className="mt-3 font-display text-[15px] font-semibold text-stone-900 first:mt-0 dark:text-stone-100">
          {withCitations(children)}
        </h4>
      ),
      h4: ({ children }) => (
        <h5 className="mt-2.5 text-sm font-semibold text-stone-900 first:mt-0 dark:text-stone-100">
          {withCitations(children)}
        </h5>
      ),
      ul: ({ className, children }) => (
        <ul className={cn("list-disc space-y-1.5 pl-5", className)}>
          {children}
        </ul>
      ),
      ol: ({ start, className, children }) => (
        <ol
          start={start}
          className={cn("list-decimal space-y-1.5 pl-5", className)}
        >
          {children}
        </ol>
      ),
      li: ({ className, children }) => (
        <li className={cn("leading-[1.7]", className)}>
          {withCitations(children)}
        </li>
      ),
      strong: ({ children }) => (
        <strong className="font-semibold text-stone-900 dark:text-stone-100">
          {withCitations(children)}
        </strong>
      ),
      em: ({ children }) => <em className="italic">{withCitations(children)}</em>,
      del: ({ children }) => (
        <del className="line-through">{withCitations(children)}</del>
      ),
      blockquote: ({ children }) => (
        <blockquote className="border-l-2 border-primary/40 pl-3.5 text-stone-600 italic dark:border-emerald-400/40 dark:text-stone-300">
          {withCitations(children)}
        </blockquote>
      ),
      a: ({ href, children }) => (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium text-primary underline underline-offset-2 dark:text-emerald-300"
        >
          {withCitations(children)}
        </a>
      ),
      pre: ({ children }) => (
        <pre className="overflow-x-auto rounded-lg border border-stone-200/90 bg-stone-100/80 p-3 font-mono text-xs leading-relaxed text-stone-800 dark:border-stone-800 dark:bg-stone-900/80 dark:text-stone-200">
          {children}
        </pre>
      ),
      code: ({ className, children }) => {
        const isBlock = Boolean(
          (className && className.includes("language-")) ||
            String(children).includes("\n"),
        );
        if (isBlock) {
          return <code className={className}>{children}</code>;
        }
        return (
          <code className="rounded bg-stone-200/70 px-1.5 py-0.5 font-mono text-[13px] text-stone-800 dark:bg-stone-800 dark:text-stone-200">
            {children}
          </code>
        );
      },
      table: ({ children }) => (
        <div className="overflow-x-auto rounded-lg border border-stone-200/90 dark:border-stone-800">
          <table className="w-full border-collapse text-left text-xs">
            {children}
          </table>
        </div>
      ),
      thead: ({ children }) => (
        <thead className="border-b border-stone-200/90 bg-stone-100/70 text-stone-700 dark:border-stone-800 dark:bg-stone-900/60 dark:text-stone-300">
          {children}
        </thead>
      ),
      th: ({ style, children }) => (
        <th style={style} className="px-3 py-2 font-semibold">
          {withCitations(children)}
        </th>
      ),
      td: ({ style, children }) => (
        <td
          style={style}
          className="border-t border-stone-200/70 px-3 py-2 align-top dark:border-stone-800/80"
        >
          {withCitations(children)}
        </td>
      ),
      hr: () => (
        <hr className="border-stone-200/80 dark:border-stone-800" />
      ),
    };
  }, [turnId, citations, activeEvidence, onSelectCitation]);

  if (!trimmed) {
    return null;
  }

  return (
    <div
      lang="zh"
      className="max-w-[68ch] space-y-3.5 text-[15px] leading-[1.75] text-stone-800 dark:text-stone-200"
    >
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components}>
        {trimmed}
      </ReactMarkdown>
    </div>
  );
}

function MemberDossierCardList({
  people,
  copiedPersonId,
  basketKeySet,
  onCopyGrantFormat,
  onToggleBasket,
}: {
  people: MatchedPersonCardData[];
  copiedPersonId: string | null;
  basketKeySet: Set<string>;
  onCopyGrantFormat: (person: MatchedPersonCardData) => void;
  onToggleBasket: (person: MatchedPersonCardData) => void;
}) {
  if (people.length === 0) return null;

  return (
    <section
      aria-label="课题组成员专长档案"
      className="overflow-hidden rounded-[14px] border border-stone-200/90 bg-white shadow-[0_1px_3px_rgba(28,25,23,0.05)] dark:border-stone-800 dark:bg-[#22221F]"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-200/70 bg-[#F6F4EE]/80 px-4 py-2.5 dark:border-stone-800 dark:bg-stone-900/60">
        <div className="flex items-center gap-2">
          <span className="inline-flex h-5 w-5 items-center justify-center rounded-md bg-primary/10 text-xs text-primary dark:bg-emerald-500/15 dark:text-emerald-300">
            👥
          </span>
          <h3 className="text-xs font-semibold tracking-wide text-stone-700 dark:text-stone-200">
            课题组成员专长档案
          </h3>
          <span className="rounded-md bg-stone-200/70 px-1.5 py-0.5 text-[11px] text-stone-600 dark:bg-stone-800 dark:text-stone-400">
            自适应结构化卡片
          </span>
        </div>
        <span className="font-mono text-[11px] tabular-nums text-stone-500 dark:text-stone-400">
          关联 {people.length} 位课题组成员
        </span>
      </div>

      <div className="divide-y divide-stone-200/70 dark:divide-stone-800/80">
        {people.map((person) => {
          const isCopied = copiedPersonId === person.id;
          const inBasket = basketKeySet.has(`person:${person.id}`);

          return (
            <div
              key={person.id}
              className="flex flex-col gap-3 px-4 py-3.5 transition-colors hover:bg-stone-50/60 dark:hover:bg-stone-800/20"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="flex flex-wrap items-baseline gap-2.5">
                  <span className="font-display text-base font-semibold text-stone-900 dark:text-stone-100">
                    {person.name}
                  </span>
                  <span className="text-xs font-medium text-stone-600 dark:text-stone-400">
                    {person.roleSubtitle}
                  </span>
                </div>
                <span
                  className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-[11px] tabular-nums ${
                    person.hasSignature
                      ? "bg-emerald-950/6 text-primary ring-1 ring-primary/20 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-400/20"
                      : "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-400"
                  }`}
                >
                  {person.signatureLabel}
                </span>
              </div>

              <div className="flex flex-col gap-1.5 text-xs text-stone-600 dark:text-stone-300 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium text-stone-500 dark:text-stone-400">
                    研究专长:
                  </span>
                  <span className="font-medium text-stone-800 dark:text-stone-200">
                    {person.specialtyText}
                  </span>
                </div>

                <div className="flex flex-wrap items-center gap-2 pt-1 sm:pt-0">
                  <button
                    type="button"
                    onClick={() => onCopyGrantFormat(person)}
                    className={`inline-flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-[10px] border px-2.5 py-1 text-xs font-medium transition-colors ${
                      isCopied
                        ? "border-primary/40 bg-primary/10 text-primary dark:border-emerald-500/40 dark:bg-emerald-500/15 dark:text-emerald-300"
                        : "border-stone-200/90 bg-[#FAF9F6] text-stone-700 hover:border-stone-300 hover:bg-stone-100/80 dark:border-stone-700 dark:bg-stone-800/80 dark:text-stone-200 dark:hover:bg-stone-800"
                    }`}
                  >
                    <span>{isCopied ? "✓" : "📋"}</span>
                    <span>{isCopied ? "已复制申报格式" : "复制申报格式"}</span>
                  </button>

                  <button
                    type="button"
                    onClick={() => onToggleBasket(person)}
                    className={`inline-flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-[10px] border px-2.5 py-1 text-xs font-medium transition-colors ${
                      inBasket
                        ? "border-primary bg-primary text-primary-foreground shadow-xs"
                        : "border-stone-200/90 bg-[#FAF9F6] text-stone-700 hover:border-stone-300 hover:bg-stone-100/80 dark:border-stone-700 dark:bg-stone-800/80 dark:text-stone-200 dark:hover:bg-stone-800"
                    }`}
                  >
                    <span>{inBasket ? "✓" : "🧺"}</span>
                    <span>{inBasket ? "已加入素材篮" : "加入素材篮"}</span>
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function AcademicBoundaryMemo({
  turn,
  onSelectCitation,
}: {
  turn: Turn;
  onSelectCitation: (turnId: string, citeIndex: number, citation?: Citation) => void;
}) {
  const { t } = useI18n();
  const [diagOpen, setDiagOpen] = useState(false);
  const isModelDeclined = turn.refusal?.reason === "model_declined";
  const neighbors = turn.refusal?.neighbors || [];

  return (
    <div className="rounded-[14px] border border-stone-200/90 bg-[#F5F3EE] px-4 py-3.5 dark:border-stone-800 dark:bg-[#20201D]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-600/80 dark:bg-amber-400" />
            <span className="text-xs font-medium text-stone-600 dark:text-stone-400">
              {isModelDeclined ? t.modelDeclinedTitle : "知识库证据边界备忘"}
            </span>
          </div>
          <p className="text-sm leading-[1.7] text-stone-800 dark:text-stone-200">
            {t.refusalTitle}
            <span className="ml-1 text-stone-500 dark:text-stone-400">
              {isModelDeclined
                ? t.modelDeclinedBody
                : "为保证科研数据严谨性，系统未在缺乏直接依据时生成推测性结论。"}
            </span>
          </p>
        </div>

        <button
          type="button"
          onClick={() => setDiagOpen((prev) => !prev)}
          className="shrink-0 cursor-pointer rounded-md px-2 py-1 font-mono text-[11px] text-stone-500 transition-colors hover:bg-stone-200/60 hover:text-stone-700 dark:text-stone-400 dark:hover:bg-stone-800 dark:hover:text-stone-200"
        >
          {diagOpen ? "[收起技术诊断]" : "[查看技术诊断]"}
        </button>
      </div>

      {diagOpen ? (
        <div className="mt-3 space-y-2.5 border-t border-stone-200/80 pt-3 text-xs text-stone-600 dark:border-stone-800 dark:text-stone-400">
          {turn.refusal ? (
            <div className="flex flex-wrap items-center gap-3 font-mono text-[11px] tabular-nums">
              <span>
                {t.score}: {Number(turn.refusal.score || 0).toFixed(2)}
              </span>
              <span>·</span>
              <span>
                {t.threshold}: {Number(turn.refusal.threshold || 0).toFixed(2)}
              </span>
              <span>·</span>
              <span>
                {t.coverage}: {Number(turn.refusal.coverage || 0).toFixed(2)}
              </span>
              {turn.refusal.reason ? (
                <>
                  <span>·</span>
                  <span>判定原因: {String(turn.refusal.reason)}</span>
                </>
              ) : null}
            </div>
          ) : (
            <p className="text-xs text-stone-500">{turn.answer}</p>
          )}

          {neighbors.length > 0 ? (
            <div className="space-y-1.5">
              <p className="text-[11px] text-stone-500">{t.refusalBody}</p>
              <div className="grid gap-1.5">
                {neighbors.map((nb, idx) => {
                  const citeIdx = nb.index ?? idx + 1;
                  return (
                    <div
                      key={`${nb.chunk_id || idx}`}
                      onClick={() => onSelectCitation(turn.id, citeIdx, nb)}
                      className="cursor-pointer rounded-lg border border-stone-200/80 bg-white/80 px-3 py-2 text-xs transition-colors hover:border-stone-300 dark:border-stone-800 dark:bg-stone-900/50"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-medium text-stone-700 dark:text-stone-300">
                          [{citeIdx}] {nb.title || "相邻文档"}
                          {nb.heading ? ` · ${nb.heading}` : ""}
                        </span>
                        {typeof nb.score === "number" ? (
                          <span className="font-mono text-[11px] tabular-nums text-stone-400">
                            {nb.score.toFixed(2)}
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-1 line-clamp-2 text-stone-600 dark:text-stone-400">
                        {nb.excerpt}
                      </p>
                      {nb.document_id ? (
                        <Link
                          href={`/documents/${nb.document_id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="mt-1 inline-block text-[11px] text-primary underline"
                        >
                          {t.openDoc}
                        </Link>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function SmartTurnBlock({
  turn,
  labPeople,
  copiedPersonId,
  basketKeySet,
  activeEvidence,
  onSelectCitation,
  onCopyGrantFormat,
  onToggleBasket,
}: {
  turn: Turn;
  labPeople: LabPersonItem[];
  copiedPersonId: string | null;
  basketKeySet: Set<string>;
  activeEvidence: ActiveEvidenceSelection | null;
  onSelectCitation: (turnId: string, citeIndex: number, citation?: Citation) => void;
  onCopyGrantFormat: (person: MatchedPersonCardData) => void;
  onToggleBasket: (person: MatchedPersonCardData) => void;
}) {
  const { t } = useI18n();
  const matchedPeople = extractMatchedPeopleForTurn(turn, labPeople);

  return (
    <article className="space-y-4 border-b border-stone-200/70 pb-7 last:border-b-0 dark:border-stone-800/80">
      {turn.question ? (
        <header className="flex items-start gap-2.5">
          <span className="mt-1 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-stone-200/80 font-mono text-[11px] font-medium text-stone-700 dark:bg-stone-800 dark:text-stone-300">
            Q
          </span>
          <h2 className="font-display text-lg font-semibold leading-snug text-stone-900 dark:text-stone-100">
            {turn.question}
          </h2>
        </header>
      ) : null}

      {turn.refused ? (
        <AcademicBoundaryMemo turn={turn} onSelectCitation={onSelectCitation} />
      ) : (
        <div className="space-y-4 pl-0 sm:pl-7">
          {turn.pending && !turn.answer ? (
            <div className="flex items-center gap-2.5 py-2 text-sm text-stone-500 dark:text-stone-400">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-primary" />
              <span>{turn.phase || t.asking}</span>
            </div>
          ) : (
            <EditorialProse
              answer={turn.answer}
              turnId={turn.id}
              citations={turn.citations}
              activeEvidence={activeEvidence}
              onSelectCitation={onSelectCitation}
            />
          )}

          {/* Smart Structured Block: Member Dossier Cards */}
          {matchedPeople.length > 0 ? (
            <MemberDossierCardList
              people={matchedPeople}
              copiedPersonId={copiedPersonId}
              basketKeySet={basketKeySet}
              onCopyGrantFormat={onCopyGrantFormat}
              onToggleBasket={onToggleBasket}
            />
          ) : null}

          {/* Citation Verification Notes */}
          {turn.citationCheck?.invalid && turn.citationCheck.invalid.length > 0 ? (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              {t.invalidCitationsNote}
              {turn.citationCheck.invalid.map((n) => `[${n}]`).join(", ")}
            </p>
          ) : null}
          {turn.citationCheck?.uncited && turn.citations.length > 0 ? (
            <p className="text-xs text-stone-500 dark:text-stone-400">
              {t.uncitedNote}
            </p>
          ) : null}
        </div>
      )}

      {turn.paperStatus === "not_configured" ? (
        <p className="pl-0 text-xs text-stone-500 sm:pl-7 dark:text-stone-400">
          {t.paperNote}
        </p>
      ) : null}
      {turn.error ? (
        <p className="pl-0 text-sm text-destructive sm:pl-7">{turn.error}</p>
      ) : null}
    </article>
  );
}
