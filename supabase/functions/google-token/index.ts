// ─────────────────────────────────────────────────────────────────────────────
//  Edge Function `google-token`
//
//  페어링된 Agent 에게 **단기 Google access token** 만 발급한다.
//  Google 서비스계정 private_key 는 이 함수의 Secret 안에만 존재하고,
//  Agent·설치본·jobs 테이블·로그 어디에도 내려가지 않는다.
//
//      Agent ──POST {device_token, operation}──▶ 여기
//                                                ① sha256(device_token) → devices 조회
//                                                ② rate limit
//                                                ③ scope allowlist (서버가 결정)
//                                                ④ 캐시 확인 (scope+SA 별)
//                                                ⑤ RS256 JWT 서명 → Google 토큰 교환
//                                             ◀── {access_token, expires_at, scope, client_email}
//
//  ★Agent 는 scope URL 을 보내지 못한다. operation("readonly"|"write") 만 보낸다.
//  ★로그에는 device_token / access_token / private_key 를 절대 찍지 않는다.
//
//  배포:  supabase functions deploy google-token --no-verify-jwt
//         (--no-verify-jwt: Agent 는 publishable key 만 들고 있고, 인증은 device_token 으로 한다)
// ─────────────────────────────────────────────────────────────────────────────

const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";

// ★서버가 정하는 allowlist — 이 둘 외에는 어떤 scope 도 발급하지 않는다.
const SCOPES: Record<string, string> = {
  readonly: "https://www.googleapis.com/auth/spreadsheets.readonly",
  write: "https://www.googleapis.com/auth/spreadsheets",
};

const TOKEN_TTL_SEC = 3600;      // Google 이 주는 수명(고정 1시간)
const CACHE_SKEW_SEC = 300;      // 만료 5분 전이면 캐시를 버리고 새로 받는다

// ── 인스턴스 메모리 캐시 ─────────────────────────────────────────────────────
//  ★이것은 **Google 호출을 줄이기 위한 캐시일 뿐** 이고, 보안 판단에는 쓰지 않는다.
//    인스턴스마다 따로이고 재시작하면 사라지지만, 비면 그냥 다시 발급받으면 된다.
//    (인증·rate limit 은 전부 DB 의 google_token_gate 가 담당한다.)
//  key = `${client_email}|${scope}`  ★scope 별로 완전히 분리해서 보관한다.
type Cached = { token: string; expiresAt: number };
const tokenCache = new Map<string, Cached>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function b64url(bytes: Uint8Array | string): string {
  const bin = typeof bytes === "string"
    ? bytes
    : Array.from(bytes).map((b) => String.fromCharCode(b)).join("");
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToPkcs8(pem: string): Uint8Array {
  const body = pem.replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "").replace(/\s+/g, "");
  const raw = atob(body);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** 서비스계정 private_key 로 RS256 JWT 를 만들어 Google access token 을 받는다. */
async function mintGoogleToken(sa: Record<string, string>, scope: string) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const claim = b64url(JSON.stringify({
    iss: sa.client_email,
    scope,
    aud: GOOGLE_TOKEN_URL,
    exp: now + TOKEN_TTL_SEC,
    iat: now,
  }));
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToPkcs8(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = new Uint8Array(await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${header}.${claim}`)));
  const assertion = `${header}.${claim}.${b64url(sig)}`;

  const res = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }),
  });
  if (!res.ok) {
    // ★응답 본문에 assertion 이 섞일 수 있어 상태코드만 남긴다.
    console.error(`google token exchange failed: HTTP ${res.status}`);
    throw new Error("google_token_exchange_failed");
  }
  const data = await res.json();
  return {
    token: data.access_token as string,
    expiresAt: Date.now() + ((data.expires_in ?? TOKEN_TTL_SEC) as number) * 1000,
  };
}

/** 기기 확인 + rate limit — **DB 한 곳**에서 한 번에 판정한다.
 *
 *  ★인스턴스 메모리로 세지 않는 이유: Edge Function 은 여러 인스턴스로 뜨고 재시작되면
 *    카운터가 사라진다. 그러면 제한을 우회할 수 있으므로 보안 판단은 DB 에 맡긴다.
 *  ★해시로만 조회한다 — 원문은 저장돼 있지 않고, 다른 기기 행은 조회 대상이 아니다.
 *  ★revoked_at 이 채워진 기기는 함수 안에서 즉시 걸러진다(연결 해제 = 발급 차단).
 *
 *  반환 {allowed, reason, device_id}
 */
async function gate(tokenHash: string, operation: string) {
  const url = Deno.env.get("SUPABASE_URL")!;
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const res = await fetch(`${url}/rest/v1/rpc/google_token_gate`, {
    method: "POST",
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ p_token_hash: tokenHash, p_operation: operation }),
  });
  if (!res.ok) {
    console.error(`gate rpc failed: HTTP ${res.status}`);
    return { allowed: false, reason: "server_error" };
  }
  return await res.json() as { allowed: boolean; reason: string; device_id?: string };
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  let body: { device_token?: string; operation?: string };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }

  const operation = (body.operation ?? "readonly").trim();
  const scope = SCOPES[operation];
  if (!scope) return json({ error: "unknown_operation" }, 400);   // ★allowlist 밖은 거부

  const deviceToken = (body.device_token ?? "").trim();
  if (!deviceToken) return json({ error: "unauthorized" }, 401);

  const tokenHash = await sha256Hex(deviceToken);
  const bucket = tokenHash.slice(0, 12);          // 로그용 식별자 (원문 아님)

  // ★기기 확인·해제 여부·rate limit 을 DB 가 한 번에 판정한다(인스턴스와 무관).
  const verdict = await gate(tokenHash, operation);
  if (!verdict.allowed) {
    console.warn(`denied op=${operation} hash=${bucket}… reason=${verdict.reason}`);
    if (verdict.reason === "rate_limited") return json({ error: "rate_limited" }, 429);
    if (verdict.reason === "server_error") return json({ error: "server_error" }, 500);
    return json({ error: "unauthorized" }, 401);
  }
  const deviceId = verdict.device_id!;

  let sa: Record<string, string>;
  try {
    sa = JSON.parse(Deno.env.get("GOOGLE_SA_JSON")!);
  } catch {
    console.error("GOOGLE_SA_JSON secret is missing or not valid JSON");
    return json({ error: "server_misconfigured" }, 500);
  }

  const cacheKey = `${sa.client_email}|${scope}`;               // ★scope + SA 별 분리
  let hit = tokenCache.get(cacheKey);
  if (!hit || hit.expiresAt - Date.now() < CACHE_SKEW_SEC * 1000) {
    try {
      hit = await mintGoogleToken(sa, scope);
    } catch {
      return json({ error: "google_auth_failed" }, 502);
    }
    tokenCache.set(cacheKey, hit);
  }

  console.log(`token issued op=${operation} device=${deviceId} cache=${tokenCache.size}`);
  return json({
    access_token: hit.token,
    expires_at: new Date(hit.expiresAt).toISOString(),
    scope,
    client_email: sa.client_email,
  });
});
