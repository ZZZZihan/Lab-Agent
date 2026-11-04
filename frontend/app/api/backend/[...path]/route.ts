import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const COOKIE = "yz_session";

function apiBase() {
  return (process.env.API_INTERNAL_URL || "http://127.0.0.1:43124").replace(/\/$/, "");
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
