"""Chromium 실행 · 수동 로그인 대기 · 에디터 frame 탐색 (공통 저수준 헬퍼)."""
from __future__ import annotations

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

                path = session_store.save_state(account, cookies, blog_id=logged or "")
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


async def _persist_session(ctx, account: str, blog_id: str, log) -> int:
    """지금 컨텍스트의 쿠키를 계정 폴더에 저장한다(창을 닫기 **전에** 불러야 한다)."""
    if not account:
        return 0
    from . import session_store

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
