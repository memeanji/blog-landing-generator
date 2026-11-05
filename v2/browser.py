"""Chromium 실행 · 수동 로그인 대기 · 에디터 frame 탐색 (공통 저수준 헬퍼)."""
from __future__ import annotations

import asyncio
import re

NAVER_LOGIN_URL = "https://nid.naver.com/nidlogin.login"


async def launch(settings, log):
    """기존 playwright-profile 을 재사용하는 persistent context 를 연다."""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(settings.user_data_dir),
        headless=settings.headless,
        viewport={"width": 1480, "height": 980},
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        await ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                                    origin="https://blog.naver.com")
    except Exception as exc:                                   # noqa: BLE001
        log(f"[브라우저] 클립보드 권한 부여 실패(무시): {type(exc).__name__}")
    log(f"[브라우저] 실행 완료 · 프로필={settings.user_data_dir}"
        f"{' · headless(창 안 뜸)' if settings.headless else ''}")
    return pw, ctx


# 2차 인증 / 기기 등록 / 보안 확인 화면에서 나오는 문구(본계정 로그인 대비)
TWOFA_MARKS = ("2단계 인증", "일회용 로그인 번호", "인증번호", "새로운 기기", "기기 등록",
               "등록되지 않은 기기", "본인확인", "보안 인증", "OTP", "휴대전화 인증")


async def _on_login_host(page) -> bool:
    u = page.url or ""
    return "nid.naver.com" in u or "nidlogin" in u


async def _confirm_logged_in(ctx, settings_url: str, log) -> str:
    """정말 로그인됐는지 **직접 확인**한다. 로그인 상태면 블로그 ID, 아니면 빈 문자열.

    ★쿠키(NID_SES)만 보고 판단하면 안 된다. 세션이 프로필에 남아 있으면 사람이 로그인하지
      않았는데도 통과하고, 본계정처럼 2차 인증이 걸리는 계정에서는 인증 화면을 그대로
      지나쳐 버린다(2026-08-21 사용자 지적).
    """
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto(settings_url, wait_until="domcontentloaded")
    except Exception as exc:                                   # noqa: BLE001
        log(f"[로그인] 확인 페이지 이동 실패({type(exc).__name__}) — 계속 기다립니다")
        return ""

    # ★#mainFrame 의 src(blogId=…)가 채워지는 데 시간이 걸린다.
    #   1.5초만 보고 판정했더니 로그인돼 있는데도 '아님'으로 봤다(2026-08-21 실측).
    for _ in range(8):                                         # 최대 약 6초
        await page.wait_for_timeout(800)
        if await _on_login_host(page):
            return ""
        try:
            marks = await page.evaluate(BLOG_ID_JS)
        except Exception:                                      # noqa: BLE001
            continue
        for key in ("mainFrame", "writeLink", "canonical", "og", "href"):
            got = _extract_blog_id(str(marks.get(key) or ""))
            if got:
                return got
    return ""


async def open_session(settings, log, relogin: bool = False):
    """브라우저를 열고 **로그인된 상태**로 만들어 (pw, ctx, blog_id) 를 돌려준다.

    ★2026-08-25 사용자 요청: headless 로 돌리되 **로그인할 때만 창을 잠깐 띄운다.**
      로그인이 끝나면 그 창을 닫고(세션은 프로필에 저장된다) 다시 headless 로 돌아온다.
      작업하는 동안 창이 떠 있으면 다른 일을 못 하기 때문이다.

    ★2026-08-27: `settings.account` 가 있으면 쿠키를 **계정별 파일**
      (`sessions/<account>/state.json`)로도 저장·복원한다. 계정을 오가도 서로의 세션을
      덮어쓰지 않고, 한 번 로그인해 두면 그 계정은 창 없이 계속 돌아간다.
    """
    from dataclasses import replace

    # ★`settings.account` 가 있을 때만 계정별 세션 파일을 쓴다.
    #   비어 있으면(=`--account` 없이 돌린 기존 CLI) 예전과 완전히 동일하게 동작한다.
    account = (getattr(settings, "account", "") or "").strip()
    if account and relogin:
        from . import session_store

        gone = session_store.clear_state(account)
        if gone:
            log(f"[세션] --relogin — `{account}` 저장 세션 삭제: {gone}")

    if not settings.headless:
        pw, ctx = await launch(settings, log)
        if not relogin:
            await _prime_session(ctx, account, log)
        logged = await wait_manual_login(ctx, log, force=relogin,
                                         blog_home_url=settings.blog_home_url)
        await _persist_session(ctx, account, logged, log)
        return pw, ctx, logged

    # 1) headless 로 먼저 열어 저장된 세션이 살아 있는지 확인한다(창이 안 뜬다).
    if not relogin:
        pw, ctx = await launch(settings, log)
        await _prime_session(ctx, account, log)
        logged = await confirm_login(ctx, settings.blog_home_url, log)
        if logged:
            log(f"[로그인] 저장된 세션 확인 — 계정 {logged} · 창 없이 진행합니다")
            await _persist_session(ctx, account, logged, log)   # 갱신된 쿠키로 덮어쓴다
            return pw, ctx, logged
        log("[로그인] 저장된 세션이 없습니다 — **로그인 창만** 잠깐 띄웁니다.")
        await _shutdown(pw, ctx)

    # 2) 로그인 전용으로 창을 띄운다. ★프로필은 하나뿐이라 반드시 앞의 것을 닫고 연다.
    pw, ctx = await _launch_retry(replace(settings, headless=False), log)
    cookies: list = []
    try:
        logged = await wait_manual_login(ctx, log, force=relogin,
                                         blog_home_url=settings.blog_home_url)
        # ★★네이버 `NID_SES` 는 **세션 쿠키**라 창을 닫으면 프로필에 남지 않는다.
        #   (2026-08-25: 창에서 로그인 성공 → headless 재실행에서 로그인 화면으로 튕겼다)
        #   그래서 닫기 **전에** 쿠키를 통째로 들고 나와 headless 컨텍스트에 심는다.
        try:
            cookies = await ctx.cookies()
            log(f"[로그인] 세션 쿠키 {len(cookies)}개 확보 — headless 로 넘깁니다")
            if account:
                from . import session_store

                # ★계정이 어긋나면 **저장하지 않는다.** 뒤에서 호출한 쪽이
                #   어긋남을 보고 멈춘다. 이 경로에서는 쿠키를 headless 로
                #   넘기지도 않는다(넘기면 그 세션으로 작업이 이어진다).
                verdict, why = save_verdict(account, logged or "")
                if verdict == "mismatch":
                    log(f"[세션] ❌ 저장하지 않았습니다 — {why}")
                    cookies = []              # headless 로 넘기지도 않는다
                    await _wipe_context_cookies(ctx, log)
                elif verdict == "unreadable":
                    log(f"[세션] ❌ 저장하지 않았습니다 — {why}")
                else:
                    path = session_store.save_state(account, cookies,
                                                    blog_id=logged or "")
                    log(f"[세션] `{account}` 세션 저장 완료 → {path}")
        except Exception as exc:                               # noqa: BLE001
            log(f"[로그인] 쿠키를 읽지 못했습니다({type(exc).__name__})")
    finally:
        await _shutdown(pw, ctx)
    if not logged:
        raise RuntimeError("[로그인] 로그인을 확인하지 못했습니다.")
    log(f"[로그인] 완료 — 계정 {logged}. 창을 닫고 headless 로 이어서 진행합니다.")

    # 3) 다시 headless 로 열어 쿠키를 심고 세션을 확인한다.
    pw, ctx = await _launch_retry(settings, log)
    await _restore_cookies(ctx, cookies, log)
    got = ""
    for i in range(2):                                         # 한 번은 더 확인해 본다
        got = await confirm_login(ctx, settings.blog_home_url, log)
        if got:
            break
        log(f"[로그인] headless 세션 확인 재시도 {i + 1}/2")
    if not got:
        await _shutdown(pw, ctx)
        raise RuntimeError("[로그인] 창에서는 로그인됐는데 headless 에서 세션이 확인되지 "
                           "않습니다. `--no-headless` 로 돌려 주세요.")
    log(f"[로그인] headless 세션 확인 완료 — 계정 {got} · 창 없이 진행합니다")
    return pw, ctx, got


async def _prime_session(ctx, account: str, log) -> int:
    """계정 폴더(`sessions/<id>/state.json`)에 저장해 둔 쿠키를 컨텍스트에 심는다.

    ★네이버 `NID_SES` 는 세션 쿠키라 프로필 폴더에 남지 않는다. 이 복원이 있어야
      다음 실행에서 사람이 다시 로그인하지 않는다(2026-08-25 실측 문제의 계정별 버전).
    """
    if not account:
        return 0
    from . import session_store

    cookies = session_store.load_cookies(account)
    if not cookies:
        return 0
    n = await _restore_cookies(ctx, cookies, log)
    info = session_store.describe(account)
    log(f"[세션] `{account}` 저장 세션 복원 — 쿠키 {n}개 "
        f"(저장 {info.get('saved_at') or '?'})")
    return n


def registered_blog_id(account: str) -> str:
    """이 세션 폴더(계정 id)에 **등록된** blog_id. 모르면 빈 문자열.

    ★`id` **정확일치**로만 찾는다. login_id·blog_id 로 느슨하게 찾으면, 한 계정의
      폴더 이름이 다른 계정의 blog_id 와 같을 때 엉뚱한 계정을 집어온다(2026-09-16).
    """
    try:
        from . import accounts

        for acc in accounts.load_accounts(include_disabled=True):
            if acc.id == account:
                return (acc.blog_id or "").strip()
    except Exception:                                          # noqa: BLE001
        return ""                             # 목록을 못 읽으면 '모른다'(예전 동작)
    return ""


def saved_blog_id(account: str) -> str:
    """그 세션 폴더에 **지금까지 저장돼 있던** blog_id. 없으면 빈 문자열.

    ★등록(accounts) 쪽에 blog_id 가 비어 있는 계정을 지키는 두 번째 기준이다.
      이미 그 폴더로 A 블로그에 로그인해 쓰고 있었는데 B 블로그로 들어왔다면,
      등록값이 없어도 **그 세션을 덮어써서는 안 된다**.
    """
    try:
        from . import session_store

        return (session_store.describe(account).get("blog_id") or "").strip()
    except Exception:                                          # noqa: BLE001
        return ""


def reference_blog_id(account: str) -> tuple[str, str]:
    """대조 기준 blog_id 와 그 출처. ① 등록값 ② 그 세션에 저장돼 있던 값 순."""
    want = registered_blog_id(account)
    if want:
        return want, "등록 blog_id"
    return saved_blog_id(account), "이 세션에 저장돼 있던 blog_id"


def save_verdict(account: str, blog_id: str) -> tuple[str, str]:
    """`(판정, 사유)` — `verified` / `mismatch` / `unreadable` / `unknown`.

        verified    기준값과 일치한다 → 저장한다
        mismatch    기준값과 **다르다** → 저장하지 않고, 프로필 쿠키까지 지운다
        unreadable  기준값은 있는데 **지금 블로그를 못 읽었다**
                    → 저장하지 않는다(기존 정상 세션을 보존한다). 쿠키는 **지우지 않는다**.
        unknown     기준값이 아예 없다(첫 로그인) → 예전처럼 저장하되
                    '검증됨' 으로 기록하지 않는다

    ★`login_id` 가 같은지는 여기서 알 수 없다 — 그래서 `verified` 도 '블로그가
      맞다' 는 뜻일 뿐이고, 로그인 계정 확인 등급은 `login_state` 가 따로 매긴다.
    """
    got = (blog_id or "").strip()
    want, where = reference_blog_id(account)
    if not got:
        # ★기준값이 있는데 지금 블로그를 못 읽었다 = 대조 불가.
        #   여기서 저장하면 **기준값이 지워져** 다음부터 영구히 대조 불가가 된다.
        if want:
            return "unreadable", (
                f"로그인된 블로그를 읽지 못했습니다 — {where}({want}) 로 쓰던 "
                f"`{account}` 세션을 대조 없이 덮어쓰지 않습니다")
        return "unknown", "로그인된 블로그를 읽지 못했습니다(기준값도 없음)"
    if not want:
        return "unknown", "대조할 기준 blog_id 가 없습니다(첫 로그인)"
    if want.casefold() == got.casefold():
        return "verified", f"{where}({want}) 와 일치"
    return "mismatch", (f"로그인된 블로그({got})가 {where}({want}) 와 다릅니다 — "
                        f"남의 인증정보를 `{account}` 세션으로 저장하지 않습니다")


BLOCK_SAVE = ("mismatch", "unreadable")


def save_blocked(account: str, blog_id: str) -> str:
    """저장하면 안 되는 이유. 저장해도 되면 빈 문자열(`unknown` 은 허용).

    ★`unknown`(기준값 없음)은 **예전과 똑같이** 저장한다 — 첫 로그인과 기존 계정의
      동작을 바꾸지 않기 위해서다. 다만 검증됐다고 기록하지는 않는다.
    """
    verdict, why = save_verdict(account, blog_id)
    return why if verdict in BLOCK_SAVE else ""


async def _wipe_context_cookies(ctx, log) -> bool:
    """어긋난 로그인의 흔적을 **영구 프로필에서도** 지운다.

    ★`launch_persistent_context` 는 세션이 아닌 쿠키(NID_AUT 등)를 프로필 파일에
      남긴다. state.json 을 저장하지 않아도, 다음 실행에서 그 프로필만으로 **엉뚱한
      계정으로 자동 로그인**될 수 있다. 저장을 막은 경로에서는 여기서 끊는다.
      (프로필 폴더 자체는 지우지 않는다 — 기기 등록 흔적은 남긴다.)
    """
    try:
        await ctx.clear_cookies()
    except Exception as exc:                                   # noqa: BLE001
        log(f"[세션] ⚠ 어긋난 로그인 쿠키를 지우지 못했습니다"
            f"({type(exc).__name__}) — 다음 실행에서 자동 로그인될 수 있습니다. "
            f"필요하면 `--relogin` 으로 다시 로그인해 주세요.")
        return False
    log("[세션] 어긋난 로그인 쿠키를 프로필에서도 지웠습니다(프로필 폴더는 보존)")
    return True


async def _persist_session(ctx, account: str, blog_id: str, log) -> int:
    """지금 컨텍스트의 쿠키를 계정 폴더에 저장한다(창을 닫기 **전에** 불러야 한다)."""
    if not account:
        return 0
    from . import session_store

    verdict, why = save_verdict(account, blog_id)
    if verdict == "mismatch":
        log(f"[세션] ❌ 저장하지 않았습니다 — {why}")
        await _wipe_context_cookies(ctx, log)       # 어긋난 로그인 흔적을 끊는다
        return 0
    if verdict == "unreadable":
        # ★쿠키를 지우지 않는다 — 기존 정상 세션을 끊으면 재로그인을 유발한다.
        log(f"[세션] ❌ 저장하지 않았습니다 — {why}")
        return 0
    if verdict == "unknown":
        log(f"[세션] (참고) {why} — 저장은 하지만 '검증됨' 으로 기록하지 않습니다")

    try:
        cookies = await ctx.cookies()
    except Exception as exc:                                   # noqa: BLE001
        log(f"[세션] 쿠키를 읽지 못해 저장하지 못했습니다({type(exc).__name__})")
        return 0
    try:
        path = session_store.save_state(account, cookies, blog_id=blog_id or "")
    except Exception as exc:                                   # noqa: BLE001
        log(f"[세션] 저장 실패({type(exc).__name__}: {exc})")
        return 0
    log(f"[세션] `{account}` 세션 저장 완료 → {path}")
    return len(cookies)


async def _restore_cookies(ctx, cookies: list, log) -> int:
    """로그인 창에서 들고 온 쿠키를 headless 컨텍스트에 심는다.

    ★`NID_SES` 같은 세션 쿠키는 프로필 파일에 저장되지 않으므로 이 이식이 없으면
      headless 가 로그인 화면으로 튕긴다(2026-08-25 실측).
    """
    if not cookies:
        return 0
    try:
        await ctx.add_cookies(cookies)
        log(f"[로그인] 쿠키 {len(cookies)}개 이식 완료")
        return len(cookies)
    except Exception as exc:                                   # noqa: BLE001
        log(f"[로그인] 쿠키 일괄 이식 실패({type(exc).__name__}) — 하나씩 넣습니다")
    ok = 0
    for c in cookies:
        try:
            await ctx.add_cookies([c])
            ok += 1
        except Exception:                                      # noqa: BLE001
            continue
    log(f"[로그인] 쿠키 {ok}/{len(cookies)}개 이식")
    return ok


async def _launch_retry(settings, log, tries: int = 4):
    """프로필을 닫자마자 다시 열면 아직 잠겨 있을 수 있다(프로필은 하나뿐) — 몇 번 재시도."""
    import asyncio

    last = None
    for i in range(1, tries + 1):
        try:
            return await launch(settings, log)
        except Exception as exc:                               # noqa: BLE001
            last = exc
            log(f"[브라우저] 프로필이 아직 잠겨 있습니다({type(exc).__name__}) — "
                f"{i}/{tries} 재시도")
            await asyncio.sleep(2)
    raise last


async def _shutdown(pw, ctx) -> None:
    for close in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
        if close is None:
            continue
        try:
            await close()
        except Exception:                                      # noqa: BLE001
            pass


async def confirm_login(ctx, blog_home_url: str, log) -> str:
    """**창을 띄우지 않고** 저장된 세션으로 이미 로그인돼 있는지만 확인한다(headless 용).

    로그인돼 있으면 블로그 ID, 아니면 빈 문자열. 사람을 기다리지 않는다.
    """
    home = blog_home_url or "https://blog.naver.com/MyBlog.naver"
    return await _confirm_logged_in(ctx, home, log)


async def wait_manual_logout(ctx, log, timeout_sec: int = 600,
                             blog_home_url: str = "") -> str:
    """사람이 **창을 닫을 때까지** 기다린다. 그게 전부다.

    돌려주는 값 — 언제나 `unknown`.
    ★여기서는 **아무 판정도 하지 않는다.** 로그아웃됐는지는 호출한 쪽이 창을
      닫은 뒤 **쿠키 없이 프로필을 다시 열어** 한 번 확인한다. 그것만이 근거다.

    ★2026-11-05 — 예전에는 2초마다 `_confirm_logged_in` 을 불렀는데, 그 함수는
      **페이지를 확인 주소로 이동시킨다**(`page.goto`). 사용자가 네이버에서
      로그아웃하려는 중에 화면이 계속 튀어 쓸 수가 없었다. 그래서 폴링을 뺐다.
      이제 **창을 건드리지 않는다** — 열어 두고 닫히기만 기다린다.
      (`blog_home_url` 은 더 쓰지 않는다. 호출부 호환을 위해 인자만 남겨 둔다.)

    ★타임아웃 — 사용자가 창을 닫지 않고 오래 두면 Agent 가 묶인다(작업이 직렬이다).
      그래서 상한을 두고, 넘으면 호출부가 창을 닫고 재확인으로 넘어간다.
    """
    log("[로그아웃] 브라우저 창을 띄웠습니다. **네이버 화면에서 직접 로그아웃**해 "
        "주세요.")
    log("         다 하셨으면 **창을 닫아** 주세요 — 그때 로그아웃됐는지 확인합니다.")
    log("         (기다리는 동안 화면을 건드리지 않습니다. 자유롭게 쓰셔도 됩니다.)")
    log(f"         ※ {timeout_sec // 60}분 안에 닫지 않으면 자동으로 창을 닫고 "
        f"확인합니다. 급히 멈추려면 화면 맨 위 [진행 중인 작업] 에서 중단하세요.")

    closed = asyncio.Event()
    try:
        ctx.on("close", lambda *_a: closed.set())
    except Exception:                                          # noqa: BLE001
        pass                                   # 이벤트를 못 걸면 아래 폴링으로 본다

    waited = 0.0
    while waited < timeout_sec:
        try:
            await asyncio.wait_for(closed.wait(), timeout=2.0)
            log(f"[로그아웃] 창이 닫혔습니다({waited:.0f}초) — 이제 로그아웃됐는지 "
                f"확인합니다.")
            return "unknown"
        except asyncio.TimeoutError:
            waited += 2.0
        #  ★페이지를 건드리지 않고 **열려 있는지만** 본다(이동·새로고침 없음).
        try:
            if not ctx.pages:
                log(f"[로그아웃] 창이 모두 닫혔습니다({waited:.0f}초) — 이제 "
                    f"로그아웃됐는지 확인합니다.")
                return "unknown"
        except Exception:                                      # noqa: BLE001
            log(f"[로그아웃] 창이 닫혔습니다({waited:.0f}초) — 이제 로그아웃됐는지 "
                f"확인합니다.")
            return "unknown"
        if waited and waited % 120 == 0:
            log(f"[로그아웃] 기다리는 중 {waited:.0f}/{timeout_sec}초 — "
                f"끝나면 창을 닫아 주세요.")
    log(f"[로그아웃] {timeout_sec}초 동안 창이 닫히지 않아 **자동으로 닫습니다** — "
        f"그래도 로그아웃됐는지는 확인합니다.")
    return "unknown"


async def wait_manual_login(ctx, log, timeout_sec: int = 600,
                            force: bool = False, blog_home_url: str = "") -> str:
    """사람이 직접 로그인할 때까지 기다린다. 로그인된 블로그 ID 를 돌려준다.

    · `force=True` 면 저장된 네이버 세션을 지우고 **반드시** 사람이 로그인하게 한다.
    · 2차 인증/기기 등록 화면이 뜨면 그 화면이 끝날 때까지 계속 기다린다.
    · 자동 로그인은 하지 않는다(아이디/비밀번호를 다루지 않는다).
    """
    home = blog_home_url or "https://blog.naver.com/MyBlog.naver"
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    if force:
        try:
            await ctx.clear_cookies()
            log("[로그인] --relogin — 저장된 네이버 세션을 지웠습니다.")
        except Exception as exc:                               # noqa: BLE001
            log(f"[로그인] 세션 삭제 실패({type(exc).__name__}) — 그대로 진행합니다")
    else:
        got = await _confirm_logged_in(ctx, home, log)
        if got:
            log(f"[로그인] 이미 로그인되어 있습니다 — 계정 {got} "
                f"(다른 계정으로 하려면 --relogin)")
            return got

    await page.goto(NAVER_LOGIN_URL, wait_until="domcontentloaded")
    log("[로그인] 네이버 로그인 창을 띄웠습니다. 브라우저에서 직접 로그인해 주세요.")
    log("       2차 인증·기기 등록 화면이 떠도 끝까지 진행하시면 됩니다. 계속 기다립니다.")

    waited, warned = 0, False
    while waited < timeout_sec:
        await page.wait_for_timeout(2000)
        waited += 2

        if await _on_login_host(page):
            if not warned:
                try:
                    body = await page.evaluate(
                        r"() => (document.body ? document.body.innerText : '')")
                except Exception:                              # noqa: BLE001
                    body = ""
                hit = [m for m in TWOFA_MARKS if m in body]
                if hit:
                    log(f"[로그인] 2차 인증/보안 화면 감지 {hit[:2]} — 끝내실 때까지 기다립니다.")
                    warned = True
            if waited % 30 == 0:
                log(f"[로그인] 대기 중… {waited}초")
            continue

        got = await _confirm_logged_in(ctx, home, log)
        if got:
            log(f"[로그인] 확인 완료 — 계정 {got} (대기 {waited}초)")
            return got
        if waited % 30 == 0:
            log(f"[로그인] 아직 로그인 상태가 아닙니다… {waited}초")

    raise RuntimeError(f"{timeout_sec}초 안에 로그인이 완료되지 않았습니다.")


BLOG_ID_JS = r"""() => {
     const pick = el => (el ? (el.getAttribute('href') || el.getAttribute('src')
                               || el.getAttribute('content') || '') : '');
     return {
       href: location.href,
       mainFrame: pick(document.querySelector('#mainFrame, iframe[name="mainFrame"]')),
       writeLink: pick(document.querySelector(
           "a[href*='postwrite'], a[href*='PostWriteForm'], a[href*='GoBlogWrite']")),
       og: pick(document.querySelector("meta[property='og:url']")),
       canonical: pick(document.querySelector("link[rel='canonical']")),
     };
   }"""

_BAD_IDS = {"myblog", "postlist", "postview", "bloghome", "goblogwrite",
            "prologue", "blogwrite", "section"}


def _extract_blog_id(text: str) -> str:
    """문자열에서 블로그 ID 를 뽑는다(페이지 이름은 걸러낸다)."""
    for pat in (r"blogId=([A-Za-z0-9_\-]+)",
                r"blog\.naver\.com/([A-Za-z0-9_\-]+)/(?:postwrite|\d)",
                r"blog\.naver\.com/([A-Za-z0-9_\-]+)(?:[/?#]|$)"):
        for m in re.finditer(pat, text or "", re.I):
            cand = m.group(1)
            if cand.lower() not in _BAD_IDS:
                return cand
    return ""


async def resolve_blog_id(ctx, settings, log) -> str:
    """내 블로그 ID (blog.naver.com/{id}) 를 알아낸다.

    ★blog.naver.com/MyBlog.naver 는 주소만으로는 ID 를 알 수 없다.
      실제 ID 는 #mainFrame 의 src(/PostList.naver?blogId=xxx) 에 들어 있다.
    """
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(settings.blog_home_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    try:
        marks = await page.evaluate(BLOG_ID_JS)
    except Exception as exc:                                   # noqa: BLE001
        marks = {"href": page.url or "", "err": type(exc).__name__}
    for k, v in marks.items():
        log(f"   [블로그] {k}={str(v)[:90]!r}")

    for key in ("mainFrame", "writeLink", "canonical", "og", "href"):
        got = _extract_blog_id(str(marks.get(key) or ""))
        if got:
            log(f"[블로그] 내 블로그 ID = {got} (출처: {key})")
            return got

    # 프레임 주소 / 페이지 HTML 까지 훑는다
    for fr in page.frames:
        got = _extract_blog_id(fr.url or "")
        if got:
            log(f"[블로그] 내 블로그 ID = {got} (출처: frame url)")
            return got
    got = _extract_blog_id(await page.content())
    if got:
        log(f"[블로그] 내 블로그 ID = {got} (출처: HTML)")
        return got

    raise RuntimeError(f"내 블로그 ID 를 찾지 못했습니다(url={page.url}). --blog-id 로 지정하세요.")


# ══════════════════════════════════════════════════════════════════
#  네이버 **인증 ID** 읽기 (2026-11-04 · 기본 꺼짐)
#
#  왜 필요한가
#    `login_state` 의 검증 등급 네 단계 중 `authenticated` 는 "네이버가 인증한
#    로그인 ID 를 **실제로 읽어** 기대값과 맞췄다" 는 뜻이다. 그런데 그 ID 를
#    채우는 코드가 없어서(11/03 기준) 등급이 `manual` 이상으로 올라갈 길이
#    없었다 — 4단계가 사실상 2단계였다.
#
#  왜 기본으로 꺼 두는가
#    · 선택자·주소는 **실제 네이버 화면에서 한 번 확인**해야 확정된다. 지금은
#      오프라인 테스트(가짜 page)로 '구조가 안전한가' 까지만 검증했다.
#    · 이 함수는 `--login/--check` 경로에서만 불린다(발행 경로는 부르지 않는다).
#      그래도 기존 로그인 흐름에 페이지 이동 한 번을 더하는 일이므로,
#      `BLOG_FAST_WAIT` 과 같은 규칙으로 **설정을 빠뜨리면 꺼진 쪽**이 되게 한다.
#    · 켠 상태에서 실패해도 **빈 문자열과 사유**만 돌려준다. 예외를 던지지 않고
#      등급을 올리지도 않는다 → 기존 동작과 같아진다.
#
#  ★쿠키·비밀번호·토큰을 읽지도 기록하지도 않는다. 읽는 것은 로그인 ID 뿐이다.
# ══════════════════════════════════════════════════════════════════
LOGIN_ID_ENV = "BLOG_READ_LOGIN_ID"
LOGIN_ID_ON = ("1", "on", "true", "yes", "y")

#  (이름, 주소) — 앞에서부터 시도하고 하나라도 읽으면 멈춘다.
LOGIN_ID_SOURCES = (
    ("내정보", "https://nid.naver.com/user2/help/myInfoV2?lang=ko_KR"),
    ("네이버홈", "https://www.naver.com/"),
)

#  로그인 ID 가 들어 있을 법한 자리만 **콕 집어** 읽는다(본문 전체를 훑지 않는다).
LOGIN_ID_JS = r"""() => {
     const out = [];
     const push = v => { if (v) out.push(String(v).trim()); };
     const sels = [
       '[data-login-id]', '#id', 'input[name="id"]',
       '.ident', '.txt_id', '.id_text', '.user_id', '.my_id',
       '[class*="desc_email"]', '[class*="link_login"]',
       '[class*="MyView-module__desc"]', '#account .link_login',
     ];
     for (const s of sels) {
       for (const el of document.querySelectorAll(s)) {
         push(el.getAttribute && el.getAttribute('data-login-id'));
         push(el.value);
         push(el.getAttribute && el.getAttribute('title'));
         push(el.getAttribute && el.getAttribute('aria-label'));
         push(el.textContent);
       }
     }
     return {href: location.href, cands: out.slice(0, 40)};
   }"""

#  화면 단어가 ID 로 잡히는 일을 막는다(값 검증과 함께 쓴다).
_BAD_LOGIN_WORDS = {
    "id", "아이디", "로그인", "logout", "login", "naver", "네이버", "내정보",
    "myinfo", "me", "mail", "메일", "회원정보", "undefined", "null", "none",
}
_LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{2,39}(@naver\.com)?$", re.I)


def read_login_id_enabled(env: dict | None = None) -> bool:
    """`BLOG_READ_LOGIN_ID` 가 **명시적으로 켜져** 있을 때만 True."""
    import os

    src = os.environ if env is None else env
    return (str(src.get(LOGIN_ID_ENV) or "").strip().lower() in LOGIN_ID_ON)


def _extract_login_id(cands) -> str:
    """후보 문자열들 중 **로그인 ID 로 쓸 수 있는 모양**인 첫 값. 없으면 빈 문자열."""
    for raw in list(cands or []):
        for piece in re.split(r"[\s,;|/()\[\]]+", str(raw or "")):
            v = piece.strip().strip(".:·-")
            if not v or v.casefold() in _BAD_LOGIN_WORDS:
                continue
            if _LOGIN_ID_RE.match(v):
                return v
    return ""


async def read_login_id(ctx, log=None) -> tuple[str, str]:
    """네이버가 인증한 로그인 ID 를 읽는다 → `(id, 사유)`.

    · 꺼져 있으면 `("", "꺼짐…")` — 아무 페이지도 열지 않는다.
    · 못 읽으면 `("", 사유)`. **예외를 던지지 않는다.**
    · 읽으면 `(id, 출처)`.
    """
    say = log or (lambda *_a, **_k: None)
    if not read_login_id_enabled():
        return "", f"꺼짐({LOGIN_ID_ENV} 미설정) — 인증 ID 를 읽지 않습니다"

    tried: list[str] = []
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    except Exception as exc:                                   # noqa: BLE001
        return "", f"페이지를 열지 못했습니다({type(exc).__name__})"

    for name, url in LOGIN_ID_SOURCES:
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            marks = await page.evaluate(LOGIN_ID_JS)
            got = _extract_login_id((marks or {}).get("cands"))
            if got:
                say(f"[검증] 네이버 인증 ID = {got} (출처: {name})")
                return got, f"출처 {name}"
            tried.append(f"{name}: 후보 {len((marks or {}).get('cands') or [])}개 중 없음")
        except Exception as exc:                               # noqa: BLE001
            tried.append(f"{name}: {type(exc).__name__}")
    return "", "읽지 못했습니다 — " + " · ".join(tried or ["시도한 출처 없음"])


# ── frame 탐색 ────────────────────────────────────────────────────────
#   ★수정화면(PostUpdateForm)의 실제 에디터는 about:blank 중첩 iframe 에 있다.
#     frame.url 로 거르면 절대 못 찾는다 → 모든 frame 을 훑어 '점수'가 가장 높은 것을 쓴다.
SCORE_JS = r"""() => {
     const comps = document.querySelectorAll('.se-component').length;
     const ph = document.querySelectorAll("[class*='se-placeholder']").length;
     const title = document.querySelectorAll('.se-documentTitle').length;
     const main = document.querySelectorAll('.se-main-container, .se-content').length;
     return {comps, ph, title, main,
             score: comps * 10 + ph * 5 + title * 3 + main};
   }"""


async def find_editor_frame(page, log, label: str, timeout_sec: int = 30,
                            min_score: int = 10):
    """`.se-component` 가 가장 많은 frame 을 에디터로 본다. 못 찾으면 진단 후 예외."""
    import time

    deadline = time.time() + timeout_sec
    best = None
    while time.time() < deadline:
        best = None
        for fr in [page.main_frame] + list(page.frames):
            try:
                if fr.is_detached():
                    continue
                info = await fr.evaluate(SCORE_JS)
            except Exception:                                  # noqa: BLE001
                continue
            if best is None or info["score"] > best[1]["score"]:
                best = (fr, info)
        if best and best[1]["score"] >= min_score:
            fr, info = best
            log(f"[{label}] 에디터 frame 확보 — url={(fr.url or 'about:blank')[:50]} "
                f"컴포넌트 {info['comps']}개 · placeholder {info['ph']}개")
            return fr
        await page.wait_for_timeout(700)

    await dump_frames(page, log, label)
    raise RuntimeError(f"[{label}] 에디터 frame 을 찾지 못했습니다 "
                       f"(최고점 {best[1] if best else None})")


async def dump_frames(page, log, label: str) -> None:
    """frame 별 DOM 현황을 전부 남긴다(선택자 추측 금지용)."""
    log(f"[{label}] ── frame 진단 ──  page.url={(page.url or '')[:90]}")
    for i, fr in enumerate([page.main_frame] + list(page.frames)):
        try:
            r = await fr.evaluate(SCORE_JS)
            body = await fr.evaluate(
                r"() => (document.body ? document.body.innerText : '')"
                r".replace(/\s+/g,' ').trim().slice(0,80)")
        except Exception as exc:                               # noqa: BLE001
            log(f"   frame#{i} {(fr.url or '')[:60]} → 평가 실패 {type(exc).__name__}")
            continue
        log(f"   frame#{i} {(fr.url or 'about:blank')[:60]} {r}")
        if body:
            log(f"            text={body!r}")


async def fresh(page, frame=None, log=None, label: str = ""):
    """detached 된 frame 을 다시 잡는다(단계마다 호출)."""
    try:
        if frame is not None and not frame.is_detached():
            return frame
    except Exception:                                          # noqa: BLE001
        pass
    return await find_editor_frame(page, log or (lambda *_: None), label or "frame")
