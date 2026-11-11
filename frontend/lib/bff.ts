import { isIP } from "node:net";
import { NextResponse } from "next/server";

const SAFE_REQUEST_ID_RE = /^[\x21-\x7e]{1,64}$/;

export function apiBase(env: NodeJS.ProcessEnv = process.env): string {
  return (env.API_INTERNAL_URL || "http://127.0.0.1:43124").replace(/\/$/, "");
}

export function normalizeIpCandidate(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let candidate = raw.trim().replace(/^"|"$/g, "");
  if (!candidate) return null;

  if (candidate.startsWith("[")) {
    const closeIdx = candidate.indexOf("]");
    if (closeIdx <= 1) return null;
    candidate = candidate.slice(1, closeIdx);
  } else {
    const firstColon = candidate.indexOf(":");
    const lastColon = candidate.lastIndexOf(":");
    if (firstColon > 0 && firstColon === lastColon) {
      const maybePort = candidate.slice(firstColon + 1);
      if (/^\d{1,5}$/.test(maybePort)) {
        candidate = candidate.slice(0, firstColon);
      }
    }
  }

  const lower = candidate.toLowerCase();
  if (lower.startsWith("::ffff:")) {
    const v4Part = candidate.slice(7);
    if (isIP(v4Part) === 4) {
      return v4Part;
    }
  }

  return isIP(candidate) !== 0 ? candidate : null;
}

export function isPrivateOrLoopbackIp(ip: string): boolean {
  const version = isIP(ip);
  if (version === 4) {
    const parts = ip.split(".").map((p) => Number.parseInt(p, 10));
    const [a, b] = parts;
    if (a === 127 || a === 10 || a === 0) return true;
    if (a === 192 && b === 168) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 169 && b === 254) return true;
    return false;
  }
  if (version === 6) {
    const lower = ip.toLowerCase();
    if (lower === "::1" || lower === "0:0:0:0:0:0:0:1" || lower === "::") {
      return true;
    }
    if (lower.startsWith("fc") || lower.startsWith("fd")) return true;
    if (
      lower.startsWith("fe8") ||
      lower.startsWith("fe9") ||
      lower.startsWith("fea") ||
      lower.startsWith("feb")
    ) {
      return true;
    }
    return false;
  }
  return false;
}

/**
 * Extract a validated client IP from reverse-proxy headers without trusting
 * attacker-controlled leftmost X-Forwarded-For entries:
 * - When TRUSTED_PROXY_HOPS is explicitly set:
 *   - <= 0 disables forwarded-IP trust completely.
 *   - >= 1 selects the N-th hop from the right of validated X-Forwarded-For IPs.
 * - Otherwise, walks validated X-Forwarded-For entries from right to left,
 *   skipping internal/private/loopback proxy hops to return the rightmost
 *   external client IP (or the rightmost validated peer IP on pure LAN setups).
 */
export function extractClientIp(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
): string | null {
  const rawHops = env.TRUSTED_PROXY_HOPS?.trim();
  const hasExplicitHops = Boolean(rawHops && /^-?\d+$/.test(rawHops));
  const explicitHops = hasExplicitHops ? Number.parseInt(rawHops!, 10) : null;

  if (explicitHops !== null && explicitHops <= 0) {
    return null;
  }

  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) {
    const validIps: string[] = [];
    for (const part of forwarded.split(",")) {
      const normalized = normalizeIpCandidate(part);
      if (normalized) {
        validIps.push(normalized);
      }
    }
    if (validIps.length > 0) {
      if (explicitHops !== null) {
        const idx = Math.max(0, validIps.length - explicitHops);
        return validIps[idx];
      }
      for (let i = validIps.length - 1; i >= 0; i -= 1) {
        if (!isPrivateOrLoopbackIp(validIps[i])) {
          return validIps[i];
        }
      }
      return validIps[validIps.length - 1];
    }
  }

  return normalizeIpCandidate(request.headers.get("x-real-ip"));
}

export function buildProxyHeaders(
  request: Request,
  init?: Record<string, string>,
  env: NodeJS.ProcessEnv = process.env,
): Headers {
  const headers = new Headers(init);
  const clientIp = extractClientIp(request, env);
  if (clientIp) {
    headers.set("X-Client-IP", clientIp);
  }
  const secret = env.INTERNAL_PROXY_SECRET?.trim();
  if (secret) {
    headers.set("X-Internal-Proxy-Secret", secret);
  }
  const requestId = request.headers.get("x-request-id")?.trim();
  if (requestId && SAFE_REQUEST_ID_RE.test(requestId)) {
    headers.set("X-Request-Id", requestId);
  }
  return headers;
}

export function upstreamUnavailableResponse(): NextResponse {
  return NextResponse.json(
    {
      error: {
        code: "upstream_unavailable",
        message: "后端服务暂时不可用，请稍后重试",
      },
    },
    { status: 502 },
  );
}
