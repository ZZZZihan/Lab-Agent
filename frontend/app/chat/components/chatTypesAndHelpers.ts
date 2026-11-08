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

export type SpecTableRow = {
  parameter: string;
  indicator: string;
  condition: string;
  conditionTone: "limit" | "interlock" | "cycle" | "neutral";
};

export type SpecTableData = {
  title: string;
  rows: SpecTableRow[];
  basis: string;
};

export const CANVAS_SUGGESTIONS = [
  "主磁体额定工作电流上限是多少？液氦液位低于多少会触发联锁停机？",
  "课题组里万振凯和郭建民的研究专长分别是什么？",
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
    label: "万振凯与郭建民研究专长档案",
    prompt: "课题组里万振凯和郭建民的研究专长分别是什么？",
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
  } else {
    roleParts.push("项目组成员");
  }

  const rawSpec = (person.research_specialty || "").trim();
  const specialtyTags = rawSpec
    ? rawSpec
        .split(/[、/，,;；\s]+/)
        .map((s) => s.trim())
        .filter(Boolean)
    : [];
  const specialtyText =
    specialtyTags.length > 0 ? specialtyTags.join(" / ") : "课题组核心研究方向";

  const hasSig = Boolean(person.has_signature ?? person.signature_sha256);
  const signatureLabel = hasSig
    ? "✓ 真实内嵌签名已核验"
    : "暂未绑定内嵌签名";

  const affiliationPart =
    person.affiliation && person.affiliation !== "无"
      ? `，${person.affiliation}`
      : "";
  const fallbackAppText = `${person.name}${person.degree && person.degree !== "无" ? `，${person.degree}` : ""}${affiliationPart}${person.title && person.title !== "无" ? `，${person.title}` : ""}；研究专长：${specialtyTags.join("、") || "课题组研究"}；在项目中承担角色：${person.project_role === "applicant" ? "课题负责人" : "项目组成员"}。`;

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

  const isMemberRelatedQuery =
    /专长|成员|团队|人员|职称|导师|申请人|负责人|谁|简介|简历|研究方向|课题组/.test(
      turn.question,
    ) ||
    sourceCitations.some(
      (c) =>
        (c.doc_key && c.doc_key.startsWith("lab-profile-")) ||
        (c.heading && /团队成员|成员|申请人|课题组概况/.test(c.heading)) ||
        (c.title && /课题组资料库|申报书/.test(c.title)),
    );

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
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const negatedRe = new RegExp(
      `(?:未提及|没有提及|未包含|不含|不包括|未找到|没有找到|并非|不是)\\s*${escaped}`,
    );
    const cleaned = turn.answer.replace(negatedRe, "");
    return cleaned.includes(name);
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

  // 2. If the query is about team/member profiles and citations hit lab-profile member chunks
  if (isMemberRelatedQuery) {
    const namedInQuestion = sourcePool.filter(
      (p) => p.name && turn.question.includes(p.name),
    );
    const candidatePool =
      namedInQuestion.length > 0 ? namedInQuestion : sourcePool;
    const matchedInCitations = candidatePool.filter(
      (p) => p.name && citationCorpus.includes(p.name),
    );
    if (matchedInCitations.length > 0) {
      return matchedInCitations.slice(0, 4).map(buildPersonCardData);
    }
  }

  return [];
}

const NEGATED_OR_NON_STANDARD_CLAUSE_RE =
  /不是|并非|而非|不等于|不触发|未触发|不会触发|不执行|无需|不需要|不必|仅报警|只报警|原为|曾为|旧规程|修订前|测试模式|调试模式|旁路/;

function extractUnambiguousSopRows(text: string): SpecTableRow[] {
  const sentences = text
    .split(/[。；;\n]+/)
    .map((s) => s.trim())
    .filter(Boolean);

  const magnetLimits = new Set<string>();
  const magnetRatedFallbacks = new Set<string>();
  const heliumInterlocks = new Map<string, string>();
  const calibrationDays = new Set<string>();

  for (const sentence of sentences) {
    const clauses = sentence
      .split(/[，,]+/)
      .map((c) => c.trim())
      .filter(Boolean);

    const hasMagnetSubject = /主磁体|超导磁体|额定(?:工作)?电流/.test(sentence);
    const hasHeliumSubject = /液氦/.test(sentence);
    const hasCalibrationSubject = /阻抗校准/.test(sentence);

    for (let i = 0; i < clauses.length; i += 1) {
      const clause = clauses[i];
      if (NEGATED_OR_NON_STANDARD_CLAUSE_RE.test(clause)) continue;

      if (hasMagnetSubject) {
        for (const m of clause.matchAll(
          /(?:额定(?:工作)?电流|工作电流|主磁体电流|电流)(?:的)?(?:上限(?:阈值)?(?:为|是)?|(?:不得|不)超过|最大(?:允许)?(?:值|电流)?(?:为|是)?|≤|<=)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*A\b/gi,
        )) {
          magnetLimits.add(`${m[1]} A`);
        }
        for (const m of clause.matchAll(
          /(?:上限(?:阈值)?|最大(?:允许)?电流)\s*(?:为|是|≤|<=)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*A\b/gi,
        )) {
          magnetLimits.add(`${m[1]} A`);
        }
        if (!/正常|日常|平均|最小|下限|低于|小于/.test(clause)) {
          for (const m of clause.matchAll(
            /额定(?:工作)?电流\s*(?:为|是)\s*(\d+(?:\.\d+)?)\s*A\b/gi,
          )) {
            magnetRatedFallbacks.add(`${m[1]} A`);
          }
        }
      }

      if (hasHeliumSubject) {
        const nextClause =
          i + 1 < clauses.length &&
          !NEGATED_OR_NON_STANDARD_CLAUSE_RE.test(clauses[i + 1]) &&
          !/%|低于|小于|下限|不足|<=|≤|<|高于|大于/.test(clauses[i + 1])
            ? `${clause}，${clauses[i + 1]}`
            : clause;

        for (const m of nextClause.matchAll(
          /(低于|小于|下限(?:为|是)?|不足|<=|≤|<)\s*(\d+(?:\.\d+)?)\s*%\s*(?:时|后|则)?(?:，)?\s*(?:立即|自动|将)?(?:触发)?(?:联锁(?:保护)?停机|停机联锁)/g,
        )) {
          const cond =
            /<=|≤|下限/.test(m[1]) ? "≤ 触发联锁停机" : "< 触发联锁停机";
          heliumInterlocks.set(`${m[2]}%`, cond);
        }
        for (const m of clause.matchAll(
          /(?:联锁(?:保护)?停机|停机联锁)(?:液位|阈值)\s*(低于|小于|下限(?:为|是)?|为|是|<=|≤|<)\s*(\d+(?:\.\d+)?)\s*%/g,
        )) {
          const cond =
            /<=|≤|下限/.test(m[1]) ? "≤ 触发联锁停机" : "< 触发联锁停机";
          heliumInterlocks.set(`${m[2]}%`, cond);
        }
      }

      if (hasCalibrationSubject) {
        for (const m of clause.matchAll(
          /(?:阻抗校准(?:周期)?(?:为|是)?\s*每\s*(\d+)\s*天(?:\s*一次)?|每\s*(\d+)\s*天\s*(?:进行|完成|执行)?(?:一次)?阻抗校准)/g,
        )) {
          const day = m[1] || m[2];
          if (day) calibrationDays.add(day);
        }
      }
    }
  }

  const rows: SpecTableRow[] = [];
  const chosenMagnetSet =
    magnetLimits.size > 0 ? magnetLimits : magnetRatedFallbacks;
  if (chosenMagnetSet.size === 1) {
    const [indicator] = Array.from(chosenMagnetSet);
    rows.push({
      parameter: "主磁体额定电流",
      indicator,
      condition: "上限阈值",
      conditionTone: "limit",
    });
  }

  if (heliumInterlocks.size === 1) {
    const [[indicator, condition]] = Array.from(heliumInterlocks.entries());
    rows.push({
      parameter: "冷却液氦液位",
      indicator,
      condition,
      conditionTone: "interlock",
    });
  }

  if (calibrationDays.size === 1) {
    const [day] = Array.from(calibrationDays);
    rows.push({
      parameter: "阻抗校准周期",
      indicator: `每 ${day} 天一次`,
      condition: "维护周期",
      conditionTone: "cycle",
    });
  }

  return rows;
}

export function extractSpecTableForTurn(turn: Turn): SpecTableData | null {
  if (
    turn.refused ||
    turn.pending ||
    Boolean(turn.error) ||
    !turn.answer.trim() ||
    turn.citations.length === 0
  ) {
    return null;
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

  const citationText = sourceCitations
    .map((c) => `${c.title || ""} ${c.heading || ""} ${c.excerpt || ""}`)
    .join("\n");

  // Extract unambiguous rows independently from source citations and from the assistant answer,
  // and only emit rows where both the cited source and the answer agree on the exact value.
  const citationRows = extractUnambiguousSopRows(citationText);
  const answerRows = extractUnambiguousSopRows(turn.answer);
  const answerByParam = new Map(answerRows.map((r) => [r.parameter, r]));

  const rows: SpecTableRow[] = [];
  for (const citeRow of citationRows) {
    const ansRow = answerByParam.get(citeRow.parameter);
    if (ansRow && ansRow.indicator === citeRow.indicator) {
      rows.push(citeRow);
    }
  }

  if (rows.length === 0) return null;

  // Determine source basis title from the turn's actual citations
  const sopCitation =
    sourceCitations.find(
      (c) => c.title && /规程|约定|规范|手册|标准/.test(c.title),
    ) || sourceCitations[0];

  let basis = "当前回合引用文档";
  if (sopCitation?.title) {
    const cleanTitle = sopCitation.title.replace(/-E2E实测$/, "");
    const cleanHeading = sopCitation.heading
      ? sopCitation.heading.replace(/^#+\s*/, "").trim()
      : "";
    basis = cleanHeading
      ? `《${cleanTitle}》${cleanHeading.startsWith("§") ? cleanHeading : `· ${cleanHeading}`}`
      : `《${cleanTitle}》`;
  }

  return {
    title: "规程参数标准表",
    rows,
    basis,
  };
}

export function formatSourcePillTitle(citation: Citation): string {
  const rawTitle = (citation.title || "知识库文档").trim();
  const rawHeading = (citation.heading || "").replace(/^#+\s*/, "").trim();

  if (
    (citation.doc_key && citation.doc_key.startsWith("lab-profile-")) ||
    rawTitle.includes("课题组资料库")
  ) {
    // Extract member name or concise section name if present
    const personMatch = rawHeading.match(/([一-龥]{2,4})/);
    if (personMatch && !/课题组|团队成员|承担项目|前期成果|支撑条件/.test(personMatch[1])) {
      return `申报书·${personMatch[1]}`;
    }
    if (rawHeading) {
      const shortHeading = rawHeading
        .replace(/^课题组概况与/, "")
        .replace(/^团队成员[：:·\s]*/, "")
        .slice(0, 12);
      return `申报书·${shortHeading || "课题组档案"}`;
    }
    return "2026申报书·课题组档案";
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
