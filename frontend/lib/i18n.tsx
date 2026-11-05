"use client";

import { createContext, useContext, useMemo, useState } from "react";

export type Locale = "zh" | "en";

const copy = {
  zh: {
    brand: "Lab Agent",
    lab: "清衡实验室",
    tagline: "只根据你有权阅读的实验室文档作答。",
    login: "登录",
    logout: "退出",
    email: "邮箱",
    password: "密码",
    signingIn: "正在核对账号…",
    loginError: "邮箱或密码不正确。",
    demoTitle: "本地演示账号",
    demoNote: "这些账号只存在于本机数据库，密码写在仓库里，不能用于任何真实环境。",
    chat: "问答",
    documents: "文档",
    audit: "审计",
    ask: "提问",
    asking: "正在检索并核对依据…",
    toolSearchKnowledgeBase: "检索实验室文档",
    toolSearchPapers: "查询论文库",
    toolFetchDocument: "读取全文",
    placeholder: "例如：周三学术例会在哪间实体会议室？",
    emptyTitle: "从一条可以核对的问题开始",
    emptyBody: "回答会标出具体小节。证据不够时，Lab Agent 会拒绝作答，而不是补一段听起来合理的话。",
    suggestions: "可以先问这些",
    citations: "依据",
    openDoc: "打开原文",
    refusalTitle: "检索证据不足，未生成答案",
    refusalBody: "下面这些段落只是词面相近的邻居，不是回答。",
    modelDeclinedTitle: "模型判断证据不足，未生成答案",
    modelDeclinedBody: "检索分数已通过门槛，但模型阅读段落后认为现有证据不足以回答这个问题。",
    uncitedNote: "模型没有标注引用，以下为检索到的证据",
    invalidCitationsNote: "模型标注了越界的引用编号：",
    llmUnavailable: "模型服务暂时不可用，没有生成回答。请稍后重试。",
    embeddingUnavailable: "检索服务暂时不可用",
    score: "决策分数",
    threshold: "阈值",
    coverage: "稀有词覆盖",
    paperNote: "论文检索接口尚未接入，这次没有查询外部论文库。",
    send: "发送",
    error: "这次请求没有完成。",
    upload: "上传文档",
    uploadTitle: "把一份实验室笔记放进知识库",
    title: "标题",
    body: "正文",
    bodyHint: "用 ## 标题分节。引用会指向具体小节，而不是整篇文档。",
    collection: "类型",
    visibility: "可见性",
    grants: "额外授权邮箱",
    grantsHint: "仅在「受限」时生效，多个邮箱用逗号分隔。",
    knowledge: "实验室知识",
    project: "项目",
    meeting: "组会",
    paper: "论文笔记",
    labVis: "实验室可见",
    restricted: "受限",
    private: "仅本人与管理员",
    save: "入库",
    saving: "正在切分并写入…",
    cancel: "取消",
    docsEmpty: "你目前看不到任何文档。",
    docsEmptyBody: "管理员和成员可以上传 Markdown。查看者只能阅读实验室公开文档。",
    delete: "删除",
    viewerNoUpload: "查看者不能上传。",
    back: "返回文档列表",
    auditEmpty: "还没有审计记录。",
    auditHint: "这里只记录动作、资源编号和模型用量，不记录问题原文、答案或邮箱。",
    retention: "按保留期清理",
    action: "动作",
    when: "时间",
    actor: "操作者",
    detail: "细节",
    language: "EN",
    role: "角色",
    loading: "正在读取…",
    noSession: "登录状态已失效，请重新登录。",
  },
  en: {
    brand: "Lab Agent",
    lab: "Qingheng Lab",
    tagline: "Answers stay inside documents you are allowed to read.",
    login: "Sign in",
    logout: "Sign out",
    email: "Email",
    password: "Password",
    signingIn: "Checking the account…",
    loginError: "Email or password is not right.",
    demoTitle: "Local demo accounts",
    demoNote: "These accounts exist only in the local database. The passwords are committed for the demo and must not be reused anywhere real.",
    chat: "Ask",
    documents: "Documents",
    audit: "Audit",
    ask: "Ask",
    asking: "Searching and checking evidence…",
    toolSearchKnowledgeBase: "Searching lab documents",
    toolSearchPapers: "Querying paper library",
    toolFetchDocument: "Reading full document",
    placeholder: "For example: which room hosts the Wednesday seminar?",
    emptyTitle: "Start with a question the notes can answer",
    emptyBody: "Citations point at a section. If the evidence is too thin, Lab Agent refuses instead of filling the gap.",
    suggestions: "Try one of these",
    citations: "Evidence",
    openDoc: "Open source",
    refusalTitle: "Insufficient retrieved evidence. No answer was generated.",
    refusalBody: "The passages below are lexical neighbors, not an answer.",
    modelDeclinedTitle: "The model judged the evidence insufficient. No answer was generated.",
    modelDeclinedBody: "Retrieval cleared the threshold, but after reading the passages the model determined the evidence could not answer this question.",
    uncitedNote: "The model did not cite sources; showing the retrieved evidence below.",
    invalidCitationsNote: "Ignored out-of-range citation numbers: ",
    llmUnavailable: "The model service is temporarily unavailable. Please try again later.",
    embeddingUnavailable: "The retrieval service is temporarily unavailable.",
    score: "Decision score",
    threshold: "Threshold",
    coverage: "Rare-term coverage",
    paperNote: "Paper search is not configured, so no external library was queried.",
    send: "Send",
    error: "The request did not finish.",
    upload: "Upload",
    uploadTitle: "Add a lab note to the knowledge base",
    title: "Title",
    body: "Body",
    bodyHint: "Split sections with ## headings. Citations point at the section, not the whole file.",
    collection: "Collection",
    visibility: "Visibility",
    grants: "Extra emails",
    grantsHint: "Used only for restricted notes. Separate emails with commas.",
    knowledge: "Lab knowledge",
    project: "Project",
    meeting: "Meeting",
    paper: "Paper note",
    labVis: "Whole lab",
    restricted: "Restricted",
    private: "Owner and admins",
    save: "Ingest",
    saving: "Splitting and storing…",
    cancel: "Cancel",
    docsEmpty: "No documents are visible to you.",
    docsEmptyBody: "Admins and members can upload Markdown. Viewers only read lab-visible notes.",
    delete: "Delete",
    viewerNoUpload: "Viewers cannot upload.",
    back: "Back to documents",
    auditEmpty: "No audit events yet.",
    auditHint: "The log stores actions, resource ids, and model usage. It does not store the question, the answer, or email addresses.",
    retention: "Apply retention",
    action: "Action",
    when: "When",
    actor: "Actor",
    detail: "Detail",
    language: "中文",
    role: "Role",
    loading: "Loading…",
    noSession: "The session expired. Sign in again.",
  },
} as const;

export type Copy = { [K in keyof (typeof copy)["zh"]]: string };

const LocaleContext = createContext<{
  locale: Locale;
  t: Copy;
  toggle: () => void;
} | null>(null);

export function LocaleProvider({
  initial,
  children,
}: {
  initial: Locale;
  children: React.ReactNode;
}) {
  const [locale, setLocale] = useState<Locale>(initial);
  const value = useMemo(
    () => ({
      locale,
      t: copy[locale] as Copy,
      toggle: () => {
        const next = locale === "zh" ? "en" : "zh";
        document.cookie = `la_locale=${next}; path=/; max-age=31536000; samesite=lax`;
        setLocale(next);
      },
    }),
    [locale],
  );
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useI18n() {
  const value = useContext(LocaleContext);
  if (!value) throw new Error("LocaleProvider missing");
  return value;
}
