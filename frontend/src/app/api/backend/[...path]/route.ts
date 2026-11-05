import { NextRequest, NextResponse } from "next/server";

const ACCESS_COOKIE = "solvyn_access";
const REFRESH_COOKIE = "solvyn_refresh";
const API_URL = (process.env.SOLVYN_API_URL || "http://localhost:8000/api/v1").replace(/\/$/, "");

type RouteContext = { params: Promise<{ path: string[] }> };
type TokenPair = { access_token: string; refresh_token: string; expires_in_minutes: number };
const refreshResults = new Map<string, { promise: Promise<TokenPair | null>; expiresAt: number }>();

function cookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/",
    maxAge,
  };
}

async function sendToBackend(
  request: NextRequest,
  backendPath: string,
  accessToken?: string,
  body?: BodyInit,
  method = request.method,
) {
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  const tenantSlug = request.headers.get("x-tenant-slug") || process.env.SOLVYN_TENANT_SLUG;
  if (contentType) headers.set("content-type", contentType);
  if (tenantSlug) headers.set("x-tenant-slug", tenantSlug);
  if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);

  return fetch(`${API_URL}/${backendPath}${request.nextUrl.search}`, {
    method,
    headers,
    body: ["GET", "HEAD"].includes(method) ? undefined : body,
    cache: "no-store",
  });
}

function jsonError(status: number, detail: string) {
  return NextResponse.json({ detail }, { status });
}

function rotateRefreshToken(request: NextRequest, refreshToken: string): Promise<TokenPair | null> {
  const now = Date.now();
  for (const [key, entry] of refreshResults) {
    if (entry.expiresAt <= now) refreshResults.delete(key);
  }
  const cached = refreshResults.get(refreshToken);
  if (cached) return cached.promise;

  const promise = (async () => {
    const response = await sendToBackend(
      request,
      "auth/refresh",
      undefined,
      JSON.stringify({ refresh_token: refreshToken }),
      "POST",
    );
    if (!response.ok) return null;
    return await response.json() as TokenPair;
  })();
  const entry = { promise, expiresAt: Number.POSITIVE_INFINITY };
  refreshResults.set(refreshToken, entry);
  void promise.then((tokens) => {
    if (tokens) entry.expiresAt = Date.now() + 10_000;
    else refreshResults.delete(refreshToken);
  }).catch(() => refreshResults.delete(refreshToken));
  return promise;
}

export async function GET(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function PUT(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

async function proxy(request: NextRequest, context: RouteContext) {
  const origin = request.headers.get("origin");
  if (!(["GET", "HEAD"].includes(request.method)) && origin && new URL(origin).origin !== request.nextUrl.origin) {
    return jsonError(403, "Cross-origin requests are not allowed");
  }
  const { path } = await context.params;
  const backendPath = path.map((part) => encodeURIComponent(part)).join("/");
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer();

  if (backendPath === "auth/login" && request.method === "POST") {
    const upstream = await sendToBackend(request, backendPath, undefined, body);
    const payload = await upstream.json().catch(() => null) as TokenPair | { detail?: string } | null;
    if (!upstream.ok || !payload || !("access_token" in payload)) {
      return NextResponse.json(payload || { detail: "Login failed" }, { status: upstream.status });
    }
    const response = NextResponse.json({ authenticated: true, expires_in_minutes: payload.expires_in_minutes });
    response.cookies.set(ACCESS_COOKIE, payload.access_token, cookieOptions(payload.expires_in_minutes * 60));
    response.cookies.set(REFRESH_COOKIE, payload.refresh_token, cookieOptions(30 * 24 * 60 * 60));
    return response;
  }

  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (backendPath === "auth/logout" && request.method === "POST") {
    if (refreshToken) {
      await sendToBackend(request, "auth/logout", undefined, JSON.stringify({ refresh_token: refreshToken }));
    }
    const response = NextResponse.json({ ok: true });
    response.cookies.set(ACCESS_COOKIE, "", cookieOptions(0));
    response.cookies.set(REFRESH_COOKIE, "", cookieOptions(0));
    return response;
  }

  let accessToken = request.cookies.get(ACCESS_COOKIE)?.value;
  let upstream = await sendToBackend(request, backendPath, accessToken, body);
  let rotatedTokens: TokenPair | null = null;

  if (upstream.status === 401 && refreshToken && !["auth/refresh", "auth/login"].includes(backendPath)) {
    rotatedTokens = await rotateRefreshToken(request, refreshToken);
    if (rotatedTokens) {
      accessToken = rotatedTokens.access_token;
      upstream = await sendToBackend(request, backendPath, accessToken, body);
    }
  }

  const responseBody = await upstream.arrayBuffer();
  const response = new NextResponse(responseBody, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
  });
  if (rotatedTokens) {
    response.cookies.set(ACCESS_COOKIE, rotatedTokens.access_token, cookieOptions(rotatedTokens.expires_in_minutes * 60));
    response.cookies.set(REFRESH_COOKIE, rotatedTokens.refresh_token, cookieOptions(30 * 24 * 60 * 60));
  }
  if (upstream.status === 401 && refreshToken) {
    response.cookies.set(ACCESS_COOKIE, "", cookieOptions(0));
    response.cookies.set(REFRESH_COOKIE, "", cookieOptions(0));
  }
  return response;
}
