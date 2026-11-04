import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const COOKIE = "la_session";

function apiBase() {
  return (process.env.API_INTERNAL_URL || "http://127.0.0.1:43124").replace(/\/$/, "");
}

function cookieOptions(maxAge?: number) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: process.env.COOKIE_SECURE === "true",
    path: "/",
    ...(maxAge ? { maxAge } : {}),
  };
}

export async function GET() {
  const jar = await cookies();
  const token = jar.get(COOKIE)?.value;
  if (!token) return NextResponse.json({ error: { code: "unauthorized" } }, { status: 401 });
  const upstream = await fetch(`${apiBase()}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
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
  const payload = await request.text();
  const upstream = await fetch(`${apiBase()}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  jar.set(COOKIE, parsed.access_token, cookieOptions(parsed.expires_in));
  return NextResponse.json({ user: parsed.user });
}

export async function DELETE() {
  const jar = await cookies();
  const token = jar.get(COOKIE)?.value;
  if (token) {
    await fetch(`${apiBase()}/api/v1/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => undefined);
  }
  jar.set(COOKIE, "", { ...cookieOptions(1), maxAge: 0 });
  return NextResponse.json({ status: "ok" });
}
