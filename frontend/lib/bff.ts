import { BlockList, isIP } from "node:net";
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

export function parseTrustedProxies(raw: string | undefined): BlockList | null {
  if (!raw || !raw.trim()) return null;
  const blockList = new BlockList();
  for (const token of raw.split(",")) {
    const trimmed = token.trim();
    if (!trimmed) continue;
    const slashIdx = trimmed.indexOf("/");
    if (slashIdx > 0) {
      const addr = normalizeIpCandidate(trimmed.slice(0, slashIdx));
      const prefixRaw = trimmed.slice(slashIdx + 1).trim();
      if (!addr || !/^\d{1,3}$/.test(prefixRaw)) continue;
      const version = isIP(addr);
      const prefix = Number.parseInt(prefixRaw, 10);
      if (version === 4 && prefix >= 0 && prefix <= 32) {
        blockList.addSubnet(addr, prefix, "ipv4");
      } else if (version === 6 && prefix >= 0 && prefix <= 128) {
        blockList.addSubnet(addr, prefix, "ipv6");
      }
    } else {
      const addr = normalizeIpCandidate(trimmed);
      if (!addr) continue;
      const version = isIP(addr);
      if (version === 4) {
        blockList.addAddress(addr, "ipv4");
      } else if (version === 6) {
        blockList.addAddress(addr, "ipv6");
      }
    }
  }
  return blockList;
}

function isTrustedProxyIp(ip: string, trustedProxies: BlockList): boolean {
  const version = isIP(ip);
  if (version === 4) return trustedProxies.check(ip, "ipv4");
  if (version === 6) return trustedProxies.check(ip, "ipv6");
  return false;
}

/**
 * Extract a validated client IP from reverse-proxy headers:
 * - When TRUSTED_PROXY_HOPS is explicitly set:
 *   - <= 0 disables forwarded-IP trust completely.
 *   - >= 1 selects the N-th hop from the right of validated X-Forwarded-For IPs.
 * - Else when TRUSTED_PROXIES (comma-separated IPs/CIDRs) is set:
 *   - Walks validated X-Forwarded-For entries from right to left, stripping only
 *     configured trusted proxy hops so both public and LAN client IPs are preserved
 *     while attacker-prepended leftmost entries are ignored.
 * - Otherwise (edge proxy overwrites X-Forwarded-For per docs/deploy.md):
 *   - Uses the first validated IP in X-Forwarded-For, preserving distinct LAN or
 *     public client addresses when internal hops append downstream proxy IPs.
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

  const trustedProxies = parseTrustedProxies(env.TRUSTED_PROXIES);

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
      if (trustedProxies !== null) {
        let idx = validIps.length - 1;
        while (idx > 0 && isTrustedProxyIp(validIps[idx], trustedProxies)) {
          idx -= 1;
        }
        return validIps[idx];
      }
      return validIps[0];
    }
  }

  if (explicitHops !== null && explicitHops !== 1) {
    return null;
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
  const incomingReqId = request.headers.get("x-request-id")?.trim();
  if (incomingReqId && SAFE_REQUEST_ID_RE.test(incomingReqId)) {
    headers.set("X-Request-Id", incomingReqId);
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
