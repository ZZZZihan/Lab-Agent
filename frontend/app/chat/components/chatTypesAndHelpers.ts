"use client";

import type {
  BasketItem,
  BasketOptions,
  ChatEvent,
  Citation,
  LabPersonItem,
  RetrievalHit,
} from "@/lib/api";
import type { Copy } from "@/lib/i18n";
import {
  BASKET_MAX_ITEMS,
  DEFAULT_BASKET_OPTIONS,
  basketKey,
  getBasketStorageKey,
  parseBasketStorage,
} from "@/app/lab/components/labTypesAndHelpers";

export type CitationCheckState = {
  used: number[];
  invalid: number[];
  uncited: boolean;
};

export type RetrievalTelemetry = {
  mode: string;
  coverage: number;
  elapsedMs: number;
  hits: RetrievalHit[];
  refused?: boolean;
};

export type Turn = {
  id: string;
  question: string;
  answer: string;
  pending: boolean;
  error?: string;
  refused?: boolean;
  refusal?: ChatEvent;
  citations: Citation[];
  citationCheck?: CitationCheckState;
  retrieval?: RetrievalTelemetry;
  paperStatus?: string | null;
  phase?: string;
};

export type ConversationSummary = {
  id: string;
  title: string;
  created_at: string;
};

export type StoredMessage = {
  id: string;
  role: string;
  content: string;
  refused: boolean;
  citations: Citation[];
};

export type ConversationDetail = {
  id: string;
  title: string;
  messages: StoredMessage[];
};

export type ActiveEvidenceSelection = {
  turnId: string;
  citeIndex: number;
  citation: Citation;
};

export type MatchedPersonCardData = {
  id: string;
  name: string;
  roleSubtitle: string;
  specialtyTags: string[];
  specialtyText: string;
  hasSignature: boolean;
  signatureLabel: string;
  signatureUrl?: string | null;
  applicationText: string;
  projectRole: "applicant" | "member" | string;
};

export const CANVAS_SUGGESTIONS = [
  "主磁体额定工作电流上限是多少？液氦液位低于多少会触发联锁停机？",
  "课题组里陈予安和陆星河的研究专长分别是什么？",
  "清衡-A100 一共有多少台节点？",
  "周三学术例会在哪间实体会议室？",
];

export const COMPACT_RAIL_SUGGESTIONS: Array<{
  label: string;
  prompt: string;
}> = [
  {
    label: "主磁体额定电流与液氦联锁阈值",
    prompt: "主磁体额定工作电流上限是多少？液氦液位低于多少会触发联锁停机？",
  },
  {
    label: "陈予安与陆星河研究专长档案",
    prompt: "课题组里陈予安和陆星河的研究专长分别是什么？",
  },
  {
    label: "清衡-A100 算力节点规模",
    prompt: "清衡-A100 一共有多少台节点？",
  },
  {
    label: "周三学术例会实体会议室",
    prompt: "周三学术例会在哪间实体会议室？",
  },
];

export function mapErrorCode(code: string | undefined, t: Copy): string {
  if (code === "llm_unavailable") return t.llmUnavailable;
  if (code === "embedding_unavailable") return t.embeddingUnavailable;
  if (code === "unauthorized") return t.noSession;
  return t.error;
}

export function mapToolPhase(name: string | undefined, t: Copy): string {
  if (name === "search_knowledge_base") return t.toolSearchKnowledgeBase;
  if (name === "query_lab_assets") return t.toolQueryLabAssets;
  if (name === "search_papers") return t.toolSearchPapers;
  if (name === "fetch_document") return t.toolFetchDocument;
  return t.asking;
}

export function reconstructCitationCheck(
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

export function messagesToTurns(messages: StoredMessage[]): Turn[] {
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

export function buildPersonCardData(
  person: {
    id: string;
    name: string;
    title?: string | null;
    degree?: string | null;
    admin_duty?: string | null;
    tutor_role?: string | null;
    affiliation?: string | null;
    research_specialty?: string | null;
    project_role?: string;
    has_signature?: boolean;
    signature_sha256?: string | null;
    signature_url?: string | null;
    application_text?: string;
    copy_text?: string;
  },
): MatchedPersonCardData {
  const roleParts: string[] = [];
  if (person.title && person.title !== "无") {
    roleParts.push(person.title);
  }
  if (person.degree && person.degree !== "无") {
    roleParts.push(person.degree);
  }
  if (person.admin_duty && person.admin_duty !== "无") {
    roleParts.push(person.admin_duty);
  } else if (person.project_role === "applicant") {
    roleParts.push("课题负责人");
  } else if (person.project_role === "member") {
    roleParts.push("项目组成员");
  }
  if (roleParts.length === 0) {
    roleParts.push("课题组成员");
  }

  const rawSpec = (person.research_specialty || "").trim();
  const specialtyTags = rawSpec
    ? rawSpec
        .split(/[、/，,;；\s]+/)
        .map((s) => s.trim())
        .filter(Boolean)
    : [];
  const specialtyText =
    specialtyTags.length > 0 ? specialtyTags.join(" / ") : "暂未登记研究专长";

  const hasSig = Boolean(person.has_signature ?? person.signature_sha256);
  const signatureLabel = hasSig
    ? "✓ 真实内嵌签名已核验"
    : "暂未绑定内嵌签名";

  const affiliationPart =
    person.affiliation && person.affiliation !== "无"
      ? `，${person.affiliation}`
      : "";
  const specPart =
    specialtyTags.length > 0 ? `；研究专长：${specialtyTags.join("、")}` : "";
  const rolePart =
    person.project_role === "applicant"
      ? "；在项目中承担角色：课题负责人"
      : person.project_role === "member"
        ? "；在项目中承担角色：项目组成员"
        : "";
  const fallbackAppText = `${person.name}${person.degree && person.degree !== "无" ? `，${person.degree}` : ""}${affiliationPart}${person.title && person.title !== "无" ? `，${person.title}` : ""}${specPart}${rolePart}。`;

  return {
    id: person.id,
    name: person.name,
    roleSubtitle: roleParts.join(" · "),
    specialtyTags,
    specialtyText,
    hasSignature: hasSig,
    signatureLabel,
    signatureUrl: person.signature_url ?? null,
    applicationText: person.application_text || person.copy_text || fallbackAppText,
    projectRole: person.project_role || "member",
  };
}

export function extractMatchedPeopleForTurn(
  turn: Turn,
  labPeople: LabPersonItem[],
): MatchedPersonCardData[] {
  if (
    turn.refused ||
    turn.pending ||
    Boolean(turn.error) ||
    !turn.answer.trim() ||
    turn.citations.length === 0 ||
    labPeople.length === 0
  ) {
    return [];
  }

  const usedSet =
    turn.citationCheck && turn.citationCheck.used.length > 0
      ? new Set(turn.citationCheck.used)
      : null;
  const activeCitations = usedSet
    ? turn.citations.filter((c, i) => usedSet.has(c.index ?? i + 1))
    : turn.citations;
  const sourceCitations =
    activeCitations.length > 0 ? activeCitations : turn.citations;

  const citationCorpus = sourceCitations
    .map((c) => `${c.title || ""} ${c.heading || ""} ${c.excerpt || ""}`)
    .join(" ");

  const sourcePool = labPeople.map((p) => ({
    id: p.id,
    name: p.name,
    title: p.title,
    degree: p.degree,
    admin_duty: p.admin_duty,
    tutor_role: p.tutor_role,
    affiliation: p.affiliation,
    research_specialty: p.research_specialty,
    project_role: p.project_role,
    has_signature: p.has_signature ?? Boolean(p.signature_sha256),
    signature_sha256: p.signature_sha256,
    signature_url: p.signature_url,
    application_text: p.application_text,
    copy_text: p.copy_text,
  }));

  const isAffirmedInAnswer = (name: string): boolean => {
    if (!name || !turn.answer.includes(name)) return false;
    const clauses = turn.answer
      .split(/[。；;，,\n]+/)
      .map((c) => c.trim())
      .filter((c) => c.includes(name));
    if (clauses.length === 0) return false;
    return clauses.some(
      (clause) =>
        !/未提及|没有提及|未包含|不含|不包括|未找到|没有找到|并非|不是|无关|除外/.test(
          clause,
        ),
    );
  };

  // 1. Check people affirmed in the answer AND backed by the turn's citations
  const matchedInAnswer = sourcePool.filter(
    (p) =>
      p.name &&
      citationCorpus.includes(p.name) &&
      isAffirmedInAnswer(p.name),
  );

  if (matchedInAnswer.length > 0) {
    matchedInAnswer.sort((a, b) => {
      const aInQ = turn.question.includes(a.name) ? 0 : 1;
      const bInQ = turn.question.includes(b.name) ? 0 : 1;
      if (aInQ !== bInQ) return aInQ - bInQ;
      return 0;
    });
    return matchedInAnswer.slice(0, 6).map(buildPersonCardData);
  }

  return [];
}

export function formatSourcePillTitle(citation: Citation): string {
  const rawTitle = (citation.title || "知识库文档").trim();
  const rawHeading = (citation.heading || "").replace(/^#+\s*/, "").trim();

  if (
    (citation.doc_key && citation.doc_key.startsWith("lab-profile-")) ||
    rawTitle.includes("课题组资料库")
  ) {
    if (rawHeading) {
      const shortHeading = rawHeading
        .replace(/^课题组概况与/, "")
        .replace(/^团队成员[：:·\s]*/, "")
        .slice(0, 12);
      return `申报书·${shortHeading || "课题组档案"}`;
    }
    return rawTitle || "课题组资料库";
  }

  const cleanDocTitle = rawTitle.replace(/-E2E实测$/, "");
  if (rawHeading && !cleanDocTitle.includes(rawHeading) && rawHeading.length <= 10) {
    return `${cleanDocTitle}·${rawHeading}`;
  }
  return cleanDocTitle;
}

export function readReuseBasket(userId: string | null): {
  items: BasketItem[];
  options: BasketOptions;
} {
  if (typeof window === "undefined") {
    return { items: [], options: DEFAULT_BASKET_OPTIONS };
  }
  const key = getBasketStorageKey(userId);
  const parsed = parseBasketStorage(window.localStorage.getItem(key));
  if (parsed) {
    return { items: parsed.items, options: parsed.options };
  }
  return { items: [], options: DEFAULT_BASKET_OPTIONS };
}

export function togglePersonInReuseBasket(
  userId: string | null,
  person: { id: string; name: string; roleSubtitle?: string },
): { items: BasketItem[]; added: boolean } {
  const current = readReuseBasket(userId);
  const targetKey = basketKey("person", person.id);
  const exists = current.items.some((it) => basketKey(it.kind, it.id) === targetKey);
  let nextItems: BasketItem[];
  let added = false;

  if (exists) {
    nextItems = current.items.filter((it) => basketKey(it.kind, it.id) !== targetKey);
  } else {
    const newItem: BasketItem = {
      kind: "person",
      id: person.id,
      label: person.name,
    };
    nextItems = [...current.items, newItem].slice(0, BASKET_MAX_ITEMS);
    added = true;
  }

  if (typeof window !== "undefined") {
    const key = getBasketStorageKey(userId);
    window.localStorage.setItem(
      key,
      JSON.stringify({
        version: 1,
        items: nextItems,
        options: current.options,
      }),
    );
  }

  return { items: nextItems, added };
}
