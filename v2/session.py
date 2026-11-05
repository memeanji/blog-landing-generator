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
    # ── Stage 2 (2026-11-03) ──────────────────────────────────────
    p.add_argument("--logout", metavar="ACCOUNT",
                   help="**이 계정만** 로그아웃한다(프로필 보존 · 다른 계정 영향 없음)")
    p.add_argument("--hard", action="store_true",
                   help="--logout 시 프로필 안 로그인 파일까지 지운다"
                        "(별도 승인 기능: BLOG_ALLOW_HARD_LOGOUT=1 필요)")
    p.add_argument("--confirm", metavar="ACCOUNT",
                   help="사용자가 계정을 확인했다고 기록한다(manual 등급, 인증 아님)")
    p.add_argument("--confirm-shared", action="store_true",
                   help="--logout 시 세션을 공유하는 계정이 있어도 진행한다"
                        "(사용자가 영향 범위를 확인한 경우에만)")
    p.add_argument("--relogin", action="store_true", help="--login 시 저장 세션을 먼저 지운다")
    p.add_argument("--ref-tab", metavar="탭이름",
                   help="이 기준랜딩 탭의 계정을 쓴다(없으면 이 PC 에 만든다)")
    p.add_argument("--brand", metavar="브랜드", help="--ref-tab 과 함께 쓴다")
    p.add_argument("--events", action="store_true",
                   help="진행 상황을 `@@EVENT {json}` 로도 알린다(화면이 읽는다)")
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
        if acc.login_id:
            log(f"[로그인] ★이 계정으로 들어가 주세요 — {acc.title} · {acc.login_id}")
        pw, ctx, blog_id = await browser.open_session(settings, log, relogin=relogin)
        log("")
        log(f"[결과] 로그인된 계정 — {blog_id}")

        if acc.blog_id and blog_id.casefold() != acc.blog_id.casefold():
            # ★다른 계정으로 들어왔다. 그냥 넘기면 그 사람 블로그에 글이 올라간다.
            #   세션을 저장하지 않고 여기서 멈춘다.
            # ★검증 실패한 인증 정보를 **다른 계정의 정상 세션으로 저장하지 않는다.**
            #   세션 저장(save_state)·별칭 연결을 둘 다 하지 않고 여기서 끝낸다.
            from . import login_state
            login_state.set_auth_state(
                acc, grade=login_state.UNAVAILABLE, blog_id=blog_id,
                expected_login_id=acc.login_id, by="blog_id_mismatch",
                note=f"기대 blog_id={acc.blog_id} / 확인 {blog_id}")
            log(f"[결과] ❌ 계정이 다릅니다 — {acc.title} 자리에 {blog_id} 로 "
                f"로그인했습니다.")
            log(f"       {acc.title} 은(는) {acc.blog_id} 입니다"
                + (f" (로그인 ID {acc.login_id})." if acc.login_id else ".")
                + " 다시 [실행 준비] 를 눌러 그 계정으로 들어가 주세요.")
            return 1

        if not acc.blog_id and blog_id:
            # ★처음 확인된 블로그 주소를 적어 둔다. 다음부터는 위 검사가 걸려
            #   계정이 어긋나는 일을 막을 수 있다.
            try:
                accounts.set_blog_id(acc.id, blog_id)
                log(f"[계정] {acc.title} 의 블로그 주소를 {blog_id} 로 적어 뒀습니다. "
                    f"다음부터는 다른 계정으로 들어오면 막습니다.")
            except Exception as exc:                           # noqa: BLE001
                log(f"[계정] 블로그 주소를 적지 못했습니다: {exc}")

        log(f"[결과] ✅ 로그인 확인 — {acc.title} ({blog_id})")
        session_store.write_meta(acc, blog_id=blog_id)

        # ── Stage 2 — 무엇을 어떻게 확인했는지 등급으로 남긴다 ──────
        #   ★headless 로 저장 세션을 재사용한 경우는 입력 ID 를 알 수 없다 →
        #     기존 등급을 **승계**한다(올리지도 내리지도 않는다). blog_id 대조는 매번 한다.
        from . import login_state
        login_state.set_account_map(acc, login_id=acc.login_id, blog_id=blog_id,
                                    ref_tab=acc.ref_tab, brand=acc.brand)

        # ★네이버 인증 ID 읽기 — **기본 꺼짐**(BLOG_READ_LOGIN_ID=1 에서만).
        #   읽히면 등급이 authenticated 로 올라갈 수 있고, 못 읽으면 예전과 똑같다.
        #   실패해도 로그인 흐름을 멈추지 않는다(사유만 남긴다).
        auth_lid = ""
        try:
            auth_lid, why_lid = await browser.read_login_id(ctx, log)
            if not auth_lid:
                log(f"[검증] 네이버 인증 ID — {why_lid}")
        except Exception as exc:                               # noqa: BLE001
            log(f"[검증] 네이버 인증 ID 읽기 실패({type(exc).__name__}) — "
                f"등급을 올리지 않고 계속합니다")

        state = login_state.record_login_result(
            acc, seen_blog_id=blog_id, expected_blog_id=acc.blog_id,
            expected_login_id=acc.login_id, authenticated_login_id=auth_lid,
            inherit=bool(headless))
        log(f"[검증] 로그인 계정 확인 수준 = {state['grade']}"
            f"({state['grade_label']}) · 발행 대상 블로그 대조="
            f"{'일치' if state.get('blog_verified') else '확인 불가'}"
            f" · 적용 단계={login_state.mode()}")
        if acc.login_id:
            linked, why = login_state.link_session(
                acc.login_id, acc, grade=state["grade"],
                ref_tab=acc.ref_tab, brand=acc.brand)
            log(f"[검증] 세션 연결 — {why}")
        # ★화면은 이 응답으로 판단한다(클라우드에는 계정 파일이 없다).
        log.event("session_ready", session_ready=True, account=acc.id,
                  account_name=acc.title, login_id=acc.login_id or "",
                  blog_id=blog_id, grade=state["grade"],
                  grade_label=state["grade_label"],
                  blog_verified=bool(state.get("blog_verified")),
                  auth_id_read=bool(auth_lid),
                  check_mode=login_state.mode())
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


# ══════════════════════════════════════════════════════════════════
#  선택 계정 로그아웃 (Stage 2 · 2026-11-03)
#    · 고른 계정 **하나만** 초기화한다. 다른 계정 폴더는 열지도 않는다.
#    · 영구 프로필은 **보존**(기기 등록 흔적 유지) — 다음 로그인에서 2차 인증을
#      다시 겪지 않는다. hard 는 별도 승인 기능이다.
#    · 쿠키 **백업 파일을 만들지 않는다.** 쿠키 값은 로그에도 남기지 않는다.
#    · 네이버 **서버 로그아웃**과 **로컬 초기화**를 따로 판정해 보고한다.
# ══════════════════════════════════════════════════════════════════
NAVER_LOGOUT_URL = "https://nid.naver.com/nidlogin.logout"


async def _server_logout(acc, log) -> str:
    """네이버 서버 로그아웃. `ok` / `unknown` / `failed` 를 돌려준다.

    ★저장 쿠키를 **지우기 전에** 해야 의미가 있다(쿠키가 없으면 서버는 누군지 모른다).
    """
    from dataclasses import replace

    from . import browser, session_store
    settings = replace(load_settings(account=acc), headless=True)
    pw = ctx = None
    try:
        pw, ctx = await browser.launch(settings, log)
        n = len(session_store.load_cookies(acc))
        if not n:
            log("[로그아웃] 저장된 쿠키가 없습니다 — 서버 로그아웃은 건너뜁니다")
            return "unknown"
        await browser._restore_cookies(ctx, session_store.load_cookies(acc), log)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(NAVER_LOGOUT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        got = await browser.confirm_login(ctx, settings.blog_home_url, log)
        if got:
            log(f"[로그아웃] ⚠ 서버 로그아웃 뒤에도 로그인 상태로 보입니다({got})")
            return "failed"
        log("[로그아웃] 네이버 서버 로그아웃 확인 ✅")
        return "ok"
    except Exception as exc:                                   # noqa: BLE001
        log(f"[로그아웃] 서버 로그아웃 결과를 확인하지 못했습니다"
            f"({type(exc).__name__}) — '확인 불가' 로 둡니다")
        return "unknown"
    finally:
        for close in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
            if close is None:
                continue
            try:
                await close()
            except Exception:                                  # noqa: BLE001
                pass


async def _auto_login_recheck(acc, log) -> str:
    """로그아웃 뒤 **자동 로그인이 되살아나는지** 확인한다(쿠키를 심지 않고 띄운다).

    `clean`(재로그인 필요) / `auto_login_detected` / `unknown`.
    """
    from dataclasses import replace

    from . import browser
    settings = replace(load_settings(account=acc), headless=True)
    pw = ctx = None
    try:
        pw, ctx = await browser.launch(settings, log)
        got = await browser.confirm_login(ctx, settings.blog_home_url, log)
        if got:
            log(f"[로그아웃] ⚠ 프로필만으로 다시 로그인됩니다 — 계정 {got}. "
                f"완전히 끊으려면 hard logout(별도 승인)이 필요합니다.")
            return "auto_login_detected"
        log("[로그아웃] 자동 로그인 재발 없음 — 재로그인 필요 상태 ✅")
        return "clean"
    except Exception as exc:                                   # noqa: BLE001
        log(f"[로그아웃] 자동 로그인 재발 확인 실패({type(exc).__name__}) — 확인 불가")
        return "unknown"
    finally:
        for close in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
            if close is None:
                continue
            try:
                await close()
            except Exception:                                  # noqa: BLE001
                pass


def cmd_logout(acc, log, hard: bool = False, confirm_shared: bool = False) -> int:
    """선택 계정 로그아웃 — 서버/로컬 결과를 **따로** 보고한다."""
    import os

    from . import login_state, session_store

    lid = (acc.login_id or "").strip() or login_state.account_map(acc).get("login_id", "")
    log(f"[로그아웃] 대상 — {acc.title} (세션 {acc.id}"
        + (f" · login_id={lid}" if lid else " · login_id 없음") + ")")
    log("           ★이 계정만 초기화합니다. 다른 로그인 계정의 세션은 그대로 둡니다.")

    # ── 세션 공유 영향 범위 — 불분명하면 **하지 않는다** ──────────
    allowed, peers, why = login_state.logout_allowed(acc)
    if peers:
        #  ★무엇이 영향을 받을 수 있는지 **빠짐없이** 적는다(2026-11-05).
        #    `--confirm-shared` 로 넘어갈지 사람이 판단하는 근거가 이 목록이다.
        #    세션(쿠키)이 있는 등록만 서버 로그아웃으로 실제로 끊긴다.
        _live = [p for p in peers if p.get("has_session")]
        log(f"[로그아웃] 영향받을 수 있는 등록 {len(peers)}건"
            + (f" (그중 {len(_live)}건은 이 PC 에 세션이 저장돼 있습니다)"
               if _live else "") + ":")
        for pr in peers:
            _sess = (f"쿠키 {pr.get('cookies', 0)}개 저장됨"
                     if pr.get("has_session") else "이 PC 에 세션 없음")
            log(f"           · {pr['label']} (id={pr['id']}, "
                f"login_id={pr['login_id'] or '없음'}, 브랜드={pr['brand'] or '기본'}) "
                f"— {pr['why']} · {_sess}")
        log("           ★네이버 서버 로그아웃은 **같은 네이버 계정** 전체의 세션을 "
            "끊습니다.")
        log("             쿠키 폴더는 계정별로 분리돼 있어도 이 영향은 막지 못합니다.")
    if not allowed and not confirm_shared:
        log(f"[로그아웃] ❌ 중단 — {why}")
        log("           영향 범위를 확인하신 뒤 다시 실행해 주세요"
            "(화면에서 '공유 세션 확인' 을 체크하면 진행됩니다).")
        log.event("logout_blocked", account=acc.id, login_id=lid,
                  reason="shared_session_unverified",
                  peers=[pr["id"] for pr in peers])
        return 1
    if peers and confirm_shared and not allowed:
        log("[로그아웃] 사용자가 영향 범위를 확인했습니다 — 진행합니다.")

    if hard and (os.getenv("BLOG_ALLOW_HARD_LOGOUT") or "").strip() != "1":
        log("[로그아웃] ❌ hard logout 은 별도 승인 기능입니다 "
            "(BLOG_ALLOW_HARD_LOGOUT=1 이 없어 중단).")
        return 1

    server = asyncio.run(_server_logout(acc, log))

    if hard:
        ok, why = session_store.browser_closed(acc)
        if not ok:
            log(f"[로그아웃] ❌ {why} — hard logout 을 하지 않았습니다.")
            return 1
        log(f"[로그아웃] 브라우저 종료 확인 — {why}")

    res = session_store.logout(acc, hard=hard)
    login_state.clear_auth_state(acc)
    log(f"[로그아웃] 로컬 초기화 — 지운 항목 {res['local_cleared'] or '없음'}"
        + (f" · 건너뜀 {res['skipped']}" if res["skipped"] else "")
        + f" · 프로필 {'보존' if res['profile_kept'] else '삭제'}")

    auto = asyncio.run(_auto_login_recheck(acc, log))
    if lid:
        login_state.clear_logout_pending(lid)

    local = "cleared" if res["local_cleared"] else "nothing_to_clear"
    log("")
    log(f"[결과] 서버 로그아웃={server} · 로컬 초기화={local} · 자동로그인재발={auto}")
    log.event("logout_done", account=acc.id, login_id=lid, server=server,
              local=local, auto_login=auto, hard=bool(res["hard"]),
              profile_kept=bool(res["profile_kept"]),
              shared_with=[pr["id"] for pr in peers])
    return 0 if server in ("ok", "unknown") and auto != "unknown" else 1


def cmd_confirm(acc, log) -> int:
    """사용자가 '이 계정 맞다' 고 한 것을 **기록**한다(manual · 인증 아님)."""
    from . import login_state, session_store

    info = session_store.describe(acc)
    if not info["state_exists"]:
        log("[확인] ❌ 저장된 세션이 없습니다 — 먼저 [실행 준비] 로 로그인해 주세요.")
        return 1
    lid = (acc.login_id or "").strip()
    state = login_state.record_login_result(
        acc, seen_blog_id=info.get("blog_id") or "", expected_blog_id=acc.blog_id,
        expected_login_id=lid, manual_ok=True)
    login_state.set_account_map(acc, login_id=lid, blog_id=info.get("blog_id") or "",
                               ref_tab=acc.ref_tab, brand=acc.brand)
    linked, why = login_state.link_session(lid, acc, grade=state["grade"],
                                           ref_tab=acc.ref_tab, brand=acc.brand)
    log(f"[확인] 기록 완료 — 등급 {state['grade']}({state['grade_label']}) "
        f"· ★네이버 인증 확인이 아니라 **사용자 확인 기록**입니다.")
    log(f"[확인] 세션 연결 — {why}")
    log.event("login_confirmed", account=acc.id, login_id=lid,
              grade=state["grade"], linked=bool(linked))
    return 0


def account_for(key, ref_tab: str = "", brand: str = "", log=None):
    """쓸 계정을 정한다(공용 해석기를 그대로 쓴다)."""
    before = None
    try:
        before = find_existing(key, ref_tab, brand)
    except Exception:                                          # noqa: BLE001
        before = None
    acc = accounts.resolve_for(key, ref_tab, brand or None)
    if log and before is None and acc is not None:
        log(f"[계정] 이 PC 에 새로 등록했습니다 — {acc.title} ({acc.id})")
    return acc


def find_existing(key, ref_tab: str = "", brand: str = ""):
    """이미 있는 계정만 찾아본다(없으면 None) — 새로 만들지 않는다."""
    return accounts.resolve_for(key, ref_tab, brand or None, create=False)


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    log = Log(settings.out_dir, tag="v2_session",
              events=bool(getattr(args, "events", False)))
    try:
        actions = (args.adopt, args.login, args.check, args.clear,
                   args.logout, args.confirm)
        if args.list or not any(actions):
            return cmd_list(log)

        key = (args.adopt or args.login or args.check or args.clear
               or args.logout or args.confirm)
        acc = account_for(key, getattr(args, "ref_tab", ""),
                          getattr(args, "brand", ""), log)

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
        if args.logout:
            return cmd_logout(acc, log, hard=bool(args.hard),
                              confirm_shared=bool(getattr(args, "confirm_shared",
                                                          False)))
        if args.confirm:
            return cmd_confirm(acc, log)
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
