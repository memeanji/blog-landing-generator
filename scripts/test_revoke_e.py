r"""E 단계 — revoked_at 검증 **+ 원복 보장**.

    ① 지금 정상(200)인지 먼저 확인
    ② 사용자가 revoke SQL 을 실행할 때까지 폴링 → 401 이 되면 E 통과
    ③ **finally** 에서 원복 SQL 을 안내하고, 사용자가 그 SQL 을 실행한 뒤
       HTTP 200 으로 돌아오는 것까지 확인한다
       (돌아오지 않으면 실패로 끝난다 — 조용히 넘어가지 않는다)

★★이 스크립트는 **DB 를 직접 고치지 않는다.** 서비스키가 없어서 쓰기 권한 자체가 없다.
   `finally` 가 하는 일은 **자동 원복이 아니라** ⓐ원복 SQL 안내 ⓑ복구 파일 기록
   ⓒ사용자가 실행한 뒤 200 복귀 확인 — 이 셋뿐이다.
   실제 `update … set revoked_at = null` 은 **사용자가 Supabase SQL Editor 에서 실행**한다.

★그래서 **revoke 를 요청하는 순간** 복구 안내를 파일로도 남긴다:
      out/REVOKE_RECOVERY.txt
  프로세스가 죽거나 창이 닫혀도 그 파일만 보면 되돌릴 수 있다.
  ⚠️이 파일에는 **device_id 와 원복 SQL 만** 넣는다 — device_token·access_token·
    구글 자격증명·Supabase URL/키는 한 글자도 기록하지 않는다(검사로 확인함).

★대상은 **이 개발 PC 의 device** 뿐이다. 표님 device 는 조회도 하지 않는다.
★토큰 원문은 출력하지 않는다.

    .\.venv\Scripts\python.exe scripts\test_revoke_e.py
    .\.venv\Scripts\python.exe scripts\test_revoke_e.py --restore-only   # 원복 확인만
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests                                                # noqa: E402

from v2 import pairing                                         # noqa: E402

TIMEOUT = 30
POLL_SEC = 5
WAIT_LIMIT_SEC = 900          # 사용자가 SQL 을 실행할 때까지 최대 15분 기다린다
RECOVERY_FILE = ROOT / "out" / "REVOKE_RECOVERY.txt"


def call(url: str, key: str, token: str, operation: str = "readonly"):
    return requests.post(
        f"{url}/functions/v1/google-token",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"device_token": token, "operation": operation}, timeout=TIMEOUT)


def sql_revoke(device_id: str) -> str:
    return ("update public.devices set revoked_at = now() "
            f"where device_id = '{device_id}';")


def sql_restore(device_id: str) -> str:
    return ("update public.devices set revoked_at = null "
            f"where device_id = '{device_id}';")


def write_recovery(device_id: str) -> None:
    """revoke 하는 순간 복구 방법을 디스크에 남긴다(프로세스가 죽어도 남는다)."""
    RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECOVERY_FILE.write_text(
        "블로그 랜딩 Agent — revoke 테스트 복구 안내\n"
        f"작성 시각: {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"대상 device_id: {device_id}  (개발 PC)\n\n"
        "이 기기는 테스트를 위해 일시적으로 연결 해제(revoked)되었습니다.\n"
        "아래 SQL 을 Supabase SQL Editor 에서 실행하면 원래대로 돌아갑니다.\n\n"
        f"    {sql_restore(device_id)}\n\n"
        "확인:\n"
        "    .\\.venv\\Scripts\\python.exe scripts\\test_revoke_e.py --restore-only\n",
        encoding="utf-8")


def wait_for(url: str, key: str, token: str, want: int, label: str) -> bool:
    """원하는 상태코드가 될 때까지 폴링."""
    started = time.time()
    last = None
    while time.time() - started < WAIT_LIMIT_SEC:
        try:
            code = call(url, key, token).status_code
        except Exception as exc:                               # noqa: BLE001
            code = f"연결오류({type(exc).__name__})"
        if code != last:
            print(f"    [{int(time.time() - started):4d}s] 현재 응답 {code}", flush=True)
            last = code
        if code == want:
            return True
        time.sleep(POLL_SEC)
    print(f"    ⏱ {WAIT_LIMIT_SEC}초 안에 {label} 이 되지 않았습니다.", flush=True)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore-only", action="store_true",
                    help="원복이 됐는지만 확인한다")
    args = ap.parse_args()

    cfg, dev = pairing._config(), pairing.load_device()
    url, key = (cfg.get("url") or "").rstrip("/"), cfg.get("key") or ""
    token = (dev.get("device_token") or "").strip()
    device_id = dev.get("device_id") or ""
    if not (url and key and token and device_id):
        print("❌ 이 PC 에 페어링 정보가 없습니다.")
        return 2

    h = hashlib.sha256(token.encode()).hexdigest()
    print(f"대상 device_id : {device_id}   ← 개발 PC(표님 아님)")
    print(f"토큰           : 길이 {len(token)}자 · sha256 앞12 {h[:12]}…\n")

    if args.restore_only:
        print("원복 확인 중 — 200 이 돌아오면 정상입니다.")
        okay = wait_for(url, key, token, 200, "정상(200)")
        if okay:
            RECOVERY_FILE.unlink(missing_ok=True)
            print("  ✅ 원복 확인 — revoked_at = null")
            return 0
        print("  ❌ 아직 원복되지 않았습니다. 아래 SQL 을 실행해 주세요:")
        print(f"\n    {sql_restore(device_id)}\n")
        return 1

    revoked = False
    try:
        print("① 현재 상태 확인")
        r = call(url, key, token)
        if not (r.status_code == 200):
            print(f"  ❌ 시작 상태가 200 이 아닙니다(HTTP {r.status_code}) — "
                  "이미 revoke 돼 있거나 배포가 안 됐습니다.")
            print(f"     필요하면: {sql_restore(device_id)}")
            return 1
        print("  ✅ 정상(200) — revoke 전 상태 확인됨\n")

        print("② 아래 SQL 을 Supabase SQL Editor 에서 실행해 주세요:")
        print(f"\n    {sql_revoke(device_id)}\n")
        write_recovery(device_id)
        print(f"  (복구 안내를 {RECOVERY_FILE} 에 남겼습니다)")
        print("  실행하시면 자동으로 감지합니다. 기다리는 중…")
        revoked = wait_for(url, key, token, 401, "차단(401)")
        if not revoked:
            print("  ❌ E 실패 — 401 을 확인하지 못했습니다.")
            return 1

        r2 = call(url, key, token)
        print("  ✅ E 통과 — 연결 해제된 기기는 새 토큰을 받지 못합니다(401)")
        leak = (token in r2.text) or ("private_key" in r2.text)
        print(f"  {'✅' if not leak else '❌'} 응답에 비밀 없음 — {r2.text[:80]}")
        return 0
    finally:
        # ★여기는 성공·실패·예외·Ctrl+C 어느 경우에도 지나간다.
        #   ⚠️이 블록이 DB 를 되돌리는 것이 아니다 — 안내하고, 사용자가 실행한 결과를
        #     200 복귀로 확인할 뿐이다(이 스크립트에는 DB 쓰기 권한이 없다).
        if revoked:
            print("\n" + "=" * 68)
            print("③ 반드시 원복해 주세요 — 아래 SQL 을 **사용자가** 실행하십시오")
            print("   (이 스크립트는 DB 를 고치지 못합니다. 실행 결과만 확인합니다.)")
            print("=" * 68)
            print(f"\n    {sql_restore(device_id)}\n")
            print("실행하시면 200 복귀를 자동으로 확인합니다. 기다리는 중…", flush=True)
            if wait_for(url, key, token, 200, "정상(200)"):
                RECOVERY_FILE.unlink(missing_ok=True)
                print("  ✅ 원복 완료 — revoked_at = null (개발 PC 정상)")
            else:
                print("  ❌ 아직 원복되지 않았습니다!")
                print(f"     복구 안내 파일: {RECOVERY_FILE}")
                print(f"     SQL: {sql_restore(device_id)}")
                print("     확인: scripts\\test_revoke_e.py --restore-only")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass
    raise SystemExit(main())
