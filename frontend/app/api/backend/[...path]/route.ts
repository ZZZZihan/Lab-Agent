import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import {
  apiBase,
  buildProxyHeaders,
  upstreamUnavailableResponse,
} from "../../../../lib/bff";
import {
  sessionCookieName,
  sessionCookieOptions,
  verifyOrigin,
} from "../../../../lib/security";

type StreamingRequestInit = RequestInit & { duplex?: "half" };

async function forward(request: Request, path: string[]) {
  const originDenied = verifyOrigin(request);
  if (originDenied) return originDenied;
  const jar = await cookies();
  const cookieName = sessionCookieName();
  const token = jar.get(cookieName)?.value;
  if (!token) {
    return NextResponse.json(
      { error: { code: "unauthorized", message: "需要登录" } },
      { status: 401 },
    );
  }
  const headers = buildProxyHeaders(request, {
    Authorization: `Bearer ${token}`,
  });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const contentLength = request.headers.get("content-length");
  if (contentLength) headers.set("content-length", contentLength);
  const ifMatch = request.headers.get("if-match");
  if (ifMatch) headers.set("If-Match", ifMatch);
  const ifNoneMatch = request.headers.get("if-none-match");
  if (ifNoneMatch) headers.set("If-None-Match", ifNoneMatch);

  const method = request.method;
  const hasBody = method !== "GET" && method !== "HEAD" && request.body !== null;
  const fetchInit: StreamingRequestInit = {
    method,
    headers,
    body: hasBody ? request.body : undefined,
    ...(hasBody ? { duplex: "half" as const } : {}),
    cache: "no-store",
  };

  let upstream: Response;
  try {
    upstream = await fetch(
      `${apiBase()}/api/v1/${path.join("/")}${new URL(request.url).search}`,
      fetchInit,
    );
  } catch {
    return upstreamUnavailableResponse();
  }

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

  const joinedPath = path.join("/");
  if (joinedPath === "auth/change-password") {
    const raw = await upstream.text();
    if (upstream.ok) {
      try {
        const parsed = JSON.parse(raw) as {
          access_token?: string;
          expires_in?: number;
        };
        if (parsed.access_token) {
          jar.set(
            cookieName,
            parsed.access_token,
            sessionCookieOptions(parsed.expires_in || 28800),
          );
        }
      } catch {
        // ignore JSON parse errors
      }
    } else if (upstream.status === 401) {
      let errorCode = "";
      try {
        const parsedErr = JSON.parse(raw) as {
          error?: { code?: string };
          detail?: { code?: string };
        };
        errorCode = parsedErr.error?.code || parsedErr.detail?.code || "";
      } catch {
        // ignore JSON parse errors
      }
      if (errorCode !== "invalid_credentials") {
        jar.set(cookieName, "", { ...sessionCookieOptions(1), maxAge: 0 });
      }
    }
    return new NextResponse(raw, {
      status: upstream.status,
      headers: responseHeaders,
    });
  }

  if (upstream.status === 401) {
    jar.set(cookieName, "", { ...sessionCookieOptions(1), maxAge: 0 });
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
