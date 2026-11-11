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

export async function POST(request: Request) {
  const originDenied = verifyOrigin(request);
  if (originDenied) return originDenied;
  const payload = await request.text();
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/v1/auth/bootstrap-admin`, {
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
