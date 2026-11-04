export type User = { id: string; display_name: string; role: "admin" | "member" | "viewer" };

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
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

export async function currentUser(): Promise<User | null> {
  const response = await fetch("/api/session", { cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw await parseError(response);
  const body = (await response.json()) as { user: User };
  return body.user;
}

export async function login(email: string, password: string): Promise<User> {
  const response = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw await parseError(response);
  const body = (await response.json()) as { user: User };
  return body.user;
}

export async function logout(): Promise<void> {
  await fetch("/api/session", { method: "DELETE" });
}
