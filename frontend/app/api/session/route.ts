import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import {
  sessionCookieName,
  sessionCookieOptions,
  verifyOrigin,
} from "../../../lib/security";

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

export async function GET(request: Request) {
  const jar = await cookies();
  const token = jar.get(sessionCookieName())?.value;
  if (!token) return NextResponse.json({ error: { code: "unauthorized" } }, { status: 401 });
  const upstream = await fetch(`${apiBase()}/api/v1/auth/me`, {
    headers: buildProxyHeaders(request, { Authorization: `Bearer ${token}` }),
    cache: "no-store",
  });
  const body = await upstream.text();
  if (!upstream.ok) {
    return new NextResponse(body, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  }
  return NextResponse.json({ user: JSON.parse(body) });
}

export async function POST(request: Request) {
  const originDenied = verifyOrigin(request);
  if (originDenied) return originDenied;
  const payload = await request.text();
  const upstream = await fetch(`${apiBase()}/api/v1/auth/login`, {
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

export async function DELETE(request: Request) {
  const originDenied = verifyOrigin(request);
  if (originDenied) return originDenied;
  const jar = await cookies();
  const cookieName = sessionCookieName();
  const token = jar.get(cookieName)?.value;
  if (token) {
    await fetch(`${apiBase()}/api/v1/auth/logout`, {
      method: "POST",
      headers: buildProxyHeaders(request, { Authorization: `Bearer ${token}` }),
    }).catch(() => undefined);
  }
  jar.set(cookieName, "", { ...sessionCookieOptions(1), maxAge: 0 });
  return NextResponse.json({ status: "ok" });
}
