import { NextResponse } from "next/server";

import {
  apiBase,
  buildProxyHeaders,
  upstreamUnavailableResponse,
} from "../../../lib/bff";

export async function GET(request: Request) {
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/v1/auth/bootstrap-status`, {
      headers: buildProxyHeaders(request),
      cache: "no-store",
    });
  } catch {
    return upstreamUnavailableResponse();
  }
  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
