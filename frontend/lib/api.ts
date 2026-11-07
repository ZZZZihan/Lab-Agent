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

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    return new ApiError(
      response.status,
      body.error?.code || "request_failed",
      body.error?.message || response.statusText,
    );
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
  application_text: string;
  application_text_with_bio: string;
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
  application_text: string;
  application_text_with_rel: string;
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
  application_text: string;
  application_text_plain: string;
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
  application_text: string;
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
    warnings?: Array<{ code: string; count?: number; section?: string }>;
  };
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
  roster_text: string;
  roster_csv: string;
};

export type RosterRegenResponse = {
  job_id: string;
  format: "text" | "csv" | string;
  roster_text: string;
  roster_csv: string;
};

// Phase 2 provenance & merge arbitration types (pre-reserved for seamless integration)
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

export type MergeDiffItem = {
  entity_type: string;
  entity_id?: string | null;
  entity_label?: string;
  field: string;
  category:
    | "added"
    | "updated"
    | "fill_blank"
    | "time_regression"
    | "manual_protected"
    | "same_time_conflict"
    | string;
  current_value?: unknown;
  current_as_of_time?: string | null;
  current_source_kind?: string | null;
  incoming_value?: unknown;
  incoming_as_of_time?: string | null;
};

export type MergePreviewResponse = {
  job_id: string;
  as_of_time?: string | null;
  counts: {
    added: number;
    updated: number;
    fill_blank: number;
    time_regression: number;
    manual_protected: number;
    same_time_conflict: number;
  };
  items: MergeDiffItem[];
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
