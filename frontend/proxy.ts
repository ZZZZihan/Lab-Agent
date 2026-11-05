import { NextResponse, type NextRequest } from "next/server";

import { parseAllowedOrigins, sessionCookieName } from "./lib/security";

const PUBLIC_PATHS = new Set(["/login", "/favicon.ico"]);

function buildLoginUrl(request: NextRequest): URL {
  const forwardedHost = request.headers.get("x-forwarded-host")?.split(",")[0]?.trim();
  const host = forwardedHost || request.headers.get("host")?.trim();
  if (host) {
    const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
    const proto = forwardedProto || request.nextUrl.protocol.replace(/:$/, "") || "http";
    return new URL("/login", `${proto}://${host}`);
  }
  const configuredOrigin = parseAllowedOrigins(process.env.PUBLIC_ORIGIN).values().next().value;
  if (configuredOrigin && request.nextUrl.hostname === "localhost") {
    return new URL("/login", configuredOrigin);
  }
  return new URL("/login", request.url);
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
