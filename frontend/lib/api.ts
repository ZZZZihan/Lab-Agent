export type User = { id: string; display_name: string; role: "admin" | "member" | "viewer" };

export type Citation = {
  index?: number;
  chunk_id?: string;
  document_id?: string;
  title?: string;
  heading?: string;
  excerpt?: string;
  score?: number;
};

export type ChatEvent = {
  type: string;
  text?: string;
  reason?: string;
  score?: number;
  cosine?: number;
  coverage?: number;
  threshold?: number;
  items?: Citation[];
  neighbors?: Citation[];
  paper_status?: string | null;
  refused?: boolean;
  conversation_id?: string;
  name?: string;
  status?: string;
  code?: string;
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
