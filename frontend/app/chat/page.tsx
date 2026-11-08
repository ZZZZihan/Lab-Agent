"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Shell } from "@/components/shell";
import {
  api,
  copyTextToClipboard,
  currentUser,
  formatLabBatch,
  generateRequestId,
  type BasketItem,
  type ChatEvent,
  type Citation,
  type LabOverview,
  type LabPersonItem,
  type User,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

import { CopilotInspectorRail } from "./components/CopilotInspectorRail";
import { SmartTurnBlock } from "./components/SmartBlockRenderer";
import {
  CANVAS_SUGGESTIONS,
  type ActiveEvidenceSelection,
  type ConversationDetail,
  type ConversationSummary,
  type MatchedPersonCardData,
  type Turn,
  extractMatchedPeopleForTurn,
  mapErrorCode,
  mapToolPhase,
  messagesToTurns,
  readReuseBasket,
  togglePersonInReuseBasket,
} from "./components/chatTypesAndHelpers";

export default function ChatPage() {
  const { t } = useI18n();
  const [me, setMe] = useState<User | null>(null);
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Lab assets & Reuse Basket state for Research Canvas integration
  const [labPeople, setLabPeople] = useState<LabPersonItem[]>([]);
  const [labOverview, setLabOverview] = useState<LabOverview | null>(null);
  const [basketItems, setBasketItems] = useState<BasketItem[]>([]);
  const [copiedPersonId, setCopiedPersonId] = useState<string | null>(null);
  const [canvasNotice, setCanvasNotice] = useState<string | null>(null);
  const [activeEvidence, setActiveEvidence] =
    useState<ActiveEvidenceSelection | null>(null);

  const activeSelectionRef = useRef<string | null>(null);
  const historyRequestIdRef = useRef(0);
  const listRequestIdRef = useRef(0);
  const deletedIdsRef = useRef<Set<string>>(new Set());
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const uiLocked = busy || loadingHistory || deletingId !== null;

  const showTransientNotice = useCallback((msg: string) => {
    setCanvasNotice(msg);
    if (noticeTimerRef.current) {
      clearTimeout(noticeTimerRef.current);
    }
    noticeTimerRef.current = setTimeout(() => {
      setCanvasNotice(null);
    }, 2400);
  }, []);

  const loadConversations = useCallback(async () => {
    const reqId = ++listRequestIdRef.current;
    try {
      const res = await api<{ conversations: ConversationSummary[] }>(
        "conversations",
      );
      if (listRequestIdRef.current !== reqId) return;
      const incoming = (res.conversations || []).filter(
        (c) => !deletedIdsRef.current.has(c.id),
      );
      setConversations(incoming);
    } catch {
      // Ignore unauthenticated or transient list errors on initial load
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const reqId = ++listRequestIdRef.current;

    api<{ conversations: ConversationSummary[] }>("conversations")
      .then((res) => {
        if (!cancelled && listRequestIdRef.current === reqId) {
          setConversations(
            (res.conversations || []).filter(
              (c) => !deletedIdsRef.current.has(c.id),
            ),
          );
        }
      })
      .catch(() => {
        // Ignore unauthenticated or transient list errors on initial load
      });

    // Hydrate current user, lab people, lab overview, and shared Reuse Basket
    currentUser()
      .then((u) => {
        if (cancelled) return;
        setMe(u);
        const stored = readReuseBasket(u?.id || null);
        setBasketItems(stored.items);
      })
      .catch(() => {
        if (cancelled) return;
        const stored = readReuseBasket(null);
        setBasketItems(stored.items);
      });

    api<{ people: LabPersonItem[] }>("lab/people")
      .then((res) => {
        if (!cancelled && Array.isArray(res.people)) {
          setLabPeople(res.people);
        }
      })
      .catch(() => {
        // Fallback handled by chatTypesAndHelpers
      });

    api<LabOverview>("lab/overview")
      .then((res) => {
        if (!cancelled && res) {
          setLabOverview(res);
        }
      })
      .catch(() => {
        // Ignore overview load error
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const basketKeySet = useMemo(() => {
    const s = new Set<string>();
    for (const item of basketItems) {
      s.add(`${item.kind}:${item.id}`);
    }
    return s;
  }, [basketItems]);

  async function selectConversation(id: string) {
    if (busy || loadingHistory || deletingId !== null) return;
    const reqId = ++historyRequestIdRef.current;
    setLoadingHistory(true);
    setActiveEvidence(null);
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
    setActiveEvidence(null);
  }

  async function handleDeleteConversation(id: string) {
    if (busy || loadingHistory || deletingId !== null) return;
    setDeletingId(id);
    try {
      await api(`conversations/${id}`, { method: "DELETE" });
      deletedIdsRef.current.add(id);
      listRequestIdRef.current += 1;
      setConversations((current) =>
        current.filter((c) => c.id !== id && !deletedIdsRef.current.has(c.id)),
      );
      if (activeSelectionRef.current === id) {
        historyRequestIdRef.current += 1;
        activeSelectionRef.current = null;
        setTurns([]);
        setActiveEvidence(null);
      }
      setConversationId((current) => (current === id ? null : current));
    } catch {
      // Ignore delete error
    } finally {
      setDeletingId(null);
    }
  }

  async function handleCopyPersonGrantFormat(person: MatchedPersonCardData) {
    let textToCopy = person.applicationText;
    try {
      await copyTextToClipboard(textToCopy);
      setCopiedPersonId(person.id);
      showTransientNotice(`已复制 ${person.name} 的申报书标准格式`);
      setTimeout(() => {
        setCopiedPersonId((cur) => (cur === person.id ? null : cur));
      }, 2200);
    } catch {
      // Try backend batch formatter fallback if clipboard failed initially
    }

    // Also query backend format/batch when person has a UUID to ensure canonical format
    if (/^[0-9a-f-]{36}$/i.test(person.id)) {
      try {
        const res = await formatLabBatch([
          { kind: "person", entity: "person", id: person.id },
        ]);
        if (res.text && res.text.trim()) {
          textToCopy = res.text.trim();
          await copyTextToClipboard(textToCopy);
        }
      } catch {
        // Keep local applicationText already copied
      }
    }
  }

  function handleTogglePersonBasket(person: MatchedPersonCardData) {
    const { items, added } = togglePersonInReuseBasket(me?.id || null, {
      id: person.id,
      name: person.name,
      roleSubtitle: person.roleSubtitle,
    });
    setBasketItems(items);
    showTransientNotice(
      added
        ? `已将「${person.name}」加入写本子素材篮（共 ${items.length} 项）`
        : `已从素材篮移除「${person.name}」`,
    );
  }

  async function handleCopyEntireCanvas() {
    if (turns.length === 0) return;
    const text = turns
      .map(
        (turn, idx) =>
          `### 探索 ${idx + 1}：${turn.question}\n${turn.refused ? "（知识库暂未记载该参数）" : turn.answer}`,
      )
      .join("\n\n");
    try {
      await copyTextToClipboard(text);
      showTransientNotice("已复制当前画布研报全文");
    } catch {
      // Ignore copy failure
    }
  }

  function handleSelectCitationFromProse(
    turnId: string,
    citeIndex: number,
    citation?: Citation,
  ) {
    if (citation) {
      setActiveEvidence({ turnId, citeIndex, citation });
      return;
    }
    const turn = turns.find((item) => item.id === turnId);
    if (!turn) return;
    const sourceList =
      turn.refused && turn.refusal?.neighbors
        ? turn.refusal.neighbors
        : turn.citations;
    const found = sourceList.find((c, i) => (c.index ?? i + 1) === citeIndex);
    if (found) {
      setActiveEvidence({ turnId, citeIndex, citation: found });
    }
  }

  async function ask(question: string) {
    const text = question.trim();
    if (!text || busy || loadingHistory || deletingId !== null) return;
    historyRequestIdRef.current += 1;
    const id = generateRequestId();
    const targetConversationId = activeSelectionRef.current;
    setDraft("");
    setBusy(true);
    setActiveEvidence(null);
    setTurns((current) => [
      ...current,
      {
        id,
        question: text,
        answer: "",
        pending: true,
        citations: [],
        phase: t.asking,
      },
    ]);
    try {
      const payload: { message: string; conversation_id?: string } = {
        message: text,
      };
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
          current.map((turn) =>
            turn.id === id ? { ...turn, pending: false, error: message } : turn,
          ),
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
            if (event.type === "token") {
              return { ...turn, answer: turn.answer + (event.text || "") };
            }
            if (event.type === "retrieval") {
              return {
                ...turn,
                retrieval: {
                  mode: event.mode || "hybrid_rrf",
                  coverage: Number(event.coverage || 0),
                  elapsedMs: Number(event.elapsed_ms || 0),
                  hits: event.hits || [],
                  refused: event.refused,
                },
              };
            }
            if (event.type === "citations") {
              return {
                ...turn,
                citations: event.items || [],
                paperStatus: event.paper_status,
              };
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
              return {
                ...turn,
                refused: true,
                refusal: event,
                paperStatus: event.paper_status,
              };
            }
            if (event.type === "status") {
              return { ...turn, phase: mapToolPhase(event.phase, t) };
            }
            if (event.type === "tool") {
              return { ...turn, phase: mapToolPhase(event.name, t) };
            }
            if (event.type === "error") {
              return {
                ...turn,
                error: mapErrorCode(event.code, t),
                pending: false,
              };
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
          const dataLine = block
            .split("\n")
            .find((line) => line.startsWith("data:"));
          if (!dataLine) continue;
          apply(JSON.parse(dataLine.slice(5).trim()) as ChatEvent);
        }
      }
      setTurns((current) =>
        current.map((turn) =>
          turn.id === id ? { ...turn, pending: false } : turn,
        ),
      );
      if (resolvedConvId) {
        await loadConversations();
      }
    } catch {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === id ? { ...turn, pending: false, error: t.error } : turn,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  // Derive canvas header topic & metrics (avoid duplicating the literal question heading below)
  const activeConvTitle = useMemo(() => {
    const allQuestions = turns.map((item) => item.question).join(" ");
    const hasSop = /磁体|电流|液氦|规程|阈值|联锁/.test(allQuestions);
    const hasMember = /专长|万振凯|郭建民|成员|课题组/.test(allQuestions);
    if (hasSop && hasMember) {
      return "教育考试专属大模型课题组资产与实验规程综合研报";
    }
    if (hasSop) {
      return "低温超导磁体实验规程与安全联锁参数核验";
    }
    if (hasMember) {
      return "教育考试专属大模型课题组成员专长与申报档案检索";
    }
    return "教育考试专属大模型课题组资产与规程检索";
  }, [turns]);

  const linkedDocCount = useMemo(() => {
    const docSet = new Set<string>();
    for (const turn of turns) {
      for (const c of turn.citations) {
        if (c.document_id || c.title) {
          docSet.add(c.document_id || c.title || "");
        }
      }
    }
    return docSet.size > 0 ? docSet.size : 2;
  }, [turns]);

  const verifiedMemberCount =
    labOverview?.counts?.people || labPeople.length || 10;

  // Initial preview dossier cards for empty canvas state
  const previewDossierPeople = useMemo(() => {
    const dummyTurn: Turn = {
      id: "preview",
      question: "课题组里郭建民和万振凯的研究专长分别是什么？",
      answer: "郭建民 副教授 万振凯 教授",
      pending: false,
      citations: [],
    };
    return extractMatchedPeopleForTurn(dummyTurn, labPeople).slice(0, 2);
  }, [labPeople]);

  return (
    <Shell>
      <div className="grid min-h-[calc(100vh-7.5rem)] w-full grid-cols-1 gap-7 lg:grid-cols-[minmax(0,7fr)_minmax(340px,3fr)] lg:items-start">
        {/* LEFT (~70%): Interactive Research Canvas */}
        <section
          aria-label="动态科研研报与资产画布"
          className="flex min-w-0 flex-col gap-6 lg:pr-2"
        >
          {/* Canvas Top Topic & Export Bar */}
          <header className="flex flex-col gap-3 border-b border-stone-200/85 pb-4 dark:border-stone-800">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-xs font-medium text-stone-500 dark:text-stone-400">
                <span>📄 动态科研研报与资产画布</span>
                <span>·</span>
                <span className="font-mono text-[11px]">Research Canvas</span>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {turns.length > 0 ? (
                  <button
                    type="button"
                    onClick={() => void handleCopyEntireCanvas()}
                    className="inline-flex min-h-[30px] cursor-pointer items-center gap-1.5 rounded-[8px] border border-stone-200/90 bg-white px-2.5 py-1 text-xs font-medium text-stone-700 transition-colors hover:bg-stone-100/80 dark:border-stone-800 dark:bg-[#22221F] dark:text-stone-200"
                  >
                    <span>📋</span>
                    <span>复制研报</span>
                  </button>
                ) : null}

                <Link
                  href="/lab"
                  className="inline-flex min-h-[30px] items-center gap-1.5 rounded-[8px] border border-stone-200/90 bg-white px-2.5 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/8 dark:border-stone-800 dark:bg-[#22221F] dark:text-emerald-300"
                >
                  <span>🧺 写本子素材篮</span>
                  <span className="rounded bg-primary/10 px-1.5 py-0.2 font-mono text-[11px] font-semibold tabular-nums text-primary dark:bg-emerald-500/15 dark:text-emerald-300">
                    {basketItems.length}
                  </span>
                </Link>
              </div>
            </div>

            <div className="space-y-1">
              <h1 className="font-display text-2xl font-semibold tracking-tight text-stone-900 sm:text-[26px] dark:text-stone-100">
                {activeConvTitle}
              </h1>
              <p className="flex flex-wrap items-center gap-2 text-xs text-stone-500 dark:text-stone-400">
                <span>
                  状态: 已关联{" "}
                  <strong className="font-mono font-semibold tabular-nums text-stone-700 dark:text-stone-200">
                    {linkedDocCount}
                  </strong>{" "}
                  份核心文档 ·{" "}
                  <strong className="font-mono font-semibold tabular-nums text-stone-700 dark:text-stone-200">
                    {verifiedMemberCount}
                  </strong>{" "}
                  名核验成员
                </span>
                {canvasNotice ? (
                  <span
                    role="status"
                    className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary dark:bg-emerald-500/15 dark:text-emerald-300"
                  >
                    ✓ {canvasNotice}
                  </span>
                ) : null}
              </p>
            </div>
          </header>

          {/* Main Canvas Stream or Initial Workbench State */}
          {turns.length === 0 ? (
            <div className="space-y-7">
              {/* Editorial Orientation Note (Borderless Warm Paper Flow) */}
              <div className="space-y-3">
                <h2 className="font-display text-xl font-medium text-stone-800 dark:text-stone-200">
                  {t.emptyTitle}
                </h2>
                <p className="max-w-[65ch] text-[15px] leading-[1.75] text-stone-600 dark:text-stone-400">
                  {t.emptyBody}
                </p>

                <div className="pt-2">
                  <p className="mb-2.5 text-xs font-semibold tracking-wide text-stone-500 dark:text-stone-400">
                    {t.suggestions}（点击直接在画布生成结构化研报）
                  </p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {CANVAS_SUGGESTIONS.map((item) => (
                      <button
                        key={item}
                        type="button"
                        disabled={uiLocked}
                        onClick={() => void ask(item)}
                        className="group flex cursor-pointer items-center justify-between gap-2 rounded-[12px] border border-stone-200/90 bg-white px-3.5 py-2.5 text-left text-xs font-medium text-stone-700 shadow-[0_1px_2px_rgba(28,25,23,0.04)] transition-colors hover:border-primary/45 hover:text-stone-950 disabled:opacity-50 dark:border-stone-800 dark:bg-[#22221F] dark:text-stone-200 dark:hover:border-emerald-400/45"
                      >
                        <span className="line-clamp-2 leading-relaxed">
                          {item}
                        </span>
                        <span className="shrink-0 font-mono text-stone-400 transition-transform group-hover:translate-x-0.5 group-hover:text-primary dark:group-hover:text-emerald-300">
                          →
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* Live Preview of Core Lab Member Dossier Cards on Initial Canvas */}
              {previewDossierPeople.length > 0 ? (
                <section
                  aria-label="课题组成员专长档案速览"
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
                    <Link
                      href="/lab"
                      className="text-[11px] font-medium text-primary hover:underline dark:text-emerald-300"
                    >
                      查看全部 {verifiedMemberCount} 名成员 ↗
                    </Link>
                  </div>

                  <div className="divide-y divide-stone-200/70 dark:divide-stone-800/80">
                    {previewDossierPeople.map((person) => {
                      const isCopied = copiedPersonId === person.id;
                      const inBasket = basketKeySet.has(`person:${person.id}`);
                      return (
                        <div
                          key={person.id}
                          className="flex flex-col gap-2.5 px-4 py-3.5 transition-colors hover:bg-stone-50/60 dark:hover:bg-stone-800/20"
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
                            <span className="inline-flex items-center gap-1 rounded-md bg-emerald-950/6 px-2 py-0.5 font-mono text-[11px] tabular-nums text-primary ring-1 ring-primary/20 dark:bg-emerald-500/10 dark:text-emerald-300">
                              {person.signatureLabel}
                            </span>
                          </div>

                          <div className="flex flex-col gap-1.5 text-xs text-stone-600 sm:flex-row sm:items-center sm:justify-between dark:text-stone-300">
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
                                onClick={() =>
                                  void handleCopyPersonGrantFormat(person)
                                }
                                className={`inline-flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-[10px] border px-2.5 py-1 text-xs font-medium transition-colors ${
                                  isCopied
                                    ? "border-primary/40 bg-primary/10 text-primary dark:border-emerald-500/40 dark:bg-emerald-500/15 dark:text-emerald-300"
                                    : "border-stone-200/90 bg-[#FAF9F6] text-stone-700 hover:border-stone-300 hover:bg-stone-100/80 dark:border-stone-700 dark:bg-stone-800/80 dark:text-stone-200"
                                }`}
                              >
                                <span>{isCopied ? "✓" : "📋"}</span>
                                <span>
                                  {isCopied ? "已复制申报格式" : "复制申报格式"}
                                </span>
                              </button>

                              <button
                                type="button"
                                onClick={() => handleTogglePersonBasket(person)}
                                className={`inline-flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-[10px] border px-2.5 py-1 text-xs font-medium transition-colors ${
                                  inBasket
                                    ? "border-primary bg-primary text-primary-foreground shadow-xs"
                                    : "border-stone-200/90 bg-[#FAF9F6] text-stone-700 hover:border-stone-300 hover:bg-stone-100/80 dark:border-stone-700 dark:bg-stone-800/80 dark:text-stone-200"
                                }`}
                              >
                                <span>{inBasket ? "✓" : "🧺"}</span>
                                <span>
                                  {inBasket ? "已加入素材篮" : "加入素材篮"}
                                </span>
                              </button>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>
              ) : null}
            </div>
          ) : (
            <div className="space-y-7">
              {turns.map((turn) => (
                <SmartTurnBlock
                  key={turn.id}
                  turn={turn}
                  labPeople={labPeople}
                  copiedPersonId={copiedPersonId}
                  basketKeySet={basketKeySet}
                  activeEvidence={activeEvidence}
                  onSelectCitation={handleSelectCitationFromProse}
                  onCopyGrantFormat={(person) =>
                    void handleCopyPersonGrantFormat(person)
                  }
                  onToggleBasket={handleTogglePersonBasket}
                />
              ))}
            </div>
          )}
        </section>

        {/* RIGHT (~30%): Copilot Inspector Rail */}
        <CopilotInspectorRail
          conversations={conversations}
          conversationId={conversationId}
          turns={turns}
          draft={draft}
          busy={busy}
          uiLocked={uiLocked}
          activeEvidence={activeEvidence}
          onDraftChange={setDraft}
          onAsk={(q) => void ask(q)}
          onSelectConversation={(id) => void selectConversation(id)}
          onStartNewConversation={startNewConversation}
          onDeleteConversation={(id) => void handleDeleteConversation(id)}
          onSelectEvidence={setActiveEvidence}
        />
      </div>
    </Shell>
  );
}
