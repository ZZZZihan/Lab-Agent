import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "next/server") {
      return nextResolve("next/server.js", context);
    }
    if (specifier === "next/headers") {
      return nextResolve("next/headers.js", context);
    }
    const parentURL = context.parentURL || "";
    if (
      !parentURL.includes("/node_modules/") &&
      (specifier.startsWith("./") || specifier.startsWith("../")) &&
      !specifier.endsWith(".ts") &&
      !specifier.endsWith(".mjs") &&
      !specifier.endsWith(".js")
    ) {
      return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
  },
});

const {
  CONTENT_SECURITY_POLICY,
  SECURITY_HEADERS,
  isOriginAllowed,
  parseAllowedOrigins,
  sessionCookieName,
  sessionCookieOptions,
  verifyOrigin,
} = await import("../lib/security.ts");
const nextConfigModule = await import("../next.config.ts");
const { proxy } = await import("../proxy.ts");
const sessionRoute = await import("../app/api/session/route.ts");
const backendRoute = await import("../app/api/backend/[...path]/route.ts");
const { NextRequest } = await import("next/server.js");

test("next.config.ts headers() sets CSP, X-Content-Type-Options, Referrer-Policy, Permissions-Policy", async () => {
  const nextConfig = nextConfigModule.default;
  assert.equal(typeof nextConfig.headers, "function");
  const rules = await nextConfig.headers();
  assert.equal(rules.length, 1);
  assert.equal(rules[0].source, "/:path*");
  const headerMap = new Map(rules[0].headers.map((h) => [h.key, h.value]));
  assert.equal(headerMap.get("Content-Security-Policy"), CONTENT_SECURITY_POLICY);
  assert.equal(
    headerMap.get("Content-Security-Policy"),
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
  );
  assert.equal(headerMap.get("X-Content-Type-Options"), "nosniff");
  assert.equal(headerMap.get("Referrer-Policy"), "same-origin");
  assert.equal(
    headerMap.get("Permissions-Policy"),
    "camera=(), microphone=(), geolocation=()",
  );
  assert.deepEqual(rules[0].headers, SECURITY_HEADERS);
});

test("cross-origin POST/PUT/PATCH/DELETE to route handlers returns 403", async () => {
  const prevOrigin = process.env.PUBLIC_ORIGIN;
  process.env.PUBLIC_ORIGIN = "http://127.0.0.1:43123, https://lab.example.org";
  try {
    // 1. Cross-origin POST to /api/session -> 403
    const crossSessionPost = await sessionRoute.POST(
      new Request("http://127.0.0.1:43123/api/session", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Origin: "https://evil.example.com",
        },
        body: JSON.stringify({ email: "a@b.c", password: "pw" }),
      }),
    );
    assert.equal(crossSessionPost.status, 403);
    const crossSessionBody = await crossSessionPost.json();
    assert.equal(crossSessionBody.error.code, "forbidden");

    // 2. Missing Origin on POST to /api/session -> 403
    const missingOriginPost = await sessionRoute.POST(
      new Request("http://127.0.0.1:43123/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "a@b.c", password: "pw" }),
      }),
    );
    assert.equal(missingOriginPost.status, 403);

    // 3. Cross-origin DELETE to /api/session -> 403
    const crossSessionDelete = await sessionRoute.DELETE(
      new Request("http://127.0.0.1:43123/api/session", {
        method: "DELETE",
        headers: { Origin: "https://evil.example.com" },
      }),
    );
    assert.equal(crossSessionDelete.status, 403);

    // 4. Cross-origin POST, PUT, PATCH, DELETE to /api/backend/[...path] -> 403
    const ctx = { params: Promise.resolve({ path: ["documents"] }) };
    for (const [method, handler] of [
      ["POST", backendRoute.POST],
      ["PUT", backendRoute.PUT],
      ["PATCH", backendRoute.PATCH],
      ["DELETE", backendRoute.DELETE],
    ]) {
      const resp = await handler(
        new Request("http://127.0.0.1:43123/api/backend/documents", {
          method,
          headers: { Origin: "https://evil.example.com" },
        }),
        ctx,
      );
      assert.equal(resp.status, 403, `Expected 403 for cross-origin ${method}`);
    }

    // 5. Allowed origins in comma-separated PUBLIC_ORIGIN pass verifyOrigin
    assert.equal(
      verifyOrigin(
        new Request("http://127.0.0.1:43123/api/session", {
          method: "POST",
          headers: { Origin: "http://127.0.0.1:43123" },
        }),
      ),
      null,
    );
    assert.equal(
      verifyOrigin(
        new Request("https://lab.example.org/api/session", {
          method: "POST",
          headers: { Origin: "https://lab.example.org" },
        }),
      ),
      null,
    );
    assert.equal(
      isOriginAllowed("https://lab.example.org/", {
        PUBLIC_ORIGIN: "http://127.0.0.1:43123, https://lab.example.org",
      }),
      true,
    );
    assert.equal(
      isOriginAllowed("null", {
        PUBLIC_ORIGIN: "http://127.0.0.1:43123",
      }),
      false,
    );
    assert.deepEqual(
      [...parseAllowedOrigins(undefined)],
      ["http://127.0.0.1:43123"],
    );
  } finally {
    if (prevOrigin === undefined) {
      delete process.env.PUBLIC_ORIGIN;
    } else {
      process.env.PUBLIC_ORIGIN = prevOrigin;
    }
  }
});

test("COOKIE_SECURE=true switches session cookie name to __Host-la_session", () => {
  assert.equal(sessionCookieName({ COOKIE_SECURE: "false" }), "la_session");
  assert.equal(sessionCookieName({}), "la_session");
  assert.equal(sessionCookieName({ COOKIE_SECURE: "true" }), "__Host-la_session");

  const insecureOpts = sessionCookieOptions(3600, { COOKIE_SECURE: "false" });
  assert.equal(insecureOpts.secure, false);
  assert.equal(insecureOpts.httpOnly, true);
  assert.equal(insecureOpts.path, "/");

  const secureOpts = sessionCookieOptions(3600, { COOKIE_SECURE: "true" });
  assert.equal(secureOpts.secure, true);
  assert.equal(secureOpts.httpOnly, true);
  assert.equal(secureOpts.path, "/");
  assert.equal(secureOpts.sameSite, "lax");
});

test("proxy redirects unauthenticated protected page requests to /login", () => {
  const prevSecure = process.env.COOKIE_SECURE;
  try {
    process.env.COOKIE_SECURE = "false";

    // Unauthenticated /chat -> redirects to /login
    const unauthChat = proxy(new NextRequest("http://127.0.0.1:43123/chat"));
    assert.equal(unauthChat.status, 307);
    assert.equal(unauthChat.headers.get("location"), "http://127.0.0.1:43123/login");

    // Unauthenticated /documents -> redirects to /login
    const unauthDocs = proxy(new NextRequest("http://127.0.0.1:43123/documents"));
    assert.equal(unauthDocs.status, 307);
    assert.equal(unauthDocs.headers.get("location"), "http://127.0.0.1:43123/login");

    // Public /login -> passes through
    const loginResp = proxy(new NextRequest("http://127.0.0.1:43123/login"));
    assert.equal(loginResp.status, 200);
    assert.equal(loginResp.headers.get("x-middleware-next"), "1");

    // Authenticated with la_session -> passes through
    const authChat = proxy(
      new NextRequest("http://127.0.0.1:43123/chat", {
        headers: { cookie: "la_session=valid-jwt-token" },
      }),
    );
    assert.equal(authChat.status, 200);
    assert.equal(authChat.headers.get("x-middleware-next"), "1");

    // When COOKIE_SECURE=true, requires __Host-la_session
    process.env.COOKIE_SECURE = "true";
    const wrongCookie = proxy(
      new NextRequest("https://lab.example.org/chat", {
        headers: { cookie: "la_session=valid-jwt-token" },
      }),
    );
    assert.equal(wrongCookie.status, 307);
    assert.equal(wrongCookie.headers.get("location"), "https://lab.example.org/login");

    const hostCookie = proxy(
      new NextRequest("https://lab.example.org/chat", {
        headers: { cookie: "__Host-la_session=valid-jwt-token" },
      }),
    );
    assert.equal(hostCookie.status, 200);
    assert.equal(hostCookie.headers.get("x-middleware-next"), "1");
  } finally {
    if (prevSecure === undefined) {
      delete process.env.COOKIE_SECURE;
    } else {
      process.env.COOKIE_SECURE = prevSecure;
    }
  }
});
