import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const COOKIE = "la_session";

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
  const jar = await cookies();
  const token = jar.get(COOKIE)?.value;
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
  const method = request.method;
  const upstream = await fetch(`${apiBase()}/api/v1/${path.join("/")}${new URL(request.url).search}`, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer(),
    cache: "no-store",
  });
  const type = upstream.headers.get("content-type") || "application/json";
  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": type,
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
export async function POST(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
export async function DELETE(request: Request, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
