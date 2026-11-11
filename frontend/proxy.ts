import { NextResponse, type NextRequest } from "next/server";

import {
  DEFAULT_PUBLIC_ORIGIN,
  parseAllowedOrigins,
  sessionCookieName,
} from "./lib/security";

const PUBLIC_PATHS = new Set(["/login", "/favicon.ico"]);

function normalizeOriginCandidate(protoRaw: string, hostRaw: string): string | null {
  const proto = protoRaw.trim().toLowerCase();
  if (proto !== "http" && proto !== "https") return null;
  const host = hostRaw.trim();
  if (!host || /[/?#@\\\s]/.test(host)) return null;
  try {
    const parsed = new URL(`${proto}://${host}`);
    if (parsed.username || parsed.password || parsed.pathname !== "/") {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

export function buildLoginUrl(
  request: NextRequest,
  env: NodeJS.ProcessEnv = process.env,
): URL {
  const allowedOrigins = parseAllowedOrigins(env.PUBLIC_ORIGIN);
  const firstAllowedOrigin =
    allowedOrigins.values().next().value || DEFAULT_PUBLIC_ORIGIN;
  const hasConfiguredPublicOrigin = Boolean(env.PUBLIC_ORIGIN?.trim());

  const forwardedHost = request.headers
    .get("x-forwarded-host")
    ?.split(",")[0]
    ?.trim();
  const host = forwardedHost || request.headers.get("host")?.trim();
  if (host) {
    const forwardedProto = request.headers
      .get("x-forwarded-proto")
      ?.split(",")[0]
      ?.trim();
    const proto =
      forwardedProto || request.nextUrl.protocol.replace(/:$/, "") || "http";
    const candidateOrigin = normalizeOriginCandidate(proto, host);
    if (candidateOrigin && allowedOrigins.has(candidateOrigin)) {
      return new URL("/login", candidateOrigin);
    }
  }

  if (hasConfiguredPublicOrigin) {
    if (allowedOrigins.has(request.nextUrl.origin)) {
      return new URL("/login", request.nextUrl.origin);
    }
    return new URL("/login", firstAllowedOrigin);
  }

  if (
    request.nextUrl.hostname === "localhost" ||
    request.nextUrl.hostname === "0.0.0.0"
  ) {
    return new URL("/login", firstAllowedOrigin);
  }
  return new URL("/login", request.nextUrl.origin);
}

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (
    PUBLIC_PATHS.has(pathname) ||
    pathname.startsWith("/login/") ||
    pathname.startsWith("/api/") ||
    pathname.startsWith("/_next/")
  ) {
    return NextResponse.next();
  }
  const token = request.cookies.get(sessionCookieName())?.value;
  if (!token) {
    return NextResponse.redirect(buildLoginUrl(request));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon\\.ico|login).*)"],
};
