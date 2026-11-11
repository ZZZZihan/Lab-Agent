import { NextResponse } from "next/server";

export const DEFAULT_PUBLIC_ORIGIN = "http://127.0.0.1:43123";

/**
 * Public repository default initial password sentinel.
 * Used strictly for blocklist validation and warning detection — never prefill inputs with it.
 */
export const KNOWN_PUBLIC_DEFAULT_PASSWORD = "LabAgent@2026";

export const CONTENT_SECURITY_POLICY =
  "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'";

export const SECURITY_HEADERS = [
  {
    key: "Content-Security-Policy",
    value: CONTENT_SECURITY_POLICY,
  },
  {
    key: "X-Content-Type-Options",
    value: "nosniff",
  },
  {
    key: "Referrer-Policy",
    value: "same-origin",
  },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=()",
  },
];

export function sessionCookieName(env: NodeJS.ProcessEnv = process.env): string {
  return env.COOKIE_SECURE === "true" ? "__Host-la_session" : "la_session";
}

export function sessionCookieOptions(
  maxAge?: number,
  env: NodeJS.ProcessEnv = process.env,
) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: env.COOKIE_SECURE === "true",
    path: "/",
    ...(maxAge !== undefined ? { maxAge } : {}),
  };
}

export function parseAllowedOrigins(raw?: string): Set<string> {
  const source = raw && raw.trim().length > 0 ? raw : DEFAULT_PUBLIC_ORIGIN;
  const origins = source
    .split(",")
    .map((item) => item.trim().replace(/\/$/, ""))
    .filter((item) => item.length > 0);
  return new Set(origins.length > 0 ? origins : [DEFAULT_PUBLIC_ORIGIN]);
}

export function isOriginAllowed(
  originHeader: string | null | undefined,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  if (!originHeader) return false;
  const normalized = originHeader.trim().replace(/\/$/, "");
  if (!normalized || normalized === "null") return false;
  const allowed = parseAllowedOrigins(env.PUBLIC_ORIGIN);
  return allowed.has(normalized);
}

export function verifyOrigin(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
): NextResponse | null {
  if (request.method === "GET" || request.method === "HEAD") {
    return null;
  }
  const origin = request.headers.get("origin");
  if (!isOriginAllowed(origin, env)) {
    return NextResponse.json(
      { error: { code: "forbidden", message: "跨源请求被拒绝" } },
      { status: 403 },
    );
  }
  return null;
}

export const LOGIN_NAME_CLIENT_RE = /^[a-z0-9][a-z0-9._-]{2,63}$/;

export const RESERVED_LOGIN_NAMES_CLIENT = new Set([
  "admin",
  "root",
  "system",
  "api",
  "null",
  "test",
  "labagent",
]);

const DEMO_PASSWORD_PREFIX = "labagent-demo-" + "2026-";

export function isValidLoginNameClient(loginName: string): boolean {
  const normalized = loginName.trim().toLowerCase();
  if (!normalized || RESERVED_LOGIN_NAMES_CLIENT.has(normalized)) {
    return false;
  }
  return LOGIN_NAME_CLIENT_RE.test(normalized);
}

export function validatePasswordClient(
  password: string,
  options?: {
    email?: string | null;
    oldPassword?: string | null;
  },
): string | null {
  if (password.length < 12) {
    return "密码长度至少为 12 个字符";
  }
  if (password.length > 256) {
    return "密码长度不能超过 256 个字符";
  }
  const lowered = password.toLowerCase();
  if (lowered.startsWith(DEMO_PASSWORD_PREFIX)) {
    return "不能使用演示账号密码";
  }
  if (password.trim() === KNOWN_PUBLIC_DEFAULT_PASSWORD) {
    return "不能使用默认公开初始口令";
  }
  if (options?.oldPassword && password === options.oldPassword) {
    return "新口令不能与当前口令相同";
  }
  if (options?.email) {
    const localPart = options.email.split("@")[0]?.trim().toLowerCase() || "";
    if (localPart && lowered.includes(localPart)) {
      return "密码不能包含邮箱的本地部分";
    }
  }
  const classes =
    (/[a-z]/.test(password) ? 1 : 0) +
    (/[A-Z]/.test(password) ? 1 : 0) +
    (/[0-9]/.test(password) ? 1 : 0) +
    (/[^\p{L}\p{N}]/u.test(password) ? 1 : 0);
  if (classes < 3) {
    return "密码至少需包含大写字母、小写字母、数字和特殊字符中的 3 类";
  }
  return null;
}

export function isInitialPasswordRequired(
  hasNewAccounts: boolean,
  oidcMode?: boolean | null,
): boolean {
  return Boolean(hasNewAccounts) && !oidcMode;
}

export function canSubmitProvisioning(params: {
  busy: boolean;
  hasAsOfTime: boolean;
  hasNewAccounts: boolean;
  oidcMode?: boolean | null;
  initialPassword: string;
  unresolvedCount?: number;
}): boolean {
  if (params.busy || !params.hasAsOfTime) return false;
  if ((params.unresolvedCount ?? 0) > 0) return false;
  if (
    isInitialPasswordRequired(params.hasNewAccounts, params.oidcMode) &&
    !params.initialPassword.trim()
  ) {
    return false;
  }
  return true;
}
