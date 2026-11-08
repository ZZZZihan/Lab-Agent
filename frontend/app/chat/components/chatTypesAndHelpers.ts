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
    !turn.answer.trim() ||
    labPeople.length === 0
  ) {
    return [];
  }

  const citationCorpus = turn.citations
    .map((c) => `${c.title || ""} ${c.heading || ""} ${c.excerpt || ""}`)
    .join(" ");
  const verifiedCorpus = `${turn.answer} ${citationCorpus}`;

  const isMemberRelatedQuery =
    /专长|成员|团队|人员|职称|导师|申请人|负责人|谁|简介|简历|研究方向|课题组/.test(
      turn.question,
    ) ||
    turn.citations.some(
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

  // 1. Check people explicitly named in the verified answer first
  const matchedInAnswer = sourcePool.filter(
    (p) => p.name && turn.answer.includes(p.name),
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
    const matchedInCitations = sourcePool.filter(
      (p) => p.name && verifiedCorpus.includes(p.name),
    );
    if (matchedInCitations.length > 0) {
      matchedInCitations.sort((a, b) => {
        const aInQ = turn.question.includes(a.name) ? 0 : 1;
        const bInQ = turn.question.includes(b.name) ? 0 : 1;
        if (aInQ !== bInQ) return aInQ - bInQ;
        return 0;
      });
      return matchedInCitations.slice(0, 4).map(buildPersonCardData);
    }
  }

  return [];
}

export function extractSpecTableForTurn(turn: Turn): SpecTableData | null {
  if (
    turn.refused ||
    turn.pending ||
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
  // Strip negated numeric claims from prose answer so e.g. "不是 420 A，上限为 300 A" never matches 420 A
  const affirmativeAnswer = turn.answer.replace(
    /(?:不是|并非|而非|不等于)\s*\d+(?:\.\d+)?\s*[A-Za-z%℃°小时天秒分钟台张卡万元]+/gi,
    "",
  );
  // Prioritize authoritative citation excerpts before affirmative answer text; never include turn.question
  const verifiedText = [citationText, affirmativeAnswer]
    .filter(Boolean)
    .join("\n");

  const rows: SpecTableRow[] = [];
  const seenParams = new Set<string>();

  const pushRow = (
    parameter: string,
    indicator: string,
    condition: string,
    conditionTone: SpecTableRow["conditionTone"],
  ) => {
    const key = parameter.trim();
    const val = indicator.trim();
    if (!key || !val || seenParams.has(key)) return;
    seenParams.add(key);
    rows.push({
      parameter: key,
      indicator: val,
      condition: condition.trim(),
      conditionTone,
    });
  };

  // Detect superconducting magnet SOP parameters strictly from verified source values
  const currentMatch = verifiedText.match(
    /(?:主磁体[^。；;\n]{0,28}?(?:额定(?:工作)?电流|工作电流|电流(?:上限)?)|额定(?:工作)?电流(?:上限)?)[^0-9\n]{0,18}?(\d+(?:\.\d+)?\s*A)\b/i,
  );
  if (currentMatch?.[1]) {
    pushRow(
      "主磁体额定电流",
      currentMatch[1].replace(/\s+/g, " "),
      "上限阈值",
      "limit",
    );
  }

  const levelMatch = verifiedText.match(
    /(?:液氦液位|冷却液氦)[^0-9\n]{0,24}?(\d+(?:\.\d+)?\s*%)/,
  );
  if (levelMatch?.[1]) {
    pushRow(
      "冷却液氦液位",
      levelMatch[1].replace(/\s+/g, ""),
      "< 触发停机",
      "interlock",
    );
  }

  const dayMatch = verifiedText.match(
    /(?:阻抗校准|失超保护)[^0-9\n]{0,24}?每\s*(\d+)\s*天/,
  );
  if (dayMatch?.[1]) {
    pushRow(
      "阻抗校准周期",
      `每 ${dayMatch[1]} 天一次`,
      "维护周期",
      "cycle",
    );
  }

  // Detect GPU cluster / node spec values strictly from verified source text
  if (
    rows.length === 0 &&
    /A100|GPU\s*集群|算力节点/i.test(verifiedText)
  ) {
    const nodeMatch = verifiedText.match(/(\d+\s*台(?:节点)?)/);
    if (nodeMatch?.[1]) {
      pushRow("清衡-A100 集群规模", nodeMatch[1], "在线算力节点", "neutral");
    }
    const gpuMatch = verifiedText.match(
      /(\d+\s*(?:张|卡)\s*[^，。；\n]{0,18})/,
    );
    if (gpuMatch?.[1]) {
      pushRow("加速卡配置", gpuMatch[1], "单机/集群规格", "limit");
    }
  }

  // Generic numeric threshold extraction from affirmative verified clauses
  if (
    rows.length === 0 &&
    /规程|参数|阈值|上限|下限|联锁|指标/.test(verifiedText)
  ) {
    const clauses = affirmativeAnswer
      .split(/[。；;\n]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    for (const clause of clauses) {
      const m = clause.match(
        /^([^，,：:]{2,14}?)(?:上限|下限|阈值|为|是|达到|低于|高于)\s*([0-9]+(?:\.[0-9]+)?\s*[A-Za-z%℃°小时天秒分钟台张万元]+)/,
      );
      if (m) {
        const isInterlock = /停机|联锁|报警|切断/.test(clause);
        const isLimit = /上限|最大|不超过|阈值/.test(clause);
        pushRow(
          m[1].trim(),
          m[2].trim(),
          isInterlock ? "联锁保护条件" : isLimit ? "上限阈值" : "标准工作指标",
          isInterlock ? "interlock" : isLimit ? "limit" : "neutral",
        );
      }
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
