import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "next/server") {
      return nextResolve("next/server.js", context);
    }
    if (specifier === "next/headers") {
      return {
        shortCircuit: true,
        url: "data:text/javascript,export async function cookies() { return { get(name) { const v = globalThis.__TEST_COOKIES__?.get(name); return v ? { name, value: v } : undefined; }, set(name, value) { globalThis.__TEST_COOKIES__?.set(name, value); }, delete(name) { globalThis.__TEST_COOKIES__?.delete(name); } }; }",
      };
    }
    if (specifier.startsWith("@/")) {
      const sub = specifier.slice(2);
      const target =
        sub.endsWith(".ts") || sub.endsWith(".mjs") || sub.endsWith(".js")
          ? new URL(`../${sub}`, import.meta.url).href
          : new URL(`../${sub}.ts`, import.meta.url).href;
      return nextResolve(target, context);
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
const apiModule = await import("../lib/api.ts");
const chatHelpers = await import("../app/chat/components/chatTypesAndHelpers.ts");
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

test("Spec v2.2 API helpers match strict backend Pydantic schemas and normalize dossier relations", async () => {
  const prevFetch = globalThis.fetch;
  const captured = [];
  globalThis.fetch = async (url, init = {}) => {
    captured.push({ url: String(url), init });
    if (String(url).includes("/dossier")) {
      return new Response(
        JSON.stringify({
          person: { id: "p-1", name: "张三" },
          projects: [
            { id: "proj-1", title: "重点研发", relation_role: "pi" },
            { id: "proj-2", title: "面上项目", relation_role: "participant" },
          ],
          achievements: [
            { id: "ach-1", title: "论文A", relation_role: "first_author", author_position: 1 },
            { id: "ach-2", title: "论文B", relation_role: "coauthor", author_position: 3 },
          ],
          counts: { projects_total: 2, achievements_total: 2 },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (String(url).includes("lab/format/batch")) {
      return new Response(
        JSON.stringify({
          items: [{ entity: "person", id: "p-1", text: "1. 张三" }],
          combined_text: "1. 张三",
          count: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (String(url).includes("signatures:zip")) {
      return new Response(new Uint8Array([80, 75, 3, 4]), {
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": "attachment; filename*=UTF-8''%E7%AD%BE%E5%90%8D.zip",
          "X-Signature-Count-Included": "2",
          "X-Signature-Count-Missing": "1",
        },
      });
    }
    if (String(url).includes("export:selected")) {
      return new Response(new Uint8Array([80, 75, 3, 4]), {
        status: 200,
        headers: {
          "Content-Type":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "Content-Disposition": "attachment; filename*=UTF-8''%E7%B4%A0%E6%9D%90.docx",
          "X-Export-Warnings-Count": "1",
        },
      });
    }
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    // 1. getPersonDossier normalizes relation_role -> relation & relation_role
    const dossier = await apiModule.getPersonDossier("p-1");
    assert.equal(dossier.projects[0].relation, "pi");
    assert.equal(dossier.projects[0].relation_role, "pi");
    assert.equal(dossier.projects[1].relation, "participant");
    assert.equal(dossier.achievements[0].relation, "first_author");
    assert.equal(dossier.achievements[0].relation_role, "first_author");
    assert.equal(dossier.achievements[1].relation, "coauthor");

    // 2. formatLabBatch sends entity and strict BatchFormatOptions including include_relationship_note
    const batchRes = await apiModule.formatLabBatch(
      [{ kind: "person", id: "p-1" }],
      {
        with_support: true,
        with_relation: true,
        include_bio: true,
        numbering: "arabic",
        group_by_kind: true,
      },
    );
    assert.equal(batchRes.text, "1. 张三");
    const batchCall = captured.find((c) => c.url.includes("lab/format/batch"));
    const batchBody = JSON.parse(batchCall.init.body);
    assert.deepEqual(batchBody, {
      items: [{ entity: "person", id: "p-1" }],
      options: {
        style: "nsfc_standard",
        numbering: "decimal",
        group_by_entity: true,
        include_person_bio: true,
        include_support_note: true,
        include_relationship_note: true,
        include_signatures_annex: false,
      },
    });

    // 3. downloadSignaturesZip sends { person_ids, filename_template } only
    const zipRes = await apiModule.downloadSignaturesZip(["p-1", "p-2"], "name_id");
    assert.equal(zipRes.filename, "签名.zip");
    assert.equal(zipRes.includedCount, 2);
    assert.equal(zipRes.missingCount, 1);
    const zipCall = captured.find((c) => c.url.includes("signatures:zip"));
    const zipBody = JSON.parse(zipCall.init.body);
    assert.deepEqual(zipBody, {
      person_ids: ["p-1", "p-2"],
      filename_template: "name_id",
    });

    // 4. exportSelectedLabAssets sends top-level title, entity items, and strict BatchFormatOptions
    const docxRes = await apiModule.exportSelectedLabAssets(
      [
        { kind: "person", id: "p-1" },
        { kind: "project", id: "proj-1" },
      ],
      {
        with_support: false,
        with_relation: false,
        include_bio: false,
        numbering: "bracket",
        group_by_kind: true,
        include_signatures: true,
        title: "自定义标题",
      },
    );
    assert.equal(docxRes.filename, "素材.docx");
    assert.equal(docxRes.warnings.length, 1);
    const exportCall = captured.find((c) => c.url.includes("export:selected"));
    const exportBody = JSON.parse(exportCall.init.body);
    assert.deepEqual(exportBody, {
      title: "自定义标题",
      items: [
        { entity: "person", id: "p-1" },
        { entity: "project", id: "proj-1" },
      ],
      options: {
        style: "nsfc_standard",
        numbering: "bracket",
        group_by_entity: true,
        include_person_bio: false,
        include_support_note: false,
        include_relationship_note: false,
        include_signatures_annex: true,
      },
    });

    // 5. deletePersonSignature sends If-Match header
    await apiModule.deletePersonSignature("p-1", "abc123sha");
    const delCall = captured.find(
      (c) => c.url.includes("lab/people/p-1/signature") && c.init.method === "DELETE",
    );
    const delHeaders = new Headers(delCall.init.headers);
    assert.equal(delHeaders.get("If-Match"), '"abc123sha"');

    // 6. 404 NOT_FOUND from formatLabBatch preserves error.missing on ApiError
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "NOT_FOUND",
            message: "部分素材不存在或已删除",
            missing: [{ entity: "project", id: "proj-stale" }],
          },
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      );
    await assert.rejects(
      () => apiModule.formatLabBatch([{ kind: "project", id: "proj-stale" }]),
      (err) => {
        assert.equal(err instanceof apiModule.ApiError, true);
        assert.equal(err.status, 404);
        assert.equal(err.code, "NOT_FOUND");
        assert.deepEqual(err.missing, [{ entity: "project", id: "proj-stale" }]);
        return true;
      },
    );
  } finally {
    globalThis.fetch = prevFetch;
  }
});

test("/api/backend/[...path] forwards signature security headers, ETag, and export counters", async () => {
  const prevOrigin = process.env.PUBLIC_ORIGIN;
  const prevSecret = process.env.INTERNAL_PROXY_SECRET;
  const prevFetch = globalThis.fetch;
  const prevCookies = globalThis.__TEST_COOKIES__;
  process.env.PUBLIC_ORIGIN = "http://127.0.0.1:43123";
  process.env.INTERNAL_PROXY_SECRET = "proxy-secret-test";

  const upstreamCalls = [];
  try {
    // 1. Unauthenticated request without session cookie returns 401
    globalThis.__TEST_COOKIES__ = new Map();
    const unauthResp = await backendRoute.GET(
      new Request("http://127.0.0.1:43123/api/backend/lab/people"),
      { params: Promise.resolve({ path: ["lab", "people"] }) },
    );
    assert.equal(unauthResp.status, 401);

    // 2. Authenticated GET signature stream forwards request headers and security/caching/ETag response headers
    globalThis.__TEST_COOKIES__ = new Map([["la_session", "test-session-jwt"]]);
    globalThis.fetch = async (url, init = {}) => {
      upstreamCalls.push({ url: String(url), init });
      const reqHeaders = new Headers(init.headers);
      if (reqHeaders.get("If-None-Match") === '"etag-304"') {
        return new Response(null, {
          status: 304,
          headers: {
            ETag: '"etag-304"',
            "Cache-Control": "private, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy":
              "default-src 'none'; img-src 'self' data:; style-src 'none'; script-src 'none'; sandbox",
          },
        });
      }
      if (String(url).endsWith("/signature") || String(url).includes("/signature?")) {
        return new Response(new Uint8Array([137, 80, 78, 71]), {
          status: 200,
          headers: {
            "Content-Type": "image/png",
            "Cache-Control": "private, max-age=0, must-revalidate",
            ETag: '"sig-sha-256"',
            "Content-Disposition":
              "inline; filename*=UTF-8''%E4%B8%87%E6%8C%AF%E5%87%AF_%E7%AD%BE%E5%90%8D.png",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy":
              "default-src 'none'; img-src 'self' data:; style-src 'none'; script-src 'none'; sandbox",
          },
        });
      }
      return new Response(new Uint8Array([80, 75, 3, 4]), {
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition":
            "attachment; filename*=UTF-8''%E7%AD%BE%E5%90%8D%E5%8C%85.zip",
          "X-Signature-Count-Included": "8",
          "X-Signature-Count-Missing": "2",
          "X-Export-Warnings-Count": "1",
          "X-Export-Warnings": '["SIG_WEBP_SKIPPED"]',
        },
      });
    };

    const sigGetResp = await backendRoute.GET(
      new Request("http://127.0.0.1:43123/api/backend/lab/people/p1/signature?raw=1", {
        method: "GET",
        headers: {
          "If-None-Match": '"old-etag"',
          "X-Forwarded-For": "198.51.100.7, 10.0.0.1",
        },
      }),
      { params: Promise.resolve({ path: ["lab", "people", "p1", "signature"] }) },
    );
    assert.equal(sigGetResp.status, 200);
    assert.equal(sigGetResp.headers.get("content-type"), "image/png");
    assert.equal(
      sigGetResp.headers.get("cache-control"),
      "private, max-age=0, must-revalidate",
    );
    assert.equal(sigGetResp.headers.get("etag"), '"sig-sha-256"');
    assert.equal(
      sigGetResp.headers.get("content-disposition"),
      "inline; filename*=UTF-8''%E4%B8%87%E6%8C%AF%E5%87%AF_%E7%AD%BE%E5%90%8D.png",
    );
    assert.equal(sigGetResp.headers.get("x-content-type-options"), "nosniff");
    assert.equal(
      sigGetResp.headers.get("content-security-policy"),
      "default-src 'none'; img-src 'self' data:; style-src 'none'; script-src 'none'; sandbox",
    );
    assert.equal(sigGetResp.headers.get("x-accel-buffering"), "no");
    assert.deepEqual(new Uint8Array(await sigGetResp.arrayBuffer()), new Uint8Array([137, 80, 78, 71]));

    const firstUpstream = upstreamCalls[0];
    assert.equal(
      firstUpstream.url,
      "http://127.0.0.1:43124/api/v1/lab/people/p1/signature?raw=1",
    );
    const firstReqHeaders = new Headers(firstUpstream.init.headers);
    assert.equal(firstReqHeaders.get("Authorization"), "Bearer test-session-jwt");
    assert.equal(firstReqHeaders.get("If-None-Match"), '"old-etag"');
    assert.equal(firstReqHeaders.get("X-Client-IP"), "198.51.100.7");
    assert.equal(firstReqHeaders.get("X-Internal-Proxy-Secret"), "proxy-secret-test");

    // 3. 304 Not Modified response preserves ETag & Cache-Control with empty body
    const sig304Resp = await backendRoute.GET(
      new Request("http://127.0.0.1:43123/api/backend/lab/people/p1/signature", {
        method: "GET",
        headers: { "If-None-Match": '"etag-304"' },
      }),
      { params: Promise.resolve({ path: ["lab", "people", "p1", "signature"] }) },
    );
    assert.equal(sig304Resp.status, 304);
    assert.equal(sig304Resp.headers.get("etag"), '"etag-304"');
    assert.equal(
      sig304Resp.headers.get("cache-control"),
      "private, max-age=0, must-revalidate",
    );
    assert.equal((await sig304Resp.arrayBuffer()).byteLength, 0);

    // 4. POST export endpoints forward signature counts, export warnings, and Content-Disposition
    const exportPostResp = await backendRoute.POST(
      new Request("http://127.0.0.1:43123/api/backend/lab/people/signatures:zip", {
        method: "POST",
        headers: {
          Origin: "http://127.0.0.1:43123",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ person_ids: ["p1"], filename_template: "seq_name" }),
      }),
      { params: Promise.resolve({ path: ["lab", "people", "signatures:zip"] }) },
    );
    assert.equal(exportPostResp.status, 200);
    assert.equal(exportPostResp.headers.get("content-type"), "application/zip");
    assert.equal(exportPostResp.headers.get("cache-control"), "no-cache");
    assert.equal(
      exportPostResp.headers.get("content-disposition"),
      "attachment; filename*=UTF-8''%E7%AD%BE%E5%90%8D%E5%8C%85.zip",
    );
    assert.equal(exportPostResp.headers.get("x-signature-count-included"), "8");
    assert.equal(exportPostResp.headers.get("x-signature-count-missing"), "2");
    assert.equal(exportPostResp.headers.get("x-export-warnings-count"), "1");
    assert.equal(
      exportPostResp.headers.get("x-export-warnings"),
      '["SIG_WEBP_SKIPPED"]',
    );

    // 5. PUT and DELETE forward If-Match optimistic concurrency header to upstream
    await backendRoute.PUT(
      new Request("http://127.0.0.1:43123/api/backend/lab/people/p1/signature", {
        method: "PUT",
        headers: {
          Origin: "http://127.0.0.1:43123",
          "If-Match": '"sig-sha-256"',
        },
        body: new Uint8Array([1, 2, 3]),
      }),
      { params: Promise.resolve({ path: ["lab", "people", "p1", "signature"] }) },
    );
    const putUpstream = upstreamCalls.at(-1);
    assert.equal(new Headers(putUpstream.init.headers).get("If-Match"), '"sig-sha-256"');

    await backendRoute.DELETE(
      new Request("http://127.0.0.1:43123/api/backend/lab/people/p1/signature", {
        method: "DELETE",
        headers: {
          Origin: "http://127.0.0.1:43123",
          "If-Match": '"sig-sha-256"',
        },
      }),
      { params: Promise.resolve({ path: ["lab", "people", "p1", "signature"] }) },
    );
    const delUpstream = upstreamCalls.at(-1);
    assert.equal(new Headers(delUpstream.init.headers).get("If-Match"), '"sig-sha-256"');

    // 6. POST /api/backend/auth/change-password rotates la_session cookie when access_token is returned
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({
          status: "ok",
          access_token: "rotated-jwt-token",
          expires_in: 28800,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    const cpResp = await backendRoute.POST(
      new Request("http://127.0.0.1:43123/api/backend/auth/change-password", {
        method: "POST",
        headers: {
          Origin: "http://127.0.0.1:43123",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ current_password: "old", new_password: "new-password-123" }),
      }),
      { params: Promise.resolve({ path: ["auth", "change-password"] }) },
    );
    assert.equal(cpResp.status, 200);
    assert.equal(globalThis.__TEST_COOKIES__.get("la_session"), "rotated-jwt-token");
  } finally {
    globalThis.fetch = prevFetch;
    globalThis.__TEST_COOKIES__ = prevCookies;
    if (prevOrigin === undefined) {
      delete process.env.PUBLIC_ORIGIN;
    } else {
      process.env.PUBLIC_ORIGIN = prevOrigin;
    }
    if (prevSecret === undefined) {
      delete process.env.INTERNAL_PROXY_SECRET;
    } else {
      process.env.INTERNAL_PROXY_SECRET = prevSecret;
    }
  }
});

test("generateRequestId falls back to crypto.getRandomValues when crypto.randomUUID is unavailable on HTTP LAN origins", () => {
  const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const normalId = apiModule.generateRequestId();
  assert.match(normalId, uuidRegex);

  const origDesc = Object.getOwnPropertyDescriptor(globalThis.crypto, "randomUUID");
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: undefined,
    configurable: true,
    writable: true,
  });
  try {
    const fallbackId = apiModule.generateRequestId();
    assert.match(fallbackId, uuidRegex);
    assert.notEqual(fallbackId, apiModule.generateRequestId());
  } finally {
    if (origDesc) {
      Object.defineProperty(globalThis.crypto, "randomUUID", origDesc);
    }
  }
});

test("chatTypesAndHelpers never fabricates member profiles or unverified safety parameter values", () => {
  // 1. Empty labPeople must return [] rather than hard-coded fallback people
  const turnWithNames = {
    id: "t-1",
    question: "课题组里郭建民和万振凯的研究专长分别是什么？",
    answer: "郭建民负责人工智能，万振凯负责教育理论。",
    pending: false,
    citations: [{ title: "课题组资料库", excerpt: "郭建民、万振凯" }],
  };
  assert.deepEqual(chatHelpers.extractMatchedPeopleForTurn(turnWithNames, []), []);

  // 2. Pending turn or question-only mention must not produce member cards
  const realPeople = [
    {
      id: "2c8022da-1e4c-4eb9-98c8-7cf0389fd352",
      name: "郭建民",
      title: "副教授",
      project_role: "applicant",
      has_signature: false,
      research_specialty: "人工智能",
    },
  ];
  assert.deepEqual(
    chatHelpers.extractMatchedPeopleForTurn(
      { ...turnWithNames, pending: true },
      realPeople,
    ),
    [],
  );
  const matched = chatHelpers.extractMatchedPeopleForTurn(turnWithNames, realPeople);
  assert.equal(matched.length, 1);
  assert.equal(matched[0].hasSignature, false);
  assert.equal(matched[0].signatureLabel, "暂未绑定内嵌签名");

  // 3. Pending turn or turn without citations must not produce spec table
  assert.equal(
    chatHelpers.extractSpecTableForTurn({
      id: "t-2",
      question: "主磁体上限是420 A吗？",
      answer: "",
      pending: true,
      citations: [],
    }),
    null,
  );

  // 4. Question mentioning 420 A when source & answer say 300 A (and citation also mentions normal operating level 80% before 65% interlock)
  const correctedSpecTurn = {
    id: "t-3",
    question: "主磁体额定工作电流上限是420 A吗？液氦联锁阈值是多少？",
    answer:
      "不是 420 A，根据规程，主磁体正常工作电流为 280 A，额定工作电流上限为 300 A；冷却液氦液位低于 65% 时触发联锁停机 [1]。",
    pending: false,
    citations: [
      {
        index: 1,
        title: "低温超导磁体实验规程",
        heading: "§2.1",
        excerpt:
          "主磁体正常工作电流为 280 A，额定工作电流上限为 300 A。液氦液位正常工作时为 80%，低于 65% 时触发联锁停机。",
      },
    ],
  };
  const table = chatHelpers.extractSpecTableForTurn(correctedSpecTurn);
  assert.ok(table);
  assert.equal(table.rows.length, 2);
  assert.equal(table.rows[0].parameter, "主磁体额定电流");
  assert.equal(table.rows[0].indicator, "300 A");
  assert.equal(table.rows[1].parameter, "冷却液氦液位");
  assert.equal(table.rows[1].indicator, "65%");
  assert.equal(table.rows[1].condition, "< 触发联锁停机");

  // 5. Citation with only normal liquid level or alarm-only / negated interlock must not produce a shutdown interlock row
  assert.equal(
    chatHelpers.extractSpecTableForTurn({
      id: "t-4",
      question: "液氦液位是多少？",
      answer: "液氦液位正常工作时为 80%，低于 65% 时仅报警，不触发联锁停机 [1]。",
      pending: false,
      citations: [
        {
          index: 1,
          title: "低温超导磁体实验规程",
          excerpt: "液氦液位正常工作时为 80%，低于 65% 时仅报警，不触发联锁停机。",
        },
      ],
    }),
    null,
  );

  // 6. Citation distinguishing warning (70%) from shutdown interlock (65%) must select 65%, never 70%
  const alarmVsShutdownTurn = {
    id: "t-5",
    question: "液氦联锁停机阈值是多少？",
    answer: "液氦液位低于 70% 时报警，低于 65% 时触发联锁停机 [1]。",
    pending: false,
    citations: [
      {
        index: 1,
        title: "低温超导磁体实验规程",
        heading: "§2.1",
        excerpt: "液氦液位低于 70% 时报警，低于 65% 时触发联锁停机。",
      },
    ],
  };
  const alarmVsShutdownTable = chatHelpers.extractSpecTableForTurn(alarmVsShutdownTurn);
  assert.ok(alarmVsShutdownTable);
  assert.equal(alarmVsShutdownTable.rows.length, 1);
  assert.equal(alarmVsShutdownTable.rows[0].indicator, "65%");
});



