r"""테스트 전용 — Supabase 대역(對役) 서버.

Edge Function 을 실제로 배포하기 전에 **Agent 쪽 코드 경로를 그대로** 검증하기 위한 것이다.
서버 계약(요청/응답 모양)은 `supabase/functions/google-token/index.ts` 와 동일하게 맞췄다.

    POST /rest/v1/rpc/pair_device      {p_code, p_label, p_version}
                                    → {device_id, device_token}     (6자리 페어링)
    POST /functions/v1/google-token    {device_token, operation}
                                    → {access_token, expires_at, scope, client_email}

★구글 토큰은 **진짜로** 발급한다(이 PC 의 서비스계정으로 서명) — 그래서 이 대역을 쓴
  테스트에서도 Google Sheets 접근은 실제 통신이다. 가짜인 것은 Supabase 홉 하나뿐이다.
★device_token 은 원문을 저장하지 않고 sha256 만 갖는다(실제 서버와 동일).

    python scripts/fake_supabase.py --port 8799 --code 482100
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCOPES = {
    "readonly": "https://www.googleapis.com/auth/spreadsheets.readonly",
    "write": "https://www.googleapis.com/auth/spreadsheets",
}

STATE = {
    "code": "",
    "devices": {},          # token_hash -> {device_id, label, revoked}
    "mint_calls": [],       # [(operation, scope)]
    "cache": {},            # f"{email}|{scope}" -> (token, expires_at)
    "throttle": {},         # DB 관문(auth_throttle) 흉내
}
_lock = threading.Lock()


def _sa_path() -> str:
    from dotenv import dotenv_values
    env = dotenv_values(ROOT / ".env")
    return (os.getenv("FAKE_SA_JSON") or env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "")


def _mint(scope: str) -> tuple[str, dt.datetime, str]:
    """서비스계정으로 실제 Google access token 발급(Edge Function 의 ⑤ 단계와 동일)."""
    from google.oauth2 import service_account
    import google.auth.transport.requests as greq

    p = _sa_path()
    c = service_account.Credentials.from_service_account_file(p, scopes=[scope])
    c.refresh(greq.Request())
    email = json.loads(Path(p).read_text(encoding="utf-8"))["client_email"]
    return c.token, c.expiry, email


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                                 # 조용히
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):                                         # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:                                      # noqa: BLE001
            return self._send({"error": "bad_request"}, 400)

        if self.path.endswith("/rpc/pair_device"):
            return self._pair(body)
        if self.path.endswith("/functions/v1/google-token"):
            return self._token(body)
        if "/rpc/agent_heartbeat" in self.path or "/rpc/claim_job" in self.path:
            return self._send(None)                            # 큐는 이 테스트 범위 밖
        return self._send({"error": "not_found"}, 404)

    # ── 6자리 페어링 ────────────────────────────────────────────────
    def _pair(self, body):
        code = str(body.get("p_code") or "")
        if not STATE["code"] or code != STATE["code"]:
            return self._send({"message": "알 수 없는 코드"}, 400)
        token = secrets.token_hex(32)
        device_id = "11111111-2222-3333-4444-555555555555"
        with _lock:
            STATE["devices"][hashlib.sha256(token.encode()).hexdigest()] = {
                "device_id": device_id,
                "label": body.get("p_label") or "",
                "revoked": False,
            }
            STATE["code"] = ""                                 # 1회용
        return self._send({"device_id": device_id, "device_token": token})

    # ── 구글 단기 토큰 ──────────────────────────────────────────────
    def _token(self, body):
        operation = (body.get("operation") or "readonly").strip()
        scope = SCOPES.get(operation)
        if not scope:
            return self._send({"error": "unknown_operation"}, 400)   # allowlist 밖
        dev_token = (body.get("device_token") or "").strip()
        if not dev_token:
            return self._send({"error": "unauthorized"}, 401)
        h = hashlib.sha256(dev_token.encode()).hexdigest()
        # ★DB 관문(google_token_gate)과 같은 판정 — 키에 operation 을 포함하고
        #   실패/발급 횟수를 창 단위로 센다. 인스턴스 메모리가 아니라 DB 몫이라는 점만 다르다.
        key = f"gt:{h[:16]}:{operation}"
        now = dt.datetime.utcnow()
        with _lock:
            row = STATE["throttle"].setdefault(
                key, {"window": now, "fail": 0, "ok": 0})
            if (now - row["window"]).total_seconds() > 60:
                row.update(window=now, fail=0, ok=0)
            if row["fail"] >= 10 or row["ok"] >= 20:
                return self._send({"error": "rate_limited"}, 429)
            dev = STATE["devices"].get(h)
            if not dev or dev["revoked"]:
                row["fail"] += 1
                return self._send({"error": "unauthorized"}, 401)
            row["ok"] += 1
            row["fail"] = 0

        token, expiry, email, cache_key = None, None, "", None
        with _lock:
            for k, (t, e, em) in list(STATE["cache"].items()):
                if k.endswith(f"|{scope}") and (e - dt.datetime.utcnow()).total_seconds() > 300:
                    token, expiry, email, cache_key = t, e, em, k
                    break
        if token is None:
            token, expiry, email = _mint(scope)
            cache_key = f"{email}|{scope}"                     # ★scope 별 분리 보관
            with _lock:
                STATE["cache"][cache_key] = (token, expiry, email)
                STATE["mint_calls"].append((operation, scope))
        return self._send({
            "access_token": token,
            "expires_at": expiry.replace(tzinfo=dt.timezone.utc).isoformat(),
            "scope": scope,
            "client_email": email,
        })


def serve(port: int, code: str) -> HTTPServer:
    STATE["code"] = code
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--code", default="482100")
    a = ap.parse_args()
    serve(a.port, a.code)
    print(f"대역 서버 http://127.0.0.1:{a.port} · 페어링 코드 {a.code}")
    threading.Event().wait()
