export type User = {
  id: string;
  display_name: string;
  role: "admin" | "member" | "viewer";
  email?: string | null;
  login_name?: string | null;
  must_change_password?: boolean;
  must_change_password_by?: string | null;
};

export type Citation = {
  index?: number;
  chunk_id?: string;
  document_id?: string;
  doc_key?: string | null;
  chunk_index?: number;
  title?: string;
  heading?: string;
  collection?: string;
  excerpt?: string;
  score?: number;
};

export type RetrievalHit = {
  chunk_id: string;
  document_id: string;
  title?: string;
  heading?: string;
  score?: number;
};

export type RefusalReason = "no_hits" | "low_score" | "low_coverage" | "model_declined";

export type TokenUsage = {
  prompt_tokens: number;
  completion_tokens: number;
};

/**
 * SSE event contract emitted by POST /api/v1/chat/stream:
 * - status:         { phase }
 * - tool:           { name, status }
 * - retrieval:      { mode, coverage, elapsed_ms, hits[], refused }
 * - citations:      { items[], paper_status }
 * - token:          { text }
 * - citation_check: { used[], invalid[], uncited }
 * - refusal:        { reason, score, cosine, coverage, threshold, neighbors[], paper_status? }
 * - error:          { code }
 * - done:           { conversation_id, message_id, refused, model, usage, error? }
 */
export type ChatEvent = {
  type:
    | "status"
    | "tool"
    | "retrieval"
    | "citations"
    | "token"
    | "citation_check"
    | "refusal"
    | "error"
    | "done"
    | string;
  phase?: string;
  name?: string;
  status?: string;
  mode?: string;
  coverage?: number;
  elapsed_ms?: number;
  hits?: RetrievalHit[];
  refused?: boolean;
  items?: Citation[];
  paper_status?: string | null;
  text?: string;
  used?: number[];
  invalid?: number[];
  uncited?: boolean;
  reason?: RefusalReason | string;
  score?: number;
  cosine?: number;
  threshold?: number;
  neighbors?: Citation[];
  code?: string;
  conversation_id?: string;
  message_id?: string;
  model?: string;
  usage?: TokenUsage;
  error?: string;
};

export type MissingBasketRef = {
  entity?: string;
  kind?: string;
  id: string | number;
};

export class ApiError extends Error {
  code: string;
  status: number;
  missing?: MissingBasketRef[];
  constructor(
    status: number,
    code: string,
    message: string,
    missing?: MissingBasketRef[],
  ) {
    super(message);
    this.status = status;
    this.code = code;
    if (missing) {
      this.missing = missing;
    }
  }
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as {
      error?: {
        code?: string;
        message?: string;
        missing?: MissingBasketRef[];
      };
      detail?:
        | {
            code?: string;
            message?: string;
            missing?: MissingBasketRef[];
          }
        | string;
    };
    const detailObj =
      body.detail && typeof body.detail === "object" ? body.detail : undefined;
    const detailStr =
      typeof body.detail === "string" ? body.detail : undefined;
    const code =
      body.error?.code || detailObj?.code || "request_failed";
    const message =
      body.error?.message ||
      detailObj?.message ||
      detailStr ||
      response.statusText;
    const missing = Array.isArray(body.error?.missing)
      ? body.error.missing
      : Array.isArray(detailObj?.missing)
        ? detailObj.missing
        : undefined;
    return new ApiError(response.status, code, message, missing);
  } catch {
    return new ApiError(response.status, "request_failed", response.statusText);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend/${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    const err = await parseError(response);
    if (
      err.status === 403 &&
      err.code === "password_change_required" &&
      typeof window !== "undefined" &&
      window.location.pathname !== "/change-password" &&
      window.location.pathname !== "/login"
    ) {
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/change-password");
    }
    throw err;
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function currentUser(): Promise<User | null> {
  const response = await fetch("/api/session", { cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw await parseError(response);
  const body = (await response.json()) as { user: User };
  return body.user;
}

export async function login(identifier: string, password: string): Promise<User> {
  const response = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier, email: identifier, password }),
  });
  if (!response.ok) throw await parseError(response);
  const body = (await response.json()) as { user: User };
  return body.user;
}

export async function changePassword(
  old_password: string,
  new_password: string,
  email?: string,
): Promise<User> {
  const res = await api<{ status: string; user: User }>("auth/change-password", {
    method: "POST",
    body: JSON.stringify({
      old_password,
      new_password,
      ...(email && email.trim() ? { email: email.trim() } : {}),
    }),
  });
  return res.user;
}

export async function logout(): Promise<void> {
  await fetch("/api/session", { method: "DELETE" });
}

export type FieldProvenanceSummary = {
  source_kind: "import" | "manual" | "legacy" | string;
  as_of_time?: string | null;
  source_job_id?: string | null;
  has_manual_override?: boolean;
  imported_value?: unknown;
};

export type LabPersonItem = {
  id: string;
  user_id?: string | null;
  name: string;
  email?: string | null;
  title?: string | null;
  admin_duty?: string | null;
  tutor_role?: string | null;
  research_specialty?: string | null;
  degree?: string | null;
  affiliation?: string | null;
  project_role: "applicant" | "member" | string;
  bio?: string | null;
  as_of_time?: string | null;
  source_import_id?: string | null;
  application_text: string;
  application_text_with_bio: string;
  copy_text?: string;
  bio_text?: string | null;
  has_signature?: boolean;
  signature_mime?: string | null;
  signature_sha256?: string | null;
  signature_updated_at?: string | null;
  signature_source?: "docx_import" | "upload" | string | null;
  signature_url?: string | null;
  name_ambiguous?: boolean;
  can_edit?: boolean;
  provenance?: Record<string, FieldProvenanceSummary>;
};

export type LabProjectItem = {
  id: string;
  title: string;
  funder?: string | null;
  grant_no?: string | null;
  pi_name?: string | null;
  lab_role: "host" | "participant" | "unknown" | string;
  start_date?: string | null;
  end_date?: string | null;
  amount_wan?: string | null;
  status: "ongoing" | "completed" | "unknown" | string;
  status_label: string;
  status_source: "explicit" | "inferred" | string;
  relationship_note?: string | null;
  raw_text: string;
  as_of_time?: string | null;
  source_import_id?: string | null;
  application_text: string;
  application_text_with_rel: string;
  provenance?: Record<string, FieldProvenanceSummary>;
};

export type LabAchievementItem = {
  id: string;
  kind: string;
  kind_label: string;
  title: string;
  first_author?: string | null;
  authors_text?: string | null;
  venue?: string | null;
  year?: number | null;
  support_note?: string | null;
  raw_text: string;
  parse_quality: string;
  as_of_time?: string | null;
  source_import_id?: string | null;
  application_text: string;
  application_text_plain: string;
  provenance?: Record<string, FieldProvenanceSummary>;
};

export type LabFacilityItem = {
  id: string;
  category: string;
  name: string;
  spec?: string | null;
  value_wan?: string | null;
  usage_scenario?: string | null;
  raw_text: string;
  as_of_time?: string | null;
  source_import_id?: string | null;
  application_text: string;
  provenance?: Record<string, FieldProvenanceSummary>;
};

export type LabOverview = {
  counts: {
    people: number;
    projects: number;
    projects_ongoing: number;
    projects_completed: number;
    achievements: number;
    facilities: number;
  };
  as_of_time?: string | null;
  latest_import?: {
    id: string;
    original_filename: string;
    as_of_time?: string | null;
    provisioned_at?: string | null;
  } | null;
};

export type ImportPreviewPerson = {
  key: string;
  name: string;
  project_role: string;
  tier: "T1" | "T2" | "T3" | string;
  login_name: string;
  email?: string | null;
  title?: string | null;
  admin_duty?: string | null;
  tutor_role?: string | null;
  research_specialty?: string | null;
  degree?: string | null;
  affiliation?: string | null;
  match?: { status: "new" | "existing" | "conflict" | string; user_id?: string };
  warnings?: string[];
  exclude?: boolean;
  override_mode?: "new" | "existing" | "exclude";
  override_login_name?: string;
  override_link_user_id?: string;
};

export type ImportJobStats = {
  people_parsed?: number;
  achievements_parsed?: number;
  projects_parsed?: number;
  facilities_parsed?: number;
  pii_dropped?: number;
  users_created?: number;
  users_linked?: number;
  people_inserted?: number;
  people_skipped?: number;
  achievements_inserted?: number;
  achievements_skipped?: number;
  projects_inserted?: number;
  projects_skipped?: number;
  facilities_inserted?: number;
  facilities_skipped?: number;
  rag_doc_key?: string;
};

export type ImportJobSummary = {
  id: string;
  job_id: string;
  uploaded_by?: string;
  original_filename: string;
  content_sha256?: string;
  parser_version?: string;
  as_of_time?: string | null;
  status: "parsed" | "provisioned" | "merged" | "failed" | string;
  stats: ImportJobStats;
  error_code?: string | null;
  rag_document_id?: string | null;
  created_at?: string | null;
  provisioned_at?: string | null;
  provisioned_by?: string | null;
};

export type ImportJobResponse = {
  id: string;
  job_id: string;
  original_filename: string;
  as_of_time?: string | null;
  status: string;
  error_code?: string | null;
  created_at?: string | null;
  provisioned_at?: string | null;
  deduplicated?: boolean;
  stats: ImportJobStats;
  preview: {
    as_of_time?: string | null;
    extraction_mode?: string;
    cover?: {
      project_title?: string | null;
      applicant_name?: string | null;
      affiliation?: string | null;
      as_of_time?: string | null;
    };
    people?: ImportPreviewPerson[];
    projects?: Array<{
      key: string;
      title: string;
      grant_no?: string | null;
      funder?: string | null;
      pi_name?: string | null;
      status: string;
      status_source: string;
    }>;
    achievements?: Array<{
      key: string;
      kind: string;
      title: string;
      first_author?: string | null;
      venue?: string | null;
      year?: number | null;
    }>;
    facilities?: Array<{
      key: string;
      category: string;
      name: string;
      spec?: string | null;
      value_wan?: string | null;
    }>;
    signatures?:
      | ImportPreviewSignatureItem[]
      | {
          summary?: {
            found_images?: number;
            bound?: number;
            rejected?: number;
            ambiguous?: number;
            warnings?: number;
          };
          items?: ImportPreviewSignatureItem[];
          warnings?: Array<string | { code: string; message?: string }>;
        };
    warnings?: Array<{
      code: string;
      count?: number;
      section?: string;
      person_temp_id?: string;
      person_name?: string;
      person_temp_ids?: string[];
      person_names?: string[];
    }>;
  };
};

export type ImportPreviewSignatureItem = {
  sig_id?: string;
  person_temp_id?: string | null;
  role: "pi" | "member" | string;
  person_ref?: string | null;
  person_key?: string | null;
  person_name?: string | null;
  status?: "bound" | "rejected" | "ambiguous" | string;
  sha256?: string | null;
  mime?: string | null;
  width?: number | null;
  height?: number | null;
  size?: number;
  source_kind?: string;
  source_location?: Record<string, unknown>;
  warnings?: string[];
};

export type ProvisionResult = {
  job_id: string;
  status: string;
  as_of_time: string;
  deadline: string | null;
  initial_password?: string | null;
  rag_document_id?: string | null;
  rag_doc_key: string;
  stats: {
    users_created: number;
    users_linked: number;
    people_inserted: number;
    people_skipped: number;
    achievements_inserted: number;
    achievements_skipped: number;
    projects_inserted: number;
    projects_skipped: number;
    facilities_inserted: number;
    facilities_skipped: number;
  };
  signature_result?: {
    written: number;
    unchanged: number;
    kept_upload: number;
    skipped: number;
    rejected: number;
    items?: Array<{ sig_id: string; person_id?: string | number; outcome: string }>;
  };
  roster_text: string;
  roster_csv: string;
};

export type RosterRegenResponse = {
  job_id: string;
  format: "text" | "csv" | string;
  roster_text: string;
  roster_csv: string;
};

// Phase 2 provenance & merge arbitration types
export type FieldProvenanceEntry = {
  id: string | number;
  entity_type: "person" | "project" | "achievement" | "facility" | string;
  entity_id: string;
  field: string;
  value: unknown;
  source_kind: "import" | "manual" | "legacy" | string;
  source_job_id?: string | null;
  as_of_time?: string | null;
  recorded_at?: string | null;
  recorded_by?: string | null;
  superseded: boolean;
};

export type MergeChangeCategory =
  | "added"
  | "updated"
  | "fill_blank"
  | "time_regression"
  | "manual_protected"
  | "same_time_conflict";

export type MergeChangeItem = {
  change_key: string;
  category: MergeChangeCategory;
  entity_type: string;
  entity_id: string;
  entity_name: string;
  field: string;
  current_value: unknown;
  current_as_of_time?: string | null;
  current_source_kind?: string | null;
  current_source_job_id?: string | null;
  incoming_value: unknown;
  incoming_as_of_time: string;
  default_action: "adopt_incoming" | "keep_current";
  requires_decision: boolean;
};

export type MergePreviewResponse = {
  job_id: string;
  as_of_time: string;
  summary: {
    added: number;
    updated: number;
    fill_blank: number;
    time_regression: number;
    manual_protected: number;
    same_time_conflict: number;
  };
  changes: MergeChangeItem[];
  added_entities: Array<{
    entity_type: string;
    name: string;
    fingerprint?: string;
  }>;
};

export type MergeDecision = {
  change_key: string;
  action: "adopt_incoming" | "keep_current" | "force_adopt";
};

export type MergeExecuteResponse = {
  job_id: string;
  status: string;
  as_of_time?: string;
  rag_document_id?: string | null;
  rag_doc_key?: string;
  stats?: {
    added_entities?: number;
    updated_fields?: number;
    kept_fields?: number;
    forced_fields?: number;
    rag_doc_key?: string;
  };
  added_count?: number;
  updated_count?: number;
  fill_blank_count?: number;
  forced_count?: number;
  summary?: Record<string, number>;
};

export type RevertMergeResponse = {
  job_id?: string;
  reverted_fields: number;
  deleted_entities: number;
  job_status: string;
  rag_doc_key?: string;
};

export async function listImportJobs(): Promise<ImportJobSummary[]> {
  const res = await api<{ imports: ImportJobSummary[] }>("admin/imports");
  return res.imports;
}

export async function getImportJob(jobId: string): Promise<ImportJobResponse> {
  return api<ImportJobResponse>(`admin/imports/${jobId}`);
}

export async function deleteImportJob(
  jobId: string,
): Promise<{ status: string; id: string; job_id: string }> {
  return api<{ status: string; id: string; job_id: string }>(
    `admin/imports/${jobId}`,
    { method: "DELETE" },
  );
}

export async function getImportRoster(
  jobId: string,
  format: "text" | "csv" = "text",
): Promise<RosterRegenResponse> {
  return api<RosterRegenResponse>(`admin/imports/${jobId}/roster?format=${format}`);
}

export async function updateImportPreview(
  jobId: string,
  payload: Record<string, unknown>,
): Promise<ImportJobResponse> {
  return api<ImportJobResponse>(`admin/imports/${jobId}/preview`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function reparseImportJob(
  jobId: string,
  mode: "llm" | "fsm" = "fsm",
): Promise<ImportJobResponse> {
  return api<ImportJobResponse>(`admin/imports/${jobId}/reparse`, {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
}

export async function previewImportMerge(
  jobId: string,
  asOfTime?: string,
): Promise<MergePreviewResponse> {
  return api<MergePreviewResponse>(`admin/imports/${jobId}/merge-preview`, {
    method: "POST",
    body: JSON.stringify(asOfTime && asOfTime.trim() ? { as_of_time: asOfTime.trim() } : {}),
  });
}

export async function executeImportMerge(
  jobId: string,
  payload: { as_of_time?: string; decisions: MergeDecision[] },
): Promise<MergeExecuteResponse> {
  return api<MergeExecuteResponse>(`admin/imports/${jobId}/merge`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function revertImportMerge(
  jobId: string,
): Promise<RevertMergeResponse> {
  return api<RevertMergeResponse>(`admin/imports/${jobId}/revert`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function updateLabEntity(
  type: "people" | "projects" | "achievements" | "facilities",
  id: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return api<Record<string, unknown>>(`lab/${type}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function restoreImportedField(
  type: "people" | "projects" | "achievements" | "facilities",
  id: string,
  field: string,
): Promise<Record<string, unknown>> {
  return api<Record<string, unknown>>(`lab/${type}/${id}/restore-imported`, {
    method: "POST",
    body: JSON.stringify({ field }),
  });
}

export async function reindexLabAssets(): Promise<{
  status: string;
  rag_doc_key: string;
  rag_document_id?: string | null;
}> {
  return api<{
    status: string;
    rag_doc_key: string;
    rag_document_id?: string | null;
  }>("lab/reindex", { method: "POST" });
}

// ============================================================================
// Spec v2.2: Person Dossier, Electronic Signatures & Proposal Reuse Basket API
// ============================================================================

export type DossierProjectItem = {
  id: string;
  title: string;
  year?: number | null;
  funder?: string | null;
  source?: string | null;
  grant_no?: string | null;
  code?: string | null;
  pi_name?: string | null;
  status?: string;
  status_label?: string;
  start_date?: string | null;
  end_date?: string | null;
  relation: "pi" | "participant" | "host" | string;
  relation_role?: "pi" | "participant" | "host" | string;
  match?: "exact" | "ambiguous" | string;
  copy_text?: string;
  application_text?: string;
  application_text_with_rel?: string;
};

export type DossierAchievementItem = {
  id: string;
  title: string;
  kind?: string;
  type?: string;
  kind_label?: string;
  venue?: string | null;
  year?: number | null;
  authors?: string | null;
  relation: "first_author" | "coauthor" | string;
  relation_role?: "first_author" | "coauthor" | string;
  author_position?: number | null;
  match?: "exact" | "ambiguous" | string;
  copy_text?: string;
  application_text?: string;
  application_text_plain?: string;
};

export type PersonDossierResponse = {
  person: LabPersonItem & {
    org?: string | null;
  };
  can_edit?: boolean;
  signature?: {
    has_signature?: boolean;
    can_edit?: boolean;
    url?: string | null;
    sha256?: string | null;
    signature_sha256?: string | null;
    mime?: string | null;
    signature_mime?: string | null;
    updated_at?: string | null;
    signature_updated_at?: string | null;
    source?: "docx_import" | "upload" | string | null;
    signature_source?: "docx_import" | "upload" | string | null;
  };
  projects: DossierProjectItem[];
  achievements?: DossierAchievementItem[];
  outputs?: DossierAchievementItem[];
  counts?: {
    projects?: number;
    projects_total?: number;
    projects_as_pi?: number;
    projects_as_participant?: number;
    outputs?: number;
    outputs_total?: number;
    outputs_first_author?: number;
    outputs_coauthor?: number;
    achievements?: number;
    achievements_total?: number;
    achievements_first_author?: number;
    achievements_coauthor?: number;
  };
  truncated?: {
    projects: boolean;
    outputs?: boolean;
    achievements?: boolean;
  };
};

export type SignatureUploadResponse = {
  person_id: string;
  has_signature: boolean;
  signature_sha256: string;
  signature_mime: string;
  signature_updated_at: string;
  signature_source: "upload" | "docx_import" | string;
  width?: number;
  height?: number;
  unchanged?: boolean;
};

export type BasketEntityKind = "person" | "project" | "achievement" | "facility";

export type BasketItemRef = {
  kind: BasketEntityKind | string;
  entity?: BasketEntityKind | string;
  id: string;
};

export type BasketItem = {
  kind: BasketEntityKind;
  id: string;
  label: string;
};

export type BasketOptions = {
  with_support: boolean;
  with_relation: boolean;
  numbering: "bracket" | "arabic" | "none";
  group_by_kind: boolean;
  include_signatures: boolean;
  naming: "seq_name" | "name" | "name_id";
};

export type BasketState = {
  version: 1;
  items: BasketItem[];
  options: BasketOptions;
};

export type BatchFormatOptions = {
  style?: "nsfc_standard" | "compact" | "plain" | string;
  with_support?: boolean;
  include_support_note?: boolean;
  with_relation?: boolean;
  include_relationship_note?: boolean;
  include_bio?: boolean;
  include_person_bio?: boolean;
  numbering?: "none" | "arabic" | "decimal" | "bracket";
  separator?: "blank_line" | "newline";
  group_by_kind?: boolean;
  group_by_entity?: boolean;
  include_signatures_annex?: boolean;
};

export type BatchFormatResponse = {
  text: string;
  combined_text?: string;
  lines?: string[];
  count?: number;
  items?: Array<{
    kind: string;
    entity?: string;
    id: string;
    ok: boolean;
    text?: string;
    error?: string;
  }>;
  stats?: {
    requested: number;
    deduplicated: number;
    ok: number;
    missing: number;
  };
};

export type ExportSelectedOptions = {
  style?: "nsfc_standard" | "compact" | "plain" | string;
  with_support?: boolean;
  include_support_note?: boolean;
  with_relation?: boolean;
  include_relationship_note?: boolean;
  include_bio?: boolean;
  include_person_bio?: boolean;
  numbering?: "none" | "arabic" | "decimal" | "bracket";
  group_by_kind?: boolean;
  group_by_entity?: boolean;
  include_signatures?: boolean;
  include_signatures_annex?: boolean;
  signature_width_cm?: number;
  title?: string;
};

export type BlobDownloadResult = {
  blob: Blob;
  filename: string;
  includedCount: number | null;
  missingCount: number | null;
};

const LAB_ERROR_MESSAGES: Record<string, string> = {
  SIGNATURE_TOO_LARGE: "签名图片需小于 2 MB",
  SIGNATURE_REJECTED_FORMAT: "仅支持 PNG / JPEG / WebP 格式",
  SIGNATURE_CORRUPT: "图片文件已损坏",
  SIGNATURE_CORRUPT_IN_STORAGE: "存储中的电子签名数据异常",
  SIGNATURE_BAD_DIMENSIONS: "图片尺寸不符合要求（8–6000 像素）",
  FORBIDDEN: "仅本人或管理员可修改签名",
  FORBIDDEN_SIGNATURE_EDIT: "仅本人或管理员可修改签名",
  PRECONDITION_FAILED: "签名已被他人更新，请刷新",
  SIGNATURE_PRECONDITION_FAILED: "签名已被他人更新，请刷新",
  SIGNATURE_NOT_FOUND: "该成员暂无签名",
  PERSON_NOT_FOUND: "成员不存在或已删除",
  NOT_FOUND: "部分条目或成员不存在",
  SIGNATURE_FILE_MISSING: "请选择要上传的签名图片文件",
  BASKET_EMPTY: "素材篮为空",
  BASKET_TOO_LARGE: "素材篮超出上限（最多 200 项）",
  BATCH_LIMIT_EXCEEDED: "素材篮超出上限（最多 200 项）",
  ZIP_PERSON_LIMIT_EXCEEDED: "单次打包签名人数超出限制（最多 50 人）",
  ZIP_SIZE_LIMIT_EXCEEDED: "签名包总大小超过 50 MB 上限",
  INVALID_FILENAME_TEMPLATE: "签名文件命名模板无效",
  NO_SIGNATURES_SELECTED: "所选成员均无签名",
  BASKET_ALL_MISSING: "所选条目均已不存在",
  FORMAT_KIND_UNSUPPORTED: "包含不支持的素材类型",
  SIG_WEBP_NOT_EMBEDDED: "部分签名为 WebP 格式，未嵌入 Word，请使用签名 ZIP",
  SIG_NOT_UPLOADED: "部分成员未上传签名",
  SIG_CORRUPT: "部分签名损坏",
  SIG_EXPORT_QUOTA: "超出导出体积上限，部分签名未嵌入",
  ITEM_NOT_FOUND: "部分条目已不存在",
  SIG_COLUMN_FALLBACK: "未找到“签字”表头，已按最后一列识别签名",
  SIG_INVALID_IMAGE: "签名图片格式或尺寸校验未通过",
  SIG_TOO_LARGE: "签名图片超过 2 MB，已跳过",
  SIG_MULTIPLE_IN_CELL: "同一签名格内存在多张图片，未自动绑定",
  SIGNATURE_MULTIPLE_IN_ROW: "同一成员行包含多张签名图片，已跳过自动绑定",
  SIGNATURE_UNEXPECTED_CELL: "非签名列发现图片，已忽略",
  SIG_ORPHAN: "签名列存在图片但该行无姓名，未绑定",
  SIG_PI_AMBIGUOUS: "负责人签章区图片无法唯一确定",
  SIGNATURE_AMBIGUOUS_PI: "负责人签章区存在多张候选图片，未自动绑定",
  SIGNATURE_AMBIGUOUS_PERSON: "存在同名成员歧义，签名已跳过自动入库",
  SIG_PI_CONFLICT: "负责人成员行签名与签章区签名不一致，已保留成员行签名",
  SIG_DUPLICATE_HASH: "同一签名图片被绑定到多名成员，请核对",
  SIG_MISSING_PART: "文档内引用的签名图片部件缺失",
  SIG_IMPORT_QUOTA: "超出单次导入签名图片配额上限",
  SIG_PERSON_AMBIGUOUS: "存在同名成员，签名已跳过自动覆盖",
};

export function mapLabErrorCode(
  code?: string | null,
  fallbackMessage?: string,
): string {
  if (!code) return fallbackMessage || "操作失败，请稍后重试";
  const upper = code.trim().toUpperCase();
  if (LAB_ERROR_MESSAGES[upper]) return LAB_ERROR_MESSAGES[upper];
  if (fallbackMessage && fallbackMessage !== code && fallbackMessage !== "request_failed") {
    return fallbackMessage;
  }
  return `操作失败（${code}），请稍后重试`;
}

export function parseFilenameFromDisposition(
  disposition: string | null,
  fallback: string,
): string {
  if (!disposition) return fallback;
  const starMatch = disposition.match(/filename\*\s*=\s*UTF-8''([^;\s]+)/i);
  if (starMatch && starMatch[1]) {
    try {
      return decodeURIComponent(starMatch[1].replace(/^"|"$/g, ""));
    } catch {
      return starMatch[1];
    }
  }
  const plainMatch = disposition.match(/filename\s*=\s*"?([^";]+)"?/i);
  if (plainMatch && plainMatch[1]) {
    return plainMatch[1].trim();
  }
  return fallback;
}

export async function getPersonDossier(
  personId: string,
  signal?: AbortSignal,
): Promise<PersonDossierResponse> {
  const res = await api<PersonDossierResponse>(
    `lab/people/${encodeURIComponent(personId)}/dossier`,
    signal ? { signal } : undefined,
  );
  const normalizeProject = (p: DossierProjectItem): DossierProjectItem => {
    const role = p.relation_role ?? p.relation ?? "participant";
    return {
      ...p,
      relation: role,
      relation_role: role,
    };
  };
  const normalizeAchievement = (a: DossierAchievementItem): DossierAchievementItem => {
    const role = a.relation_role ?? a.relation ?? "coauthor";
    return {
      ...a,
      relation: role,
      relation_role: role,
    };
  };
  return {
    ...res,
    projects: (res.projects || []).map(normalizeProject),
    achievements: res.achievements ? res.achievements.map(normalizeAchievement) : undefined,
    outputs: res.outputs ? res.outputs.map(normalizeAchievement) : undefined,
  };
}

export function getPersonSignatureUrl(
  personId: string,
  disposition: "inline" | "attachment" = "inline",
  sha256?: string | null,
): string {
  const params = new URLSearchParams({ disposition });
  if (sha256) {
    params.set("v", sha256);
  }
  return `/api/backend/lab/people/${encodeURIComponent(personId)}/signature?${params.toString()}`;
}

export async function uploadPersonSignature(
  personId: string,
  file: File,
  ifMatchSha?: string | null,
): Promise<SignatureUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const headers: Record<string, string> = {};
  if (ifMatchSha) {
    const cleanSha = ifMatchSha.replace(/^"|"$/g, "");
    headers["If-Match"] = `"${cleanSha}"`;
  }
  return api<SignatureUploadResponse>(
    `lab/people/${encodeURIComponent(personId)}/signature`,
    {
      method: "PUT",
      headers,
      body: form,
    },
  );
}

export async function deletePersonSignature(
  personId: string,
  ifMatchSha?: string | null,
): Promise<void> {
  const headers: Record<string, string> = {};
  if (ifMatchSha) {
    const cleanSha = ifMatchSha.replace(/^"|"$/g, "");
    headers["If-Match"] = `"${cleanSha}"`;
  }
  await api<void>(`lab/people/${encodeURIComponent(personId)}/signature`, {
    method: "DELETE",
    headers,
  });
}

export async function formatLabBatch(
  items: BasketItemRef[],
  options?: BatchFormatOptions,
): Promise<BatchFormatResponse> {
  const rawNumbering = options?.numbering ?? "bracket";
  const backendNumbering = rawNumbering === "arabic" ? "decimal" : rawNumbering;
  const includeSupportNote =
    options?.include_support_note ?? options?.with_support ?? true;
  const includeRelationshipNote =
    options?.include_relationship_note ??
    options?.with_relation ??
    includeSupportNote;
  const backendOptions = {
    style: options?.style ?? "nsfc_standard",
    numbering: backendNumbering,
    group_by_entity: options?.group_by_entity ?? options?.group_by_kind ?? false,
    include_person_bio: options?.include_person_bio ?? options?.include_bio ?? false,
    include_support_note: includeSupportNote,
    include_relationship_note: includeRelationshipNote,
    include_signatures_annex: options?.include_signatures_annex ?? false,
  };
  const res = await api<BatchFormatResponse>("lab/format/batch", {
    method: "POST",
    body: JSON.stringify({
      items: items.map((it) => ({
        entity: it.entity || it.kind || "person",
        id: it.id,
      })),
      options: backendOptions,
    }),
    signal:
      typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
        ? AbortSignal.timeout(60000)
        : undefined,
  });
  return {
    ...res,
    text: res.text ?? res.combined_text ?? "",
    items: res.items?.map((it) => ({
      ...it,
      kind: it.kind || it.entity || "person",
      ok: it.ok ?? true,
    })),
  };
}

export async function downloadSignaturesZip(
  personIds: string[],
  naming: "seq_name" | "name" | "name_id" = "seq_name",
  _includeManifest?: boolean,
): Promise<BlobDownloadResult> {
  void _includeManifest;
  const response = await fetch("/api/backend/lab/people/signatures:zip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      person_ids: personIds,
      filename_template: naming,
    }),
    cache: "no-store",
    signal:
      typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
        ? AbortSignal.timeout(60000)
        : undefined,
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  const blob = await response.blob();
  const filename = parseFilenameFromDisposition(
    response.headers.get("content-disposition"),
    `课题组签名_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.zip`,
  );
  const incRaw =
    response.headers.get("x-signature-count-included") ??
    response.headers.get("x-signatures-included");
  const missRaw =
    response.headers.get("x-signature-count-missing") ??
    response.headers.get("x-signatures-missing");
  return {
    blob,
    filename,
    includedCount: incRaw !== null && incRaw !== "" ? Number(incRaw) : null,
    missingCount: missRaw !== null && missRaw !== "" ? Number(missRaw) : null,
  };
}

export async function exportSelectedLabAssets(
  items: BasketItemRef[],
  options?: ExportSelectedOptions,
): Promise<BlobDownloadResult & { warnings: string[] }> {
  const rawNumbering = options?.numbering ?? "bracket";
  const backendNumbering = rawNumbering === "arabic" ? "decimal" : rawNumbering;
  const title = options?.title ?? "写本子素材汇总";
  const includeSupportNote =
    options?.include_support_note ?? options?.with_support ?? true;
  const includeRelationshipNote =
    options?.include_relationship_note ??
    options?.with_relation ??
    includeSupportNote;
  const backendOptions = {
    style: options?.style ?? "nsfc_standard",
    numbering: backendNumbering,
    group_by_entity: options?.group_by_entity ?? options?.group_by_kind ?? true,
    include_person_bio: options?.include_person_bio ?? options?.include_bio ?? false,
    include_support_note: includeSupportNote,
    include_relationship_note: includeRelationshipNote,
    include_signatures_annex:
      options?.include_signatures_annex ?? options?.include_signatures ?? true,
  };
  const response = await fetch("/api/backend/lab/export:selected", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      items: items.map((it) => ({
        entity: it.entity || it.kind || "person",
        id: it.id,
      })),
      options: backendOptions,
    }),
    cache: "no-store",
    signal:
      typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
        ? AbortSignal.timeout(60000)
        : undefined,
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  const blob = await response.blob();
  const filename = parseFilenameFromDisposition(
    response.headers.get("content-disposition"),
    `写本子素材_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.docx`,
  );
  const warnHeader = response.headers.get("x-export-warnings") || "";
  const warnings = warnHeader
    .split(",")
    .map((w) => w.trim())
    .filter(Boolean);
  const warnCountRaw = response.headers.get("x-export-warnings-count");
  const warnCount = warnCountRaw ? Number(warnCountRaw) : 0;
  if (warnings.length === 0 && warnCount > 0) {
    warnings.push(`导出包含 ${warnCount} 项签名或素材提示，请查阅文档末尾提示信息`);
  }
  return {
    blob,
    filename,
    includedCount: null,
    missingCount: null,
    warnings,
  };
}
