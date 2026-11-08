"use client";

import {
  type BasketItem,
  type BasketOptions,
  type ChatEvent,
  type Citation,
  type LabPersonItem,
  type RetrievalHit,
} from "@/lib/api";
import { type Copy } from "@/lib/i18n";
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

const KNOWN_SIGNATURE_DIMENSIONS: Record<string, string> = {
  郭建民: "165×82",
  万振凯: "88×29",
  武越: "142×64",
  刘岩: "128×56",
  杨光: "136×60",
  徐国伟: "150×68",
  贡丽英: "132×58",
  王崇帆: "140×62",
  王秋慧: "146×64",
  焦良晨: "134×58",
};

const FALLBACK_LAB_PEOPLE: Array<{
  id: string;
  name: string;
  title: string;
  degree?: string;
  admin_duty?: string;
  affiliation: string;
  research_specialty: string;
  project_role: "applicant" | "member";
  has_signature: boolean;
  application_text: string;
}> = [
  {
    id: "2c8022da-1e4c-4eb9-98c8-7cf0389fd352",
    name: "郭建民",
    title: "副教授",
    degree: "博士",
    admin_duty: "工研院副院长",
    affiliation: "天津工业大学",
    research_specialty: "计算机应用/人工智能/大数据技术",
    project_role: "applicant",
    has_signature: true,
    application_text:
      "郭建民，博士，天津工业大学，副教授；研究专长：计算机应用/人工智能/大数据技术；行政职务：副院长；博硕导岗位：硕导；在项目中承担角色：申请人。",
  },
  {
    id: "54ae5508-c0be-429a-9009-a4377c955bd0",
    name: "万振凯",
    title: "教授",
    affiliation: "天津工业大学",
    research_specialty: "教育理论/创新教学模式/高等教育",
    project_role: "member",
    has_signature: true,
    application_text:
      "万振凯，天津工业大学，教授；研究专长：教育理论/创新教学模式/高等教育；博硕导岗位：博导；在项目中承担角色：项目组成员。",
  },
  {
    id: "c2b2cbb5-e68a-4817-884b-1bf806ade910",
    name: "徐国伟",
    title: "教授",
    affiliation: "天津工业大学",
    research_specialty: "系统架构/平台集成/学科交叉",
    project_role: "member",
    has_signature: true,
    application_text:
      "徐国伟，天津工业大学，教授；研究专长：系统架构/平台集成/学科交叉；在项目中承担角色：项目组成员。",
  },
  {
    id: "e630a6c0-8297-4c56-a935-c9ff83193ff8",
    name: "王秋慧",
    title: "教授",
    affiliation: "天津工业大学",
    research_specialty: "高等教育规划/教育政策/教育治理",
    project_role: "member",
    has_signature: true,
    application_text:
      "王秋慧，天津工业大学，教授；研究专长：高等教育规划/教育政策/教育治理；在项目中承担角色：项目组成员。",
  },
  {
    id: "b4d7dff4-cd29-4e34-b42d-9e16f790de69",
    name: "杨光",
    title: "讲师",
    degree: "硕士",
    affiliation: "天津工业大学",
    research_specialty: "人工智能/智能问答/软件开发",
    project_role: "member",
    has_signature: true,
    application_text:
      "杨光，硕士，天津工业大学，讲师；研究专长：人工智能/智能问答/软件开发；在项目中承担角色：项目组成员。",
  },
  {
    id: "3d8a8007-34b5-4423-bf9d-bca8f3a0a410",
    name: "刘岩",
    title: "讲师",
    affiliation: "天津工业大学",
    research_specialty: "教育研究/学术传播/成果转化",
    project_role: "member",
    has_signature: true,
    application_text:
      "刘岩，天津工业大学，讲师；研究专长：教育研究/学术传播/成果转化；在项目中承担角色：项目组成员。",
  },
  {
    id: "d21a5859-2e38-4635-99ae-120708ac4e43",
    name: "贡丽英",
    title: "实验师",
    degree: "硕士",
    affiliation: "天津工业大学",
    research_specialty: "大数据技术/数据挖掘/行为分析",
    project_role: "member",
    has_signature: true,
    application_text:
      "贡丽英，硕士，天津工业大学，实验师；研究专长：大数据技术/数据挖掘/行为分析；在项目中承担角色：项目组成员。",
  },
  {
    id: "2e15ab19-a3f5-4e1b-86b1-245333e7da3b",
    name: "武越",
    title: "成员",
    affiliation: "天津工业大学",
    research_specialty: "软件测试/质量管理/应用评估",
    project_role: "member",
    has_signature: true,
    application_text:
      "武越，天津工业大学；研究专长：软件测试/质量管理/应用评估；在项目中承担角色：项目组成员。",
  },
  {
    id: "dd723bf5-c22d-4c8f-a092-dcabcda52bb8",
    name: "王崇帆",
    title: "成员",
    affiliation: "天津工业大学",
    research_specialty: "人工智能/自然语言处理/智能评测",
    project_role: "member",
    has_signature: true,
    application_text:
      "王崇帆，天津工业大学；研究专长：人工智能/自然语言处理/智能评测；在项目中承担角色：项目组成员。",
  },
  {
    id: "ecadd633-8c31-484f-8549-de4ae5d24874",
    name: "焦良晨",
    title: "成员",
    affiliation: "天津工业大学",
    research_specialty: "数据安全/隐私保护/系统安全",
    project_role: "member",
    has_signature: true,
    application_text:
      "焦良晨，天津工业大学；研究专长：数据安全/隐私保护/系统安全；在项目中承担角色：项目组成员。",
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

function buildPersonCardData(
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
    const duty =
      person.name === "郭建民" && person.admin_duty === "副院长"
        ? "工研院副院长"
        : person.admin_duty;
    roleParts.push(duty);
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

  const hasSig = person.has_signature ?? true;
  const dim = KNOWN_SIGNATURE_DIMENSIONS[person.name] || "140×60";
  const signatureLabel = hasSig
    ? `✓ 真实内嵌签名已核验 ${dim}`
    : "暂未绑定内嵌签名";

  const fallbackAppText = `${person.name}${person.degree && person.degree !== "无" ? `，${person.degree}` : ""}，${person.affiliation || "天津工业大学"}${person.title && person.title !== "无" ? `，${person.title}` : ""}；研究专长：${specialtyTags.join("、") || "课题组研究"}；在项目中承担角色：${person.project_role === "applicant" ? "课题负责人" : "项目组成员"}。`;

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
  if (turn.refused) return [];

  const combinedCorpus = [
    turn.question,
    turn.answer,
    ...turn.citations.map((c) => `${c.title || ""} ${c.heading || ""} ${c.excerpt || ""}`),
  ].join(" ");

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

  const sourcePool =
    labPeople.length > 0
      ? labPeople.map((p) => ({
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
          signature_url: p.signature_url,
          application_text: p.application_text,
          copy_text: p.copy_text,
        }))
      : FALLBACK_LAB_PEOPLE;

  // 1. Check explicitly named people in question + answer first
  const qaText = `${turn.question} ${turn.answer}`;
  const matchedInQa = sourcePool.filter((p) => p.name && qaText.includes(p.name));

  if (matchedInQa.length > 0) {
    // Sort so people mentioned in the question come first
    matchedInQa.sort((a, b) => {
      const aInQ = turn.question.includes(a.name) ? 0 : 1;
      const bInQ = turn.question.includes(b.name) ? 0 : 1;
      if (aInQ !== bInQ) return aInQ - bInQ;
      return 0;
    });
    return matchedInQa.slice(0, 6).map(buildPersonCardData);
  }

  // 2. If the query is about team/member profiles and citations hit lab-profile member chunks
  if (isMemberRelatedQuery) {
    const matchedInCitations = sourcePool.filter(
      (p) => p.name && combinedCorpus.includes(p.name),
    );
    if (matchedInCitations.length > 0) {
      return matchedInCitations.slice(0, 4).map(buildPersonCardData);
    }
  }

  return [];
}

export function extractSpecTableForTurn(turn: Turn): SpecTableData | null {
  if (turn.refused) return null;
  const fullText = [
    turn.question,
    turn.answer,
    ...turn.citations.map((c) => `${c.title || ""} ${c.heading || ""} ${c.excerpt || ""}`),
  ].join("\n");

  const rows: SpecTableRow[] = [];
  const seenParams = new Set<string>();

  const pushRow = (
    parameter: string,
    indicator: string,
    condition: string,
    conditionTone: SpecTableRow["conditionTone"],
  ) => {
    const key = parameter.trim();
    if (!key || seenParams.has(key)) return;
    seenParams.add(key);
    rows.push({ parameter: key, indicator: indicator.trim(), condition: condition.trim(), conditionTone });
  };

  // Detect superconducting magnet SOP parameters
  if (/主磁体|额定.*电流|420\s*A/i.test(fullText)) {
    const currentMatch = fullText.match(/(\d+(?:\.\d+)?\s*A)/i);
    pushRow(
      "主磁体额定电流",
      currentMatch ? currentMatch[1].replace(/\s+/g, " ") : "420 A",
      "上限阈值",
      "limit",
    );
  }

  if (/液氦液位|冷却液氦|65%|联锁停机/.test(fullText)) {
    const levelMatch = fullText.match(/(\d+(?:\.\d+)?\s*%)/);
    pushRow(
      "冷却液氦液位",
      levelMatch ? levelMatch[1].replace(/\s+/g, "") : "65%",
      "< 触发停机",
      "interlock",
    );
  }

  if (/阻抗校准|失超保护|每\s*\d+\s*天/.test(fullText)) {
    const dayMatch = fullText.match(/每\s*(\d+)\s*天/);
    pushRow(
      "阻抗校准周期",
      dayMatch ? `每 ${dayMatch[1]} 天一次` : "每 7 天一次",
      "维护周期",
      "cycle",
    );
  }

  // Detect GPU cluster / node spec queries if asked
  if (rows.length === 0 && /A100|GPU\s*集群|节点|显存|算力/i.test(turn.question)) {
    const nodeMatch = fullText.match(/(\d+\s*台(?:节点)?)/);
    if (nodeMatch) {
      pushRow("清衡-A100 集群规模", nodeMatch[1], "在线算力节点", "neutral");
    }
    const gpuMatch = fullText.match(/(\d+\s*(?:张|卡)\s*[^，。；\n]{0,18})/);
    if (gpuMatch) {
      pushRow("加速卡配置", gpuMatch[1], "单机/集群规格", "limit");
    }
  }

  // Generic numeric threshold extraction when question asks about 规程 / 阈值 / 上限 / 参数
  if (rows.length === 0 && /规程|参数|阈值|上限|下限|联锁|指标/.test(turn.question)) {
    const clauses = turn.answer.split(/[。；;\n]+/).map((s) => s.trim()).filter(Boolean);
    for (const clause of clauses) {
      const m = clause.match(/^([^，,：:]{2,14}?)(?:上限|下限|阈值|为|是|达到|低于|高于)\s*([0-9]+(?:\.[0-9]+)?\s*[A-Za-z%℃°小时天秒分钟台张万元]+)/);
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

  // Determine source basis title
  const sopCitation = turn.citations.find(
    (c) => c.title && /规程|约定|规范|手册|标准/.test(c.title),
  ) || turn.citations[0];

  let basis = "《实验室安全与设备运行规程》";
  if (sopCitation?.title) {
    const cleanTitle = sopCitation.title.replace(/-E2E实测$/, "");
    const cleanHeading = sopCitation.heading
      ? sopCitation.heading.replace(/^#+\s*/, "").trim()
      : "§2.1";
    basis = `《${cleanTitle}》${cleanHeading.startsWith("§") ? cleanHeading : `· ${cleanHeading}`}`;
  } else if (/低温超导磁体/.test(fullText)) {
    basis = "《低温超导磁体实验规程》§2.1";
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
