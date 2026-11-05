import { NextResponse } from "next/server";

export const DEFAULT_PUBLIC_ORIGIN = "http://127.0.0.1:43123";

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
