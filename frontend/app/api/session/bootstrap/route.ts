import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import {
  sessionCookieName,
  sessionCookieOptions,
  verifyOrigin,
} from "../../../../lib/security";

function apiBase() {
  return (process.env.API_INTERNAL_URL || "http://127.0.0.1:43124").replace(/\/$/, "");
}

function extractClientIp(request: Request): string | null {
  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) {
    const first = forwarded.split(",")[0]?.trim();
    if (first) return first;
  }
  const realIp = request.headers.get("x-real-ip")?.trim();
  if (realIp) return realIp;
  return null;
}

function buildProxyHeaders(request: Request, init?: Record<string, string>): Headers {
  const headers = new Headers(init);
  const clientIp = extractClientIp(request);
  if (clientIp) {
    headers.set("X-Client-IP", clientIp);
  }
  const secret = process.env.INTERNAL_PROXY_SECRET?.trim();
  if (secret) {
    headers.set("X-Internal-Proxy-Secret", secret);
  }
  return headers;
}

export async function POST(request: Request) {
  const originDenied = verifyOrigin(request);
  if (originDenied) return originDenied;
  const payload = await request.text();
  const upstream = await fetch(`${apiBase()}/api/v1/auth/bootstrap-admin`, {
    method: "POST",
    headers: buildProxyHeaders(request, { "Content-Type": "application/json" }),
    body: payload,
  });
  const raw = await upstream.text();
  if (!upstream.ok) {
    return new NextResponse(raw, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  }
  const parsed = JSON.parse(raw) as {
    access_token: string;
    expires_in: number;
    user: { id: string; display_name: string; role: string };
  };
  const jar = await cookies();
  jar.set(sessionCookieName(), parsed.access_token, sessionCookieOptions(parsed.expires_in));
  return NextResponse.json({ user: parsed.user });
}
