import { NextResponse } from "next/server";

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
  const upstream = await fetch(`${apiBase()}/api/v1/auth/bootstrap-status`, {
    headers: buildProxyHeaders(request),
    cache: "no-store",
  });
  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
