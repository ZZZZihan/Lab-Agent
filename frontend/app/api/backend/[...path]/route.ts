import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { sessionCookieName, verifyOrigin } from "../../../../lib/security";

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

async function forward(request: Request, path: string[]) {
  const originDenied = verifyOrigin(request);
  if (originDenied) return originDenied;
  const jar = await cookies();
  const token = jar.get(sessionCookieName())?.value;
  if (!token) {
    return NextResponse.json(
      { error: { code: "unauthorized", message: "需要登录" } },
      { status: 401 },
    );
  }
  const headers = new Headers();
  headers.set("Authorization", `Bearer ${token}`);
  const clientIp = extractClientIp(request);
  if (clientIp) headers.set("X-Client-IP", clientIp);
  const secret = process.env.INTERNAL_PROXY_SECRET?.trim();
  if (secret) headers.set("X-Internal-Proxy-Secret", secret);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const ifMatch = request.headers.get("if-match");
  if (ifMatch) headers.set("If-Match", ifMatch);
  const ifNoneMatch = request.headers.get("if-none-match");
  if (ifNoneMatch) headers.set("If-None-Match", ifNoneMatch);
  const method = request.method;
  const upstream = await fetch(`${apiBase()}/api/v1/${path.join("/")}${new URL(request.url).search}`, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer(),
    cache: "no-store",
  });
  const type = upstream.headers.get("content-type") || "application/json";
  const responseHeaders: Record<string, string> = {
    "Content-Type": type,
    "Cache-Control": upstream.headers.get("cache-control") || "no-cache",
    "X-Accel-Buffering": "no",
  };
  for (const hdr of [
    "content-disposition",
    "etag",
    "x-signature-count-included",
    "x-signature-count-missing",
    "x-export-warnings-count",
    "x-export-warnings",
    "x-content-type-options",
    "content-security-policy",
  ]) {
    const val = upstream.headers.get(hdr);
    if (val) responseHeaders[hdr] = val;
  }
  return new NextResponse(upstream.status === 304 ? null : upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
export async function POST(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
export async function PATCH(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
export async function PUT(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
export async function DELETE(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
