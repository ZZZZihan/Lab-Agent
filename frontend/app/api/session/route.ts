import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import {
  apiBase,
  buildProxyHeaders,
  upstreamUnavailableResponse,
} from "../../../lib/bff";
import {
  sessionCookieName,
  sessionCookieOptions,
  verifyOrigin,
} from "../../../lib/security";

export async function GET(request: Request) {
  const jar = await cookies();
  const cookieName = sessionCookieName();
  const token = jar.get(cookieName)?.value;
  if (!token) {
    return NextResponse.json({ error: { code: "unauthorized" } }, { status: 401 });
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/v1/auth/me`, {
      headers: buildProxyHeaders(request, { Authorization: `Bearer ${token}` }),
      cache: "no-store",
    });
  } catch {
    return upstreamUnavailableResponse();
  }
  const body = await upstream.text();
  if (!upstream.ok) {
    if (upstream.status === 401) {
      jar.set(cookieName, "", { ...sessionCookieOptions(1), maxAge: 0 });
    }
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
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/v1/auth/login`, {
      method: "POST",
      headers: buildProxyHeaders(request, { "Content-Type": "application/json" }),
      body: payload,
    });
  } catch {
    return upstreamUnavailableResponse();
  }
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
  jar.set(
    sessionCookieName(),
    parsed.access_token,
    sessionCookieOptions(parsed.expires_in),
  );
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
