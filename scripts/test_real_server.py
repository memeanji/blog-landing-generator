r"""실서버 검증 — 배포된 Supabase Edge Function `google-token` 을 대상으로 A~G.

★비밀값을 화면에 내보내지 않는다.
  · device_token 은 **이 PC 의 device.json 에서 직접 읽는다**(대화·인자로 받지 않는다).
  · 출력에는 길이·해시 앞자리·HTTP 상태만 찍는다.
  · 6자리 페어링 코드는 이 스크립트가 쓰지 않는다(이미 페어링된 기기를 그대로 쓴다).

★시트에 쓰지 않는다. 쓰기 권한은 '열리는가 / 토큰 scope 가 맞는가' 까지만 확인한다.

    .\.venv\Scripts\python.exe scripts\test_real_server.py
    .\.venv\Scripts\python.exe scripts\test_real_server.py --expect-revoked   # E 단계
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests                                                # noqa: E402

from v2 import pairing                                         # noqa: E402

TIMEOUT = 30
ok_n, fail_n = 0, 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        fail_n += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return cond


def call(url: str, key: str, token: str, operation: str):
    return requests.post(
        f"{url}/functions/v1/google-token",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"device_token": token, "operation": operation}, timeout=TIMEOUT)


def leaked(text: str, token: str) -> bool:
    """응답 안에 비밀이 섞였는지."""
    return (token and token in text) or "BEGIN PRIVATE KEY" in text \
        or "private_key" in text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-revoked", action="store_true",
                    help="E 단계 — 이 기기를 revoke 한 뒤 실행해 401 을 확인한다")
    args = ap.parse_args()

    cfg = pairing._config()
    dev = pairing.load_device()
    url, key = (cfg.get("url") or "").rstrip("/"), cfg.get("key") or ""
    token = (dev.get("device_token") or "").strip()
    if not (url and key and token):
        print("❌ 이 PC 에 페어링 정보가 없습니다(device.json / agent_config.json 확인)")
        return 2

    h = hashlib.sha256(token.encode()).hexdigest()
    print(f"대상 서버 : {url}")
    print(f"device_id : {dev.get('device_id')}")
    print(f"토큰      : 길이 {len(token)}자 · sha256 앞12 {h[:12]}…  (원문 출력 안 함)\n")

    # ── A. 정상 기기 · readonly ────────────────────────────────────
    print("A. 정상 paired device — readonly")
    r = call(url, key, token, "readonly")
    check("HTTP 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
    if r.status_code != 200:
        print("\n(배포/시크릿 설정을 먼저 확인해 주세요)")
        return 1
    data = r.json()
    check("scope = spreadsheets.readonly",
          data.get("scope") == "https://www.googleapis.com/auth/spreadsheets.readonly",
          str(data.get("scope")))
    check("expires_at 내려옴", bool(data.get("expires_at")), str(data.get("expires_at")))
    check("client_email 내려옴(공개값)", bool(data.get("client_email")),
          str(data.get("client_email")))
    check("응답에 비밀 없음", not leaked(r.text, token))

    import gspread
    from google.oauth2.credentials import Credentials
    from v2 import brands

    b = brands.default_brand()
    gc = gspread.authorize(Credentials(token=data["access_token"]))
    try:
        sh = gc.open_by_key(b.reference_sheet_id)
        check("기준시트 open_by_key 성공", True, f"'{sh.title}'")
    except Exception as exc:                                   # noqa: BLE001
        check("기준시트 open_by_key 성공", False, f"{type(exc).__name__}: {exc}"[:120])

    # ── B. 같은 기기 · write ───────────────────────────────────────
    print("\nB. 동일 device — write")
    r2 = call(url, key, token, "write")
    check("HTTP 200", r2.status_code == 200, f"HTTP {r2.status_code} {r2.text[:120]}")
    if r2.status_code == 200:
        d2 = r2.json()
        check("scope = spreadsheets(쓰기)",
              d2.get("scope") == "https://www.googleapis.com/auth/spreadsheets",
              str(d2.get("scope")))
        check("readonly 토큰과 다른 토큰", d2.get("access_token") != data.get("access_token"))
        info = requests.get("https://oauth2.googleapis.com/tokeninfo",
                            params={"access_token": d2["access_token"]},
                            timeout=TIMEOUT).json()
        check("구글이 확인한 scope 도 쓰기",
              info.get("scope") == "https://www.googleapis.com/auth/spreadsheets",
              str(info.get("scope")))
        gc2 = gspread.authorize(Credentials(token=d2["access_token"]))
        try:
            ub = gc2.open_by_key(b.utm_sheet_id)
            check("UTM 빌더 open_by_key 성공", True,
                  f"'{ub.title}' · 탭 {len(ub.worksheets())}개")
            # ★쓰기 인증만 확인한다 — 빈 batchUpdate 는 아무것도 바꾸지 않는다.
            #   403 이면 권한 없음, 그 외(200/400)면 쓰기 인증 자체는 통과한 것.
            resp = ub.batch_update({"requests": []})
            check("쓰기 API 인증 통과(변경 0건)", True, f"{str(resp)[:60]}")
        except Exception as exc:                               # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            check("쓰기 API 인증 통과(변경 0건)", "403" not in msg and
                  "PERMISSION_DENIED" not in msg, msg[:120])

    # ── C. 잘못된 device_token ─────────────────────────────────────
    print("\nC. 잘못된 device_token")
    bogus = secrets.token_hex(32)
    r3 = call(url, key, bogus, "readonly")
    check("HTTP 401", r3.status_code == 401, f"HTTP {r3.status_code}")
    check("응답에 토큰 원문 없음", bogus not in r3.text and not leaked(r3.text, token),
          r3.text[:80])

    # ── D. 허용되지 않은 operation ─────────────────────────────────
    print("\nD. 허용되지 않은 operation")
    for bad in ("https://www.googleapis.com/auth/drive", "admin", ""):
        rr = call(url, key, token, bad)
        check(f"'{bad or '(빈값)'}' 거부", rr.status_code == 400,
              f"HTTP {rr.status_code}")

    # ── F. 캐시 미스에서도 발급 ────────────────────────────────────
    print("\nF. 캐시 miss / 반복 요청")
    r4 = call(url, key, token, "readonly")
    check("재요청도 HTTP 200", r4.status_code == 200, f"HTTP {r4.status_code}")
    check("토큰이 유효한 값", bool(r4.status_code == 200 and r4.json().get("access_token")))
    print("     ※ Edge Function 을 재배포(=인스턴스 초기화)한 직후 이 스크립트를 한 번 더")
    print("       돌리면 '캐시 완전 소멸 → 재발급' 경로가 실서버에서 그대로 확인됩니다.")

    # ── E. revoke ─────────────────────────────────────────────────
    if args.expect_revoked:
        print("\nE. revoked_at 적용 후")
        r5 = call(url, key, token, "readonly")
        check("HTTP 401", r5.status_code == 401, f"HTTP {r5.status_code} {r5.text[:80]}")
        check("응답에 비밀 없음", not leaked(r5.text, token))
    else:
        print("\nE. revoked_at — 건너뜀 (--expect-revoked 로 별도 실행)")

    print("\n" + "=" * 60)
    print(f"통과 {ok_n} · 실패 {fail_n}")
    print("\nG. Edge Function 로그 확인(Dashboard → Edge Functions → Logs)")
    print("   나와야 하는 줄은 이 다섯 가지뿐입니다:")
    print("     token issued op=… device=… cache=…")
    print("     denied op=… hash=<앞12자>… reason=…")
    print("     gate rpc failed: HTTP …")
    print("     google token exchange failed: HTTP …")
    print("     GOOGLE_SA_JSON secret is missing or not valid JSON")
    print("   ★'BEGIN PRIVATE KEY' · 'ya29.' 로 시작하는 값 · 64자 hex 가 보이면 실패입니다.")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass
    raise SystemExit(main())
