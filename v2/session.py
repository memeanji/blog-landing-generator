r"""계정 세션 관리 — 브라우저를 켜지 않고 현황을 보고, 필요할 때만 로그인 창을 띄운다.

    .\.venv\Scripts\python.exe -m v2.session --list                  # 계정·세션 현황(브라우저 X)
    .\.venv\Scripts\python.exe -m v2.session --adopt my_account   # 기존 프로필을 계정 폴더로 복사
    .\.venv\Scripts\python.exe -m v2.session --login my_account   # 로그인 창 → 세션 저장
    .\.venv\Scripts\python.exe -m v2.session --check my_account   # 저장 세션이 살아 있는지(headless)
    .\.venv\Scripts\python.exe -m v2.session --clear old_account       # 저장 세션 삭제(--profile 이면 폴더까지)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from dataclasses import replace

from . import accounts, browser, session_store
from .config import load_settings
from .logger import Log


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="네이버 계정 세션 관리")
    p.add_argument("--list", action="store_true", help="계정·세션 현황(브라우저 안 켬)")
    p.add_argument("--adopt", metavar="ACCOUNT",
                   help="기존 playwright-profile 을 이 계정 폴더로 복사(기기 등록 흔적 유지)")
    p.add_argument("--from-profile", metavar="PATH", help="--adopt 원본 경로 직접 지정")
    p.add_argument("--overwrite", action="store_true", help="--adopt 대상이 있어도 덮어쓴다")
    p.add_argument("--login", metavar="ACCOUNT", help="로그인 창을 띄우고 세션을 저장한다")
    p.add_argument("--check", metavar="ACCOUNT",
                   help="저장 세션으로 로그인되는지 headless 로 확인한다(창 안 뜸)")
    p.add_argument("--clear", metavar="ACCOUNT", help="저장 세션 삭제")
    p.add_argument("--profile", action="store_true", help="--clear 시 프로필 폴더까지 삭제")
    p.add_argument("--relogin", action="store_true", help="--login 시 저장 세션을 먼저 지운다")
    return p.parse_args(argv)


def cmd_list(log) -> int:
    rows = accounts.load_accounts(include_disabled=True)
    if not rows:
        log(f"[계정] {accounts.ACCOUNTS_PATH} 에 계정이 없습니다.")
        return 1
    log(f"[계정] {len(rows)}개 — {accounts.ACCOUNTS_PATH}")
    for acc in rows:
        info = session_store.describe(acc)
        state = (f"세션 있음 (쿠키 {info['cookies']}개 · 저장 {info['saved_at']}"
                 + (f" · blog_id={info['blog_id']}" if info["blog_id"] else "") + ")"
                 if info["state_exists"] else "세션 없음 — 로그인 필요")
        log("")
        log(f"  ● {acc.id}  {acc.title}{'' if acc.enabled else '  (사용 안 함)'}")
        log(f"      blog_id  : {acc.blog_id or '(미지정)'}")
        log(f"      기준랜딩 : {acc.ref_tab or '(기본값)'}")
        log(f"      프로필   : {info['profile']}"
            f"{'' if info['profile_exists'] else '  ← 아직 없음(첫 실행 때 생성)'}")
        log(f"      상태     : {state}")
        if acc.note:
            log(f"      메모     : {acc.note}")
    return 0


async def _open(acc, log, headless: bool, relogin: bool) -> int:
    settings = replace(load_settings(account=acc), headless=headless)
    pw = ctx = None
    try:
        pw, ctx, blog_id = await browser.open_session(settings, log, relogin=relogin)
        log("")
        log(f"[결과] ✅ 로그인 확인 — 계정 {blog_id}")
        if acc.blog_id and blog_id.casefold() != acc.blog_id.casefold():
            log(f"[경고] accounts.json 의 blog_id({acc.blog_id})와 다릅니다. "
                f"accounts.json 을 고치거나 다른 계정으로 로그인하세요.")
            return 1
        session_store.write_meta(acc, blog_id=blog_id)
        return 0
    except Exception as exc:                                   # noqa: BLE001
        log(f"[결과] ❌ {exc}")
        return 1
    finally:
        for close in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
            if close is None:
                continue
            try:
                await close()
            except Exception:                                  # noqa: BLE001
                pass


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    log = Log(settings.out_dir, tag="v2_session")
    try:
        if args.list or not any((args.adopt, args.login, args.check, args.clear)):
            return cmd_list(log)

        key = args.adopt or args.login or args.check or args.clear
        acc = accounts.resolve(key)

        if args.adopt:
            src = args.from_profile or None
            dest = session_store.adopt_legacy_profile(acc, src, overwrite=args.overwrite)
            log(f"[복사] 완료 → {dest}")
            log("       ※ 쿠키가 아니라 **기기 등록 흔적**을 옮긴 것입니다. "
                "로그인은 `--login` 으로 한 번 해 주세요.")
            return 0
        if args.clear:
            gone = session_store.clear_state(acc, profile=args.profile)
            log(f"[삭제] {acc.id} — {gone or '지울 것이 없었습니다'}")
            return 0
        if args.login:
            log(f"[로그인] {acc.title} — 창이 뜨면 직접 로그인해 주세요.")
            return asyncio.run(_open(acc, log, headless=False, relogin=args.relogin))
        log(f"[확인] {acc.title} — 저장 세션으로 headless 확인합니다(창 안 뜸).")
        return asyncio.run(_open(acc, log, headless=True, relogin=False))
    except Exception as exc:                                   # noqa: BLE001
        log(f"[오류] {exc}")
        log(traceback.format_exc())
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
