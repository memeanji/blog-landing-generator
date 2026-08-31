from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


LogFn = Callable[[str], None]
WaitFn = Callable[[str], None]


@dataclass(frozen=True)
class EditorOpenResult:
    page_url: str
    title_area_found: bool
    body_area_found: bool


@dataclass(frozen=True)
class EditorFillResult:
    page_url: str
    title: str
    body: str
    title_filled: bool
    body_filled: bool


class LoginNotCompleteError(RuntimeError):
    pass


# ★ 이 자동화는 어떤 경우에도 아래 동작을 하지 않는다.
#   발행/저장/예약 버튼은 '찾지도 누르지도' 않으며, 기존 글 수정 화면으로도 들어가지 않는다.
#   (초안 입력까지만 하고 사람이 직접 확인·발행한다.)
FORBIDDEN_ACTIONS = ("발행", "저장", "예약", "publish", "save")


class BrowserAutomation:
    def __init__(
        self,
        enabled: bool,
        headless: bool,
        user_data_dir: Path,
        naver_blog_home_url: str,
        log: LogFn,
    ) -> None:
        self.enabled = enabled
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.naver_blog_home_url = naver_blog_home_url
        self.log = log

    def open_editor_from_my_blog(self, wait_for_continue: WaitFn) -> EditorOpenResult:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 Playwright 실제 실행이 가능합니다.")
        return asyncio.run(self._open_editor_from_my_blog(wait_for_continue))

    async def _open_editor_from_my_blog(self, wait_for_continue: WaitFn) -> EditorOpenResult:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"Persistent profile 경로: {self.user_data_dir}")

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                self.log("네이버 로그인 페이지를 엽니다. 로그인/보안 인증을 직접 완료하세요.")
                await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")

                await self._wait_until_logged_in(page, wait_for_continue)

                while True:
                    self.log("로그인된 계정의 내 블로그 메인으로 이동합니다.")
                    await page.goto(self.naver_blog_home_url, wait_until="domcontentloaded")
                    await self._settle(page, lambda: self._write_button_exists(page), 8000)

                    if self._is_login_url(page.url) or not await self._is_logged_in(page):
                        self.log("로그인이 아직 완료되지 않았습니다. 네이버 로그인/보안 인증을 완료한 뒤 다시 [계속]을 누르세요.")
                        await self._wait_until_logged_in(page, wait_for_continue)
                        continue
                    break

                self.log("내 블로그에서 글쓰기 버튼을 찾습니다.")
                await self._click_write_button(page)
                await self._settle(
                    page,
                    lambda: self._editor_appeared(page),
                    8000,
                )

                active_page = page.context.pages[-1] if page.context.pages else page
                try:
                    await active_page.bring_to_front()
                except Exception:
                    pass

                if self._is_login_url(active_page.url):
                    raise LoginNotCompleteError("글쓰기 버튼 클릭 후 로그인 페이지로 이동했습니다.")

                title_found, body_found = await self._detect_editor(active_page)
                if not title_found or not body_found:
                    raise RuntimeError(
                        f"새 글 작성 에디터 확인 실패: 제목 영역={title_found}, 본문 영역={body_found}, URL={active_page.url}"
                    )

                self.log("새 글 작성 에디터 진입 확인 완료. 제목/본문 영역이 감지되었습니다.")
                return EditorOpenResult(
                    page_url=active_page.url,
                    title_area_found=title_found,
                    body_area_found=body_found,
                )
            finally:
                await context.close()


    async def _settle(self, page, check, timeout_ms: int = 6000, step_ms: int = 200) -> bool:
        """조건이 참이 될 때까지만 기다린다(고정 sleep 대신).

        check 는 async 함수. 참이 되는 즉시 반환하므로 평소엔 수백 ms 로 끝난다.
        """
        waited = 0
        while waited < timeout_ms:
            try:
                if await check():
                    return True
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(step_ms)
            waited += step_ms
        return False

    async def _wait_until_logged_in(self, page, wait_for_continue: WaitFn) -> None:
        # 프로필에 세션이 남아 있으면 굳이 [계속]을 묻지 않는다.
        # (두 번째 실행부터는 사람이 개입할 필요가 없어진다.)
        if await self._is_logged_in(page):
            self.log("이미 로그인된 세션을 찾았습니다. 로그인 단계를 건너뜁니다.")
            return
        while True:
            wait_for_continue("로그인/보안 인증 완료 후 GUI의 [계속] 버튼을 누르세요.")
            await page.wait_for_timeout(1000)

            if self._is_login_url(page.url):
                self.log("로그인이 아직 완료되지 않았습니다.")
                continue

            if await self._is_logged_in(page):
                self.log("로그인 상태 확인 완료.")
                return

            self.log("로그인이 아직 완료되지 않았습니다. 현재 페이지에서 로그인 사용자 영역 또는 세션 쿠키를 확인하지 못했습니다.")

    async def _is_logged_in(self, page) -> bool:
        if self._is_login_url(page.url):
            return False

        cookies = await page.context.cookies()
        if any(cookie.get("name") in {"NID_AUT", "NID_SES"} and cookie.get("value") for cookie in cookies):
            return True

        selectors = [
            "a[href*='nid.naver.com/nidlogin.logout']",
            "button:has-text('로그아웃')",
            "a:has-text('로그아웃')",
            "a[href*='MyBlog.naver']",
            "a[href*='blog.naver.com/MyBlog']",
            ".MyView-module__my_info",
            "#account",
        ]
        for selector in selectors:
            try:
                if await page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                pass
        return False

    def _is_login_url(self, url: str) -> bool:
        normalized = url.casefold()
        return "nid.naver.com" in normalized and "nidlogin" in normalized


    # 글쓰기 버튼 후보 — 존재 확인과 클릭이 같은 목록을 쓰도록 상수로 뺀다.
    WRITE_BUTTON_SELECTORS = [
        "a:has-text('글쓰기')",
        "button:has-text('글쓰기')",
        "a[title*='글쓰기']",
        "button[title*='글쓰기']",
        "a[href*='PostWrite']",
        "a[href*='postwrite']",
    ]

    async def _write_button_exists(self, page) -> bool:
        """글쓰기 버튼이 화면에 나타났는지(페이지 렌더 완료 판정용)."""
        for scope in [page] + list(page.frames):
            for sel in self.WRITE_BUTTON_SELECTORS:
                try:
                    if await scope.locator(sel).first.count() > 0:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    async def _click_write_button(self, page) -> None:
        candidates = self.WRITE_BUTTON_SELECTORS

        for selector in candidates:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                before_pages = len(page.context.pages)
                await locator.click(timeout=3000)
                await page.wait_for_timeout(1500)
                if len(page.context.pages) > before_pages:
                    popup = page.context.pages[-1]
                    await popup.wait_for_load_state("domcontentloaded")
                return
            except Exception:
                continue

        for frame in page.frames:
            for selector in candidates:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    await locator.click(timeout=3000)
                    return
                except Exception:
                    continue

        raise RuntimeError("내 블로그 메인에서 글쓰기 버튼을 찾지 못했습니다.")

    async def _detect_editor(self, page) -> tuple[bool, bool]:
        title_selectors = [
            "textarea[placeholder*='제목']",
            "textarea[title*='제목']",
            "input[placeholder*='제목']",
            ".se-title-text",
            "[contenteditable='true'][data-placeholder*='제목']",
        ]
        body_selectors = [
            ".se-main-container [contenteditable='true']",
            ".se-component-content [contenteditable='true']",
            "[contenteditable='true']",
            "textarea[placeholder*='내용']",
        ]

        title_found = await self._exists_in_page_or_frames(page, title_selectors)
        body_found = await self._exists_in_page_or_frames(page, body_selectors)
        return title_found, body_found

    async def _exists_in_page_or_frames(self, page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                if await page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                pass

        for frame in page.frames:
            for selector in selectors:
                try:
                    if await frame.locator(selector).first.count() > 0:
                        return True
                except Exception:
                    pass
        return False

    # ══════════════════════════════════════════════════════════════════
    # 초안 작성(제목/본문 입력까지). 발행·저장은 절대 하지 않는다.
    # ══════════════════════════════════════════════════════════════════
    def write_draft(self, title: str, body: str, wait_for_continue: WaitFn) -> EditorFillResult:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 Playwright 실제 실행이 가능합니다.")
        return asyncio.run(self._write_draft(title, body, wait_for_continue))

    async def _write_draft(self, title: str, body: str, wait_for_continue: WaitFn) -> EditorFillResult:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"Persistent profile 경로: {self.user_data_dir}")

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                # 제목/본문을 한 번에 붙여넣기 위해 클립보드 권한이 필요하다.
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            step = "브라우저 시작"
            try:
                step = "네이버 로그인"
                self.log("[1/6] 네이버 로그인 페이지를 엽니다. 로그인/2차 인증을 직접 완료하세요.")
                await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
                await self._wait_until_logged_in(page, wait_for_continue)

                step = "내 블로그 이동"
                while True:
                    self.log("[2/6] 내 블로그 메인으로 이동합니다.")
                    await page.goto(self.naver_blog_home_url, wait_until="domcontentloaded")
                    await self._settle(page, lambda: self._write_button_exists(page), 8000)
                    if self._is_login_url(page.url) or not await self._is_logged_in(page):
                        self.log("로그인이 아직 완료되지 않았습니다. 완료 후 다시 [계속]을 누르세요.")
                        await self._wait_until_logged_in(page, wait_for_continue)
                        continue
                    break

                step = "글쓰기 진입"
                self.log("[3/6] 글쓰기 버튼을 찾습니다.")
                await self._click_write_button(page)
                await self._settle(
                    page,
                    lambda: self._editor_appeared(page),
                    8000,
                )

                editor = page.context.pages[-1] if page.context.pages else page
                try:
                    await editor.bring_to_front()
                except Exception:  # noqa: BLE001
                    pass
                if self._is_login_url(editor.url):
                    raise LoginNotCompleteError("글쓰기 클릭 후 로그인 페이지로 이동했습니다.")

                step = "에디터 초기화 대기"
                await self._handle_write_popup(editor, restore=False)   # 새 글로 시작
                title_found, body_found = await self._wait_editor_ready(editor)
                if not (title_found and body_found):
                    raise RuntimeError(
                        f"에디터 입력 영역을 찾지 못했습니다: 제목={title_found}, 본문={body_found}, URL={editor.url}"
                    )
                self.log(f"[4/6] 에디터 준비 완료: {editor.url}")

                step = "제목 입력"
                frame0 = await self._main_frame(editor)
                spots0 = await self._editor_spots(frame0)      # 제목 입력 전에 한 번만 잰다
                self.log(f"   미리 잰 좌표: 제목={bool(spots0.get('title'))} "
                         f"본문={bool(spots0.get('body'))}")
                ok_title = await self._type_title(editor, title, spots0.get("title"))
                self.log(f"[5/6] 제목 입력 {'성공' if ok_title else '실패'}")

                step = "본문 입력"
                ok_body = await self._type_body(editor, body, spots0.get("body"))
                self.log(f"[6/6] 본문 입력 {'성공' if ok_body else '실패'}")

                # 결과 확인용 스크린샷(사람이 눈으로 검증할 수 있게)
                try:
                    shot = self.user_data_dir.parent / "out"
                    shot.mkdir(parents=True, exist_ok=True)
                    path = shot / "write_result.png"
                    await editor.screenshot(path=str(path))
                    self.log(f"결과 스크린샷: {path}")
                except Exception:  # noqa: BLE001
                    pass

                self.log("발행/저장/예약 버튼은 누르지 않았습니다. 초안 상태로 둡니다.")
                await editor.wait_for_timeout(1500)
                return EditorFillResult(
                    page_url=editor.url,
                    title=title,
                    body=body,
                    title_filled=ok_title,
                    body_filled=ok_body,
                )
            except Exception as exc:
                self.log(f"[실패] 단계='{step}' 사유={type(exc).__name__}: {exc}")
                raise
            finally:
                await context.close()
                self.log("브라우저를 정상 종료했습니다.")

    async def _dismiss_restore_popup(self, page) -> None:
        """'작성 중인 글이 있습니다' 팝업이 뜨면 새로 작성으로 넘긴다(기존 글 수정 방지)."""
        for sel in ("button:has-text('취소')", "button:has-text('새로 작성')",
                    ".se-popup-button-cancel", "button.se-popup-button-cancel"):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    self.log("이전 작성글 복구 팝업을 닫고 새 글로 시작합니다.")
                    await page.wait_for_timeout(1000)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _editor_appeared(self, page) -> bool:
        """글쓰기 클릭 뒤 에디터가 떴는지(제목 또는 본문 영역 등장) 빠르게 판정."""
        target = page.context.pages[-1] if page.context.pages else page
        t, b = await self._detect_editor(target)
        return t or b

    async def _wait_editor_ready(self, page, timeout_ms: int = 30_000) -> tuple[bool, bool]:
        """에디터 초기화가 끝날 때까지 폴링. 고정 selector 하나에 의존하지 않는다."""
        waited = 0
        title_found = body_found = False
        while waited < timeout_ms:
            title_found, body_found = await self._detect_editor(page)
            if title_found and body_found:
                return True, True
            await page.wait_for_timeout(300)
            waited += 300
        return title_found, body_found

    async def _target(self, page, selectors: list[str]):
        """page → 각 frame 순으로 훑어 보이는 첫 요소를 돌려준다(없으면 None)."""
        scopes = [page] + list(page.frames)
        for scope in scopes:
            for sel in selectors:
                try:
                    loc = scope.locator(sel).first
                    if await loc.count() > 0:
                        return loc
                except Exception:  # noqa: BLE001
                    continue
        return None

    # ── 입력 공통 ────────────────────────────────────────────────────
    @staticmethod
    def _norm(text: str) -> str:
        return "".join((text or "").split())

    async def _read_text(self, loc) -> str:
        try:
            return await loc.evaluate(
                "el => ('value' in el && el.value !== undefined && el.tagName !== 'DIV')"
                " ? (el.value || '') : (el.innerText || el.textContent || '')"
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _verify_text(self, loc, expected: str) -> bool:
        """정말 들어갔는지 다시 읽어 확인. 앞 30자가 보이면 성공으로 본다."""
        got = self._norm(await self._read_text(loc))
        want = self._norm(expected)[:30]
        return bool(want) and want in got

    async def _focus_target(self, page, loc) -> bool:
        """클릭 가능한 위치로 스크롤한 뒤 포커스. 클릭이 막히면 JS focus() 로 폴백."""
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await loc.click(timeout=2000)          # 실패가 확실한데 오래 붙잡지 않는다
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"   클릭 실패 → JS focus 로 전환: {type(exc).__name__}")
        try:
            await loc.evaluate(
                "el => { el.scrollIntoView({block:'center'}); (el.focus ? el.focus() : null); }"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"   focus 실패: {exc}")
            return False

    async def _paste_text(self, page, loc, text: str) -> bool:
        """포커스된 요소에 text 를 **한 번에** 넣고, 실제로 들어갔는지 확인한다."""
        # ① 클립보드 → Ctrl+V (문서가 포커스돼 있어야 writeText 가 허용된다)
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.evaluate("t => navigator.clipboard.writeText(t)", text)
            await page.keyboard.press("Control+V")
            await page.wait_for_timeout(1200)
            if await self._verify_text(loc, text):
                self.log("   입력 경로: 클립보드 붙여넣기")
                return True
            self.log("   클립보드 붙여넣기 후 내용이 확인되지 않음 → insertText 시도")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   클립보드 붙여넣기 실패 → insertText 로 전환: {type(exc).__name__}")

        # ② execCommand('insertText') — contenteditable 에서도 입력 이벤트가 정상 발생
        try:
            await loc.evaluate(
                """(el, t) => {
                     el.focus();
                     const d = el.ownerDocument;
                     const sel = d.getSelection();
                     if (sel && el.isContentEditable) {
                       const r = d.createRange();
                       r.selectNodeContents(el);
                       r.collapse(false);
                       sel.removeAllRanges();
                       sel.addRange(r);
                     }
                     d.execCommand('insertText', false, t);
                   }""",
                text,
            )
            await page.wait_for_timeout(800)
            if await self._verify_text(loc, text):
                self.log("   입력 경로: execCommand insertText")
                return True
            self.log("   insertText 후에도 내용이 확인되지 않음")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   insertText 실패: {exc}")

        # ※ value/textContent 직접 대입은 하지 않는다.
        #    SmartEditor 는 자체 모델로 렌더링해서 DOM 만 바꾸면 화면에 안 나오고,
        #    예전엔 이 경로가 무조건 True 를 돌려줘 '거짓 성공' 로그가 찍혔다.
        return False

    async def _dump_editable_candidates(self, page) -> None:
        """입력 실패 시 에디터 구조를 로그로 남긴다(다음 조정의 근거)."""
        self.log("   [진단] contenteditable / textarea 후보를 훑습니다.")
        scopes = [("page", page)] + [
            (f"frame[{(f.name or '?')[:20]}]{(f.url or '')[:50]}", f)
            for f in page.frames if f != page.main_frame
        ]
        for label, scope in scopes:
            try:
                items = await scope.evaluate(
                    r"""
                    () => Array.from(document.querySelectorAll(
                             "[contenteditable='true'],textarea,input[type='text']"))
                      .slice(0, 12).map(el => {
                        const r = el.getBoundingClientRect();
                        return {
                          tag: el.tagName.toLowerCase(),
                          cls: (el.className || '').toString().slice(0, 70),
                          ph: el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
                          editable: !!el.isContentEditable,
                          w: Math.round(r.width), h: Math.round(r.height),
                          text: (el.innerText || el.value || '').replace(/\s+/g,' ').trim().slice(0, 40)
                        };
                      })
                    """
                )
            except Exception:  # noqa: BLE001
                continue
            if not items:
                continue
            self.log(f"   [진단:{label}] {len(items)}개")
            for it in items:
                self.log(
                    f"      <{it['tag']}> ce={it['editable']} {it['w']}x{it['h']} "
                    f"cls={it['cls']!r} ph={it['ph']!r} text={it['text']!r}"
                )


    async def _dump_filled_elements(self, page) -> None:
        """텍스트가 들어있는 편집 요소를 CSS 경로와 함께 출력 — selector 확정용."""
        for f in [page.main_frame] + [x for x in page.frames if x != page.main_frame]:
            label = f"frame[{(f.name or '?')[:20]}]"
            try:
                items = await f.evaluate(
                    r"""
                    () => {
                      const path = (el) => {
                        const out = [];
                        while (el && el.nodeType === 1 && out.length < 5) {
                          let seg = el.tagName.toLowerCase();
                          if (el.id) { seg += '#' + el.id; out.unshift(seg); break; }
                          const c = (el.className || '').toString().trim().split(/\s+/)
                                     .filter(Boolean).slice(0, 3).join('.');
                          if (c) seg += '.' + c;
                          out.unshift(seg);
                          el = el.parentElement;
                        }
                        return out.join(' > ');
                      };
                      return Array.from(document.querySelectorAll(
                          "[contenteditable='true'],textarea,input[type='text']"))
                        .map(el => ({
                          text: (el.innerText || el.value || '').replace(/\s+/g,' ').trim(),
                          ph: el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
                          sel: path(el)
                        }))
                        .filter(x => x.text)
                        .slice(0, 10);
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                continue
            if not items:
                continue
            self.log(f"   [내용 있음:{label}]")
            for it in items:
                self.log(f"      selector : {it['sel']}")
                self.log(f"      placeholder={it['ph']!r}")
                self.log(f"      text     : {it['text'][:70]!r}")
                self.log("")


    async def _find_text_location(self, page, needles: list) -> None:
        """입력한 문자열이 DOM(shadow 포함) 어디에 있는지 찾아 CSS 경로를 찍는다."""
        for f in [page.main_frame] + [x for x in page.frames if x != page.main_frame]:
            label = f"frame[{(f.name or '?')[:20]}]"
            try:
                found = await f.evaluate(
                    r"""
                    (needles) => {
                      const path = (el) => {
                        const out = [];
                        while (el && el.nodeType === 1 && out.length < 6) {
                          let seg = el.tagName.toLowerCase();
                          if (el.id) { seg += '#' + el.id; out.unshift(seg); break; }
                          const c = (el.className || '').toString().trim().split(/\s+/)
                                     .filter(Boolean).slice(0, 3).join('.');
                          if (c) seg += '.' + c;
                          out.unshift(seg);
                          el = el.parentElement || (el.getRootNode() || {}).host;
                        }
                        return out.join(' > ');
                      };
                      // shadow root 까지 관통하며 전체 요소 수집
                      const all = [];
                      const walk = (root, depth) => {
                        if (!root || depth > 8) return;
                        root.querySelectorAll('*').forEach(el => {
                          all.push(el);
                          if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
                        });
                      };
                      walk(document, 0);

                      const hits = [];
                      for (const n of needles) {
                        if (!n) continue;
                        for (const el of all) {
                          const t = (el.innerText || el.value || el.textContent || '');
                          if (!t || t.indexOf(n) < 0) continue;
                          // 가장 안쪽(자식이 같은 문자열을 갖지 않는) 요소만
                          const inner = Array.from(el.children).some(
                            c => ((c.innerText || c.textContent || '')).indexOf(n) >= 0);
                          if (inner) continue;
                          hits.push({
                            needle: n,
                            sel: path(el),
                            tag: el.tagName.toLowerCase(),
                            ce: !!el.isContentEditable,
                            shadow: el.getRootNode() !== document,
                            ph: el.getAttribute && (el.getAttribute('data-placeholder')
                                 || el.getAttribute('placeholder') || ''),
                            text: t.replace(/\s+/g, ' ').trim().slice(0, 60)
                          });
                          break;
                        }
                      }
                      return { total: all.length, hits };
                    }
                    """,
                    needles,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"   [{label}] 탐색 실패: {type(exc).__name__}")
                continue
            self.log(f"   [{label}] 요소 {found.get('total', 0)}개(shadow 포함) 탐색")
            for h in found.get("hits", []):
                self.log(f"      ▶ '{h['needle']}' 발견")
                self.log(f"        selector    : {h['sel']}")
                self.log(f"        tag={h['tag']} contenteditable={h['ce']} shadowDOM={h['shadow']}")
                self.log(f"        placeholder : {h['ph']!r}")
                self.log(f"        text        : {h['text']!r}")
                self.log("")

    async def _dump_shadow_editables(self, page) -> None:
        """shadow root 까지 포함해 편집 가능한 요소를 훑는다."""
        for f in [page.main_frame] + [x for x in page.frames if x != page.main_frame]:
            label = f"frame[{(f.name or '?')[:20]}]"
            try:
                items = await f.evaluate(
                    r"""
                    () => {
                      const all = [];
                      const walk = (root, depth) => {
                        if (!root || depth > 8) return;
                        root.querySelectorAll('*').forEach(el => {
                          all.push(el);
                          if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
                        });
                      };
                      walk(document, 0);
                      return all
                        .filter(el => el.isContentEditable
                                   || el.tagName === 'TEXTAREA'
                                   || (el.tagName === 'INPUT' && /text|search/i.test(el.type || '')))
                        .slice(0, 15)
                        .map(el => {
                          const r = el.getBoundingClientRect();
                          return {
                            tag: el.tagName.toLowerCase(),
                            cls: (el.className || '').toString().slice(0, 60),
                            ph: el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
                            shadow: el.getRootNode() !== document,
                            w: Math.round(r.width), h: Math.round(r.height),
                            text: (el.innerText || el.value || '').replace(/\s+/g,' ').trim().slice(0, 40)
                          };
                        });
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                continue
            if not items:
                continue
            self.log(f"   [shadow포함:{label}] {len(items)}개")
            for it in items:
                self.log(
                    f"      <{it['tag']}> shadow={it['shadow']} {it['w']}x{it['h']} "
                    f"cls={it['cls']!r} ph={it['ph']!r} text={it['text']!r}"
                )


    # ══════════════════════════════════════════════════════════════════
    # 입력 전략 자동 테스트 — 실제로 글자가 들어가는 방법 1개를 찾는다.
    # 저장·발행은 하지 않는다.
    # ══════════════════════════════════════════════════════════════════
    TITLE_MARK = "TITLEMARK123"
    BODY_MARK = "BODYMARK456"

    def test_input(self, wait_for_continue: WaitFn, shot_dir=None) -> dict:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 실행할 수 있습니다.")
        return asyncio.run(self._test_input(wait_for_continue, shot_dir))

    async def _main_frame(self, page):
        """에디터가 들어있는 프레임. 매 단계 직전에 새로 호출해 쓴다(저장해두지 않는다).

        ★page 자체가 이미 /postwrite 면 iframe 탐색 없이 main_frame 을 쓴다.
        """
        u = (page.url or "").lower()
        if "postwrite" in u:
            return page.main_frame
        for _ in range(30):
            for f in page.frames:
                if f.is_detached():
                    continue
                if (f.name or "") == "mainFrame" or "postwrite" in (f.url or "").lower():
                    return f
            await page.wait_for_timeout(300)
        return page.main_frame

    async def _fresh_frame(self, page, frame=None, retries: int = 3):
        """쓰기 직전에 프레임을 확보한다. detached 면 다시 잡는다."""
        for _ in range(max(1, retries)):
            if frame is not None:
                try:
                    if not frame.is_detached():
                        return frame
                except Exception:  # noqa: BLE001
                    pass
            frame = await self._main_frame(page)
            try:
                if not frame.is_detached():
                    return frame
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(300)
            frame = None
        return await self._main_frame(page)

    async def _frame_offset(self, page, frame) -> tuple:
        """프레임 좌표 → 페이지 좌표 오프셋.

        frame_element() 를 쓰지 않는다(detached 시 바로 터짐).
        부모 문서에서 iframe 의 rect 를 매번 새로 조회한다.
        """
        try:
            if frame == page.main_frame:
                return (0, 0)
            name = frame.name or ""
            rect = await page.main_frame.evaluate(
                r"""(nm) => {
                     const els = Array.from(document.querySelectorAll('iframe'));
                     const el = els.find(e => (e.name || '') === nm)
                             || els.find(e => (e.getAttribute('src') || '').indexOf('postwrite') >= 0)
                             || els[0];
                     if (!el) return null;
                     const r = el.getBoundingClientRect();
                     return {x: Math.round(r.x), y: Math.round(r.y)};
                   }""",
                name,
            )
            if rect:
                return (rect["x"], rect["y"])
        except Exception:  # noqa: BLE001
            pass
        return (0, 0)

    # 편집 후보에서 무조건 빼는 것들 — 검색창/태그입력/글감/숨은 프록시
    EXCLUDE_HINTS = ("search", "flayer", "tag_input", "fake_input", "unified-search")
    EXCLUDE_PLACEHOLDERS = ("글감", "검색", "태그")

    async def _visible_editables(self, frame) -> list:
        """프레임 안의 '진짜' 편집 후보만. 검색/태그/글감/0x0 은 제외."""
        return await frame.evaluate(
            r"""
            (cfg) => {
              const badCls = cfg.cls, badPh = cfg.ph;
              const els = Array.from(document.querySelectorAll(
                  "[contenteditable='true'],textarea,input[type='text'],input:not([type])"));
              const out = [];
              els.forEach((el, i) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return;
                if (r.width < 5 || r.height < 5) return;              // 0x0 숨은 프록시 제외
                const cls = (el.className || '').toString().toLowerCase();
                const ph  = (el.getAttribute('data-placeholder')
                             || el.getAttribute('placeholder') || '');
                if (badCls.some(b => cls.indexOf(b) >= 0)) return;    // 검색/태그/프록시 제외
                if (badPh.some(b => ph.indexOf(b) >= 0)) return;
                if (el.tagName === 'INPUT' && /search/i.test(el.type || '')) return;
                if (el.closest("[class*='search'],[class*='flayer']")) return;
                el.setAttribute('data-blg-cand', String(i));
                out.push({
                  i, tag: el.tagName.toLowerCase(), cls: (el.className||'').toString().slice(0,50),
                  ph, w: Math.round(r.width), h: Math.round(r.height),
                  x: Math.round(r.x), y: Math.round(r.y),
                  ce: !!el.isContentEditable
                });
              });
              return out.sort((a, b) => (b.w * b.h) - (a.w * a.h));
            }
            """,
            {"cls": list(self.EXCLUDE_HINTS), "ph": list(self.EXCLUDE_PLACEHOLDERS)},
        )

    async def _frame_has(self, frame, marker: str) -> bool:
        """마커가 **실제 편집 영역에 시각적으로** 나타났는지.

        ★input/textarea 의 value 는 성공으로 치지 않는다.
          (글감 검색창에 들어간 걸 본문 입력 성공으로 오판했던 원인)
        """
        try:
            return await frame.evaluate(
                r"""(cfg) => {
                     const m = cfg.marker, badCls = cfg.cls;
                     const els = Array.from(document.querySelectorAll('*'));
                     for (const el of els) {
                       if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') continue;
                       const cls = (el.className || '').toString().toLowerCase();
                       if (badCls.some(b => cls.indexOf(b) >= 0)) continue;
                       if (el.closest("[class*='search'],[class*='flayer']")) continue;
                       const t = el.innerText || '';
                       if (t.indexOf(m) < 0) continue;
                       // 자식이 같은 문자열을 가지면 더 안쪽이 진짜 → 여기선 넘어감
                       if (Array.from(el.children).some(c => (c.innerText||'').indexOf(m) >= 0)) continue;
                       const r = el.getBoundingClientRect();
                       if (r.width < 30 || r.height < 10) continue;   // 화면에 보이는 크기여야
                       return true;
                     }
                     return false;
                   }""",
                {"marker": marker, "cls": list(self.EXCLUDE_HINTS)},
            )
        except Exception:  # noqa: BLE001
            return False

    async def _active_element(self, frame) -> str:
        try:
            return await frame.evaluate(
                """() => {
                     const a = document.activeElement;
                     if (!a) return '(none)';
                     return a.tagName.toLowerCase()
                       + ' cls=' + ((a.className || '').toString().slice(0, 40))
                       + ' ce=' + (!!a.isContentEditable);
                   }"""
            )
        except Exception:  # noqa: BLE001
            return "(조회 실패)"

    async def _clear_marker(self, frame, marker: str) -> None:
        """다음 전략이 오염되지 않게 이전 마커를 지운다(있으면)."""
        try:
            await frame.evaluate(
                """(m) => {
                     document.querySelectorAll('input,textarea').forEach(el => {
                       if ((el.value || '').indexOf(m) >= 0) el.value = '';
                     });
                   }""",
                marker,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _run_strategies(self, page, frame, cand, marker, shot_dir, tag) -> list:
        """후보 하나에 대해 전략을 순서대로 시도. 성공한 전략명을 결과에 담아 반환."""
        sel = f"[data-blg-cand='{cand['i']}']"
        results = []
        loc = frame.locator(sel).first

        async def snap(name):
            if shot_dir is None:
                return
            try:
                await page.screenshot(path=str(shot_dir / f"{tag}_{name}.png"))
            except Exception:  # noqa: BLE001
                pass

        async def report(name, err=""):
            ok = await self._frame_has(frame, marker)
            active = await self._active_element(frame)
            try:
                dump = await loc.evaluate(
                    "el => ({html: (el.innerHTML || '').slice(0,80),"
                    " val: (el.value !== undefined ? String(el.value) : '').slice(0,60),"
                    " txt: (el.textContent || '').slice(0,60)})"
                )
            except Exception:  # noqa: BLE001
                dump = {"html": "", "val": "", "txt": ""}
            self.log(f"      · {name:<24} 화면반영={'✅' if ok else '❌'}"
                     + (f" ({err})" if err else ""))
            self.log(f"        activeElement={active}")
            self.log(f"        innerHTML={dump['html']!r} value={dump['val']!r} text={dump['txt']!r}")
            await snap(name)
            results.append({"strategy": name, "ok": ok})
            return ok

        # ① fill
        try:
            await loc.fill(marker, timeout=3000)
            await page.wait_for_timeout(700)
            if await report("fill"):
                return results
        except Exception as exc:  # noqa: BLE001
            await report("fill", type(exc).__name__)
        await self._clear_marker(frame, marker)

        # ② press_sequentially
        try:
            await loc.click(timeout=3000)
            await loc.press_sequentially(marker, delay=20, timeout=8000)
            await page.wait_for_timeout(700)
            if await report("press_sequentially"):
                return results
        except Exception as exc:  # noqa: BLE001
            await report("press_sequentially", type(exc).__name__)
        await self._clear_marker(frame, marker)

        # ③ click + keyboard.type (페이지 레벨 키 입력)
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
            await loc.click(timeout=3000)
            await page.keyboard.type(marker, delay=20)
            await page.wait_for_timeout(700)
            if await report("click+keyboard.type"):
                return results
        except Exception as exc:  # noqa: BLE001
            await report("click+keyboard.type", type(exc).__name__)
        await self._clear_marker(frame, marker)

        # ④ focus + execCommand insertText
        try:
            await loc.evaluate("el => el.focus()")
            await frame.evaluate("(m) => document.execCommand('insertText', false, m)", marker)
            await page.wait_for_timeout(700)
            if await report("focus+insertText"):
                return results
        except Exception as exc:  # noqa: BLE001
            await report("focus+insertText", type(exc).__name__)
        await self._clear_marker(frame, marker)

        # ⑤ click + 클립보드 붙여넣기
        try:
            await loc.click(timeout=3000)
            await page.evaluate("t => navigator.clipboard.writeText(t)", marker)
            await page.keyboard.press("Control+V")
            await page.wait_for_timeout(900)
            if await report("click+clipboard"):
                return results
        except Exception as exc:  # noqa: BLE001
            await report("click+clipboard", type(exc).__name__)
        await self._clear_marker(frame, marker)

        return results

    async def _test_input(self, wait_for_continue: WaitFn, shot_dir) -> dict:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if shot_dir is not None:
            shot_dir.mkdir(parents=True, exist_ok=True)
        winner = {"title": None, "body": None}

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                self.log("[1/4] 네이버 로그인. 완료 후 Enter 만 눌러주세요(이후는 전부 자동).")
                await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
                await self._wait_until_logged_in(page, wait_for_continue)

                self.log("[2/4] 내 블로그 → 글쓰기")
                await page.goto(self.naver_blog_home_url, wait_until="domcontentloaded")
                await self._settle(page, lambda: self._write_button_exists(page), 8000)
                await self._click_write_button(page)
                editor = context.pages[-1] if context.pages else page
                await self._settle(editor, lambda: self._editor_appeared(editor), 8000)
                await self._dismiss_restore_popup(editor)
                await editor.wait_for_timeout(1500)

                await self._handle_write_popup(editor, restore=False)   # 새 글로 시작
                frame = await self._main_frame(editor)
                self.log(f"[3/4] mainFrame 확보: name={frame.name!r} url={(frame.url or '')[:60]}")

                # ★사람이 하는 경로: 보이는 제목/본문 자리를 클릭하고 키보드로 친다.
                spots = await self._placeholder_boxes(frame)
                self.log(f"   화면에서 찾은 자리: 제목={bool(spots.get('title'))} "
                         f"본문={bool(spots.get('body'))}")
                if shot_dir is not None:
                    try:
                        await editor.screenshot(path=str(shot_dir / "00_editor_ready.png"))
                    except Exception:  # noqa: BLE001
                        pass
                # ★실제 배포 경로(_type_title/_type_body)를 그대로 시험한다.
                spots0 = await self._editor_spots(frame)       # 제목 입력 전에 한 번만 잰다
                self.log(f"   미리 잰 좌표: 제목={bool(spots0.get('title'))} "
                         f"본문={bool(spots0.get('body'))}")
                t_ok = await self._type_title(editor, self.TITLE_MARK, spots0.get("title"))
                b_ok = await self._type_body(editor, self.BODY_MARK, spots0.get("body"))
                if shot_dir is not None:
                    try:
                        await editor.screenshot(path=str(shot_dir / "01_after_click_type.png"))
                    except Exception:  # noqa: BLE001
                        pass
                if t_ok or b_ok:
                    self.log("")
                    self.log("── 확정 결과 (좌표 클릭 + keyboard.type) ──")
                    self.log(f"   title: {'성공' if t_ok else '실패'}")
                    self.log(f"   body : {'성공' if b_ok else '실패'}")
                    self.log("저장·발행은 하지 않았습니다.")
                    return {"title": {"strategy": "click_xy+keyboard.type"} if t_ok else None,
                            "body": {"strategy": "click_xy+keyboard.type"} if b_ok else None}
                self.log("   좌표 방식 실패 → 기존 후보×전략 매트릭스로 계속합니다.")

                await self._wait_editor_ready(editor)
                cands = await self._visible_editables(frame)
                self.log(f"   보이는 편집 후보 {len(cands)}개 (검색/태그/글감/0x0 제외, 면적 큰 순)")
                for c in cands:
                    self.log(f"      #{c['i']} <{c['tag']}> ce={c['ce']} {c['w']}x{c['h']} "
                             f"cls={c['cls']!r} ph={c['ph']!r}")

                self.log("")
                self.log(f"[4/4] 본문 입력 전략 테스트 (마커 {self.BODY_MARK})")
                for c in cands:
                    self.log(f"   후보 #{c['i']} <{c['tag']}> {c['w']}x{c['h']} cls={c['cls']!r}")
                    res = await self._run_strategies(
                        page, frame, c, self.BODY_MARK, shot_dir, f"body{c['i']}")
                    ok = next((r for r in res if r["ok"]), None)
                    if ok:
                        winner["body"] = {"cand": c, "strategy": ok["strategy"]}
                        self.log(f"   ✅ 본문 입력 성공: 후보 #{c['i']} · 전략 {ok['strategy']}")
                        break

                self.log("")
                self.log(f"   제목 입력 전략 테스트 (마커 {self.TITLE_MARK})")
                for c in cands:
                    if winner["body"] and c["i"] == winner["body"]["cand"]["i"]:
                        continue          # 본문으로 확정된 후보는 제외
                    self.log(f"   후보 #{c['i']} <{c['tag']}> {c['w']}x{c['h']} cls={c['cls']!r}")
                    res = await self._run_strategies(
                        page, frame, c, self.TITLE_MARK, shot_dir, f"title{c['i']}")
                    ok = next((r for r in res if r["ok"]), None)
                    if ok:
                        winner["title"] = {"cand": c, "strategy": ok["strategy"]}
                        self.log(f"   ✅ 제목 입력 성공: 후보 #{c['i']} · 전략 {ok['strategy']}")
                        break

                self.log("")
                self.log("── 확정 결과 ──")
                for key in ("title", "body"):
                    w = winner[key]
                    if w:
                        c = w["cand"]
                        self.log(f"   {key}: 전략={w['strategy']} · <{c['tag']}> "
                                 f"cls={c['cls']!r} ph={c['ph']!r} {c['w']}x{c['h']}")
                    else:
                        self.log(f"   {key}: 성공한 전략 없음")
                self.log("저장·발행은 하지 않았습니다.")
                return winner
            finally:
                await context.close()
                self.log("브라우저를 정상 종료했습니다.")


    # ══════════════════════════════════════════════════════════════════
    # 임시저장 글 역추적 — 사람이 입력한 결과에서 제목/본문 구조를 알아낸다.
    # 저장·발행은 하지 않는다.
    # ══════════════════════════════════════════════════════════════════
    def analyze_draft(self, wait_for_continue: WaitFn, shot_dir=None) -> dict:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 실행할 수 있습니다.")
        return asyncio.run(self._analyze_draft(wait_for_continue, shot_dir))

    async def _restore_saved_draft(self, page) -> bool:
        """'작성 중인 글' 팝업에서 **취소가 아닌** 쪽을 눌러 임시저장 글을 불러온다."""
        cands = [
            "button:has-text('불러오기')",
            "a:has-text('불러오기')",
            "button:has-text('이어쓰기')",
            "button:has-text('확인')",
            ".se-popup-button-confirm",
        ]
        for sel in cands:
            for scope in [page] + list(page.frames):
                try:
                    loc = scope.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2500)
                        self.log(f"   임시저장 글 불러오기 클릭: {sel}")
                        await page.wait_for_timeout(2500)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        self.log("   불러오기 버튼을 못 찾았습니다(팝업이 없거나 이미 로드됨).")
        return False

    async def _describe_text_owner(self, frame, needle: str, label: str) -> None:
        """문자열이 들어있는 요소와 조상 체인을 상세히 찍는다."""
        try:
            info = await frame.evaluate(
                r"""
                (needle) => {
                  const attrs = (el) => {
                    const o = {};
                    for (const a of el.attributes || []) {
                      if (a.name.startsWith('data-') || ['role','aria-label','contenteditable',
                          'placeholder','id','class'].includes(a.name)) {
                        o[a.name] = a.value.slice(0, 60);
                      }
                    }
                    return o;
                  };
                  const desc = (el) => {
                    const r = el.getBoundingClientRect();
                    return {
                      tag: el.tagName.toLowerCase(),
                      ce: !!el.isContentEditable,
                      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                      attrs: attrs(el)
                    };
                  };
                  const all = [];
                  const walk = (root, d) => {
                    if (!root || d > 6) return;
                    root.querySelectorAll('*').forEach(el => {
                      all.push(el);
                      if (el.shadowRoot) walk(el.shadowRoot, d + 1);
                    });
                  };
                  walk(document, 0);

                  for (const el of all) {
                    const t = (el.innerText || el.value || el.textContent || '');
                    if (!t || t.indexOf(needle) < 0) continue;
                    if (Array.from(el.children).some(
                        c => ((c.innerText || c.textContent || '')).indexOf(needle) >= 0)) continue;
                    const chain = [];
                    let cur = el;
                    for (let i = 0; i < 5 && cur; i++) {
                      chain.push(desc(cur));
                      cur = cur.parentElement;
                    }
                    return { found: true, chain, text: t.replace(/\s+/g,' ').trim().slice(0, 80) };
                  }
                  return { found: false };
                }
                """,
                needle,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [{label}] 탐색 실패: {type(exc).__name__}")
            return
        if not info.get("found"):
            self.log(f"   [{label}] '{needle[:20]}' DOM 에서 못 찾음")
            return
        self.log(f"   [{label}] 발견: {info['text']!r}")
        for depth, d in enumerate(info["chain"]):
            pad = "      " + ("  " * depth)
            self.log(f"{pad}<{d['tag']}> ce={d['ce']} box={d['box']}")
            for k, v in (d["attrs"] or {}).items():
                self.log(f"{pad}   {k}={v!r}")

    async def _click_and_report(self, page, frame, x_ratio, y_ratio, label) -> None:
        """화면 좌표를 클릭하고 activeElement 가 무엇으로 바뀌는지 본다."""
        try:
            box = await frame.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight})"
            )
            x = int(box["w"] * x_ratio)
            y = int(box["h"] * y_ratio)
            ox, oy = await self._frame_offset(page, frame)
            await page.mouse.click(ox + x, oy + y)
            await page.wait_for_timeout(600)
            active = await frame.evaluate(
                r"""() => {
                     const a = document.activeElement;
                     if (!a) return '(none)';
                     const r = a.getBoundingClientRect();
                     return a.tagName.toLowerCase()
                       + ' ce=' + (!!a.isContentEditable)
                       + ' cls=' + ((a.className||'').toString().slice(0,50))
                       + ' box=' + [Math.round(r.x),Math.round(r.y),
                                    Math.round(r.width),Math.round(r.height)].join(',');
                   }"""
            )
            self.log(f"   [{label}] 좌표({x},{y}) 클릭 → activeElement = {active}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [{label}] 좌표 클릭 실패: {type(exc).__name__}: {exc}")

    async def _analyze_draft(self, wait_for_continue: WaitFn, shot_dir) -> dict:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if shot_dir is not None:
            shot_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                self.log("[1/5] 네이버 로그인. 완료 후 Enter 만 눌러주세요(이후 자동).")
                await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
                await self._wait_until_logged_in(page, wait_for_continue)

                self.log("[2/5] 내 블로그 → 글쓰기")
                await page.goto(self.naver_blog_home_url, wait_until="domcontentloaded")
                await self._settle(page, lambda: self._write_button_exists(page), 8000)
                await self._click_write_button(page)
                editor = context.pages[-1] if context.pages else page
                await self._settle(editor, lambda: self._editor_appeared(editor), 8000)

                self.log("[3/5] 임시저장 글 불러오기(팝업 대기 후 '확인')")
                await self._handle_write_popup(editor, restore=True)
                frame = await self._main_frame(editor)
                await self._wait_editor_ready(editor)
                await editor.wait_for_timeout(2000)
                if shot_dir is not None:
                    try:
                        await editor.screenshot(path=str(shot_dir / "draft_loaded.png"))
                    except Exception:  # noqa: BLE001
                        pass

                self.log(f"[4/5] mainFrame={frame.name!r} — 화면에 보이는 긴 텍스트 블록")
                blocks = await frame.evaluate(
                    r"""
                    () => Array.from(document.querySelectorAll('*'))
                      .filter(el => {
                        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return false;
                        if (Array.from(el.children).some(c => (c.innerText||'').length > 10)) return false;
                        const t = (el.innerText || '').trim();
                        const r = el.getBoundingClientRect();
                        return t.length > 3 && r.width > 50 && r.height > 8;
                      })
                      .slice(0, 25)
                      .map(el => {
                        const r = el.getBoundingClientRect();
                        return {
                          tag: el.tagName.toLowerCase(),
                          ce: !!el.isContentEditable,
                          cls: (el.className||'').toString().slice(0,50),
                          box: [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],
                          text: (el.innerText||'').replace(/\s+/g,' ').trim().slice(0,60)
                        };
                      })
                    """
                )
                for b in blocks:
                    self.log(f"      <{b['tag']}> ce={b['ce']} box={b['box']} cls={b['cls']!r}")
                    self.log(f"         text={b['text']!r}")

                self.log("")
                self.log("[5/5] 좌표 클릭으로 제목/본문 영역 확인")
                await self._click_and_report(page, frame, 0.5, 0.18, "제목 추정 영역")
                await self._click_and_report(page, frame, 0.5, 0.45, "본문 추정 영역")

                self.log("분석만 수행했습니다. 저장·발행은 하지 않았습니다.")
                return {"blocks": blocks}
            finally:
                await context.close()
                self.log("브라우저를 정상 종료했습니다.")


    # ── '작성 중인 글이 있습니다' 팝업 ────────────────────────────────
    POPUP_MARKS = ("작성 중인 글이 있습니다", "이어서 작성하시겠습니까")

    async def _popup_visible(self, page) -> bool:
        for scope in [page] + list(page.frames):
            for mark in self.POPUP_MARKS:
                try:
                    if await scope.locator(f"text={mark}").first.count() > 0:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    async def _popup_gone(self, page, timeout_ms: int = 6000) -> bool:
        waited = 0
        while waited < timeout_ms:
            if not await self._popup_visible(page):
                return True
            await page.wait_for_timeout(250)
            waited += 250
        return False

    async def _click_popup_cancel(self, page) -> bool:
        """'취소'만 누른다. '확인'은 어떤 경우에도 누르지 않는다.

        selector 하나에 의존하지 않고 ① text ② role=button+name ③ 팝업 내부 button 순으로 시도.
        """
        for scope in [page] + list(page.frames):
            # ① text 기반
            for sel in ("button:has-text('취소')", "a:has-text('취소')"):
                try:
                    loc = scope.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        return True
                except Exception:  # noqa: BLE001
                    pass
            # ② role=button + name
            try:
                loc = scope.get_by_role("button", name="취소").first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    return True
            except Exception:  # noqa: BLE001
                pass
            # ③ 팝업 컨테이너 안의 버튼 중 '취소'
            try:
                clicked = await scope.evaluate(
                    r"""(marks) => {
                         const all = Array.from(document.querySelectorAll('*'));
                         const host = all.find(el =>
                           marks.some(m => (el.innerText || '').indexOf(m) >= 0)
                           && el.querySelectorAll('button,a,[role=button]').length
                           && (el.innerText || '').length < 400);
                         if (!host) return false;
                         const btn = Array.from(
                             host.querySelectorAll('button,a,[role=button]'))
                           .find(b => ((b.innerText || '').trim()) === '취소');
                         if (!btn) return false;
                         btn.click();
                         return true;
                       }""",
                    list(self.POPUP_MARKS),
                )
                if clicked:
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _handle_write_popup(self, page, restore: bool = False,
                                  timeout_ms: int = 5_000) -> bool:
        """'작성 중인 글이 있습니다' 팝업을 최대 5초 폴링해 '취소'로 닫는다.
        (2026-08-20 사용자 요청으로 10초 → 5초. 0초 판정은 여전히 금지 — 팝업이 화면을 덮은 채
         진행되면 전부 실패한다.)"""
        if restore:                      # 분석 모드에서만 '확인'
            return await self._handle_write_popup_restore(page, timeout_ms)

        self.log(f"   [팝업] 최대 {timeout_ms // 1000}초 대기")
        waited = 0
        while waited < timeout_ms:
            if await self._popup_visible(page):
                self.log("   [팝업] 작성중 글 감지")
                for _ in range(3):
                    if await self._click_popup_cancel(page):
                        self.log("   [팝업] '취소' 클릭")
                        if await self._popup_gone(page):
                            self.log("   [팝업] 닫힘 확인 ✅")
                            await page.wait_for_timeout(700)
                            return True
                    await page.wait_for_timeout(500)
                self.log("   [팝업] ⚠ 취소 클릭에 실패했습니다")
                return False
            await page.wait_for_timeout(300)
            waited += 300
        self.log("   [팝업] 작성중 글 없음")
        return False

    async def _handle_write_popup_restore(self, page, timeout_ms: int) -> bool:
        """분석 모드 전용 — '확인'을 눌러 임시저장 글을 불러온다."""
        waited = 0
        while waited < timeout_ms:
            if await self._popup_visible(page):
                for scope in [page] + list(page.frames):
                    for sel in ("button:has-text('확인')", "a:has-text('확인')"):
                        try:
                            loc = scope.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible():
                                await loc.click(timeout=2000)
                                self.log("   [팝업] '확인' 클릭(이어쓰기)")
                                await self._popup_gone(page)
                                await page.wait_for_timeout(1500)
                                return True
                        except Exception:  # noqa: BLE001
                            continue
            await page.wait_for_timeout(300)
            waited += 300
        self.log("   [팝업] 작성중 글 없음")
        return False

    async def _placeholder_boxes(self, frame) -> dict:
        """화면에 보이는 제목/본문 자리(placeholder)의 좌표를 찾는다."""
        return await frame.evaluate(
            r"""
            () => {
              const res = {};
              const all = Array.from(document.querySelectorAll('*'));
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 40 && r.height > 10
                       && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const leaf = (el, t) => !Array.from(el.children)
                    .some(c => ((c.innerText || '')).indexOf(t) >= 0);
              for (const el of all) {
                const t = (el.innerText || '').trim();
                if (!t || !vis(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.y < 140) continue;                       // 툴바 영역 제외
                if (!res.title && t === '제목' && leaf(el, '제목')) {
                  res.title = {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                               box:[Math.round(r.x),Math.round(r.y),
                                    Math.round(r.width),Math.round(r.height)],
                               cls:(el.className||'').toString().slice(0,50),
                               tag: el.tagName.toLowerCase()};
                }
                if (!res.body && /나를 돌아보|내용을 입력|본문/.test(t) && leaf(el, t.slice(0,4))) {
                  res.body = {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                              box:[Math.round(r.x),Math.round(r.y),
                                   Math.round(r.width),Math.round(r.height)],
                              cls:(el.className||'').toString().slice(0,50),
                              tag: el.tagName.toLowerCase(), text: t.slice(0,30)};
                }
              }
              return res;
            }
            """
        )

    async def _click_xy_in_frame(self, page, frame, x: int, y: int) -> None:
        """프레임 내부 좌표를 페이지 좌표로 바꿔 클릭(frame_element 사용 안 함)."""
        frame = await self._fresh_frame(page, frame)
        ox, oy = await self._frame_offset(page, frame)
        await page.mouse.click(ox + x, oy + y)
        await page.wait_for_timeout(500)

    async def _type_at(self, page, frame, spot: dict, text: str, label: str) -> bool:
        """보이는 영역을 클릭하고 키보드로 친다 — 사람이 하는 것과 같은 경로."""
        if not spot:
            self.log(f"   [{label}] 위치를 못 찾음")
            return False
        self.log(f"   [{label}] <{spot['tag']}> box={spot['box']} cls={spot['cls']!r} "
                 f"→ ({spot['x']},{spot['y']}) 클릭")
        await self._click_xy_in_frame(page, frame, spot["x"], spot["y"])
        active = await self._active_element(frame)
        self.log(f"      클릭 후 activeElement = {active}")
        await page.keyboard.type(text, delay=10)
        await page.wait_for_timeout(800)
        ok = await self._frame_has(frame, text)
        self.log(f"      화면반영 = {'✅' if ok else '❌'}")
        return ok

    # ══════════════════════════════════════════════════════════════════
    # 제목/본문 입력 — 실측으로 확정된 경로
    #   ① placeholder 의 bounding box 로 '보이는 자리' 계산(좌표 하드코딩 없음)
    #   ② 그 자리를 mouse.click  ③ 서식 초기화  ④ page.keyboard.type
    # locator.fill / insertText 는 이 에디터에서 화면에 안 나오므로 fallback 으로만 둔다.
    # ══════════════════════════════════════════════════════════════════
    FORMAT_LABELS = ("굵게", "기울임", "밑줄", "취소선", "bold", "italic", "underline", "strike")


    BODY_PROBE = "BODYPROBE123"

    async def _log_state(self, page, frame, label: str) -> None:
        """두 경로에서 같은 형식으로 상태를 남긴다(차이 비교용)."""
        try:
            phs = await frame.evaluate(
                r"""() => Array.from(document.querySelectorAll("[class*='se-placeholder']"))
                     .filter(el => {
                       const r = el.getBoundingClientRect();
                       const st = getComputedStyle(el);
                       return r.width > 5 && r.height > 5
                              && st.display !== 'none' && st.visibility !== 'hidden';
                     })
                     .map(el => {
                       const r = el.getBoundingClientRect();
                       return {tag: el.tagName.toLowerCase(),
                               cls: (el.className||'').toString().slice(0,50),
                               text: (el.innerText||'').replace(/\s+/g,' ').trim().slice(0,24),
                               box: [Math.round(r.x),Math.round(r.y),
                                     Math.round(r.width),Math.round(r.height)]};
                     })"""
            )
        except Exception:  # noqa: BLE001
            phs = []
        self.log(f"   [상태:{label}]")
        self.log(f"      page.url   = {(page.url or '')[:70]}")
        self.log(f"      frame.url  = {(frame.url or '')[:70]}")
        self.log(f"      placeholder {len(phs)}개")
        for p in phs:
            self.log(f"         <{p['tag']}> box={p['box']} text={p['text']!r} cls={p['cls']!r}")
        self.log(f"      activeElement = {await self._active_element(frame)}")

    async def _shot(self, page, name: str) -> None:
        try:
            d = self.user_data_dir.parent / "out"
            d.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(d / f"{name}.png"))
        except Exception:  # noqa: BLE001
            pass

    async def _clear_all(self, page) -> None:
        """현재 포커스된 편집 영역의 내용을 전부 지운다(probe 문자열 제거용)."""
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(150)
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(300)

    async def _editor_spots(self, frame) -> dict:
        """제목/본문 클릭 지점을 화면에서 계산한다.

        · 제목: placeholder 박스 중앙
        · 본문: placeholder(또는 본문 컨테이너) 박스의 **왼쪽에서 30px 들어간 빈 지점**
                — <strike>/<strong>/<span> 같은 서식 자식을 직접 집지 않기 위해
                  se-placeholder 클래스를 우선하고, 없으면 본문 컨테이너를 쓴다.
        """
        return await frame.evaluate(
            r"""
            () => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 30 && r.height > 8
                       && st.display !== 'none' && st.visibility !== 'hidden' && r.y > 140;
              };
              const info = (el, x, y) => {
                const r = el.getBoundingClientRect();
                return {x: Math.round(x), y: Math.round(y),
                        box: [Math.round(r.x), Math.round(r.y),
                              Math.round(r.width), Math.round(r.height)],
                        tag: el.tagName.toLowerCase(),
                        cls: (el.className || '').toString().slice(0, 60)};
              };
              const res = {};

              // ① se-placeholder 우선(서식 요소가 아니라 '자리' 그 자체)
              const phs = Array.from(document.querySelectorAll("[class*='se-placeholder']"))
                            .filter(vis)
                            .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
              if (phs.length) {
                const t = phs[0].getBoundingClientRect();
                res.title = info(phs[0], t.x + t.width / 2, t.y + t.height / 2);
              }
              // ★인덱스로 고르지 않는다. 제목을 채우면 제목 placeholder 가 사라져서
              //   [1] 이 없어지고 본문을 못 찾는 사고가 났다.
              //   '제목 영역보다 아래'에 있는 placeholder 를 본문으로 본다.
              const titleY = res.title ? res.title.box[1] : -1;
              const bodyPh = phs.find(el => el.getBoundingClientRect().y > titleY + 20);
              if (bodyPh) {
                const b = bodyPh.getBoundingClientRect();
                // 왼쪽에서 30px 들어간 지점(서식 걸린 텍스트 위를 피한다)
                res.body = info(bodyPh, b.x + 30, b.y + b.height / 2);
              }

              // ② 본문 placeholder 가 없으면 제목 아래 가장 넓은 편집 컨테이너의 빈 위쪽
              if (!res.body) {
                const cands = Array.from(document.querySelectorAll(
                    "[class*='se-component'],[class*='se-section'],[class*='se-main']"))
                  .filter(vis)
                  .filter(el => !res.title || el.getBoundingClientRect().y > res.title.box[1] + 10)
                  .sort((a, b) => {
                    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                    return (rb.width * rb.height) - (ra.width * ra.height);
                  });
                if (cands.length) {
                  const r = cands[0].getBoundingClientRect();
                  res.body = info(cands[0], r.x + 30, r.y + Math.min(20, r.height / 2));
                }
              }
              return res;
            }
            """
        )

    async def _format_buttons(self, frame) -> list:
        """툴바의 서식 버튼과 활성 여부. 클래스명을 모르므로 라벨/속성으로 찾는다."""
        try:
            return await frame.evaluate(
                r"""
                (labels) => Array.from(document.querySelectorAll("button,[role='button']"))
                  .map((el, i) => {
                    const r = el.getBoundingClientRect();
                    const lab = ((el.getAttribute('aria-label') || '')
                                 + ' ' + (el.getAttribute('title') || '')
                                 + ' ' + (el.className || '').toString()
                                 + ' ' + (el.innerText || '')).toLowerCase();
                    const on = el.getAttribute('aria-pressed') === 'true'
                            || /active|selected|is-on|se-is-on/.test((el.className||'').toString());
                    el.setAttribute('data-blg-fmt', String(i));
                    return {i, lab: lab.replace(/\s+/g, ' ').trim().slice(0, 60),
                            on, y: Math.round(r.y), w: Math.round(r.width)};
                  })
                  .filter(b => b.y < 145 && b.w > 0
                            && labels.some(L => b.lab.indexOf(L) >= 0))
                """,
                [l.lower() for l in self.FORMAT_LABELS],
            )
        except Exception:  # noqa: BLE001
            return []

    async def _reset_formatting(self, frame) -> None:
        """켜져 있는 서식(굵게/기울임/밑줄/취소선)을 툴바에서 꺼준다.

        ★검증되지 않은 단축키(Ctrl+\\ 등)는 쓰지 않는다 — 툴바 상태만 보고 토글한다.
        """
        btns = await self._format_buttons(frame)
        if not btns:
            self.log("      서식 버튼을 찾지 못했습니다(초기화 생략).")
            return
        active = [b for b in btns if b["on"]]
        if not active:
            self.log(f"      서식 버튼 {len(btns)}개 확인 · 활성 없음")
            return
        for b in active:
            try:
                await frame.locator(f"[data-blg-fmt='{b['i']}']").first.click(timeout=1500)
                self.log(f"      활성 서식 해제: {b['lab'][:30]!r}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"      서식 해제 실패({b['lab'][:20]!r}): {type(exc).__name__}")

    async def _click_spot(self, page, frame, spot: dict) -> None:
        """프레임 내부 좌표 → 페이지 좌표로 변환해 클릭(frame_element 사용 안 함)."""
        frame = await self._fresh_frame(page, frame)
        ox, oy = await self._frame_offset(page, frame)
        await page.mouse.click(ox + spot["x"], oy + spot["y"])
        await page.wait_for_timeout(500)

    async def _type_lines(self, page, text: str) -> None:
        """문단 구조를 유지하며 입력. 빈 줄은 Enter 두 번."""
        blocks = [b for b in text.split("\n\n") if b.strip()]
        for i, block in enumerate(blocks):
            for j, line in enumerate(block.split("\n")):
                if j:
                    await page.keyboard.press("Enter")
                if line:
                    await page.keyboard.type(line, delay=3)
            if i < len(blocks) - 1:
                await page.keyboard.press("Enter")
                await page.keyboard.press("Enter")

    async def _check_unwanted_format(self, frame, marker: str) -> list:
        """입력된 텍스트에 취소선/굵게 같은 서식이 붙었는지 확인."""
        try:
            return await frame.evaluate(
                r"""(m) => {
                     const bad = [];
                     document.querySelectorAll('strike,s,del,strong,b,u,em,i').forEach(el => {
                       if ((el.innerText || '').indexOf(m) >= 0) bad.push(el.tagName.toLowerCase());
                     });
                     return Array.from(new Set(bad));
                   }""",
                marker,
            )
        except Exception:  # noqa: BLE001
            return []



    async def _strip_formatting_selection(self, page, frame) -> None:
        """본문을 전체 선택한 상태에서 켜져 있는 서식을 벗긴다.

        캐럿 스타일 토글(타이핑 전)과 달리, **선택 영역 토글**은 그 범위에 확정 적용되므로
        문단에 남아 있던 취소선까지 확실히 제거된다.
        """
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(300)
        btns = await self._format_buttons(frame)
        active = [b for b in btns if b["on"]]
        if not active:
            self.log("      선택 상태에서 활성 서식 없음")
        for b in active:
            try:
                await frame.locator(f"[data-blg-fmt='{b['i']}']").first.click(timeout=1500)
                self.log(f"      선택 영역 서식 해제: {b['lab'][:30]!r}")
                await page.wait_for_timeout(250)
            except Exception as exc:  # noqa: BLE001
                self.log(f"      해제 실패({b['lab'][:20]!r}): {type(exc).__name__}")
        # 선택 해제 + 커서를 끝으로
        await page.keyboard.press("End")
        await page.wait_for_timeout(200)

    async def _probe_body_focus(self, page, frame, spot: dict) -> bool:
        """본문 자리를 클릭하고 짧은 probe 로 '키가 실제로 들어가는지' 확인.

        · 클릭 후 충분히 기다린 뒤 친다(첫 글자 유실 방지).
        · 판정은 접미사('PROBE123')로 — 앞글자가 몇 개 잘려도 입력 자체는 된 것으로 본다.
          잘렸으면 경고를 남기고 대기시간을 늘려 한 번 더 시도한다.
        """
        for attempt, settle in enumerate((600, 1200), start=1):
            await self._click_spot(page, frame, spot)
            await page.wait_for_timeout(settle)
            self.log(f"      [시도 {attempt}] 클릭 후 {settle}ms · "
                     f"activeElement = {await self._active_element(frame)}")
            await page.keyboard.type(self.BODY_PROBE, delay=8)
            await page.wait_for_timeout(700)
            await self._shot(page, f"body_probe{attempt}")

            full = await self._frame_has(frame, self.BODY_PROBE)
            tail = await self._frame_has(frame, "PROBE123")
            if full:
                self.log("      probe 완전일치 ✅")
                return True
            if tail:
                self.log("      ⚠ 앞글자 유실 감지(입력은 됨) — 대기시간을 늘려 재시도")
                await self._clear_all(page)
                continue
            self.log("      probe 미반영 ❌")
            await self._clear_all(page)
        return False

    async def _type_title(self, page, title: str, spot: dict | None = None) -> bool:
        """제목 입력 — placeholder 자리를 클릭하고 키보드로 친다.

        spot 을 받으면 그대로 쓴다(제목 입력 전에 미리 잰 좌표 — 두 경로를 동일하게 유지).
        """
        frame = await self._fresh_frame(page)
        await self._log_state(page, frame, "제목 입력 직전")
        if spot is None:
            spot = (await self._editor_spots(frame)).get("title")
        if spot:
            self.log(f"   [제목] <{spot['tag']}> box={spot['box']} → ({spot['x']},{spot['y']}) 클릭")
            await self._click_spot(page, frame, spot)
            await page.keyboard.type(title, delay=5)
            await page.wait_for_timeout(600)
            frame = await self._fresh_frame(page, frame)     # 클릭 후 재획득
            if await self._frame_has(frame, title[:20]):
                self.log("   [제목] 화면반영 ✅")
                await self._log_state(page, frame, "제목 입력 직후")
                return True
            self.log("   [제목] 좌표 방식 실패 → selector 폴백")
        return await self._type_title_fallback(page, title)

    async def _type_body(self, page, body: str, spot: dict | None = None) -> bool:
        """본문 입력 — 빈 자리를 클릭하고 서식을 끈 뒤 키보드로 친다.

        순서를 test-input 과 한 줄도 다르지 않게 맞춘다:
          제목 입력 완료 → 500ms → (미리 잰) 본문 좌표 클릭 → 300ms
          → 서식 초기화 → probe 입력 확인 → 지우고 → 실제 원고 입력
        """
        frame = await self._fresh_frame(page)
        await page.wait_for_timeout(500)
        await self._log_state(page, frame, "본문 입력 직전")
        if spot is None:
            spot = (await self._editor_spots(frame)).get("body")
        await self._shot(page, "body_before")
        if spot:
            self.log(f"   [본문] <{spot['tag']}> box={spot['box']} → ({spot['x']},{spot['y']}) 클릭")
            # ★타이핑 전에는 툴바를 절대 건드리지 않는다.
            #   툴바 클릭이 포커스를 가져가 첫 글자가 잘렸다(BODYPROBE123 → DYPROBE123).
            ok_probe = await self._probe_body_focus(page, frame, spot)
            if not ok_probe:
                self.log("   [본문] probe 실패 — test-input 성공 상태와 비교하세요.")
                await self._log_state(page, frame, "probe 실패 시점")
                return await self._type_body_fallback(page, body)

            # ★타이핑 전에 스타일을 맞추려 하지 않는다.
            #   툴바로 끈 뒤 본문을 다시 클릭하면 그 문단의 스타일을 도로 물려받는다.
            #   서식은 '다 친 다음 전체 선택해서 벗기는' 방식이 확실하다(아래 참조).
            probe_fmt = await self._check_unwanted_format(frame, "PROBE123")
            if probe_fmt:
                self.log(f"   [본문] probe 서식 {probe_fmt} — 입력 후 일괄 제거합니다")
            await self._clear_all(page)

            await self._type_lines(page, body)
            await page.wait_for_timeout(800)
            head = (body.strip().split("\n")[0] or "")[:20]
            if await self._frame_has(frame, head):
                bad = await self._check_unwanted_format(frame, head)
                if bad:
                    self.log(f"   [본문] 서식 {bad} 감지 → 전체 선택 후 제거")
                    await self._strip_formatting_selection(page, frame)
                    bad = await self._check_unwanted_format(frame, head)
                if bad:
                    self.log(f"   [본문] ⚠ 서식이 남아 있습니다: {bad}")
                else:
                    self.log("   [본문] 화면반영 ✅ · 서식 이상 없음")
                await self._shot(page, "body_done")
                return True
            self.log("   [본문] 좌표 방식 실패 → selector 폴백")
        return await self._type_body_fallback(page, body)

    # ── 폴백(예전 경로). 이 에디터에선 잘 안 되지만 다른 스킨 대비로 남긴다 ──
    async def _type_title_fallback(self, page, title: str) -> bool:
        loc = await self._target(page, [
            "textarea[placeholder*='제목']", "input[placeholder*='제목']",
            "[contenteditable='true'][data-placeholder*='제목']",
            ".se-documentTitle", ".se-title-text",
        ])
        if loc is None or not await self._focus_target(page, loc):
            return False
        try:
            await loc.fill(title, timeout=3000)
            return await self._verify_text(loc, title)
        except Exception:  # noqa: BLE001
            return await self._paste_text(page, loc, title)

    async def _type_body_fallback(self, page, body: str) -> bool:
        loc = await self._target(page, [
            ".se-main-container .se-component.se-text [contenteditable='true']",
            "[contenteditable='true'][data-placeholder*='내용']",
            "textarea[placeholder*='내용']",
        ])
        if loc is None or not await self._focus_target(page, loc):
            return False
        try:
            await loc.fill(body, timeout=3000)
            return await self._verify_text(loc, body)
        except Exception:  # noqa: BLE001
            return await self._paste_text(page, loc, body)

    # ══════════════════════════════════════════════════════════════════
    # 클립보드 모드 — 사람이 하던 '구간 선택 → 복사 → 붙여넣기'와 같은 경로.
    # 서식/이미지가 그대로 넘어간다. 발행·저장은 여전히 하지 않는다.
    # ══════════════════════════════════════════════════════════════════
    def paste_from_landing(
        self,
        landing_url: str,
        title: str,
        wait_for_continue: WaitFn,
        section_selectors: list[str] | None = None,
        bulk: int = 1,
        publish: bool = False,
        edit_copy: bool = False,
        mobile_preview: bool = True,
        capture_align: bool = False,
        on_published=None,
    ) -> EditorFillResult:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 Playwright 실제 실행이 가능합니다.")
        return asyncio.run(
            # ★전부 keyword 로 넘긴다(2026-08-20) — 시그니처에 인자를 끼워 넣었을 때
            #   위치가 밀려 'got multiple values for argument' 가 나던 것을 막는다.
            self._paste_from_landing(
                landing_url=landing_url,
                title=title,
                wait_for_continue=wait_for_continue,
                section_selectors=section_selectors or [],
                bulk=bulk,
                edit_copy=edit_copy,
                mobile_preview=mobile_preview,
                publish=publish,
                capture_align=capture_align,
                on_published=on_published,
            )
        )


    # ── 붙여넣기 후처리 ──────────────────────────────────────────────
    #   복사 경로는 건드리지 않는다. 아래 순서로만 처리한다.
    #     전체 선택 → 취소선만 해제 → 선택 해제 → 출처 문단 삭제 → Re:purely 3문단 삭제
    #   ※구분선 제거는 하지 않는다(툴바 요소까지 잡아 본문을 날린 전례가 있어 제외).
    PROMO_TEXTS = ("Re:purely | 올레놀샷 NMN 포뮬러",
                   "Re:purely의 올레놀샷 NMN 포뮬러",
                   "repurely.com")
    # 검증 대상: 남아 있으면 안 되는 문구. 제품 블록도 포함(2026-08-20 — 레모니티에서 잔존).
    VERIFY_TEXTS = ("[출처]", "Re:purely", "repurely.com", "사용 후 불만족시")
    # 안전장치: 텍스트 길이 감소는 기준으로 쓰지 않는다.
    #   출처/제품 링크 삭제는 '의도된 삭제'라 길이가 줄어드는 게 정상이다.
    #   대신 이미지가 줄거나, 지운 문단 수보다 훨씬 많이 문단이 사라진 경우만 실패로 본다.
    MAX_PARA_EXTRA = 3          # (지운 문단 수 + 이 값) 보다 더 줄면 비정상

    async def _measure_body(self, frame) -> dict:
        return await frame.evaluate(
            r"""() => {
                 const root = document.querySelector('.se-main-container') || document.body;
                 return {len: (root.innerText || '').trim().length,
                         img: root.querySelectorAll('img').length,
                         strike: root.querySelectorAll('s,strike,del').length,
                         para: root.querySelectorAll('p').length};
               }"""
        )

    async def _remove_strike_only(self, page, frame, spot: dict | None) -> None:
        """본문 전체를 선택해 **취소선만** 해제한다. 다른 서식은 손대지 않는다."""
        if spot:
            await self._click_spot(page, frame, spot)
            await page.wait_for_timeout(300)
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(400)

        btns = await self._format_buttons(frame)
        strike = [b for b in btns
                  if ("취소선" in b["lab"] or "strike" in b["lab"]) and b["on"]]
        if not strike:
            self.log("   [후처리] 취소선 활성 아님 — 해제할 것 없음")
        for b in strike:
            try:
                await frame.locator(f"[data-blg-fmt='{b['i']}']").first.click(timeout=1500)
                self.log(f"   [후처리] 취소선 해제: {b['lab'][:30]!r}")
                await page.wait_for_timeout(300)
            except Exception as exc:  # noqa: BLE001
                self.log(f"   [후처리] 취소선 해제 실패: {type(exc).__name__}")

        # 선택 해제 + 커서를 본문 끝으로
        await page.keyboard.press("Control+End")
        await page.wait_for_timeout(250)

    SOURCE_MARK = "[출처]"
    SOURCE_MAX_LEN = 150

    async def _find_source_blocks(self, frame) -> list:
        """'[출처]' 를 품은 **가장 작은 문단 블록**을 찾아 표시하고 정보를 돌려준다.

        p 부터 찾지 않는다. 문자열이 b/span 안에 있어도 잡히도록 전체 요소를 훑고,
        거기서 위로 올라가며 p.se-text-paragraph(또는 최소 문단)를 고른다.
        """
        return await frame.evaluate(
            r"""
            (cfg) => {
              const root = document.querySelector('.se-main-container') || document.body;
              const norm = (t) => (t || '')
                    .replace(/[\u200B\uFEFF\u00A0]/g, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();

              // ① '[출처]' 를 포함하는 가장 안쪽 요소들
              const inner = Array.from(root.querySelectorAll('*')).filter(el => {
                if (norm(el.textContent).indexOf(cfg.mark) < 0) return false;
                return !Array.from(el.children)
                        .some(c => norm(c.textContent).indexOf(cfg.mark) >= 0);
              });

              const out = [];
              const seen = new Set();
              inner.forEach((el, i) => {
                // ② 위로 올라가며 삭제해도 되는 '최소 문단 블록' 찾기
                let node = el, target = null;
                for (let d = 0; d < 6 && node && node !== root; d++) {
                  const t = norm(node.textContent);
                  const ok = !node.querySelector('img')
                          && node.querySelectorAll('p').length === 0
                          && t.length <= cfg.maxLen
                          && t.indexOf(cfg.mark) >= 0;
                  if (ok) {
                    target = node;
                    if (node.matches && node.matches('p.se-text-paragraph')) break;
                  }
                  node = node.parentElement;
                }
                if (!target || seen.has(target)) return;
                seen.add(target);
                target.setAttribute('data-blg-src', String(out.length));
                const r = target.getBoundingClientRect();
                const p = target.parentElement;
                out.push({
                  i: out.length,
                  tag: target.tagName.toLowerCase(),
                  cls: (target.className || '').toString().slice(0, 60),
                  text: norm(target.textContent).slice(0, 70),
                  len: norm(target.textContent).length,
                  box: [Math.round(r.x), Math.round(r.y),
                        Math.round(r.width), Math.round(r.height)],
                  parentTag: p ? p.tagName.toLowerCase() : '',
                  parentCls: p ? (p.className || '').toString().slice(0, 50) : ''
                });
              });
              return out;
            }
            """,
            {"mark": self.SOURCE_MARK, "maxLen": self.SOURCE_MAX_LEN},
        )

    async def _remove_source_block(self, frame, idx: int) -> bool:
        return await frame.evaluate(
            r"""(i) => {
                 const el = document.querySelector(`[data-blg-src="${i}"]`);
                 if (!el || !el.parentElement) return false;
                 el.remove();
                 return true;
               }""",
            idx,
        )

    async def _count_source(self, frame) -> int:
        try:
            return await frame.evaluate(
                r"""(mark) => {
                     const root = document.querySelector('.se-main-container') || document.body;
                     const t = (root.textContent || '')
                       .replace(/[\u200B\uFEFF\u00A0]/g, ' ');
                     let n = 0, i = 0;
                     while ((i = t.indexOf(mark, i)) >= 0) { n += 1; i += mark.length; }
                     return n;
                   }""",
                self.SOURCE_MARK,
            )
        except Exception:  # noqa: BLE001
            return -1

    async def _keyboard_delete_line(self, page, frame, box: list) -> bool:
        """DOM 삭제가 되돌려질 때의 fallback — 그 줄을 클릭해 선택하고 지운다."""
        try:
            x = box[0] + max(10, box[2] // 2)
            y = box[1] + max(5, box[3] // 2)
            await self._click_xy_in_frame(page, frame, x, y)
            await page.keyboard.press("Home")
            await page.keyboard.press("Shift+End")
            await page.wait_for_timeout(150)
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(150)
            await page.keyboard.press("Backspace")     # 빈 줄까지 정리
            await page.wait_for_timeout(300)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"      키보드 삭제 실패: {type(exc).__name__}")
            return False

    async def _delete_paragraphs(self, page, frame) -> dict:
        """'[출처]' 문단을 0개가 될 때까지 하나씩 제거(최대 20회).

        DOM 삭제 후 되살아나면 키보드 방식으로 한 번 더 시도한다.
        """
        removed = 0
        for attempt in range(20):
            blocks = await self._find_source_blocks(frame)
            if not blocks:
                break
            b = blocks[0]
            before = await self._count_source(frame)
            ok = await self._remove_source_block(frame, b["i"])
            await page.wait_for_timeout(400)
            after = await self._count_source(frame)

            if ok and after < before:
                removed += 1
                self.log(f"   [후처리] 출처 #{removed} 제거  <{b['tag']}> "
                         f"cls={b['cls']!r} text={b['text']!r}")
                continue

            # DOM 삭제가 안 먹었다 → 키보드 fallback
            self.log(f"   [후처리] DOM 삭제 미반영 → 키보드 삭제 시도 (box={b['box']})")
            await self._keyboard_delete_line(page, frame, b["box"])
            after2 = await self._count_source(frame)
            if after2 < before:
                removed += 1
                self.log(f"   [후처리] 출처 #{removed} 제거(키보드)")
                continue
            self.log("   [후처리] 이 블록은 제거하지 못했습니다 — 중단")
            break
        return {"source": removed}

    async def _report_leftover_source(self, frame) -> None:
        blocks = await self._find_source_blocks(frame)
        for b in blocks:
            self.log(f"      <{b['tag']}> cls={b['cls']!r}")
            self.log(f"         text={b['text']!r} (len={b['len']})")
            self.log(f"         parent <{b['parentTag']}> cls={b['parentCls']!r}")


    # ── 하단 제품 링크 3문단 삭제 ────────────────────────────────────
    # ★제품명으로 열거하지 않는다(2026-08-20). 올레놀샷 전용 문구로 박아뒀더니 레모니티-C 랜딩에서
    #   하단 제품 블록이 그대로 남았다. 제품이 뭐든 공통으로 들어가는 표식만 본다.
    #     · 'Re:purely'  — 브랜드명(예: 'Re:purely | 올레놀샷 NMN 포뮬러', '레모니티-C - Re:purely')
    #     · 'repurely.com'
    #     · '사용 후 불만족시' — 제품 설명 줄의 공통 문구
    #   ⚠️ 안전장치는 그대로: 이미지 없고 하위 문단 없는 p 만 삭제(상위로 올라가면 본문이 날아간다).
    PROMO_CONTAINS = ("re:purely", "repurely.com", "사용 후 불만족시")
    PROMO_EXACT = ()          # (하위호환) 더 이상 쓰지 않음
    PROMO_PREFIX = ()

    async def _delete_promo_paragraphs(self, frame) -> dict:
        """제품 링크 3문단만 삭제. p 문단 자체만 지우고 상위는 건드리지 않는다."""
        return await frame.evaluate(
            r"""
            (cfg) => {
              const root = document.querySelector('.se-main-container') || document.body;
              const norm = (t) => (t || '')
                    .replace(/[\u200B\uFEFF\u00A0]/g, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();
              const res = {removed: 0, detail: []};

              // 이미지 없고, 다른 문단을 품지 않은 p 만 대상
              const killable = (el) => el && root.contains(el) && el !== root
                    && !el.querySelector('img')
                    && el.querySelectorAll('p').length === 0;

              let paras = Array.from(root.querySelectorAll('p.se-text-paragraph'));
              if (!paras.length) paras = Array.from(root.querySelectorAll('p'));

              paras.forEach(el => {
                if (!el.isConnected) return;
                const t = norm(el.textContent);
                if (!t) return;
                const low = t.toLowerCase();
                const hit = cfg.contains.some(x => low.indexOf(x) >= 0)
                         || cfg.exact.some(x => t === x)
                         || cfg.prefix.some(x => t.indexOf(x) === 0);
                if (!hit || !killable(el)) return;
                res.detail.push({text: t.slice(0, 70),
                                 cls: (el.className || '').toString().slice(0, 50)});
                el.remove();
                res.removed += 1;
              });
              return res;
            }
            """,
            {"exact": list(self.PROMO_EXACT), "prefix": list(self.PROMO_PREFIX),
             "contains": [x.lower() for x in self.PROMO_CONTAINS]},
        )

    async def _count_promo(self, frame) -> dict:
        """제품 링크 문구가 몇 개 남았는지."""
        try:
            return await frame.evaluate(
                r"""(words) => {
                     const root = document.querySelector('.se-main-container') || document.body;
                     const t = (root.textContent || '')
                       .replace(/[\u200B\uFEFF\u00A0]/g, ' ').replace(/\s+/g, ' ');
                     const out = {};
                     words.forEach(w => {
                       let n = 0, i = 0;
                       while ((i = t.indexOf(w, i)) >= 0) { n += 1; i += w.length; }
                       out[w] = n;
                     });
                     return out;
                   }""",
                list(self.PROMO_CONTAINS),   # 제품 무관 표식으로 검증(2026-08-20)
            )
        except Exception:  # noqa: BLE001
            return {}

    async def _mark_promo_block(self, frame) -> dict:
        """하단 제품 링크 3문단을 찾아 표시하고 선택 범위를 만든다."""
        return await frame.evaluate(
            r"""
            (promo) => {
              const root = document.querySelector('.se-main-container') || document.body;
              const own = (el) => (el.innerText || '').trim();
              const hits = [];
              Array.from(root.querySelectorAll('p')).forEach(el => {
                const t = own(el);
                if (!t || t.length > 300) return;
                if (promo.some(j => t.indexOf(j) >= 0)) hits.push(el);
              });
              if (!hits.length) return {found: 0};

              hits.forEach((el, i) => el.setAttribute('data-blg-promo', String(i)));
              // 첫 문단 ~ 마지막 문단까지 한 번에 선택(전체 본문을 잡지 않는다)
              const r = document.createRange();
              r.setStartBefore(hits[0]);
              r.setEndAfter(hits[hits.length - 1]);
              const sel = window.getSelection();
              sel.removeAllRanges();
              sel.addRange(r);
              return {found: hits.length,
                      texts: hits.map(el => own(el).slice(0, 50)),
                      selected: sel.toString().trim().length};
            }
            """,
            list(self.PROMO_TEXTS),
        )

    async def _align_buttons(self, frame) -> list:
        """가운데 정렬 버튼 후보."""
        try:
            return await frame.evaluate(
                r"""
                () => Array.from(document.querySelectorAll("button,[role='button']"))
                  .map((el, i) => {
                    const r = el.getBoundingClientRect();
                    const lab = ((el.getAttribute('aria-label') || '')
                                 + ' ' + (el.getAttribute('title') || '')
                                 + ' ' + (el.className || '').toString()).toLowerCase();
                    el.setAttribute('data-blg-align', String(i));
                    return {i, lab: lab.replace(/\s+/g,' ').trim().slice(0, 60),
                            y: Math.round(r.y), w: Math.round(r.width)};
                  })
                  .filter(b => b.y < 160 && b.w > 0
                            && (b.lab.indexOf('가운데') >= 0
                                || b.lab.indexOf('align-center') >= 0
                                || b.lab.indexOf('aligncenter') >= 0
                                || b.lab.indexOf('center') >= 0))
                """
            )
        except Exception:  # noqa: BLE001
            return []

    async def _center_promo_block(self, page, frame) -> bool:
        """하단 제품 링크 3문단만 선택해 중앙정렬. 본문 전체는 건드리지 않는다."""
        info = await self._mark_promo_block(frame)
        if not info.get("found"):
            self.log("   [정렬] 제품 링크 문단을 찾지 못했습니다(정렬 생략)")
            return False
        self.log(f"   [정렬] 제품 링크 {info['found']}개 문단 선택")
        for t in info.get("texts", []):
            self.log(f"      · {t!r}")

        btns = await self._align_buttons(frame)
        if not btns:
            self.log("   [정렬] 가운데 정렬 버튼을 찾지 못했습니다")
            return False
        target = btns[0]
        try:
            await frame.locator(f"[data-blg-align='{target['i']}']").first.click(timeout=2000)
            self.log(f"   [정렬] 가운데 정렬 적용: {target['lab'][:40]!r}")
            await page.wait_for_timeout(400)
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [정렬] 적용 실패: {type(exc).__name__}")
            return False
        await page.keyboard.press("Control+End")          # 선택 해제
        await page.wait_for_timeout(200)
        return True

    async def _rollback(self, frame) -> bool:
        return await frame.evaluate(
            r"""() => {
                 if (!window.__blgRoot || typeof window.__blgSnapshot !== 'string') return false;
                 window.__blgRoot.innerHTML = window.__blgSnapshot;
                 return true;
               }"""
        )

    async def _cleanup_pasted(self, page, frame, spot: dict | None = None) -> dict:
        """고정 순서 후처리. 본문 손실이 크면 문단 삭제분을 롤백한다."""
        await page.wait_for_timeout(1200)                 # paste 렌더링 완료 대기
        before = await self._measure_body(frame)
        self.log(f"   [후처리] 시작 — 본문 {before['len']}자 · 이미지 {before['img']}개 "
                 f"· 취소선 {before['strike']}개")

        # ① 전체 선택 → 취소선만 해제 → 선택 해제
        await self._remove_strike_only(page, frame, spot)

        # ② '[출처]' 문단 완전 제거
        res = await self._delete_paragraphs(page, frame)
        left = await self._count_source(frame)
        if left == 0:
            self.log(f"   [검증] [출처] 0개 ✅")
        else:
            self.log(f"   [검증] ❌ [출처] {left}개 남음")
            await self._report_leftover_source(frame)
        await page.wait_for_timeout(300)

        # ③ 하단 제품 링크 3문단 삭제
        promo = await self._delete_promo_paragraphs(frame)
        for d in promo.get("detail", []):
            self.log(f"      삭제(제품링크): {d['text']!r}  cls={d.get('cls','')!r}")
        self.log(f"   [후처리] 하단 제품 링크 블록 {promo.get('removed', 0)}개 제거")
        left = await self._count_promo(frame)
        if left and sum(left.values()) == 0:
            self.log("   [검증] 하단 제품 링크 없음 ✅")
        elif left:
            self.log("   [검증] ❌ 제품 링크 문구가 남아 있습니다:")
            for k, v in left.items():
                if v:
                    self.log(f"      {k!r}: {v}개")
        await page.wait_for_timeout(300)

        after = await self._measure_body(frame)
        removed_paras = res.get("source", 0) + promo.get("removed", 0)
        para_drop = before.get("para", 0) - after.get("para", 0)
        img_lost = after["img"] < before["img"]
        para_abnormal = para_drop > removed_paras + self.MAX_PARA_EXTRA

        if img_lost or para_abnormal:
            why = []
            if img_lost:
                why.append(f"이미지 {before['img']}→{after['img']}")
            if para_abnormal:
                why.append(f"문단 {para_drop}개 감소(삭제한 문단 {removed_paras}개)")
            self.log(f"   [안전장치] {' · '.join(why)} → 후처리 롤백")
            await self._rollback(frame)
            await page.wait_for_timeout(400)
            after = await self._measure_body(frame)
            return {"source": 0, "rolled_back": True}

        self.log(f"   [후처리] 본문 {before['len']}자 → {after['len']}자 "
                 f"(의도된 삭제 {removed_paras}문단) · "
                 f"이미지 {before['img']}→{after['img']}개 · "
                 f"문단 {before.get('para', 0)}→{after.get('para', 0)}개 · "
                 f"취소선 {before['strike']}→{after['strike']}개")
        return {"source": res.get("source", 0), "before": before, "after": after}

    async def _verify_clean(self, frame) -> list:
        """불필요 문구가 남았는지 확인. 남았으면 위치(tag/class)를 돌려준다."""
        try:
            return await frame.evaluate(
                r"""
                (words) => {
                  const root = document.querySelector('.se-main-container') || document.body;
                  const out = [];
                  for (const w of words) {
                    const hit = Array.from(root.querySelectorAll('*')).find(el => {
                      if (Array.from(el.children).some(
                          c => ((c.innerText||'')).indexOf(w) >= 0)) return false;
                      return ((el.innerText || '')).indexOf(w) >= 0;
                    });
                    if (hit) out.push({
                      word: w, tag: hit.tagName.toLowerCase(),
                      cls: (hit.className || '').toString().slice(0, 60),
                      text: (hit.innerText || '').replace(/\s+/g,' ').trim().slice(0, 60)
                    });
                  }
                  return out;
                }
                """,
                list(self.VERIFY_TEXTS),
            )
        except Exception:  # noqa: BLE001
            return []

    async def _save_template(self, frame, tag: str) -> None:
        """정상 복제된 본문 HTML을 템플릿으로 저장(같은 랜딩 대량 처리 재사용용)."""
        try:
            html = await frame.evaluate(
                "() => (document.querySelector('.se-main-container') || {}).innerHTML || ''"
            )
            if not html:
                return
            d = self.user_data_dir.parent / "out" / "templates"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{tag}.html"
            p.write_text(html, encoding="utf-8")
            self.log(f"   [템플릿] 본문 구조 저장: {p} ({len(html):,}자)")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [템플릿] 저장 실패: {type(exc).__name__}")

    async def _content_frame(self, page):
        """본문이 실제로 들어있는 프레임(네이버 블로그는 mainFrame)."""
        best, best_n = page.main_frame, -1
        for f in page.frames:
            try:
                n = await f.evaluate(
                    "() => ((document.body && document.body.innerText) || '').length"
                )
            except Exception:  # noqa: BLE001
                continue
            if n > best_n:
                best, best_n = f, n
        self.log(f"   본문 프레임: name={best.name!r} (텍스트 {best_n:,}자)")
        return best

    async def _content_sections(self, frame) -> list:
        """본문 컨테이너의 구간 선택자. 사람이 구간 잡아 복사하던 것과 같은 단위."""
        return await frame.evaluate(
            r"""
            () => {
              const root = document.querySelector('.se-main-container')
                        || document.querySelector('#postViewArea, .post-view, article, main')
                        || document.body;
              root.setAttribute('data-blg-root', '1');
              const kids = Array.from(root.children).filter(el => {
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                return ((el.innerText || '').trim().length > 0) || !!el.querySelector('img');
              });
              kids.forEach((el, i) => el.setAttribute('data-blg-section', String(i)));
              return kids.length ? kids.map((_, i) => `[data-blg-section="${i}"]`)
                                 : ['[data-blg-root="1"]'];
            }
            """
        )

    async def _select_in_frame(self, frame, selector: str) -> bool:
        """프레임 안에서 해당 요소 전체를 드래그 선택한 상태로 만든다."""
        try:
            return await frame.evaluate(
                r"""(sel) => {
                     const el = document.querySelector(sel);
                     if (!el) return false;
                     el.scrollIntoView({block: 'center'});
                     const r = document.createRange();
                     r.selectNodeContents(el);
                     const s = window.getSelection();
                     s.removeAllRanges();
                     s.addRange(r);
                     return (s.toString().trim().length > 0) || !!el.querySelector('img');
                   }""",
                selector,
            )
        except Exception:  # noqa: BLE001
            return False

    async def _format_census(self, frame, root_sel: str) -> dict:
        """서식 요소 개수 — 원문과 결과를 비교해 차이를 보기 위한 지표."""
        try:
            return await frame.evaluate(
                r"""(sel) => {
                     const root = document.querySelector(sel) || document.body;
                     const c = {};
                     ['strong','b','s','strike','del','u','em','i','img','p','div','br','a','h2','h3']
                       .forEach(t => { const n = root.querySelectorAll(t).length; if (n) c[t] = n; });
                     c['_텍스트길이'] = ((root.innerText || '').trim()).length;
                     return c;
                   }""",
                root_sel,
            )
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _is_editor_page(page) -> bool:
        u = (page.url or "").lower()
        return ("redirect=write" in u) or ("postwrite" in u)

    async def _open_new_write(self, context, nav_page):
        """내 블로그 → 글쓰기 → 팝업 취소 → 에디터 준비.

        ★탭을 추측하지 않는다. 클릭 전후 페이지 집합을 비교해 '새로 열린 에디터'를 특정하고,
          frame.url(/postwrite) 과 placeholder 존재를 검증한 뒤에만 반환한다.
          검증 실패 시 입력하지 않고 예외를 던진다(거짓 성공 방지).
        """
        before = set(context.pages)
        await nav_page.goto(self.naver_blog_home_url, wait_until="domcontentloaded")
        await self._settle(nav_page, lambda: self._write_button_exists(nav_page), 8000)
        await self._click_write_button(nav_page)

        editor = None
        for _ in range(40):                              # 최대 12초
            fresh = [p for p in context.pages if p not in before]
            for cand in fresh + ([nav_page] if self._is_editor_page(nav_page) else []):
                if self._is_editor_page(cand):
                    editor = cand
                    break
            if editor:
                break
            await nav_page.wait_for_timeout(300)
        if editor is None:
            raise RuntimeError("새 글쓰기 탭을 찾지 못했습니다(에디터 URL 확인 실패)")

        await editor.bring_to_front()
        await self._settle(editor, lambda: self._editor_appeared(editor), 8000)
        await self._handle_write_popup(editor, restore=False)      # 새 글로 시작

        eframe = await self._main_frame(editor)
        if "postwrite" not in (eframe.url or "").lower():
            raise RuntimeError(
                f"에디터 프레임이 아닙니다 — page={editor.url[:60]!r} "
                f"frame={(eframe.url or '')[:60]!r}"
            )
        # 단건 모드에서 쓰던 준비 대기 로직을 그대로 사용(bulk 전용 함수를 두지 않는다)
        await self._wait_editor_ready(editor)
        spots = await self._editor_spots(eframe)
        if not (spots.get("title") or spots.get("body")):
            raise RuntimeError(
                f"제목/본문 placeholder 를 찾지 못했습니다 — frame={(eframe.url or '')[:60]!r}"
            )
        self.log(f"   에디터 확인 · page={editor.url[:52]!r}")
        self.log(f"                frame={(eframe.url or '')[:52]!r}")
        return editor, eframe, spots

    async def _fill_post(self, editor, eframe, spots, landing, lframe, targets,
                         title: str, shot_name: str) -> dict:
        """이미 열려 있는 글쓰기 Page 에 제목/본문을 채우고 후처리까지 한다.

        기존 단건 로직을 그대로 옮긴 것이다(동작 변경 없음).
        """
        await editor.bring_to_front()
        ok_title = await self._type_title(editor, title, spots.get("title"))
        self.log(f"   제목 입력 {'성공' if ok_title else '실패'}")

        # 본문에 캐럿을 한 번 놓고, 이후에는 붙여넣기만 반복
        if spots.get("body"):
            await self._click_spot(editor, eframe, spots["body"])
            await editor.wait_for_timeout(400)

        eframe = await self._fresh_frame(editor, eframe)      # paste 직전 재획득
        pasted = 0
        for i, sel in enumerate(targets, 1):
            await landing.bring_to_front()
            if not await self._select_in_frame(lframe, sel):
                self.log(f"   [{i}/{len(targets)}] 선택 실패(건너뜀)")
                continue
            await landing.keyboard.press("Control+C")
            await landing.wait_for_timeout(350)
            await editor.bring_to_front()
            await editor.keyboard.press("Control+End")
            await editor.keyboard.press("Control+V")
            await editor.wait_for_timeout(1500)        # 이미지 업로드 여유
            pasted += 1
        self.log(f"   붙여넣기 {pasted}/{len(targets)} 구간")

        # 후처리(취소선 → [출처] → 하단 제품 링크) — 기존 함수 그대로
        eframe = await self._fresh_frame(editor, eframe)
        await self._cleanup_pasted(editor, eframe, spots.get("body"))

        # 본문 전체 중앙정렬(제목 제외) — READY 조건에 포함
        eframe = await self._fresh_frame(editor, eframe)
        if getattr(self, "_capture_align", False):
            # 캡처 모드: 자동 정렬을 시도하지 않고, 사용자가 직접 누르는 selector 를 기록한다.
            await editor.bring_to_front()
            await self._install_click_capture(eframe)
            self.log("")
            self.log("   [캡처] 클릭 기록을 시작했습니다. 브라우저에서 직접 정렬해 주세요.")
            wait_fn = getattr(self, "_wait_fn", None)
            if wait_fn:
                wait_fn("본문 전체를 선택하고 **가운데 정렬**을 눌러 주세요. 끝나면 Enter.")
            eframe = await self._fresh_frame(editor, eframe)
            self.log("")
            self.log("── 캡처 결과 ──")
            await self._dump_captured_clicks(eframe)
            await self._dump_align_dom(eframe)
            stat = await self._center_ratio(eframe)
            self.log(f"   [정렬] 결과 {stat['centered']}/{stat['total']} 문단 · "
                     f"툴바클래스={await self._align_state(eframe)!r}")
            centered = await self._align_is_center(eframe) or (
                stat["total"] > 0 and stat["centered"] >= max(1, int(stat["total"] * 0.8)))
        else:
            # 텍스트는 paste 시점에 이미 가운데. 이미지 섹션만 개별 정렬한다.
            centered = await self._center_images(editor, eframe)
            t = await self._text_center_ratio(eframe)
            self.log(f"   [정렬] 텍스트 문단 가운데 {t['centered']}/{t['total']} (참고)")

        eframe = await self._fresh_frame(editor, eframe)
        metrics = await self._measure_body(eframe)
        promo_left = await self._count_promo(eframe)
        src_left = await self._count_source(eframe)
        census = await self._format_census(eframe, ".se-main-container")
        img_st = await self._image_align_stats(eframe)          # 이미지 정렬 현황
        txt_st = await self._text_center_ratio(eframe)          # 텍스트 정렬 비율
        await self._shot_page(editor, shot_name)

        ok = (ok_title and metrics["len"] > 0 and metrics["strike"] == 0
              and src_left == 0 and sum((promo_left or {}).values()) == 0
              and centered)
        return {
            "ok": ok, "editor_url": editor.url,
            "editor_page": editor, "editor_frame": eframe, "title": title,
            "title_ok": ok_title, "pasted": pasted,
            "len": metrics["len"], "img": metrics["img"], "strike": metrics["strike"],
            "source_left": src_left, "promo_left": sum((promo_left or {}).values()),
            "centered": centered, "census": census,
            "img_centered": img_st.get("centered", 0), "img_sections": img_st.get("total", 0),
            "txt_centered": txt_st.get("centered", 0), "txt_total": txt_st.get("total", 0),
        }

    async def _make_one_post(self, context, nav_page, landing, lframe, targets,
                             title: str, shot_name: str) -> dict:
        """글 1건 작성(발행 없음) — 단건 실행 경로. 열기 + 채우기."""
        editor, eframe, spots = await self._open_new_write(context, nav_page)
        self.log(f"   에디터 준비 · 좌표 제목={bool(spots.get('title'))} "
                 f"본문={bool(spots.get('body'))}")
        return await self._fill_post(editor, eframe, spots, landing, lframe,
                                     targets, title, shot_name)

    async def _open_write_tab(self, context, write_url: str):
        """새 탭을 만들어 글쓰기 화면을 직접 연다(탭을 재사용하지 않는다)."""
        page = await context.new_page()
        await page.goto(write_url, wait_until="domcontentloaded", timeout=60_000)
        await self._settle(page, lambda: self._editor_appeared(page), 10_000)
        await self._handle_write_popup(page, restore=False)      # 새 글로 시작
        frame = await self._fresh_frame(page)
        if "postwrite" not in (frame.url or "").lower():
            raise RuntimeError(
                f"에디터 프레임이 아닙니다 — page={page.url[:60]!r} "
                f"frame={(frame.url or '')[:60]!r}")
        await self._wait_editor_ready(page)
        await page.bring_to_front()
        spots = await self._editor_spots(frame)
        if not (spots.get("title") or spots.get("body")):
            raise RuntimeError("제목/본문 placeholder 를 찾지 못했습니다")
        self.log(f"   새 탭 에디터 확인 · frame={(frame.url or '')[:52]!r}")
        return page, frame, spots




    # ══════════════════════════════════════════════════════════════════
    # 진단 — 사용자가 직접 가운데 정렬하면 그 selector 를 역추적한다.
    # ══════════════════════════════════════════════════════════════════
    def probe_align(self, wait_for_continue: WaitFn) -> dict:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 실행할 수 있습니다.")
        return asyncio.run(self._probe_align(wait_for_continue))

    async def _toolbar_snapshot(self, frame) -> list:
        """툴바 영역 버튼의 class/data-name/aria-pressed 스냅샷."""
        try:
            return await frame.evaluate(
                r"""() => Array.from(document.querySelectorAll('button,[role="button"],[role="menuitem"],[role="option"]'))
                     .filter(el => {
                       const r = el.getBoundingClientRect();
                       return r.width > 0 && r.height > 0 && r.y < 200;
                     })
                     .map(el => ({
                       cls: (el.className || '').toString().slice(0, 90),
                       name: el.getAttribute('data-name') || '',
                       log: el.getAttribute('data-log') || '',
                       pressed: el.getAttribute('aria-pressed') || '',
                       expanded: el.getAttribute('aria-expanded') || '',
                       txt: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 24)
                     }))"""
            )
        except Exception:  # noqa: BLE001
            return []

    async def _dump_all_menu_items(self, frame) -> None:
        """지금 화면에 보이는 버튼/메뉴 항목 전부(드롭다운 펼친 상태에서 호출)."""
        try:
            items = await frame.evaluate(
                r"""() => Array.from(document.querySelectorAll(
                       'button,[role="menuitem"],[role="option"],li a,li button,li span'))
                     .filter(el => {
                       const r = el.getBoundingClientRect();
                       const st = getComputedStyle(el);
                       return r.width > 0 && r.height > 0
                              && st.display !== 'none' && st.visibility !== 'hidden';
                     })
                     .map(el => ({
                       tag: el.tagName.toLowerCase(),
                       cls: (el.className || '').toString().slice(0, 90),
                       name: el.getAttribute('data-name') || '',
                       type: el.getAttribute('data-type') || '',
                       txt: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24),
                       y: Math.round(el.getBoundingClientRect().y)
                     }))
                     .filter(x => x.txt || x.name || /align|justify/i.test(x.cls))
                     .slice(0, 60)"""
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"   항목 덤프 실패: {type(exc).__name__}")
            return
        self.log(f"   보이는 항목 {len(items)}개 (정렬 관련만 표시)")
        for it in items:
            blob = (it["cls"] + " " + it["name"] + " " + it["txt"]).lower()
            if not ("align" in blob or "justify" in blob or "정렬" in it["txt"]):
                continue
            self.log(f"      <{it['tag']}> y={it['y']} txt={it['txt']!r} "
                     f"data-name={it['name']!r} data-type={it['type']!r}")
            self.log(f"          class={it['cls']!r}")

    async def _probe_align(self, wait_for_continue: WaitFn) -> dict:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir), headless=self.headless,
                permissions=["clipboard-read", "clipboard-write"])
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                self.log("[1/5] 네이버 로그인. 완료 후 Enter.")
                await page.goto("https://nid.naver.com/nidlogin.login",
                                wait_until="domcontentloaded")
                await self._wait_until_logged_in(page, wait_for_continue)

                self.log("[2/5] 글쓰기 진입")
                editor, eframe, spots = await self._open_new_write(context, page)

                self.log("[3/5] 정렬 대상 확보용으로 짧은 제목/본문 입력")
                await self._type_title(editor, "ALIGNPROBE", spots.get("title"))
                await self._type_body(editor, "ALIGNPROBE BODY LINE", spots.get("body"))

                eframe = await self._fresh_frame(editor, eframe)
                before = await self._toolbar_snapshot(eframe)
                self.log(f"   툴바 스냅샷(전) {len(before)}개")

                wait_for_continue(
                    "브라우저에서 **정렬 드롭다운만 클릭해서 펼쳐** 주세요"
                    "(아직 항목은 고르지 마세요). 그다음 Enter.")
                eframe = await self._fresh_frame(editor, eframe)
                self.log("")
                self.log("── 펼쳐진 정렬 메뉴 항목 ──")
                await self._dump_all_menu_items(eframe)
                await self._shot_page(editor, "align_dropdown_open")

                wait_for_continue("이제 **가운데 정렬**을 클릭해 주세요. 그다음 Enter.")
                eframe = await self._fresh_frame(editor, eframe)
                after = await self._toolbar_snapshot(eframe)
                ratio = await self._center_ratio(eframe)
                await self._shot_page(editor, "align_applied")

                self.log("")
                self.log("── 정렬 적용 후 변화 ──")
                self.log(f"   문단 중앙정렬 {ratio['centered']}/{ratio['total']}")
                bmap = {(b["cls"], b["name"]): b for b in before}
                changed = 0
                for a in after:
                    b = bmap.get((a["cls"], a["name"]))
                    if b is None:
                        self.log(f"   [신규] txt={a['txt']!r} name={a['name']!r}")
                        self.log(f"          class={a['cls']!r}")
                        changed += 1
                    elif (a["pressed"], a["expanded"]) != (b["pressed"], b["expanded"]):
                        self.log(f"   [상태변화] txt={a['txt']!r} name={a['name']!r} "
                                 f"pressed {b['pressed']!r}→{a['pressed']!r}")
                        self.log(f"          class={a['cls']!r}")
                        changed += 1
                bset = {b["cls"] for b in before}
                for a in after:
                    if a["cls"] not in bset and "align" in a["cls"].lower():
                        self.log(f"   [클래스변화] name={a['name']!r} class={a['cls']!r}")
                        changed += 1
                if not changed:
                    self.log("   변화가 감지되지 않았습니다(정렬이 적용되지 않았을 수 있음)")
                self.log("저장·발행은 하지 않았습니다.")
                return {"ratio": ratio}
            finally:
                await context.close()
                self.log("브라우저를 정상 종료했습니다.")

    # ── 가운데 정렬(드롭다운 2단계) ──────────────────────────────────
    ALIGN_DROPDOWN_SELECTORS = (
        '[data-name^="align-drop-down"]',
        "button.se-align-center-toolbar-button",
        "button.se-align-left-toolbar-button",
        "button.se-align-right-toolbar-button",
        'button[data-type="drop-down"][class*="align"]',
        '[data-log="prt.align"]',
    )




    # ── 이미지 개별 중앙정렬 ─────────────────────────────────────────
    #   텍스트는 paste 시점에 이미 가운데라 건드리지 않는다. 이미지 섹션만 처리한다.
    ALIGN_CENTER_BTN = (
        # ★2026-08-20 사용자 시연 실측 — 정렬은 2단계다.
        #   ① span.se-toolbar-icon (부모 button.se-property-toolbar-drop-down-button
        #      se-align-*) 를 눌러 드롭다운을 연다
        #   ② button.se-toolbar-option-align-center-button ('가운데 정렬') 를 누른다
        'button.se-toolbar-option-align-center-button',
        '[data-name="align"][data-type="group-toggle"][class*="se-align-center"]',
        'button.se-align-group-toggle-toolbar-button.se-align-center',
        '.se-property-toolbar-image [class*="se-align-center"]',
    )

    # 정렬 드롭다운을 여는 버튼(위 ① 단계). 실측 class 를 앞에 둔다.
    ALIGN_DROPDOWN_BTN = (
        'button.se-property-toolbar-drop-down-button[class*="se-align"]',
        'button[class*="se-property-toolbar-drop-down-button"] span.se-toolbar-icon',
        'button.se-property-toolbar-drop-down-button',
    )

    async def _image_align_stats(self, frame) -> dict:
        """이미지 섹션의 정렬 현황."""
        try:
            return await frame.evaluate(
                r"""() => {
                     const root = document.querySelector('.se-main-container') || document.body;
                     const secs = [...root.querySelectorAll('[class*="se-section-image"]')];
                     const centered = secs.filter(
                       s => (s.className || '').indexOf('se-section-align-center') >= 0).length;
                     return {total: secs.length, centered};
                   }"""
            )
        except Exception:  # noqa: BLE001
            return {"total": 0, "centered": 0}

    async def _mark_next_left_image(self, frame) -> dict:
        """아직 가운데가 아닌 이미지 섹션 하나를 표시하고 좌표를 돌려준다."""
        try:
            return await frame.evaluate(
                r"""() => {
                     const root = document.querySelector('.se-main-container') || document.body;
                     document.querySelectorAll('[data-blg-img]')
                       .forEach(e => e.removeAttribute('data-blg-img'));
                     const sec = [...root.querySelectorAll('[class*="se-section-image"]')]
                       .find(s => (s.className || '').indexOf('se-section-align-center') < 0);
                     if (!sec) return {found: false};
                     const img = sec.querySelector('img') || sec;
                     img.setAttribute('data-blg-img', '1');
                     sec.scrollIntoView({block: 'center'});
                     const r = img.getBoundingClientRect();
                     return {found: true,
                             x: Math.round(r.x + r.width / 2),
                             y: Math.round(r.y + r.height / 2),
                             cls: (sec.className || '').toString().slice(0, 70)};
                   }"""
            )
        except Exception:  # noqa: BLE001
            return {"found": False}

    async def _click_image_align_center(self, frame) -> bool:
        """이미지 선택 상태에서 나타난 툴바의 '가운데 정렬'을 누른다.

        실측(시연 기록 [38]→[39]): 정렬은 드롭다운을 연 뒤 옵션을 고르는 2단계다.
        기존처럼 한 번에 눌리는 UI 도 있으므로 ① 직접 클릭을 먼저 시도하고,
        실패하면 ② 드롭다운을 열고 다시 시도한다.
        """
        async def _try_center() -> bool:
            for sel in self.ALIGN_CENTER_BTN:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        return True
                except Exception:  # noqa: BLE001
                    continue
            return False

        if await _try_center():
            return True
        for sel in self.ALIGN_DROPDOWN_BTN:          # ① 드롭다운 열기
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    await frame.wait_for_timeout(350)
                    if await _try_center():          # ② 가운데 정렬
                        return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _center_images(self, page, frame, max_rounds: int = 40) -> bool:
        """이미지를 하나씩 선택해 가운데 정렬. 텍스트/제목은 건드리지 않는다."""
        frame = await self._fresh_frame(page, frame)
        st = await self._image_align_stats(frame)
        if st["total"] == 0:
            self.log("   [정렬] 이미지 없음 — 건너뜀")
            return True
        self.log(f"   [정렬] 이미지 {st['total']}개 중 가운데 {st['centered']}개 — 나머지 처리")

        done = 0
        for _ in range(max_rounds):
            frame = await self._fresh_frame(page, frame)
            spot = await self._mark_next_left_image(frame)
            if not spot.get("found"):
                break
            await page.wait_for_timeout(250)
            # ① 이미지 클릭(선택) → 이미지 전용 툴바가 뜬다
            try:
                await frame.locator('[data-blg-img="1"]').first.click(timeout=2500)
            except Exception:
                await self._click_xy_in_frame(page, frame, spot["x"], spot["y"])
            await page.wait_for_timeout(500)

            # ② 가운데 정렬 클릭
            if not await self._click_image_align_center(frame):
                self.log("   [정렬] 이미지 툴바의 가운데 정렬 버튼을 찾지 못했습니다")
                break
            await page.wait_for_timeout(500)
            done += 1

        frame = await self._fresh_frame(page, frame)
        st2 = await self._image_align_stats(frame)
        ok = st2["centered"] >= st2["total"]
        self.log(f"   [정렬] 이미지 가운데 {st2['centered']}/{st2['total']} "
                 f"(처리 {done}회) " + ("✅" if ok else "❌"))
        return ok

    async def _text_center_ratio(self, frame) -> dict:
        """텍스트 문단 중 가운데 정렬 비율(참고용)."""
        try:
            return await frame.evaluate(
                r"""() => {
                     const root = document.querySelector('.se-main-container') || document.body;
                     const ps = [...root.querySelectorAll('p.se-text-paragraph')]
                       .filter(e => (e.innerText || '').trim());
                     const c = ps.filter(
                       e => (e.className || '').indexOf('align-center') >= 0).length;
                     return {total: ps.length, centered: c};
                   }"""
            )
        except Exception:  # noqa: BLE001
            return {"total": 0, "centered": 0}

    # ── 정렬 클릭 캡처(사용자가 직접 누르는 selector 를 기록) ──────────
    async def _install_click_capture(self, frame) -> bool:
        """클릭을 가로채 요소 경로를 window.__blgClicks 에 쌓는다(캡처 단계라 메뉴가 닫혀도 남는다)."""
        try:
            await frame.evaluate(
                r"""() => {
                     if (window.__blgCapOn) return;
                     window.__blgCapOn = true;
                     window.__blgClicks = [];
                     const path = (el) => {
                       const out = [];
                       for (let n = el, i = 0; n && i < 5; n = n.parentElement, i++) {
                         let s = n.tagName.toLowerCase();
                         if (n.id) s += '#' + n.id;
                         const c = (n.className || '').toString().trim()
                                   .split(/\s+/).filter(Boolean).join('.');
                         if (c) s += '.' + c;
                         const dn = n.getAttribute && n.getAttribute('data-name');
                         if (dn) s += '[data-name="' + dn + '"]';
                         out.unshift(s);
                       }
                       return out.join(' > ');
                     };
                     document.addEventListener('click', (e) => {
                       const t = e.target;
                       window.__blgClicks.push({
                         path: path(t),
                         tag: t.tagName.toLowerCase(),
                         name: t.getAttribute('data-name') || '',
                         cls: (t.className || '').toString(),
                         txt: (t.innerText || t.textContent || '')
                                .replace(/\s+/g, ' ').trim().slice(0, 24)
                       });
                     }, true);
                   }"""
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [캡처] 리스너 설치 실패: {type(exc).__name__}")
            return False

    async def _dump_captured_clicks(self, frame) -> None:
        try:
            clicks = await frame.evaluate("() => window.__blgClicks || []")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [캡처] 읽기 실패: {type(exc).__name__}")
            return
        self.log(f"   [캡처] 클릭 {len(clicks)}건 기록")
        for i, c in enumerate(clicks, 1):
            self.log(f"      [{i}] <{c['tag']}> txt={c['txt']!r} data-name={c['name']!r}")
            self.log(f"          class={c['cls'][:90]!r}")
            self.log(f"          path={c['path'][:150]}")

    async def _dump_align_dom(self, frame) -> None:
        """align 관련 요소를 숨김 포함 전부 덤프."""
        try:
            items = await frame.evaluate(
                r"""() => [...document.querySelectorAll('*')]
                     .filter(e => {
                       const n = (e.getAttribute && e.getAttribute('data-name')) || '';
                       const c = (e.className || '').toString();
                       return /align/i.test(n) || /align/i.test(c);
                     })
                     .slice(0, 30)
                     .map(e => ({
                       tag: e.tagName.toLowerCase(),
                       name: (e.getAttribute('data-name') || ''),
                       type: (e.getAttribute('data-type') || ''),
                       cls: (e.className || '').toString().slice(0, 90),
                       txt: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 16)
                     }))"""
            )
        except Exception:  # noqa: BLE001
            return
        self.log(f"   [DOM] align 관련 요소 {len(items)}개(숨김 포함)")
        for it in items:
            self.log(f"      <{it['tag']}> name={it['name']!r} type={it['type']!r} "
                     f"txt={it['txt']!r}")
            self.log(f"          class={it['cls']!r}")

    async def _align_state(self, frame) -> str:
        """정렬 드롭다운 버튼의 클래스 — 현재 정렬 상태가 여기 드러난다."""
        try:
            return await frame.evaluate(
                r"""() => {
                     const el = document.querySelector('[data-name^="align-drop-down"]');
                     return el ? (el.className || '').toString() : '';
                   }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _align_is_center(self, frame) -> bool:
        return "se-align-center-toolbar-button" in (await self._align_state(frame))

    async def _open_align_dropdown(self, frame) -> bool:
        """툴바의 '정렬 열기' 드롭다운을 펼친다."""
        for sel in self.ALIGN_DROPDOWN_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    self.log(f"   [정렬] 드롭다운 열기: {sel}")
                    return True
            except Exception:  # noqa: BLE001
                continue
        self.log("   [정렬] 정렬 드롭다운 버튼을 찾지 못했습니다")
        return False

    async def _click_align_center_option(self, frame) -> bool:
        """'가운데 정렬' 항목을 누른다.

        ★표시 여부로 걸러내지 않는다(펼침 상태가 유지되지 않아 놓쳤던 원인).
          data-name 에 align-center 가 있는 요소를 최우선으로 찾고,
          없으면 class/텍스트로 찾는다. 드롭다운 버튼 자체는 제외한다.
        """
        try:
            info = await frame.evaluate(
                r"""() => {
                     const all = Array.from(document.querySelectorAll(
                         'button,[role="menuitem"],[role="option"],a,li,span,div'));
                     const isDropdown = (el) =>
                       (el.getAttribute('data-type') || '') === 'drop-down'
                       || (el.getAttribute('data-name') || '').indexOf('drop-down') >= 0;

                     const scored = [];
                     let idx = 0;
                     all.forEach(el => {
                       const name = el.getAttribute('data-name') || '';
                       const cls = (el.className || '').toString();
                       const txt = (el.innerText || el.textContent || '')
                                     .replace(/\s+/g, ' ').trim();
                       if (isDropdown(el)) return;
                       let score = 0;
                       if (/align[-_]?center/i.test(name)) score = 3;
                       else if (/align[-_]?center/i.test(cls)) score = 2;
                       else if (txt === '가운데 정렬') score = 1;
                       if (!score) return;
                       // ★툴팁/라벨 span 은 클릭 대상이 아니다 — 실제 버튼(부모)으로 올라간다.
                       let target = el;
                       if (/se-toolbar-tooltip|se-blind/.test(cls)
                           || !/^(button|a)$/i.test(el.tagName)) {
                         const up = el.closest('button,[role="menuitem"],[role="option"],a');
                         if (up && !isDropdown(up)) target = up;
                       }
                       if (target.getAttribute('data-blg-alignopt')) return;   // 중복 방지
                       el = target;
                       const name2 = el.getAttribute('data-name') || '';
                       const cls2 = (el.className || '').toString();
                       el.setAttribute('data-blg-alignopt', String(idx));
                       scored.push({i: idx, score, name: name2.slice(0, 40),
                                    cls: cls2.slice(0, 80), txt: txt.slice(0, 20),
                                    tag: el.tagName.toLowerCase()});
                       idx += 1;
                     });
                     scored.sort((a, b) => b.score - a.score);
                     return {cands: scored.slice(0, 10)};
                   }"""
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [정렬] 항목 탐색 실패: {type(exc).__name__}")
            return False

        cands = info.get("cands", [])
        if not cands:
            self.log("   [정렬] '가운데 정렬' 항목을 찾지 못했습니다")
            return False
        for c in cands[:5]:
            self.log(f"      후보 <{c['tag']}> score={c['score']} txt={c['txt']!r} "
                     f"data-name={c['name']!r}")
            self.log(f"          class={c['cls']!r}")

        # 점수 높은 순으로 클릭 시도. 클래스 신호가 center 로 바뀌면 성공.
        for c in cands[:5]:
            try:
                loc = frame.locator(f"[data-blg-alignopt='{c['i']}']").first
                await loc.click(timeout=1800)
                await frame.page.wait_for_timeout(500)
                if await self._align_is_center(frame):
                    self.log(f"   [정렬] 적용 확인 — 항목 <{c['tag']}> "
                             f"data-name={c['name']!r}")
                    return True
                self.log(f"      클릭했지만 상태 변화 없음: {c['name'] or c['cls'][:40]!r}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"      클릭 실패({c['name'] or c['tag']}): {type(exc).__name__}")
        return False

    # ── 본문 전체 중앙정렬 ───────────────────────────────────────────
    async def _center_body(self, page, frame, spot: dict | None) -> bool:
        """본문 전체를 선택해 중앙정렬. 제목은 건드리지 않는다.

        본문 영역을 클릭한 뒤 Ctrl+A 를 쓰므로 선택은 본문에 한정된다.
        적용 후 실제 정렬 상태를 읽어 검증한다.
        """
        frame = await self._fresh_frame(page, frame)
        if spot:
            await self._click_spot(page, frame, spot)
            await page.wait_for_timeout(300)
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(400)

        # ★2단계: '정렬 열기' 드롭다운을 펼친 뒤 '가운데 정렬' 항목을 누른다.
        if not await self._open_align_dropdown(frame):
            return False
        await page.wait_for_timeout(600)
        if not await self._click_align_center_option(frame):
            return False
        await page.wait_for_timeout(600)
        await page.keyboard.press("Control+End")          # 선택 해제
        await page.wait_for_timeout(300)

        frame = await self._fresh_frame(page, frame)
        stat = await self._center_ratio(frame)
        by_class = await self._align_is_center(frame)          # 툴바 클래스 신호
        by_style = stat["total"] > 0 and stat["centered"] >= max(1, int(stat["total"] * 0.8))
        ok = by_class or by_style
        self.log(f"   [정렬] 중앙정렬 {stat['centered']}/{stat['total']} 문단 · "
                 f"툴바신호={'center' if by_class else '아님'} "
                 + ("✅" if ok else "❌"))
        return ok

    async def _center_ratio(self, frame) -> dict:
        """본문 문단 중 가운데 정렬된 비율(검증용)."""
        try:
            return await frame.evaluate(
                r"""() => {
                     const root = document.querySelector('.se-main-container') || document.body;
                     const ps = Array.from(root.querySelectorAll('p'))
                       .filter(el => (el.innerText || '').trim().length > 0);
                     let c = 0;
                     ps.forEach(el => {
                       const ta = getComputedStyle(el).textAlign;
                       if (ta === 'center') c += 1;
                     });
                     return {total: ps.length, centered: c};
                   }"""
            )
        except Exception:  # noqa: BLE001
            return {"total": 0, "centered": 0}

    # ── 발행(순차) ───────────────────────────────────────────────────
    #   ★되돌릴 수 없다. publish=True 로 명시했을 때만 호출된다.
    PUBLISH_LABELS = ("발행", "publish")

    async def _publish_candidates(self, page) -> list:
        """현재 화면에서 '발행'으로 보이는 클릭 후보를 모은다(페이지+모든 프레임).
        정확히 '발행' 텍스트인 것 → confirm/ok 계열 클래스 → 그 외 순으로 정렬."""
        out = []
        for scope in [page] + list(page.frames):
            try:
                els = await scope.query_selector_all(
                    "button, a[role='button'], a, [role='button'], [class*='publish'], [class*='confirm']")
            except Exception:  # noqa: BLE001
                continue
            for el in els:
                try:
                    if not await el.is_visible():
                        continue
                    info = await el.evaluate(
                        """e => ({t:(e.innerText||e.textContent||'').trim().slice(0,20),
                                  c:(e.className||'').toString().slice(0,60),
                                  id:e.id||'', tag:e.tagName.toLowerCase()})""")
                except Exception:  # noqa: BLE001
                    continue
                txt, cls = info.get("t", ""), info.get("c", "")
                if "발행" not in txt and "publish" not in cls.lower() and "confirm" not in cls.lower():
                    continue
                rank = 0 if txt == "발행" else (1 if "confirm" in cls.lower() else 2)
                out.append({"handle": el, "rank": rank,
                            "desc": f"{info['tag']}.{cls[:28]}#{info['id'][:14]} '{txt}'"})
        out.sort(key=lambda x: x["rank"])
        return out

    async def _dump_publish_layer(self, page, idx: int) -> None:
        """발행 레이어를 못 찾았을 때 화면의 클릭 가능한 요소를 전부 로그+스크린샷으로 남긴다.
        (선택자를 추측하지 않기 위한 실측 자료 — memory 원칙)"""
        await self._shot(page, f"publish_layer_{idx}")   # 프로젝트 공통 스크린샷 헬퍼 사용
        for scope in [page] + list(page.frames):
            try:
                rows = await scope.evaluate(
                    """() => Array.from(document.querySelectorAll("button,a,[role='button']"))
                         .filter(e => e.offsetParent !== null)
                         .slice(0, 60)
                         .map(e => `${e.tagName.toLowerCase()}|${(e.className||'').toString().slice(0,50)}`
                                   + `|${e.id||''}|${(e.innerText||'').trim().slice(0,18)}`)""")
            except Exception:  # noqa: BLE001
                continue
            if rows:
                self.log(f"      [진단] frame={scope.url[:50]} 보이는 클릭요소 {len(rows)}개")
                for r in rows:
                    self.log(f"         {r}")

    async def _disable_comments(self, page) -> bool:
        """발행 레이어에서 '댓글 허용'을 끈다(2026-08-20 사용자 요청).
        이미 꺼져 있으면 건드리지 않는다(토글이라 다시 누르면 켜진다). 못 찾으면 False + 덤프."""
        for scope in [page] + list(page.frames):
            try:
                found = await scope.evaluate(
                    r"""() => {
                      const txt = (el) => (el.innerText || el.textContent || '')
                            .replace(/\s+/g, ' ').trim();
                      const boxes = Array.from(document.querySelectorAll("input[type='checkbox']"))
                            .filter(b => b.offsetParent !== null || b.closest('label'));
                      for (const b of boxes) {
                        let label = '';
                        if (b.id) {
                          const l = document.querySelector(`label[for="${b.id}"]`);
                          if (l) label = txt(l);
                        }
                        if (!label && b.closest('label')) label = txt(b.closest('label'));
                        if (!label && b.parentElement) label = txt(b.parentElement);
                        if (label.includes('댓글')) {
                          b.setAttribute('data-blg-comment', '1');
                          return {checked: b.checked, label: label.slice(0, 30), id: b.id || ''};
                        }
                      }
                      return null;
                    }""")
            except Exception:  # noqa: BLE001
                continue
            if not found:
                continue
            if not found["checked"]:
                self.log(f"      [발행옵션] 댓글 허용 이미 꺼짐 — 그대로 둠 ({found['label']!r})")
                return True
            try:
                await scope.locator("[data-blg-comment='1']").first.click(timeout=3000, force=True)
                await page.wait_for_timeout(400)
                still = await scope.evaluate(
                    "() => { const b=document.querySelector(\"[data-blg-comment='1']\");"
                    " return b ? b.checked : null; }")
                if still is False:
                    self.log(f"      [발행옵션] 댓글 허용 해제 ✅ ({found['label']!r})")
                    return True
                self.log(f"      [발행옵션] 댓글 체크 해제 실패(여전히 checked={still})")
            except Exception as exc:  # noqa: BLE001
                self.log(f"      [발행옵션] 댓글 해제 클릭 실패: {type(exc).__name__}")
        self.log("      [발행옵션] '댓글' 체크박스를 찾지 못함 — 화면 요소를 덤프합니다")
        await self._dump_publish_layer(page, 902)
        return False

    async def _publish_one(self, page, idx: int, total: int) -> dict:
        """READY 탭 1개를 발행하고 게시글 URL을 확보한다."""
        before_url = page.url
        # 블로그 ID 기록 — 발행 후 RSS로 주소를 걷을 때 쓴다(blog.naver.com/{id}/postwrite).
        m = re.search(r"blog\.naver\.com/([^/?#]+)/", before_url or "")
        if m and not getattr(self, "_blog_id", ""):
            self._blog_id = m.group(1)
        await page.bring_to_front()
        frame = await self._fresh_frame(page)

        # ① 상단 '발행' 버튼 → ② 발행 레이어의 확정 '발행' 버튼
        #   ⚠️ 2단계에서 1단계와 같은 선택자를 쓰면 방금 누른 상단 버튼을 다시 눌러 레이어가 닫힌다
        #      (2026-08-20 실패 원인). 그래서 1단계에서 누른 요소를 기억해 2단계에서 제외한다.
        first_el = None
        for step_no in (1, 2):
            clicked = False
            cands = await self._publish_candidates(page)
            for c in cands:
                if step_no == 2 and first_el is not None:
                    try:
                        if await c["handle"].evaluate("(e, o) => e === o", first_el):
                            continue                      # 1단계에서 누른 그 버튼은 건너뛴다
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    await c["handle"].click(timeout=3000)
                    clicked = True
                    if step_no == 1:
                        first_el = c["handle"]
                    self.log(f"      발행 버튼 {step_no}단계 클릭 — {c['desc']}")
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                self.log(f"      발행 버튼 {step_no}단계 없음 (후보 {len(cands)}개)")
                await self._dump_publish_layer(page, idx)
                if step_no == 1:
                    return {"ok": False, "error": "발행 버튼을 찾지 못했습니다"}
            await page.wait_for_timeout(1500)
            if step_no == 1:
                # 레이어가 열린 상태에서 확정 발행 전에 옵션을 손본다(댓글 허용 OFF).
                await self._disable_comments(page)

        # 게시글 페이지로 이동할 때까지 대기(URL 이 PostView/logNo 형태로 바뀐다)
        moved = False
        for _ in range(40):                                # 최대 20초
            u = page.url or ""
            if u != before_url and ("logno" in u.lower() or "postview" in u.lower()
                                    or "Redirect=Write" not in u):
                moved = True
                break
            await page.wait_for_timeout(500)
        url = page.url
        if not moved:
            return {"ok": False, "error": f"게시글 페이지로 이동하지 않았습니다(url={url[:70]})"}
        url = self._clean_post_url(url)
        self.log(f"      발행 URL: {url[:80]}")
        return {"ok": True, "url": url}

    @staticmethod
    def _clean_post_url(url: str) -> str:
        """PostView.naver?blogId=X&...&logNo=N → https://blog.naver.com/X/N.
        시트에 남는 주소라 사람이 읽을 수 있는 형태로 맞춘다(이전 RSS 수집이 하던 정규화)."""
        u = url or ""
        if "postview" not in u.lower():
            return u
        bid = re.search(r"blogId=([^&#]+)", u, re.I)
        no = re.search(r"logNo=(\d+)", u, re.I)
        if bid and no:
            return f"https://blog.naver.com/{bid.group(1)}/{no.group(1)}"
        return u

    # 발행 사이 대기(초) — 사람이 하는 것처럼 불규칙하게(2026-08-20 사용자 요청).
    #   고정 간격은 기계적으로 보여 스팸 판정 위험이 있어 매 건 무작위로 고른다.
    PUBLISH_DELAYS = (5, 10, 20, 30)

    async def _publish_ready(self, ready_pages: list, wait_sec: float | None = None) -> list:
        """READY 탭을 1번부터 순차 발행. 실패해도 다음 글을 계속 처리한다.
        wait_sec 를 주면 고정, 생략하면 PUBLISH_DELAYS 에서 매 건 무작위."""
        out = []
        total = len(ready_pages)
        for i, page in enumerate(ready_pages, 1):
            self.log("")
            self.log(f"── [{i}/{total}] 발행 ──")
            try:
                if page.is_closed():
                    self.log("      이미 닫힌 탭 — 건너뜁니다")
                    out.append({"ok": False, "error": "탭이 닫혀 있음"})
                    continue
                r = await self._publish_one(page, i, total)
            except Exception as exc:  # noqa: BLE001
                r = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            if r.get("ok"):
                self.log(f"[{i}/{total}] 발행 ✅")
                try:
                    await page.close()                     # 발행 성공한 탭만 닫는다
                    self.log("      탭 닫음")
                except Exception:  # noqa: BLE001
                    pass
            else:
                self.log(f"[{i}/{total}] 발행 ❌ ERROR — {r.get('error')}")
            out.append(r)
            if i < total:
                delay = wait_sec if wait_sec is not None else random.choice(self.PUBLISH_DELAYS)
                self.log(f"      다음 발행까지 {delay}초 대기(랜덤)")
                await asyncio.sleep(delay)   # 발행 성공 탭은 닫히므로 page 타이머 대신 asyncio
        return out

    # 본문 root 후보(2026-08-20 확장). 수정 화면(PostUpdateForm)은 새 글 화면과 DOM 이 달라
    #   .se-main-container 하나만 보면 못 찾는다 → 후보를 넓히고 **모든 frame** 을 훑는다.
    BODY_ROOT_SELECTORS = (
        ".se-main-container",
        ".se-content",
        ".se-viewer .se-main-container",
        "[contenteditable='true'].se-main-container",
        ".se-module-text",
        ".se-component-content",
        "div[contenteditable='true']",
        "[contenteditable='true']",
    )

    async def _find_body_root(self, page, frame=None, wait_sec: int = 15) -> tuple:
        """본문 root 를 가진 (frame, selector, 미리보기텍스트) 를 찾는다. 못 찾으면 (None, '', '').

        ★편집 가능한 본문을 최우선으로 고른다(2026-08-20 실측).
          수정 화면에는 읽기 전용 뷰어(.se-content 등)가 함께 있는데, 거기서 복사하면
          제목·'본 콘텐츠의 광고주는 …' 광고표시가 섞여 들어오고 **이미지가 하나도 안 붙는다**
          (실측: 원본 이미지 5개 → 붙여넣기 0개). 그래서 contenteditable 안에 있는 후보를 먼저 쓴다.
        ★에디터 본문은 늦게 채워지므로 wait_sec 동안 폴링한다(빈 껍데기를 잡지 않게)."""
        import time as _t
        deadline = _t.time() + max(1, wait_sec)
        fallback = None                       # 편집불가(뷰어) 후보 — 정말 없을 때만 사용
        while True:
            scopes = []
            if frame is not None:
                scopes.append(frame)
            scopes += [page.main_frame] + [f for f in page.frames]
            seen = set()
            for sc in scopes:
                key = id(sc)
                if key in seen:
                    continue
                seen.add(key)
                for sel in self.BODY_ROOT_SELECTORS:
                    try:
                        info = await sc.evaluate(
                            r"""(sel) => {
                                 const els = Array.from(document.querySelectorAll(sel));
                                 for (const el of els) {
                                   const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
                                   const imgs = el.querySelectorAll('img').length;
                                   if (txt.length < 20 && imgs === 0) continue;
                                   const editable = !!(el.closest("[contenteditable='true']")
                                                    || el.getAttribute('contenteditable') === 'true');
                                   return {ok: true, len: txt.length, imgs, editable,
                                           head: txt.slice(0, 60)};
                                 }
                                 return {ok: false};
                               }""", sel)
                    except Exception:  # noqa: BLE001
                        continue
                    if not (info and info.get("ok")):
                        continue
                    kind = "편집영역" if info.get("editable") else "읽기전용(뷰어)"
                    if info.get("editable"):
                        self.log(f"   [본문] root 발견 — selector={sel} @{(sc.url or '')[:44]} "
                                 f"· {kind} (텍스트 {info['len']}자 · 이미지 {info['imgs']}개)")
                        self.log(f"   [본문] 앞부분: {info['head']!r}")
                        return sc, sel, info["head"]
                    if fallback is None:
                        fallback = (sc, sel, info["head"], info)
            if _t.time() >= deadline:
                break
            await page.wait_for_timeout(1000)     # 에디터 본문이 채워질 때까지 대기

        if fallback:
            sc, sel, head, info = fallback
            # ★여기서 고른 후보는 **복사 대상이 아니다**(2026-08-20 사용자 지시).
            #   글자수/이미지수 세기와 붙여넣기 후 대조용 텍스트를 읽는 데만 쓴다.
            #   실제 복사는 _copy_master_body 가 이 root 를 Range 로 선택해서 한다.
            self.log(f"   [본문] 측정용 컨테이너 — selector={sel} "
                     f"(텍스트 {info['len']}자 · 이미지 {info['imgs']}개)")
            self.log(f"   [본문] 앞부분: {head!r}")
            return sc, sel, head
        return None, "", ""

    async def _dump_body_diagnosis(self, page) -> None:
        """본문 root 를 못 찾았을 때 프레임·DOM 현황을 전부 남긴다(추측 금지용)."""
        self.log("   [진단] ── 본문 root 탐색 실패 · DOM 현황 ──")
        self.log(f"   [진단] page.url = {(page.url or '')[:100]}")
        for i, sc in enumerate([page.main_frame] + list(page.frames)):
            try:
                r = await sc.evaluate(
                    r"""() => {
                      const ce = document.querySelectorAll("[contenteditable='true']").length;
                      const se = {};
                      ["se-main-container","se-content","se-component-content",
                       "se-text-paragraph","se-module-text","se-viewer"].forEach(c => {
                         const n = document.querySelectorAll('.' + c).length; if (n) se[c] = n; });
                      const body = (document.body ? document.body.innerText : '')
                            .replace(/\s+/g,' ').trim().slice(0, 80);
                      const big = Array.from(document.querySelectorAll('div,section,article'))
                        .filter(e => (e.innerText || '').trim().length > 100)
                        .slice(0, 5)
                        .map(e => `${e.tagName.toLowerCase()}.${(e.className||'').toString().slice(0,40)}`);
                      return {ce, se, body, big};
                    }""")
            except Exception as exc:  # noqa: BLE001
                self.log(f"   [진단] frame#{i} {(sc.url or '')[:60]} → 평가실패 {type(exc).__name__}")
                continue
            self.log(f"   [진단] frame#{i} {(sc.url or '')[:60]}")
            self.log(f"            contenteditable={r['ce']} · se클래스={r['se']}")
            if r.get("body"):
                self.log(f"            본문텍스트: {r['body']!r}")
            for b in r.get("big") or []:
                self.log(f"            텍스트많은요소: {b}")
    # ── 에디터 본문 포커스 (2026-08-20 재작성) ─────────────────────────
    #   ★새 selector 를 만들지 않는다. 제목/본문 입력·구간 붙여넣기에서 **이미 동작이
    #     검증된** 경로를 그대로 쓴다:
    #        frame = _fresh_frame(page)      (name='mainFrame' 또는 url 에 postwrite)
    #        spot  = _editor_spots(frame)['body']
    #        _click_spot(page, frame, spot)  (frame offset + mouse.click)
    #     수정 화면(PostUpdateForm)도 같은 스마트에디터라 같은 방식으로 캐럿이 잡힌다.
    #   ⚠️ 폐기(2026-08-20): body[contenteditable] frame 탐색 · .se-content 읽기전용 복사.
    #     frame 을 URL(want='PostUpdateForm'/'PostWriteForm')로 걸렀는데 실제 에디터
    #     frame url 은 '/postwrite' / 'PostUpdateForm.naver' 라 want 와 안 맞아
    #     **매번 '편집영역을 못 찾음'** 이 됐다. 그게 이 버그의 원인이었다.
    async def _focus_editor_body(self, page, want: str = "", frame=None) -> bool:
        """본문에 캐럿을 놓는다. want 는 로그용 이름일 뿐 frame 선택에 쓰지 않는다."""
        frame = await self._fresh_frame(page, frame)
        spots = await self._editor_spots(frame)
        spot = spots.get("body")
        if not spot:
            self.log(f"   [포커스] 본문 자리를 찾지 못함 ({want or 'editor'}) "
                     f"frame={(frame.url or '')[:52]!r}")
            return False
        await self._click_spot(page, frame, spot)
        active = await self._active_element(frame)
        self.log(f"   [포커스] 본문 클릭 <{spot['tag']}> box={spot['box']} "
                 f"cls={spot['cls']!r} → activeElement={active}")
        return True

    SELECTION_JS = r"""() => {
             const s = document.getSelection();
             if (!s || s.rangeCount === 0) return {chars: 0, imgs: 0};
             const d = document.createElement('div');
             d.appendChild(s.getRangeAt(0).cloneContents());
             return {chars: (s.toString() || '').length,
                     imgs: d.querySelectorAll('img').length};
           }"""

    async def _selection_stats(self, page, want: str = "", frame=None) -> dict:
        """현재 선택 영역의 글자수/이미지수(복사 전 검증용).
        클릭한 frame 을 먼저 보고, 비어 있으면 나머지 frame 도 훑어 '어디에 잡혔는지' 남긴다."""
        best = {"chars": 0, "imgs": 0}
        seen = set()
        for sc in ([frame] if frame is not None else []) + list(page.frames):
            if sc is None or id(sc) in seen:
                continue
            seen.add(id(sc))
            try:
                r = await sc.evaluate(self.SELECTION_JS)
            except Exception:  # noqa: BLE001
                continue
            if (r or {}).get("chars", 0) > best["chars"]:
                best = {"chars": r["chars"], "imgs": r.get("imgs", 0),
                        "frame": (sc.url or "")[:52]}
        return best

    # 붙여넣은 본문에 이런 문자열이 있으면 기준글이 아니라 남아 있던 클립보드다.
    #   (2026-08-20 사고: 실행 전 CMD 명령어가 그대로 새 글에 붙여넣어졌다.)
    FORBIDDEN_TEXT = (
        "powershell", "python.exe", "main.py", "--paste", "--edit-copy",
        "cd c:" + chr(92), ".venv" + chr(92) + "scripts", "chcp 65001", "@echo off",
    )

    # 본문 텍스트 읽기 — 새 글은 .se-main-container, 수정 화면은 _mark_master 가 달아 둔 마커.
    BODY_TEXT_JS = r"""() => {
             const r = document.querySelector('[data-blg-master="1"]')
                    || document.querySelector('.se-main-container')
                    || document.body;
             return r ? (r.innerText || '').trim() : '';
           }"""

    async def _read_body_text(self, page, want: str = "", frame=None) -> str:
        """에디터 본문 텍스트를 DOM 에서 직접 읽는다(복사/붙여넣기 검증의 기준값)."""
        frame = await self._fresh_frame(page, frame)
        try:
            return await frame.evaluate(self.BODY_TEXT_JS)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _squash(t: str) -> str:
        """공백/개행 차이를 무시하고 비교하기 위해 압축."""
        return "".join((t or "").split())

    def _text_match(self, source: str, target: str) -> tuple:
        """기준글 본문과 붙여넣은 본문이 충분히 일치하는가. (ok, 사유)"""
        low = (target or "").lower()
        hit = next((b for b in self.FORBIDDEN_TEXT if b.lower() in low), "")
        if hit:
            return False, "명령어 문자열 %r 포함 — 기존 클립보드가 붙여넣어짐" % (hit,)
        a, b = self._squash(source), self._squash(target)
        if not a:
            return False, "기준글 본문을 읽지 못함"
        if not b:
            return False, "새 글 본문이 비어 있음"
        import difflib
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        # 네이버가 공백/특수문자를 약간 바꾸므로 완전 일치는 요구하지 않는다.
        if a[:80] and a[:80] in b:
            return True, "앞 80자 일치 · 유사도 %.2f (%d자 → %d자)" % (ratio, len(a), len(b))
        if ratio >= 0.80:
            return True, "유사도 %.2f (%d자 → %d자)" % (ratio, len(a), len(b))
        return False, "본문 불일치 — 유사도 %.2f (기준 %d자 / 새 글 %d자)" % (ratio, len(a), len(b))

    # ── 기준글 본문 복사 (2026-08-20 재작성: 단일 경로) ────────────────
    #   ⚠️ 폐기: 본문 component 클릭 → Ctrl+A.
    #      클릭이 se-image 컴포넌트에 걸리면 activeElement 가 body(ce=false) 가 되고,
    #      그 상태의 Ctrl+A 는 '편집영역 전체'가 아니라 **페이지 전체**를 선택한다.
    #      (흑자 기준글에서 제목·상단 메뉴까지 통째로 들어왔다. 팔자는 DOM 구조상 우연히 통과.)
    #   ★단일 경로: 이미 찾아 둔 본문 root([data-blg-master="1"]) 의 자식 컴포넌트만
    #      Range 로 잡는다. 제목 컴포넌트(.se-documentTitle)는 Range 시작점에서 제외한다.

    # 본문 컴포넌트 판별 + Range 선택 — 한 번의 evaluate 로 끝낸다.
    #   ★index 로 판단하지 않는다(2026-08-20 사용자 지시).
    #     '첫 컴포넌트=제목' 같은 순서 가정은 결핍이 바뀌어 DOM 순서가 달라지면 바로 깨진다.
    #     각 컴포넌트의 **실제 글자수 / 이미지 개수 / 종류**만 보고 본문인지 판단한다.
    #   제외 대상: 제목(.se-documentTitle) · '추가할 컴포넌트를 선택하세요.' 같은 자리표시자
    #             · 글자도 이미지도 없는 빈 컴포넌트
    MIN_COMPONENT_CHARS = 10          # 이만큼도 안 되고 이미지도 없으면 본문으로 안 본다
    PLACEHOLDER_TEXTS = ("추가할 컴포넌트를 선택하세요", "컴포넌트를 선택하세요")

    # ① 판별 + 표시 — 어디를 클릭해 포커스를 줄지, 어디부터 어디까지 잡을지 표시만 한다.
    MASTER_SCAN_JS = r"""(opt) => {
             const root = document.querySelector('[data-blg-master="1"]');
             if (!root) return {ok: false, why: '본문 root 마커를 찾지 못함'};
             ['data-blg-from', 'data-blg-to', 'data-blg-click'].forEach(a =>
               document.querySelectorAll('[' + a + ']')
                 .forEach(e => e.removeAttribute(a)));

             // 최상위 컴포넌트(중첩된 se-component 는 부모만)
             let comps = Array.from(root.querySelectorAll('.se-component'))
               .filter(el => {
                 const par = el.parentElement && el.parentElement.closest('.se-component');
                 return !par || !root.contains(par);
               });
             if (!comps.length) comps = Array.from(root.children);
             if (!comps.length) return {ok: false, why: '본문 root 에 컴포넌트가 없음'};

             // 콘텐츠로만 판별 — 순서/index 는 보지 않는다
             const info = comps.map(el => {
               const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
               const imgs = el.querySelectorAll('img').length;
               const cls = (el.className || '').toString();
               const title = el.classList.contains('se-documentTitle')
                          || !!el.querySelector('.se-documentTitle');
               const ph = /placeholder|se-component-add/i.test(cls)
                       || opt.phTexts.some(t => txt.indexOf(t) >= 0);
               const keep = !title && !ph && (imgs > 0 || txt.length >= opt.minChars);
               return {el, txt, imgs, keep,
                       why: title ? '제목' : (ph ? '자리표시자' : (keep ? '' : '내용없음')),
                       cls: cls.replace(/\s+/g, ' ').trim().slice(0, 38)};
             });
             const kept = info.filter(c => c.keep);
             if (!kept.length) return {ok: false, why: '본문으로 볼 컴포넌트가 하나도 없음',
                                       comps: info.map(c => ({cls: c.cls, chars: c.txt.length,
                                                              imgs: c.imgs, why: c.why}))};

             kept[0].el.setAttribute('data-blg-from', '1');
             kept[kept.length - 1].el.setAttribute('data-blg-to', '1');
             // 포커스용 클릭 대상은 **텍스트 컴포넌트**. 이미지 컴포넌트를 누르면
             //   이미지가 선택돼 버린다(2026-08-20 실측).
             const clickAt = kept.find(c => c.imgs === 0 && c.txt.length >= opt.minChars)
                          || kept[0];
             clickAt.el.setAttribute('data-blg-click', '1');
             const titleTxt = (info.find(c => c.why === '제목') || {}).txt || '';
             return {ok: true, total: info.length, kept: kept.length,
                     wantChars: kept.reduce((a, c) => a + c.txt.length, 0),
                     wantImgs: kept.reduce((a, c) => a + c.imgs, 0),
                     titleTxt: titleTxt.slice(0, 40),
                     comps: info.map(c => ({cls: c.cls, chars: c.txt.length,
                                            imgs: c.imgs, why: c.why}))};
           }"""

    # ② 표시해 둔 처음~끝을 하나의 Range 로 선택(클릭이 만든 캐럿을 덮어쓴다).
    MASTER_RANGE_JS = r"""() => {
             const first = document.querySelector('[data-blg-from]');
             const last = document.querySelector('[data-blg-to]');
             if (!first || !last) return {ok: false, why: '선택 시작/끝 표시가 사라짐'};
             first.scrollIntoView({block: 'center'});
             const r = document.createRange();
             r.setStartBefore(first);
             r.setEndAfter(last);
             const s = window.getSelection();
             s.removeAllRanges();
             s.addRange(r);
             const d = document.createElement('div');
             d.appendChild(r.cloneContents());
             const txt = (s.toString() || '').replace(/\s+/g, ' ').trim();
             return {ok: true, chars: txt.length, imgs: d.querySelectorAll('img').length,
                     text: txt, head: txt.slice(0, 50)};
           }"""

    async def _master_frame_with_root(self, page, frame=None):
        """[data-blg-master="1"] 마커가 실제로 붙어 있는 frame 을 돌려준다."""
        for sc in ([frame] if frame is not None else []) + list(page.frames):
            if sc is None:
                continue
            try:
                if await sc.evaluate(
                        "() => !!document.querySelector('[data-blg-master=\"1\"]')"):
                    return sc
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _copy_master_body(self, page, frame=None) -> dict:
        """기준글 본문 컴포넌트만 복사한다. 단일 경로, 분기 없음.

            판별/표시 → 포커스용 클릭 → Range 선택 → 검증 → Ctrl+C

        ★클릭은 '포커스를 주기 위해서'만 한다. JS 로만 선택하면 그 iframe 에 DOM 포커스가
          없어서 Ctrl+C 가 브라우저에 닿지 않는다(2026-08-20 실측: 옛 클립보드가 그대로 붙음).
          클릭이 만든 캐럿은 바로 뒤 Range 선택이 덮어쓰므로 선택 범위에는 영향이 없다.
        ★Ctrl+A 는 쓰지 않는다 — activeElement 가 body 면 페이지 전체가 잡힌다.
        반환 {chars, imgs, text}. chars=0 이면 복사하지 않은 것이다.
        """
        fail = {"chars": 0, "imgs": 0, "text": ""}
        fr = await self._master_frame_with_root(page, frame)
        if fr is None:
            self.log("   [기준글] ❌ 본문 root 마커를 어느 frame 에서도 못 찾음")
            return fail

        await page.bring_to_front()
        scan = await fr.evaluate(self.MASTER_SCAN_JS,
                                 {"minChars": self.MIN_COMPONENT_CHARS,
                                  "phTexts": list(self.PLACEHOLDER_TEXTS)})
        for c in (scan.get("comps") or []):
            mark = "본문" if not c["why"] else f"제외({c['why']})"
            self.log(f"      · {mark:<10} {c['chars']:>4}자 이미지 {c['imgs']}개  .{c['cls']}")
        if not scan.get("ok"):
            self.log(f"   [기준글] ❌ 본문 판별 실패 — {scan.get('why')}")
            return fail

        # 포커스 확보 — 표시해 둔 텍스트 컴포넌트를 왼쪽 위 모서리 근처에서 한 번 클릭
        try:
            await fr.locator('[data-blg-click]').first.click(
                timeout=5000, position={"x": 20, "y": 8})
            await page.wait_for_timeout(250)
            self.log(f"   [기준글] 포커스 클릭 · activeElement={await self._active_element(fr)}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [기준글] ⚠️ 포커스 클릭 실패({type(exc).__name__}) — 그대로 진행")

        sel = await fr.evaluate(self.MASTER_RANGE_JS)
        if not sel.get("ok"):
            self.log(f"   [기준글] ❌ Range 선택 실패 — {sel.get('why')}")
            return fail
        self.log(f"   [기준글] 컴포넌트 {scan['total']}개 중 본문 {scan['kept']}개 선택 — "
                 f"{sel['chars']}자 · 이미지 {sel['imgs']}개 "
                 f"(기대 {scan['wantChars']}자 · 이미지 {scan['wantImgs']}개)")
        self.log(f"   [기준글] 선택 앞부분: {sel['head']!r}")

        # ★검증 — 살린 컴포넌트들의 합계와 실제 선택이 맞아야 복사한다.
        want_c, want_i, title = scan["wantChars"], scan["wantImgs"], scan["titleTxt"]
        why = ""
        if title and len(title) >= 6 and title in sel["text"]:
            why = "선택 안에 제목이 들어 있음"
        elif sel["imgs"] != want_i:
            why = f"이미지 {sel['imgs']}개 ≠ 본문 컴포넌트 합계 {want_i}개"
        elif want_c and sel["chars"] < int(want_c * 0.9):
            why = f"선택 {sel['chars']}자 < 본문 합계 {want_c}자의 90%"
        elif want_c and sel["chars"] > int(want_c * 1.2) + 50:
            why = (f"선택 {sel['chars']}자 > 본문 합계 {want_c}자 — "
                   "제목/메뉴가 섞였을 수 있음")
        if why:
            self.log(f"   [기준글] ❌ 선택 검증 실패 — {why} (Ctrl+C 하지 않음)")
            return fail
        self.log(f"   [기준글] 선택 검증 ✅ (본문 {want_c}자 / 이미지 {want_i}개 기준)")

        # 이미지 유실 시 다시 올리기 위해 **실제 원본 URL**과 삽입 위치를 보관한다.
        #   (img.src 는 화면 밖이면 svg 자리표시자라 쓸 수 없다 → 전용 추출기)
        self._master_images = await self._master_image_manifest(page, fr)
        # 원본 서식(글자크기/글꼴/굵기/정렬) — 붙여넣은 결과와 대조한다.
        self._master_font = await self._font_census(fr, '[data-blg-master="1"]')
        if self._master_font:
            self.log(f"   [기준글] 서식 — 문단 {self._master_font.get('paras')}개 · "
                     f"크기 {self._master_font.get('size')} ({self._master_font.get('seFs')}) · "
                     f"글꼴 {self._master_font.get('family')} · "
                     f"굵기 {self._master_font.get('weight')} · "
                     f"정렬 {self._master_font.get('align')}")

        # ★복사는 키보드 Ctrl+C 가 아니라 렌더러의 copy 커맨드로 실행한다(2026-08-20 실측).
        #   Playwright 의 합성 키 이벤트는 **브라우저 창이 OS 포커스를 가진 동안에만**
        #   시스템 클립보드에 닿았다: 사람이 창을 클릭하기 전 1~3번째는 전부 실패,
        #   사람이 클릭한 뒤 4·5번째만 성공. execCommand('copy') 는 창 포커스와 무관하고
        #   Ctrl+C 와 같은 경로라 서식·이미지도 그대로 담긴다.
        copied = False
        try:
            copied = await fr.evaluate("() => document.execCommand('copy')")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [기준글] copy 실행 예외 {type(exc).__name__}")
        self.log(f"   [기준글] 복사 실행 — execCommand('copy') → {copied}")
        await page.wait_for_timeout(600)

        # ★클립보드가 실제로 바뀌었는지 여기서 확인한다.
        #   못 바뀌면 새 글 5개를 만들며 같은 실패를 반복할 뿐이다(2026-08-20 실측).
        clip = None
        try:
            clip = await page.evaluate("() => navigator.clipboard.readText()")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [기준글] ⚠️ 클립보드 확인 불가({type(exc).__name__}) — 그대로 진행")
        if clip is not None:
            low = (clip or "").lower()
            hit = next((b for b in self.FORBIDDEN_TEXT if b.lower() in low), "")
            probe = self._squash(sel["text"])[:20]
            if hit:
                self.log(f"   [기준글] ❌ 클립보드에 아직 옛 내용({hit!r}) — Ctrl+C 가 "
                         f"브라우저에 닿지 않았습니다")
                return fail
            if probe and probe not in self._squash(clip):
                self.log(f"   [기준글] ❌ 클립보드({len(clip)}자)가 선택한 본문과 다릅니다")
                return fail
            self.log(f"   [기준글] 클립보드 확인 ✅ {len(clip)}자")

        # 붙여넣기 후 대조 기준 = **실제로 선택해 복사한 텍스트**(제목 제외).
        return {"chars": sel["chars"], "imgs": sel["imgs"], "text": sel.get("text") or ""}

    async def _mark_master(self, frame, page=None) -> bool:
        """MASTER 글의 본문 컨테이너에 표시를 달아 이후 복사 대상으로 쓴다.
        여러 후보 selector 와 **모든 frame** 을 훑고, 실패 시 진단 로그를 남긴다."""
        # ① 기존 동작(빠른 경로) — 주어진 frame 의 .se-main-container
        try:
            if await frame.evaluate(
                r"""() => {
                     const root = document.querySelector('.se-main-container');
                     if (!root) return false;
                     root.setAttribute('data-blg-master', '1');
                     return true;
                   }"""):
                return True
        except Exception:  # noqa: BLE001
            pass
        if page is None:
            return False

        # ② 확장 탐색 — 후보 × 전 프레임
        sc, sel, _head = await self._find_body_root(page, frame)
        if sc is None:
            await self._dump_body_diagnosis(page)
            return False
        try:
            return await sc.evaluate(
                """(sel) => {
                     const els = Array.from(document.querySelectorAll(sel));
                     for (const el of els) {
                       const txt = (el.innerText || '').trim();
                       if (txt.length >= 20 || el.querySelectorAll('img').length) {
                         el.setAttribute('data-blg-master', '1');
                         return true;
                       }
                     }
                     return false;
                   }""", sel)
        except Exception:  # noqa: BLE001
            return False

    # ── 기준 글의 '수정' 화면에서 복사 (2026-08-20 사용자 요청) ─────────────
    #   왜: 뷰 화면에서 복사하면 네이버가 클립보드에 '[출처] …' 를 붙인다(개인계정이라 회피 불가).
    #       **수정 화면에서 복사하면 출처가 안 붙는다** → 출처 삭제 로직에 의존하지 않아도 됨.
    #   흐름: 기준 글 열기 → '수정' 클릭 → 제목/본문 확보 → 새 글에서 모바일 전환 후 붙여넣기.
    EDIT_LABELS = ("수정",)

    # ── 기준글 수정 진입 (2026-08-20 재작성: 클릭했다고 성공 로그를 찍지 않는다) ──
    #   이전 버전은 click() 이 예외만 안 나면 '클릭 성공'으로 로그를 찍어, 실제로는 다른 요소를
    #   눌렀거나 아무 일도 안 일어났는데 성공처럼 보였다. 이제 **에디터 실물 증거**로만 판정한다.
    EDIT_SELECTORS = (
        "a._modifyPost",                         # 실측(사용자 제공): href='#' + JS 동작
        "a[class*='_modifyPost']",
        "a:has-text('수정하기')",
        "button:has-text('수정하기')",
        "[role='button']:has-text('수정하기')",
        "a:has-text('수정')",
        # ⚠️ a[href*='postwrite'] / Redirect=Update 는 쓰지 않는다(2026-08-20 사용자 지시).
        #    글 목록·상단 메뉴의 '글쓰기' 링크를 잘못 눌러 엉뚱한 새 글로 들어간다.
    )

    async def _editor_evidence(self, context, page) -> tuple:
        """에디터에 실제로 들어갔는지 '증거'로 확인. (page, 이유) 또는 (None, '')."""
        for pg in list(context.pages):                    # 새 탭으로 열릴 수도 있다
            try:
                if pg.is_closed():
                    continue
                if "postwrite" in (pg.url or "").lower():
                    return pg, f"url={pg.url[:60]}"
                for fr in [pg.main_frame] + list(pg.frames):
                    try:
                        n = await fr.evaluate(
                            "() => document.querySelectorAll("
                            "'.se-main-container, .se-documentTitle, .se-content').length")
                        if n:
                            return pg, f"editorDOM({n})@{(fr.url or '')[:40]}"
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                continue
        return None, ""

    # ── 수동 selector 캡처 모드 (2026-08-20 사용자 요청) ──────────────────
    #   자동 탐색이 실패해도 브라우저를 닫지 않고, 사용자가 직접 누른 요소를 잡아
    #   **재사용 가능한 selector** 를 만들어 준다. 랜덤 해시 class 는 쓰지 않고
    #   id / aria-label / title / href / 텍스트 / 구조(nth-of-type) 순으로 안정적인 것을 고른다.
    CAPTURE_JS = r"""
      () => {
        if (window.__blgCap) return true;
        window.__blgCap = {hits: []};
        // '불안정한 토큰' 판정: 긴 숫자열(글번호) · 순수 hex 해시 · CSS 로 못 쓰는 문자.
        //   ⚠️ '_modifyPost' 같은 의미 있는 camelCase 는 반드시 살려야 한다(가장 좋은 키).
        const unstable = (c) =>
              !c
              || c.length > 40
              || /\d{4,}/.test(c)                    // 224384356096 처럼 글번호가 박힌 것
              || /^[a-f0-9]{6,}$/i.test(c)            // a1b2c3d4e5 같은 hex 해시
              || /[^a-zA-Z0-9_-]/.test(c);            // ( ) | 등 CSS 에 못 쓰는 문자
        const stable = (c) => !unstable(c);
        const cssPath = (el) => {
          const parts = [];
          let cur = el, depth = 0;
          while (cur && cur.nodeType === 1 && depth < 4) {
            let seg = cur.tagName.toLowerCase();
            if (cur.id && stable(cur.id)) { parts.unshift(`${seg}#${cur.id}`); break; }
            const cls = (cur.className || '').toString().split(/\s+/).filter(stable);
            if (cls.length) seg += '.' + cls.slice(0, 2).join('.');
            const par = cur.parentElement;
            if (par) {
              const same = Array.from(par.children).filter(x => x.tagName === cur.tagName);
              if (same.length > 1) seg += `:nth-of-type(${same.indexOf(cur) + 1})`;
            }
            parts.unshift(seg);
            cur = cur.parentElement; depth += 1;
          }
          return parts.join(' > ');
        };
        const info = (el) => el ? {
          tag: el.tagName.toLowerCase(),
          id: el.id || '',
          cls: (el.className || '').toString().slice(0, 120),
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
          aria: el.getAttribute('aria-label') || '',
          title: el.getAttribute('title') || '',
          href: el.getAttribute('href') || '',
          role: el.getAttribute('role') || '',
          ce: !!el.isContentEditable,
        } : null;
        document.addEventListener('click', (ev) => {
          const el = ev.target.closest('a,button,[role="button"],input,label,span,div') || ev.target;
          const parents = [];
          let cur = el.parentElement;
          for (let i = 0; i < 3 && cur; i++) { parents.push(info(cur)); cur = cur.parentElement; }
          const me = info(el);
          // 본문을 클릭한 경우 '실제 편집 root'(최상위 contenteditable)까지 같이 남긴다.
          let ceRoot = null, up = el;
          while (up) { if (up.isContentEditable) ceRoot = up; up = up.parentElement; }
          const ceInfo = ceRoot ? info(ceRoot) : null;
          const cePath = ceRoot ? cssPath(ceRoot) : '';
          const cands = [];
          if (me.id && stable(me.id)) cands.push(`${me.tag}#${me.id}`);
          if (me.aria) cands.push(`${me.tag}[aria-label="${me.aria}"]`);
          if (me.title) cands.push(`${me.tag}[title="${me.title}"]`);
          if (me.href && me.href !== '#') cands.push(`${me.tag}[href="${me.href}"]`);
          (me.cls || '').split(/\s+/).filter(stable).forEach(c => cands.push(`${me.tag}.${c}`));
          if (me.text) cands.push(`${me.tag}:has-text("${me.text.slice(0, 20)}")`);
          cands.push(cssPath(el));
          // 본문 컨테이너를 클릭했다면 그 자리를 본문 root 마커로 삼는다
          //   (자동 탐색이 틀렸을 때 사용자가 직접 지정하는 유일한 수단).
          const host = ceRoot
                    || el.closest('.se-main-container, .se-content, .se-container');
          let marked = '';
          if (host) {
            document.querySelectorAll('[data-blg-master]')
              .forEach(e => e.removeAttribute('data-blg-master'));
            host.setAttribute('data-blg-master', '1');
            marked = (host.className || '').toString().slice(0, 50);
          }
          window.__blgCap.hits.push({el: me, parents, cands, url: location.href,
                                     ceRoot: ceInfo, cePath, marked});
        }, true);
        return true;
      }"""

    async def _arm_capture(self, page) -> None:
        """페이지와 모든 프레임에 클릭 캡처를 심는다(중복 설치 방지 내장)."""
        for scope in [page] + list(page.frames):
            try:
                await scope.evaluate(self.CAPTURE_JS)
            except Exception:  # noqa: BLE001
                continue

    async def _read_capture(self, page) -> list:
        """캡처된 클릭 정보를 모아 반환하고 비운다."""
        out = []
        for scope in [page] + list(page.frames):
            try:
                hits = await scope.evaluate(
                    "() => { const c = window.__blgCap; if (!c) return [];"
                    " const h = c.hits.slice(); c.hits.length = 0; return h; }")
                out += hits or []
            except Exception:  # noqa: BLE001
                continue
        return out

    def _report_capture(self, what: str, hit: dict) -> str:
        """캡처 결과를 보기 좋게 로그로 남기고, 가장 안정적인 selector 를 돌려준다."""
        me, cands = hit.get("el") or {}, hit.get("cands") or []
        self.log("")
        self.log(f"   ┌─ [캡처] {what} — 클릭하신 요소")
        self.log(f"   │  tag={me.get('tag')} id={me.get('id')!r} role={me.get('role')!r}")
        self.log(f"   │  class={me.get('cls')!r}")
        self.log(f"   │  text={me.get('text')!r} aria={me.get('aria')!r} title={me.get('title')!r}")
        self.log(f"   │  href={me.get('href')!r}")
        for i, par in enumerate(hit.get("parents") or [], 1):
            if par:
                self.log(f"   │  부모{i}: <{par.get('tag')}> id={par.get('id')!r} "
                         f"class={(par.get('cls') or '')[:50]!r}")
        self.log(f"   │  URL: {(hit.get('url') or '')[:80]}")
        ce = hit.get("ceRoot") or {}
        if ce:
            self.log(f"   │  ★편집 root(최상위 contenteditable): <{ce.get('tag')}> "
                     f"id={ce.get('id')!r} class={(ce.get('cls') or '')[:60]!r}")
            self.log(f"   │     경로: {hit.get('cePath') or ''}")
        else:
            self.log("   │  ★편집 root 없음 — 클릭한 곳이 편집영역이 아닙니다")
        if hit.get("marked"):
            self.log(f"   │  ★본문 root 마커를 여기에 지정했습니다: .{hit['marked']}")
        self.log("   ├─ selector 후보(안정성 순)")
        for c in cands:
            self.log(f"   │    {c}")
        best = cands[0] if cands else ""
        self.log(f"   └─ ★사용할 selector: {best}")
        self.log(f"      → 코드에 넣으려면: {what} 목록 맨 앞에 {best!r} 추가")
        return best

    async def _manual_capture(self, page, what: str, verify, timeout_sec: int = 300,
                              succeed_on_click: bool = False):
        """자동 탐색 실패 시 사용자가 직접 클릭하게 하고 그 요소의 selector 를 잡는다.
        verify() 가 True 가 되면 성공으로 보고 (selector, True) 반환.
        시간 내 클릭이 없으면 (None, False) — **브라우저는 닫지 않는다**."""
        self.log("")
        self.log("   " + "=" * 62)
        self.log(f"   ⚠️ {what} selector를 찾지 못했습니다.")
        self.log(f"      브라우저에서 직접 '{what}'을(를) 클릭해주세요. (최대 {timeout_sec // 60}분 대기)")
        self.log("      클릭하시면 그 요소 정보를 읽어 selector를 만들어 드립니다.")
        self.log("   " + "=" * 62)
        await self._arm_capture(page)
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        best = None
        waited = 0
        while waited < timeout_sec:
            await page.wait_for_timeout(1000)
            waited += 1
            await self._arm_capture(page)          # 새로 뜬 프레임에도 계속 심는다
            hits = await self._read_capture(page)
            for hit in hits:
                cap = self._report_capture(what, hit)
                best = best or cap
            if succeed_on_click and hits:
                # 클릭 자체가 성공 조건인 경우(본문 root 지정 등) 바로 끝낸다.
                self.log(f"   [캡처] {what} — 클릭 확인 ✅")
                return best, True
            try:
                if await verify():
                    self.log(f"   [캡처] {what} — 동작 확인 ✅ (사용자 클릭으로 진행)")
                    return best, True
            except Exception:  # noqa: BLE001
                pass
            if waited % 30 == 0:
                self.log(f"   [캡처] 대기 중… {waited}s / {timeout_sec}s")
        self.log(f"   [캡처] ❌ {timeout_sec}초 안에 클릭이 감지되지 않았습니다(브라우저는 열어 둡니다)")
        return best, False

    async def _open_source_editor(self, context, source_url: str):
        """기준 글(시트의 '검수용 블로그랜딩 참고' URL)을 열고 '수정'을 눌러 에디터로 진입.
        클릭 후 **에디터 실물 증거**가 확인될 때까지 검증하고, 확인 안 되면 다음 후보로 넘어간다.
        전부 실패하면 덤프를 남기고 예외를 던진다(다음 단계로 진행하지 않는다)."""
        page = await context.new_page()

        # ★수정 화면은 '버튼 클릭'이 아니라 **URL 로 바로 들어간다**(2026-08-20 실측).
        #   로그인 상태에서 기준글 URL 로 가면 네이버가 스스로
        #     blog.naver.com/{id}?Redirect=Update&logNo={no}  (→ PostUpdateForm.naver)
        #   로 보내준다. 즉 누를 '수정' 버튼이 화면에 없다 — 지금까지 자동 탐색이
        #   계속 실패한 진짜 이유가 이것이었다. 그래서 조립한 URL 로 직접 진입한다.
        m_id = re.search(r"blog\.naver\.com/([^/?#]+)/(\d{6,})", source_url or "")
        edit_url = (f"https://blog.naver.com/{m_id.group(1)}?Redirect=Update&"
                    f"logNo={m_id.group(2)}") if m_id else ""
        first_url = edit_url or source_url
        if edit_url:
            self.log(f"   [기준글] 수정 URL 직접 진입: {edit_url}")
        await page.goto(first_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1200)

        # 진입 검증 — 에디터 실물이 확인되면 버튼 탐색 자체가 필요 없다.
        for _ in range(20):                                   # 최대 10초
            ed, why = await self._editor_evidence(context, page)
            if ed:
                self.log(f"   [기준글] 수정 화면 진입 확인 ✅ (URL 직접) · {why}")
                await ed.bring_to_front()
                frame = await self._fresh_frame(ed)
                title = await self._read_editor_title(ed, frame)
                self.log(f"   [기준글] 제목 {title[:40]!r}")
                return ed, frame, title
            await page.wait_for_timeout(500)
        self.log("   [기준글] URL 직접 진입으로는 에디터 미확인 → 버튼 탐색으로 폴백")
        if edit_url:                                          # 폴백은 원문(뷰) 화면에서 시도
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1200)

        # ① 의도한 글을 실제로 열었는지 먼저 확인(리다이렉트/로그인 페이지 방지)
        want_no = re.search(r"/(\d{6,})", source_url or "")
        landed = page.url or ""
        self.log(f"   [기준글] 요청 URL: {source_url}")
        self.log(f"   [기준글] 실제 URL: {landed[:90]}")
        if want_no and want_no.group(1) not in landed:
            body = ""
            try:
                body = (await page.title() or "")[:60]
            except Exception:  # noqa: BLE001
                pass
            self.log(f"   [기준글] ⚠️ 글번호 {want_no.group(1)} 가 URL에 없음 (title={body!r})")

        # ② '수정' 후보를 순서대로 눌러보고 매번 '에디터 증거'로 검증
        tried = []
        for sel in self.EDIT_SELECTORS:
            for scope in [page] + list(page.frames):
                try:
                    loc = scope.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    try:
                        await loc.scroll_into_view_if_needed(timeout=2000)
                    except Exception:  # noqa: BLE001
                        pass
                    await loc.click(timeout=4000, force=True)
                except Exception as exc:  # noqa: BLE001
                    tried.append(f"{sel}@{(scope.url or '')[:28]} → {type(exc).__name__}")
                    continue
                # SPA 라 늦게 뜬다 → 최대 15초 증거 폴링
                for _ in range(30):
                    ed, why = await self._editor_evidence(context, page)
                    if ed:
                        self.log(f"   [기준글] '수정' 진입 확인 ✅ selector={sel} · {why}")
                        await ed.bring_to_front()
                        frame = await self._fresh_frame(ed)
                        title = await self._read_editor_title(ed, frame)
                        self.log(f"   [기준글] 제목 {title[:40]!r}")
                        return ed, frame, title
                    await page.wait_for_timeout(500)
                tried.append(f"{sel}@{(scope.url or '')[:28]} → 클릭했지만 에디터 미확인")

        self.log("   [기준글] ❌ 자동 탐색 실패 — 시도한 selector:")
        for t in tried or ["(후보 자체를 못 찾음)"]:
            self.log(f"      · {t}")
        await self._dump_publish_layer(page, 900)          # 실측 덤프

        # ★ 바로 끝내지 않고 수동 캡처로 전환 — 사용자가 직접 '수정'을 누르면 selector 를 잡는다.
        async def _entered():
            ed, _why = await self._editor_evidence(context, page)
            return ed is not None

        sel_found, ok = await self._manual_capture(page, "기준글 '수정' 버튼", _entered)
        if ok:
            ed, why = await self._editor_evidence(context, page)
            self.log(f"   [기준글] '수정' 진입 확인 ✅ (수동) · {why}")
            if sel_found:
                self.log(f"   [기준글] 다음부터 자동화에 쓸 selector: {sel_found}")
            await ed.bring_to_front()
            frame = await self._fresh_frame(ed)
            title = await self._read_editor_title(ed, frame)
            self.log(f"   [기준글] 제목 {title[:40]!r}")
            return ed, frame, title
        raise RuntimeError("기준 글 '수정' 화면 진입 실패 — 자동 탐색·수동 클릭 모두 미확인")

    async def _read_editor_title(self, page, frame) -> str:
        """에디터 제목 영역의 텍스트를 읽는다(없으면 빈 문자열)."""
        for sel in (".se-documentTitle .se-text-paragraph", ".se-title-text",
                    "[class*='documentTitle'] [class*='text-paragraph']"):
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0:
                    t = (await loc.inner_text() or "").strip()
                    if t:
                        return t
            except Exception:  # noqa: BLE001
                continue
        return ""

    # ── 모바일 미리보기 전환 (2026-08-20 재작성: 클릭만으로 성공 처리하지 않는다) ──
    MOBILE_SELECTORS = (
        # ★실측(2026-08-20 캡처): 에디터 우측 상단 화면전환 토글.
        #   ⚠️ 버튼 라벨이 '모바일'이 아니라 **'PC 화면'**(현재 상태 표시형)이라
        #      '모바일' 텍스트로만 찾던 옛 후보는 전부 실패했다.
        "button.se-util-button.__mode-button",
        "button.se-util-button-device-desktop",
        "li.se-utils-item button.se-util-button",
        "button:has-text('PC 화면')",
        "button:has-text('모바일')",
        "a:has-text('모바일')",
        "[role='button']:has-text('모바일')",
        "[data-name='mobile']",
        "[class*='mobile'][class*='button']",
        "[class*='preview'] [class*='mobile']",
        "button[title*='모바일']",
        "[aria-label*='모바일']",
    )

    async def _mobile_state(self, page) -> dict:
        """모바일 미리보기가 켜졌다고 볼 만한 흔적을 센다(선택 상태/모바일 컨테이너)."""
        out = {"selected": 0, "container": 0}
        for scope in [page] + list(page.frames):
            try:
                r = await scope.evaluate(
                    r"""() => {
                      const sel = Array.from(document.querySelectorAll(
                        "[class*='mobile'],[data-name='mobile'],[aria-label*='모바일']"))
                        .filter(e => {
                          const c = (e.className || '').toString();
                          return /selected|active|on|checked/i.test(c)
                                 || e.getAttribute('aria-pressed') === 'true'
                                 || e.getAttribute('aria-selected') === 'true';
                        }).length;
                      const box = Array.from(document.querySelectorAll(
                        "[class*='mobile']")).filter(e => e.offsetParent !== null
                                                     && e.getBoundingClientRect().width > 200).length;
                      return {selected: sel, container: box};
                    }""")
                out["selected"] += r.get("selected", 0)
                out["container"] += r.get("container", 0)
            except Exception:  # noqa: BLE001
                continue
        return out

    async def _switch_preview_mobile(self, page) -> bool:
        """미리보기를 '모바일'로 전환하고 **상태 변화로 검증**한다.
        검증되지 않으면 성공 로그를 찍지 않고 False + 덤프(호출부가 중단 판단)."""
        before = await self._mobile_state(page)
        tried = []
        for sel in self.MOBILE_SELECTORS:
            for scope in [page] + list(page.frames):
                try:
                    loc = scope.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    if not await loc.is_visible():
                        tried.append(f"{sel} → 보이지 않음")
                        continue
                    await loc.click(timeout=3000, force=True)
                except Exception as exc:  # noqa: BLE001
                    tried.append(f"{sel} → {type(exc).__name__}")
                    continue
                for _ in range(10):                     # 전환 반영 대기(최대 5초)
                    await page.wait_for_timeout(500)
                    after = await self._mobile_state(page)
                    if (after["selected"] > before["selected"]
                            or after["container"] > before["container"]):
                        self.log(f"   [모바일] 전환 확인 ✅ selector={sel} "
                                 f"(선택 {before['selected']}→{after['selected']} · "
                                 f"영역 {before['container']}→{after['container']})")
                        return True
                tried.append(f"{sel} → 클릭했지만 상태 변화 없음")

        self.log("   [모바일] ❌ 자동 탐색 실패 — 시도한 selector:")
        for t in tried or ["(후보 자체를 못 찾음)"]:
            self.log(f"      · {t}")
        await self._dump_publish_layer(page, 901)

        # ★ 수동 캡처로 전환 — 사용자가 직접 모바일 토글을 누르면 상태 변화로 확인한다.
        base = await self._mobile_state(page)

        async def _switched():
            now = await self._mobile_state(page)
            return (now["selected"] > base["selected"]) or (now["container"] > base["container"])

        sel_found, ok = await self._manual_capture(page, "모바일 미리보기 버튼", _switched)
        if ok and sel_found:
            self.log(f"   [모바일] 다음부터 자동화에 쓸 selector: {sel_found}")
        return bool(ok)
    # ══════════════════════════════════════════════════════════════════
    # 이미지 유실 복구 (2026-08-20: 원본 5개 → 붙여넣기 1개 사고)
    #   Ctrl+V 로 넘어온 HTML 의 이미지를 네이버가 다시 받아오다 일부를 버린다
    #   ('허용되지 않는 형식의 이미지'). 그래서 **원본 파일을 직접 내려받아
    #   에디터의 사진 업로드로 같은 자리에 다시 올린다**.
    # ══════════════════════════════════════════════════════════════════
    IMAGE_BUTTON_SELECTORS = (
        "button[data-name='image']",
        "button[data-log='ent.image']",
        "button.se-image-toolbar-button",
        "button[aria-label*='사진']",
        "button[title*='사진']",
        "button:has-text('사진')",
        "[role='button'][aria-label*='사진']",
    )

    IMAGE_EXT_BY_TYPE = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/bmp": ".bmp", "image/webp": ".webp",
    }

    @staticmethod
    def _origin_image_url(src: str) -> str:
        """네이버 이미지 CDN 의 리사이즈 파라미터를 떼어 원본을 받는다.
        `...jpg?type=w966` 처럼 쿼리가 붙으면 형식이 바뀌어 내려온다."""
        u = (src or "").strip()
        if not u:
            return ""
        if u.startswith("//"):
            u = "https:" + u
        return u.split("?")[0]

    # ── 원본 이미지 URL 추출 (2026-08-20 재작성) ────────────────────────
    #   ⚠️ img.src 만 보면 안 된다. SmartEditor 는 화면 밖 이미지를
    #      `data:image/svg+xml;base64,…` **자리표시자**로 채워 둔다(lazy-load).
    #      실측: 5개 중 1개만 blogfiles URL, 나머지 4개는 전부 svg 자리표시자였다.
    #      실제 주소는 data-src / srcset / `__se_module_data` 의 JSON 속성 등에 들어 있다.
    MASTER_IMAGES_JS = r"""(opt) => {
             const root = document.querySelector('[data-blg-master="1"]');
             if (!root) return {ok: false, why: '본문 root 마커 없음'};
             const comps = Array.from(root.querySelectorAll('.se-component'))
                             .filter(e => e.querySelector('img'));

             const isReal = (u) => !!u && /^https?:\/\//i.test(u);
             const imageish = (u) => /\.(jpe?g|png|gif|bmp|webp)(\?|$)/i.test(u)
                                  || /pstatic\.net|phinf|blogfiles|postfiles/i.test(u);
             const urlsIn = (t) => {
               const out = []; const re = /https?:\/\/[^\s"'<>()\\]+/gi; let m;
               while ((m = re.exec(t || '')) !== null) out.push(m[0]);
               return out;
             };
             const firstOfSrcset = (v) => {
               if (!v) return '';
               const one = v.split(',')[0].trim().split(/\s+/)[0];
               return one || '';
             };

             // 앵커: 이 이미지 바로 앞의 '글자 있는 컴포넌트'
             const allComps = Array.from(root.querySelectorAll('.se-component'));
             const anchorOf = (comp) => {
               const i = allComps.indexOf(comp);
               for (let j = i - 1; j >= 0; j--) {
                 const t = (allComps[j].innerText || '').replace(/\s+/g, ' ').trim();
                 if (t.length >= opt.minChars
                     && !allComps[j].classList.contains('se-documentTitle')) return t;
               }
               return '';
             };

             const images = comps.map((comp, idx) => {
               const cands = [];
               const push = (why, v) => {
                 const u = (v == null ? '' : String(v)).trim();
                 if (u) cands.push({why, url: u});
               };
               const img = comp.querySelector('img');
               if (img) {
                 push('src', img.getAttribute('src'));
                 push('currentSrc', img.currentSrc);
                 ['data-src', 'data-lazy-src', 'data-original', 'data-url',
                  'data-image-src', 'data-origin', 'data-linkdata']
                   .forEach(a => push(a, img.getAttribute(a)));
                 Object.keys(img.dataset || {}).forEach(k => push('dataset.' + k,
                                                                 img.dataset[k]));
                 push('srcset', firstOfSrcset(img.getAttribute('srcset')));
               }
               const a = comp.querySelector('a[href]');
               if (a) push('a[href]', a.getAttribute('href'));

               // SE ONE 모듈 데이터(JSON 이 속성/스크립트에 들어 있다)
               comp.querySelectorAll('[data-module], [data-linkdata], script')
                 .forEach(e => {
                   const blobs = [e.getAttribute('data-module'),
                                  e.getAttribute('data-linkdata'), e.textContent];
                   blobs.forEach(b => urlsIn(b).forEach(u => push('module-json', u)));
                 });

               // 남은 모든 속성 / background-image 훑기
               comp.querySelectorAll('*').forEach(e => {
                 Array.from(e.attributes || []).forEach(at => {
                   if (at.name === 'src' || at.name === 'href') return;
                   urlsIn(at.value).forEach(u => push('attr:' + at.name, u));
                 });
                 const bg = (getComputedStyle(e).backgroundImage || '');
                 if (bg && bg !== 'none') urlsIn(bg).forEach(u => push('background', u));
               });

               // data: URL 은 절대 원본으로 인정하지 않는다
               const real = cands.filter(c => isReal(c.url));
               const best = real.find(c => imageish(c.url)) || real[0] || null;
               const seen = new Set();
               const shown = [];
               cands.forEach(c => {
                 const k = c.why + '|' + c.url.slice(0, 60);
                 if (seen.has(k)) return;
                 seen.add(k);
                 if (shown.length < 12) shown.push(c.why + ' = ' + c.url.slice(0, 72));
               });
               return {order: idx + 1,
                       url: best ? best.url : '',
                       from: best ? best.why : '',
                       anchor: anchorOf(comp).slice(0, 30),
                       cands: shown,
                       html: best ? '' : (comp.outerHTML || '')
                                          .replace(/\s+/g, ' ').slice(0, opt.htmlLen)};
             });
             return {ok: true, images};
           }"""

    async def _master_image_manifest(self, page, frame) -> list:
        """기준글의 이미지별 **실제 원본 URL**과 삽입 위치(앵커)를 뽑는다.

        lazy-load 자리표시자를 피하려고 이미지를 하나씩 화면에 띄운 뒤 읽는다."""
        try:
            n = await frame.evaluate(
                """() => document.querySelectorAll(
                     '[data-blg-master="1"] .se-component img').length""")
        except Exception:  # noqa: BLE001
            n = 0
        for i in range(n):                       # 하나씩 보여줘 lazy-load 를 깨운다
            try:
                await frame.evaluate(
                    """(i) => { const el = document.querySelectorAll(
                         '[data-blg-master="1"] .se-component img')[i];
                         if (el) el.scrollIntoView({block: 'center'}); }""", i)
                await page.wait_for_timeout(350)
            except Exception:  # noqa: BLE001
                break
        await page.wait_for_timeout(800)

        try:
            r = await frame.evaluate(self.MASTER_IMAGES_JS,
                                     {"minChars": self.MIN_COMPONENT_CHARS, "htmlLen": 700})
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [원본이미지] 추출 실패 {type(exc).__name__}")
            return []
        if not r or not r.get("ok"):
            self.log(f"   [원본이미지] 추출 실패 — {(r or {}).get('why')}")
            return []

        images = r.get("images") or []
        found = sum(1 for im in images if im.get("url"))
        self.log(f"   [원본이미지] 컴포넌트 {len(images)}개 · 실제 URL 확보 {found}개")
        for im in images:
            if im.get("url"):
                self.log(f"      [{im['order']}] {im['from']} → {im['url'][:90]}")
                self.log(f"          앞 문단: {im.get('anchor', '')!r}")
            else:
                # ★못 찾으면 원인을 볼 수 있게 후보와 outerHTML 을 남긴다
                self.log(f"      [{im['order']}] ❌ 실제 http(s) 이미지 URL을 못 찾음")
                for c in im.get("cands") or []:
                    self.log(f"          후보: {c}")
                if im.get("html"):
                    self.log(f"          outerHTML: {im['html']}")
        return images

    async def _download_images(self, context, images: list) -> list:
        """원본 이미지를 내려받아 로컬 파일로 만든다. [{path, order, anchor, ...}]"""
        out_dir = self.user_data_dir.parent / "out" / "imgs"
        out_dir.mkdir(parents=True, exist_ok=True)
        got = []
        for im in images:
            order = im.get("order", 0)
            url = self._origin_image_url(im.get("url", ""))
            if not url.lower().startswith(("http://", "https://")):
                self.log(f"      [다운로드 {order}] 건너뜀 — 실제 이미지 URL이 없습니다 "
                         f"(자리표시자/데이터URL)")
                continue
            try:
                resp = await context.request.get(url, timeout=30_000)
                if not resp.ok:
                    self.log(f"      [다운로드 {order}] 실패 HTTP {resp.status} — {url[:60]}")
                    continue
                body = await resp.body()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                ext = self.IMAGE_EXT_BY_TYPE.get(ctype) or (Path(url).suffix or ".jpg")
                path = out_dir / f"img_{order:02d}{ext}"
                path.write_bytes(body)
                self.log(f"      [다운로드 {order}] URL={url[:70]}")
                self.log(f"                    MIME={ctype or '?'} · "
                         f"{len(body):,}바이트 · 파일={path.name}")
                got.append({**im, "path": str(path), "ctype": ctype, "bytes": len(body)})
            except Exception as exc:  # noqa: BLE001
                self.log(f"      [다운로드 {order}] 예외 {type(exc).__name__} — {url[:60]}")
        return got

    async def _place_caret_after(self, page, frame, anchor: str) -> bool:
        """앵커 텍스트가 든 문단 끝에 캐럿을 놓는다(그 뒤에 이미지를 넣기 위해)."""
        if not anchor:
            return False
        try:
            box = await frame.evaluate(
                r"""(t) => {
                     const ps = Array.from(document.querySelectorAll('.se-text-paragraph'));
                     const hit = ps.find(p => (p.innerText || '')
                                    .replace(/\s+/g, ' ').indexOf(t) >= 0);
                     if (!hit) return null;
                     hit.scrollIntoView({block: 'center'});
                     const r = hit.getBoundingClientRect();
                     return {x: Math.round(r.x + r.width - 6),
                             y: Math.round(r.y + r.height / 2)};
                   }""", anchor[:20])
        except Exception:  # noqa: BLE001
            box = None
        if not box:
            return False
        await self._click_xy_in_frame(page, frame, box["x"], box["y"])
        await page.keyboard.press("End")
        await page.wait_for_timeout(200)
        return True

    async def _upload_image(self, page, frame, path: str) -> bool:
        """에디터의 '사진' 버튼을 눌러 파일 선택창을 가로채 파일을 넣는다."""
        for sel in self.IMAGE_BUTTON_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if await loc.count() == 0:
                    continue
                async with page.expect_file_chooser(timeout=8_000) as fc:
                    await loc.click(timeout=4_000)
                chooser = await fc.value
                await chooser.set_files(path)
                self.log(f"      [업로드] selector={sel} <- {Path(path).name}")
                return True
            except Exception:  # noqa: BLE001
                continue
        try:
            fi = frame.locator("input[type='file']").first
            if await fi.count() > 0:
                await fi.set_input_files(path)
                self.log(f"      [업로드] 숨은 input[type=file] <- {Path(path).name}")
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _dump_toolbar_buttons(self, frame) -> None:
        """사진 버튼을 못 찾았을 때 툴바 버튼을 남긴다(다음 실행에서 selector 확정용)."""
        try:
            btns = await frame.evaluate(
                r"""() => Array.from(document.querySelectorAll("button,[role='button']"))
                     .filter(e => e.getBoundingClientRect().y < 200)
                     .slice(0, 30)
                     .map(e => ({cls: (e.className || '').toString().slice(0, 46),
                                 name: e.getAttribute('data-name') || '',
                                 aria: e.getAttribute('aria-label') || '',
                                 text: (e.innerText || '').replace(/\s+/g, ' ')
                                         .trim().slice(0, 14)}))""")
        except Exception:  # noqa: BLE001
            return
        self.log("      [진단] 툴바 버튼 목록:")
        for b in btns:
            self.log(f"         data-name={b['name']!r} aria={b['aria']!r} "
                     f"text={b['text']!r} cls={b['cls']!r}")

    async def _restore_images(self, context, page, frame, want: int, got: int) -> int:
        """붙여넣기에서 빠진 이미지를 원본에서 받아 같은 자리에 다시 올린다. 최종 개수 반환."""
        images = list(getattr(self, "_master_images", []) or [])
        if not images:
            self.log("   [이미지] 원본 매니페스트가 없어 복구할 수 없습니다")
            return got
        have = [im for im in images if (im.get("url") or "").lower().startswith("http")]
        self.log(f"   [이미지] 복구 시작 — 원본 {want}개 / 현재 {got}개 · "
                 f"실제 URL 확보 {len(have)}/{len(images)}개")
        if len(have) < want:
            self.log(f"   [이미지] ⚠️ 원본 URL을 {want - len(have)}개 못 찾아 "
                     f"완전 복구가 불가능합니다(위 [원본이미지] 후보/outerHTML 확인)")
        files = await self._download_images(context, images)
        if not files:
            self.log("   [이미지] 원본 파일을 하나도 받지 못했습니다")
            return got

        # 지금 붙어 있는 이미지는 지우고 원본으로 전부 새로 올린다(순서·개수를 확실히 맞춘다).
        #   ⚠️ se-component 를 지우다 본문이 통째로 날아간 사고가 있었다(959→523자).
        #      그래서 **이미지 컴포넌트만** 지우고, 지운 뒤 본문 글자수가 줄지 않았는지 확인한다.
        before_len = (await self._measure_body(frame)).get("len", 0)
        try:
            removed = await frame.evaluate(
                r"""() => {
                     const secs = Array.from(document.querySelectorAll('.se-component'))
                       .filter(e => e.querySelector('img')
                                 && (e.innerText || '').trim().length < 10);
                     secs.forEach(e => e.remove());
                     return secs.length;
                   }""")
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [이미지] 기존 이미지 제거 실패({type(exc).__name__}) — 중단")
            return got
        after_len = (await self._measure_body(frame)).get("len", 0)
        if before_len and after_len < before_len - 30:
            self.log(f"   [이미지] ❌ 이미지 컴포넌트를 지우자 본문이 {before_len}→{after_len}자로 "
                     f"줄었습니다 — 복구를 중단합니다(본문 보호)")
            return got
        self.log(f"   [이미지] 기존 이미지 컴포넌트 {removed}개 제거 "
                 f"(본문 {before_len}→{after_len}자 유지) → 재삽입")

        done = 0
        for f in files:
            frame = await self._fresh_frame(page, frame)
            await page.bring_to_front()
            if not await self._place_caret_after(page, frame, f.get("anchor", "")):
                await page.keyboard.press("Control+End")
                self.log(f"      [{f['order']}] 앵커 문단을 못 찾아 본문 끝에 넣습니다")
            before = (await self._measure_body(frame)).get("img", 0)
            if not await self._upload_image(page, frame, f["path"]):
                self.log(f"      [{f['order']}] ❌ 사진 업로드 진입 실패")
                await self._dump_toolbar_buttons(frame)
                break
            grew = False
            for _ in range(30):                       # 업로드 반영 최대 15초
                await page.wait_for_timeout(500)
                frame = await self._fresh_frame(page, frame)
                if (await self._measure_body(frame)).get("img", 0) > before:
                    grew = True
                    break
            if grew:
                done += 1
            else:
                self.log(f"      [{f['order']}] ⚠️ 업로드 후 이미지가 늘지 않았습니다")
        frame = await self._fresh_frame(page, frame)
        now = (await self._measure_body(frame)).get("img", 0)
        self.log(f"   [이미지] 복구 결과 {now}/{want}개 (새로 올린 것 {done}개)")
        return now

    # ══════════════════════════════════════════════════════════════════
    # 서식 비교 (2026-08-20: 원본과 폰트 크기/정렬이 달라지는 문제)
    #   텍스트 앞 80자·유사도만으로 성공 판정하지 않는다(사용자 지시).
    # ══════════════════════════════════════════════════════════════════
    FONT_CENSUS_JS = r"""(sel) => {
             const root = document.querySelector(sel) || document.body;
             const ps = Array.from(root.querySelectorAll('.se-text-paragraph'))
               .filter(p => (p.innerText || '').trim().length > 0
                         && !p.closest('.se-documentTitle'));
             const tally = (k) => {
               const m = {};
               ps.forEach(p => { const v = k(p); if (v) m[v] = (m[v] || 0) + 1; });
               return m;
             };
             const top = (m) => (Object.entries(m).sort((a, b) => b[1] - a[1])[0] || ['', 0])[0];
             const cs = (p) => getComputedStyle(p.querySelector('span') || p);
             const size = tally(p => cs(p).fontSize);
             const family = tally(p => (cs(p).fontFamily || '').split(',')[0]
                                        .replace(/["']/g, '').trim());
             const weight = tally(p => cs(p).fontWeight);
             const align = tally(p => cs(p).textAlign);
             const seFs = tally(p => {
               const sp = p.querySelector('span');
               const c = sp ? (sp.className || '').toString() : '';
               return (c.match(/se-fs\d+/) || [''])[0];
             });
             return {paras: ps.length, size: top(size), sizeAll: size,
                     family: top(family), weight: top(weight),
                     align: top(align), alignAll: align, seFs: top(seFs)};
           }"""

    async def _font_census(self, frame, root_sel: str) -> dict:
        try:
            return await frame.evaluate(self.FONT_CENSUS_JS, root_sel)
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _compare_font(src: dict, dst: dict) -> list:
        """원본/결과 서식 차이 목록. 비어 있으면 동일."""
        if not src or not dst:
            return ["서식을 읽지 못함"]
        diffs = []
        for key, label in (("size", "글자크기"), ("family", "글꼴"),
                           ("weight", "굵기"), ("align", "정렬"), ("seFs", "se-fs")):
            a, b = src.get(key), dst.get(key)
            if a and b and a != b:
                diffs.append(f"{label} {a} -> {b}")
        return diffs

    async def _make_post_from_master(self, context, nav_page, master_page, master_frame,
                                     title: str, shot_name: str,
                                     write_url: str = "") -> dict:
        """MASTER 본문을 그대로 복사해 새 글 1건을 만든다(참고 랜딩 재분석 없음)."""
        # ① MASTER 본문 복사 — 단일 경로.
        #     본문 root([data-blg-master="1"]) 의 자식 컴포넌트만 Range 선택(제목 제외)
        #     → 글자수·이미지수 검증 → Ctrl+C.
        #   클릭/Ctrl+A 는 쓰지 않는다(se-image 컴포넌트를 눌러 activeElement=body 가 되면
        #   Ctrl+A 가 페이지 전체를 선택해 제목·상단 메뉴까지 딸려온다 — 2026-08-20 실측).
        await master_page.bring_to_front()
        master_frame = await self._fresh_frame(master_page, master_frame)
        st = await self._copy_master_body(master_page, master_frame)
        if not st["chars"]:
            # ★Ctrl+C 가 실제로 성공하기 전에는 새 글을 만들지 않는다(사용자 지시 2026-08-20).
            #   자동으로 못 잡으면 브라우저를 열어 둔 채 사용자가 본문을 직접 클릭하게 하고,
            #   그 요소의 tag/class/부모 selector 를 로그로 남긴다.
            async def _never():
                return False

            cap, ok = await self._manual_capture(
                master_page, "기준글 본문 영역(본문 글자 위를 클릭)", _never,
                succeed_on_click=True)
            if cap:
                self.log(f"   [본문] 클릭하신 요소로 만든 selector: {cap}")
            if ok:
                # 클릭한 컨테이너가 본문 root 마커를 넘겨받은 상태 → 같은 단일 경로로 재시도.
                st = await self._copy_master_body(master_page)
            if not st["chars"]:
                raise RuntimeError(
                    "기준글 본문 Range 선택/복사 실패 — 새 글을 만들지 않고 중단합니다"
                    "(위 [기준글]/[캡처] 로그 확인)")
        self.log(f"   [기준글] 본문 {st['chars']}자 · 이미지 {st['imgs']}개 복사 ✅")
        # 붙여넣기 후 대조할 기준 텍스트. 못 읽었으면 검증이 불가능하므로 중단한다.
        source_text = st.get("text") or ""
        if not self._squash(source_text):
            try:
                source_text = await master_frame.evaluate(
                    "() => { const r = document.querySelector('[data-blg-master=\"1\"]');"
                    " return r ? (r.innerText || '').trim() : ''; }")
            except Exception:  # noqa: BLE001
                source_text = ""
        if not self._squash(source_text):
            raise RuntimeError("기준글 본문 텍스트를 읽지 못해 검증할 수 없습니다 — 중단합니다.")
        self.log(f"   [기준글] 대조 기준 확보 {len(source_text)}자")
        # 원본(MASTER) 이미지 수 — 붙여넣기 후 비교해 '이미지 유실'을 즉시 잡는다.
        try:
            src_imgs = await master_frame.evaluate(
                "() => { const r = document.querySelector('[data-blg-master=\"1\"]');"
                " return r ? r.querySelectorAll('img').length : 0; }")
        except Exception:  # noqa: BLE001
            src_imgs = 0
        if st.get("imgs"):            # 실제 선택된 이미지 수가 더 정확하다
            src_imgs = max(src_imgs, st["imgs"])

        # ② 새 글 열고 제목 입력 후 붙여넣기
        #   ★복사가 확인된 뒤에만 여기까지 온다. 열자마자 화면 앞으로 올리고
        #     '제목/본문 자리'까지 잡힌 걸 로그로 확인한 다음 입력한다
        #     (글쓰기 화면이 아직 안 떴는데 붙여넣어 터지는 걸 막는다).
        #   ★2번째 글부터는 반드시 **새 탭**으로 연다(2026-08-20 실측 사고).
        #     _open_new_write 는 nav_page 를 블로그 홈으로 goto 한 뒤 '글쓰기'를 누르는데,
        #     네이버가 같은 탭에서 이동하면 editor 가 nav_page 가 된다. 그러면 다음 글을
        #     만들 때 그 탭을 다시 홈으로 보내 **앞서 완성한 READY 글이 통째로 날아갔다**
        #     (READY 2개가 같은 Page 라 발행 2번째가 TargetClosedError 로 실패).
        if write_url:
            editor, eframe, spots = await self._open_write_tab(context, write_url)
        else:
            editor, eframe, spots = await self._open_new_write(context, nav_page)
        await editor.bring_to_front()
        self.log(f"   [글쓰기] 화면 준비 완료 · {'새 탭' if write_url else '첫 탭'} · "
                 f"page={editor.url[:52]!r} "
                 f"frame={(eframe.url or '')[:52]!r} · "
                 f"제목자리={'O' if spots.get('title') else 'X'} "
                 f"본문자리={'O' if spots.get('body') else 'X'}")
        if getattr(self, "_use_mobile_preview", False):
            # 붙여넣기 전에 모바일로. **검증 실패 시 그대로 진행하지 않는다**(사용자 요청 2026-08-20).
            if not await self._switch_preview_mobile(editor):
                raise RuntimeError("모바일 미리보기 전환 실패 — 위 selector 로그/덤프 확인")
        ok_title = await self._type_title(editor, title, spots.get("title"))
        self.log(f"   제목 입력 {'성공' if ok_title else '실패'}")

        # 붙여넣기 — 구간 복사(_fill_post)에서 쓰던 것과 **완전히 같은** 경로.
        eframe = await self._fresh_frame(editor, eframe)
        await editor.bring_to_front()
        if spots.get("body"):
            await self._click_spot(editor, eframe, spots["body"])
            await editor.wait_for_timeout(400)
        else:
            await self._focus_editor_body(editor, "PostWriteForm", eframe)
        await editor.keyboard.press("Control+End")
        await editor.keyboard.press("Control+V")
        await editor.wait_for_timeout(2500)            # 이미지 업로드 여유
        self.log("   MASTER 본문 붙여넣기 완료")
        # ★붙여넣은 본문을 DOM 에서 읽어 기준글과 대조한다(2026-08-20 사고 방지).
        #   불일치하면 발행 단계로 넘기지 않고 그 자리에서 중단.
        target_text = await self._read_body_text(editor, "PostWriteForm", eframe)
        if not self._squash(target_text):
            try:
                _ef0 = await self._fresh_frame(editor, eframe)
                target_text = await _ef0.evaluate(
                    "() => { const r = document.querySelector('.se-main-container')"
                    " || document.body; return (r.innerText || '').trim(); }")
            except Exception:  # noqa: BLE001
                target_text = ""
        ok_text, why_text = self._text_match(source_text, target_text)
        if not ok_text:
            self.log(f"   [검증] ❌ {why_text}")
            raise RuntimeError(f"본문 검증 실패 — {why_text}. 발행하지 않고 중단합니다.")
        self.log(f"   [검증] ✅ 본문 일치 — {why_text}")
        # ★이미지 유실 검사 — 뷰어(읽기전용)에서 복사하면 텍스트만 넘어온다(실측 5개→0개).
        try:
            _ef = await self._fresh_frame(editor, eframe)
            got = (await self._measure_body(_ef)).get("img", 0)
        except Exception:  # noqa: BLE001
            got = 0
        self.log(f"   [확인] 이미지 {got}/{src_imgs}개 반영")
        # ★개수가 다르면 원본을 내려받아 같은 자리에 다시 올린다(2026-08-20 사용자 요청).
        if src_imgs and got != src_imgs:
            self.log(f"   ⚠️ 이미지 {src_imgs - got}개 유실 — 원본에서 복구를 시도합니다")
            eframe = await self._fresh_frame(editor, eframe)
            got = await self._restore_images(context, editor, eframe, src_imgs, got)
        img_ok = bool(src_imgs) and got == src_imgs
        if not img_ok:
            self.log(f"   [검증] ❌ 이미지 {got}/{src_imgs}개 — READY 처리하지 않습니다")

        # ③ 다시 생긴 것이 있는지만 검사 → 있을 때만 cleanup
        eframe = await self._fresh_frame(editor, eframe)      # 검사 직전 재획득
        m = await self._measure_body(eframe)
        src = await self._count_source(eframe)
        promo = sum((await self._count_promo(eframe) or {}).values())
        if m["strike"] or src or promo:
            self.log(f"   재발 감지(strike={m['strike']} 출처={src} Re:purely={promo})"
                     " → cleanup 실행")
            await self._cleanup_pasted(editor, eframe, spots.get("body"))
            m = await self._measure_body(eframe)
            src = await self._count_source(eframe)
            promo = sum((await self._count_promo(eframe) or {}).values())
        else:
            self.log("   재발 없음 — cleanup 생략")

        # ★마지막에 이미지만 개별 가운데 정렬(2026-08-20 사용자 요청).
        #   텍스트 문단은 붙여넣는 시점에 이미 가운데라 손대지 않는다 — 이미지 섹션만
        #   se-section-align-left 로 남아서 하나씩 선택해 눌러야 한다(실측 지식).
        eframe = await self._fresh_frame(editor, eframe)
        await self._center_images(editor, eframe)

        census = await self._format_census(eframe, ".se-main-container")

        # ★서식 비교 — 텍스트 유사도만으로 성공 판정하지 않는다(2026-08-20 사용자 지시).
        dst_font = await self._font_census(eframe, ".se-main-container")
        font_diffs = self._compare_font(getattr(self, "_master_font", {}) or {}, dst_font)
        if dst_font:
            self.log(f"   [서식] 결과 — 문단 {dst_font.get('paras')}개 · "
                     f"크기 {dst_font.get('size')} ({dst_font.get('seFs')}) · "
                     f"글꼴 {dst_font.get('family')} · 굵기 {dst_font.get('weight')} · "
                     f"정렬 {dst_font.get('align')}")
        if font_diffs:
            for d in font_diffs:
                self.log(f"   [서식] ❌ 원본과 다름 — {d}")
        else:
            self.log("   [서식] ✅ 원본과 동일(크기·글꼴·굵기·정렬)")
        font_ok = not font_diffs

        await self._shot_page(editor, shot_name)
        # ★READY 조건: 기존 조건 + 이미지 개수 일치 + 주요 서식 일치
        ok = (ok_title and m["len"] > 0 and m["strike"] == 0 and src == 0 and promo == 0
              and img_ok and font_ok)
        if not ok:
            self.log(f"   [READY] ❌ 보류 — 제목 {ok_title} · 이미지 {img_ok} · "
                     f"서식 {font_ok} · strike {m['strike']} · [출처] {src} · Re:purely {promo}")
        return {
            "ok": ok, "editor_url": editor.url, "title": title, "title_ok": ok_title,
            "len": m["len"], "img": m["img"], "strike": m["strike"],
            "source_left": src, "promo_left": promo, "census": census, "from_master": True,
            "img_ok": img_ok, "font_ok": font_ok, "font_diffs": font_diffs,
            "src_img": src_imgs, "font": dst_font,
            "page": editor,          # 발행 단계에서 이 탭을 그대로 쓴다
        }

    async def _paste_from_landing(self, landing_url, title, wait_for_continue,
                                  section_selectors, bulk: int = 1,
                                  edit_copy: bool = False, mobile_preview: bool = True,
                                  publish: bool = False, capture_align: bool = False,
                                  on_published=None):
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._wait_fn = wait_for_continue          # _fill_post 안에서 쓰기 위해 보관
        self._capture_align = capture_align
        # ★ edit_copy 플래그는 반드시 **이 함수에서** 세팅한다(2026-08-20).
        #   예전엔 _open_editor_from_my_blog 에 잘못 들어가 있어 _edit_copy 가 항상 False 였고,
        #   그래서 --edit-copy 를 줘도 옛 '구간 복사' 경로가 돌았다(로그만 성공처럼 보인 원인).
        self._edit_copy = bool(edit_copy)
        self._use_mobile_preview = bool(mobile_preview and edit_copy)
        self.log(f"   [모드] edit_copy={self._edit_copy} · mobile_preview={self._use_mobile_preview}")
        step = "브라우저 시작"
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                step = "네이버 로그인"
                self.log("[1/4] 네이버 로그인(1회). 완료 후 Enter 만 눌러주세요.")
                await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
                await self._wait_until_logged_in(page, wait_for_continue)

                # ★ --edit-copy 는 기준 글의 '수정' 화면에서 통째로 복사하므로
                #   랜딩 스크롤·구간 분석(복사할 구간 N개 …)이 전혀 필요 없다 → 통째로 건너뛴다.
                if getattr(self, "_edit_copy", False):
                    landing = None
                    lframe = None
                    src_census = None
                    targets = []
                    self.log("[2/4] 기준 글 '수정' 화면에서 복사 — 랜딩 구간 분석 생략")
                else:
                    step = "참고 글 새 탭"
                    self.log(f"[2/4] 참고 글을 새 탭으로 엽니다: {landing_url}")
                    landing = await context.new_page()
                    await landing.goto(landing_url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        await landing.wait_for_load_state("networkidle", timeout=6_000)
                    except Exception:  # noqa: BLE001
                        pass
                    await landing.wait_for_timeout(1200)
                    await self._scroll_to_bottom(landing)          # lazy-load 이미지 렌더
                    lframe = await self._content_frame(landing)
                    src_census = await self._format_census(lframe, '.se-main-container')
                    self.log(f"   원문 서식 구성: {src_census}")
                    await self._shot_page(landing, "cmp_source")

                    if not (title or "").strip():
                        title = (await landing.title() or "").strip()
                        title = re.sub(r"\s*[:|-]\s*네이버\s*블로그\s*$", "", title).strip()
                        self.log(f"   제목 미지정 → 참고 글 제목 사용: {title}")

                    step = "구간 목록"
                    targets = section_selectors or await self._content_sections(lframe)
                    self.log(f"[3/4] 복사할 구간 {len(targets)}개 · 작성할 글 {bulk}개")

                step = "글 작성(탭 N개 유지)"
                results = []
                ready_pages = []          # READY 상태로 열어 둘 editor Page 목록
                write_url = None

                # ★ 기준 글의 '수정' 화면에서 통째로 복사하는 경로(2026-08-20 사용자 요청).
                #   뷰 화면 복사는 네이버가 '[출처]' 를 붙이지만 수정 화면 복사는 안 붙는다.
                #   제목도 그 화면에서 그대로 가져오고, 붙여넣기 전에 미리보기를 모바일로 바꾼다.
                if getattr(self, "_edit_copy", False):
                    self.log("")
                    self.log("── 기준 글 '수정' 화면에서 전체 복사 모드 ──")
                    m_page, m_frame, m_title = await self._open_source_editor(context, landing_url)
                    if not await self._mark_master(m_frame, m_page):
                        # 자동 탐색 실패 → 브라우저를 닫지 않고 사용자가 본문을 클릭하게 한다.
                        async def _body_found():
                            sc, _sel, _h = await self._find_body_root(m_page, m_frame)
                            return sc is not None

                        cap, ok = await self._manual_capture(
                            m_page, "기준글 본문 영역", _body_found)
                        if not ok or not await self._mark_master(m_frame, m_page):
                            raise RuntimeError(
                                "기준 글 본문 root 를 찾지 못했습니다 — 위 진단/캡처 로그 확인")
                        if cap:
                            self.log(f"   [본문] 다음부터 자동화에 쓸 selector: {cap}")
                    use_title = (title or "").strip() or m_title
                    self.log(f"   제목: {use_title[:50]!r}")
                    for n in range(1, bulk + 1):
                        self.log("")
                        self.log(f"── [{n}/{bulk}] 새 글 작성(기준 글 복사) ──")
                        try:
                            # 1번은 '글쓰기' 클릭 경로로 열어 URL 을 확보하고,
                            # 2번부터는 그 URL 로 **새 탭**을 연다(앞 글 탭 보호).
                            r = await self._make_post_from_master(
                                context, page, m_page, m_frame, use_title,
                                f"editcopy_{n:03d}", write_url=write_url or "")
                            results.append(r)
                            if r.get("ok") and r.get("page"):
                                if not write_url and r.get("editor_url"):
                                    write_url = r["editor_url"]
                                    self.log(f"   글쓰기 URL 확보: {write_url[:60]!r}")
                                if r["page"] in ready_pages:
                                    self.log(f"[{n}/{bulk}] ⚠️ 앞 글과 같은 탭 — READY 목록에 "
                                             f"중복 추가하지 않습니다")
                                else:
                                    ready_pages.append(r["page"])
                                self.log(f"[{n}/{bulk}] READY — 본문 {r['len']}자 · "
                                         f"이미지 {r['img']}/{r.get('src_img', '?')}개 · "
                                         f"서식 {'일치' if r.get('font_ok') else '다름'} · "
                                         f"[출처] {r['source_left']} · "
                                         f"Re:purely {r['promo_left']}")
                            else:
                                self.log(f"[{n}/{bulk}] READY 보류 — 본문 {r.get('len')}자 · "
                                         f"이미지 {r.get('img')}/{r.get('src_img', '?')}개 · "
                                         f"서식차이 {r.get('font_diffs') or '없음'}")
                        except Exception as exc:  # noqa: BLE001
                            self.log(f"[{n}/{bulk}] 실패 — {type(exc).__name__}: {str(exc)[:120]}")
                            results.append({"ok": False, "error": str(exc)[:200]})
                    try:
                        await m_page.close()      # 기준 글은 저장하지 않고 그대로 닫는다
                        self.log("   기준 글 탭 닫음(저장 안 함)")
                    except Exception:  # noqa: BLE001
                        pass

                # 아래 기존(구간 복사) 루프는 edit_copy 모드에선 돌지 않는다.
                #   ⚠️ bulk 를 0으로 덮으면 최종 리포트 건수가 틀어지므로 루프 범위만 막는다.
                _legacy_n = 0 if getattr(self, "_edit_copy", False) else bulk
                for n in range(1, _legacy_n + 1):
                    self.log("")
                    self.log(f"── [{n}/{bulk}] 글쓰기 탭 생성 및 작성 ──")
                    try:
                        if n == 1:
                            # 1번은 검증된 단건 경로로 열고, 그 URL 을 이후 탭에 재사용
                            editor, eframe, spots = await self._open_new_write(context, page)
                            write_url = editor.url
                            self.log(f"   글쓰기 URL 확보: {write_url[:60]!r}")
                        else:
                            editor, eframe, spots = await self._open_write_tab(context, write_url)

                        r = await self._fill_post(
                            editor, eframe, spots, landing, lframe, targets, title,
                            f"bulk_{n:03d}" if bulk > 1 else "cmp_result")
                        results.append(r)
                        if r.get("ok"):
                            ready_pages.append(editor)      # ★탭을 닫지 않는다
                            self.log(f"[{n}/{bulk}] READY — 본문 {r['len']}자 · "
                                     f"이미지 {r['img']}개 · strike {r['strike']} · "
                                     f"[출처] {r['source_left']} · Re:purely {r['promo_left']}")
                        else:
                            self.log(f"[{n}/{bulk}] READY 실패 — 본문 {r['len']}자 · "
                                     f"strike {r['strike']} · [출처] {r['source_left']} · "
                                     f"Re:purely {r['promo_left']}")
                    except Exception as exc:  # noqa: BLE001  (한 탭 실패가 나머지를 막지 않게)
                        self.log(f"[{n}/{bulk}] 실패 — {type(exc).__name__}: {str(exc)[:120]}")
                        results.append({"ok": False, "error": str(exc)[:200]})

                self.log("")
                self.log(f"── READY {len(ready_pages)}/{bulk} 탭 (모두 열려 있음) ──")
                for i, p in enumerate(ready_pages, 1):
                    self.log(f"   [{i}] {p.url[:70]}")

                step = "결과 비교"
                self._report_bulk(results, bulk, src_census)

                self.log("발행/저장/예약은 하지 않았습니다.")
                published = []
                if publish and ready_pages:
                    self.log("")
                    self.log(f"── 순차 발행 시작 (READY {len(ready_pages)}개) ──")
                    import datetime as _dt
                    self._publish_started_at = _dt.datetime.now().astimezone()
                    published = await self._publish_ready(ready_pages)
                    self._published_urls = [r.get("url", "") for r in published if r.get("ok")]
                    # ★시트 저장은 **브라우저 정리보다 먼저** 한다(2026-08-20 사고).
                    #   전에는 발행이 다 끝난 뒤 context.close() 에서 TargetClosedError 가 나
                    #   예외가 위로 튀어 main.py 의 시트 기록 코드까지 못 갔다
                    #   (발행 5건 성공했는데 시트는 비어 있었다).
                    if on_published and self._published_urls:
                        self.log("")
                        self.log(f"── 시트 저장 (발행 시점 주소 {len(self._published_urls)}건) ──")
                        try:
                            rep = on_published(list(self._published_urls))
                            self._sheet_saved = True
                            self.log(f"   [시트] 저장 완료 ✅ {rep}")
                        except Exception as exc:  # noqa: BLE001
                            self.log(f"   [시트] 저장 실패 — {type(exc).__name__}: {exc}")
                else:
                    self._published_urls = []
                    self.log("발행은 하지 않았습니다(--publish 미지정). READY 탭은 열어 둔 상태입니다.")
                try:
                    wait_for_continue(f"열려 있는 READY 탭 {len(ready_pages)}개를 확인하신 뒤 "
                                      "Enter 를 누르면 브라우저를 닫습니다.")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"   [정리] 대기 건너뜀 — {type(exc).__name__}")
                first = next((r for r in results if r.get("ok")), results[0] if results else {})
                return EditorFillResult(
                    page_url=first.get("editor_url", ""),
                    title=title,
                    body=f"({sum(1 for r in results if r.get('ok'))}/{bulk} 성공)",
                    title_filled=bool(first.get("title_ok")),
                    body_filled=any(r.get("ok") for r in results),
                )
            except Exception as exc:
                self.log(f"[실패] 단계='{step}' 사유={type(exc).__name__}: {exc}")
                raise
            finally:
                # ★이미 닫힌 context 에서 나는 예외는 삼킨다 — 발행 성공/시트 저장 결과를
                #   정리 단계 실패로 뒤엎으면 안 된다(2026-08-20 사고).
                try:
                    await context.close()
                    self.log("브라우저를 정상 종료했습니다.")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"브라우저 정리 중 무시한 예외: {type(exc).__name__}")

    def _report_bulk(self, results: list, bulk: int, src_census: dict | None = None) -> None:
        """글 간 차이를 항목별로 검증한다(본문 누락/이미지 누락/정렬/잔존 문구)."""
        good = [r for r in results if r.get("ok")]
        self.log("")
        self.log("── 검증 ──")
        if src_census:
            self.log(f"   원문 기준: 이미지 {src_census.get('img', 0)}개 · "
                     f"텍스트 {src_census.get('_텍스트길이', 0)}자")
        if not good:
            self.log("   비교할 성공 결과가 없습니다.")
        else:
            def col(k):
                return [r.get(k) for r in good]

            lens, imgs = set(col("len")), set(col("img"))
            self.log(f"   [본문] 길이 {'동일 ✅' if len(lens) == 1 else '차이 ❌'} {sorted(lens)}")
            self.log(f"   [이미지] 개수 {'동일 ✅' if len(imgs) == 1 else '차이 ❌'} {sorted(imgs)}")
            if src_census and imgs:
                src_img = src_census.get("img", 0)
                got = sorted(imgs)[0]
                # 원문의 제품 링크 카드(oglink) 썸네일은 삭제되므로 원문보다 적을 수 있다
                self.log(f"   [이미지] 원문 {src_img}개 → 결과 {got}개 "
                         + ("✅" if got > 0 and got <= src_img else "❌"))

            # 정렬: 이미지 섹션이 전부 center 인지 + 글마다 동일한지
            pairs = [(r.get("img_centered", 0), r.get("img_sections", 0)) for r in good]
            all_centered = all(t > 0 and c >= t for c, t in pairs)
            same = len(set(pairs)) == 1
            self.log(f"   [정렬] 이미지 {pairs} "
                     + ("전부 중앙 ✅" if all_centered else "미정렬 있음 ❌")
                     + (" · 5개 동일 ✅" if same else " · 글마다 다름 ❌"))
            tp = [(r.get("txt_centered", 0), r.get("txt_total", 0)) for r in good]
            self.log(f"   [정렬] 텍스트 {tp} "
                     + ("동일 ✅" if len(set(tp)) == 1 else "차이 ❌"))

            junk = sum(r["strike"] + r["source_left"] + r["promo_left"] for r in good)
            self.log(f"   [잔존] 취소선/[출처]/Re:purely {'없음 ✅' if junk == 0 else f'남음 ❌({junk})'}")

            keys = sorted({k for r in good for k in (r.get("census") or {})})
            for k in keys:
                vals = {(r.get("census") or {}).get(k, 0) for r in good}
                if len(vals) > 1:
                    self.log(f"   [서식차이] {k}: {sorted(vals)}")

        self.log("")
        self.log("── 요약 ──")
        self.log(f"   전체: {bulk}")
        self.log(f"   성공: {len(good)}")
        self.log(f"   실패: {bulk - len(good)}")
        for i, r in enumerate(results, 1):
            if not r.get("ok"):
                why = r.get("error") or (
                    f"제목={r.get('title_ok')} 본문={r.get('len')}자 "
                    f"strike={r.get('strike')} 출처={r.get('source_left')} "
                    f"Re:purely={r.get('promo_left')} 정렬={r.get('centered')}")
                self.log(f"      [{i}] 실패 사유: {why}")

    async def _shot_page(self, page, name: str) -> None:
        try:
            d = self.user_data_dir.parent / "out"
            d.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(d / f"{name}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass

    async def _scroll_to_bottom(self, page, steps: int = 12) -> None:
        """lazy-load 이미지를 강제로 로드시킨다(안 하면 빈 이미지가 복사됨)."""
        try:
            for _ in range(steps):
                await page.mouse.wheel(0, 1200)
                await page.wait_for_timeout(250)
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

    async def _auto_sections(self, page) -> list[str]:
        """구간 선택자 자동 결정. 본문 루트의 직계 자식들을 순서대로 구간으로 삼는다.
        (통째로 한 번에 복사하면 실패하는 랜딩이 있어, 사람이 하던 것처럼 나눠 담는다.)"""
        sels = await page.evaluate(
            """
            () => {
              const root = document.querySelector('main,article,#content,#container,.content')
                           || document.body;
              const kids = Array.from(root.children).filter(el => {
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                if (el.matches('nav,header,footer,script,style,noscript')) return false;
                return (el.innerText || '').trim().length > 0 || el.querySelector('img');
              });
              kids.forEach((el, i) => el.setAttribute('data-blg-section', String(i)));
              return kids.map((_, i) => `[data-blg-section="${i}"]`);
            }
            """
        )
        return sels or ["body"]

    async def _select_element(self, page, selector: str) -> bool:
        """해당 요소 전체를 드래그 선택한 것과 같은 상태로 만든다."""
        try:
            return await page.evaluate(
                """
                (sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return false;
                  el.scrollIntoView({block: 'center'});
                  const range = document.createRange();
                  range.selectNodeContents(el);
                  const s = window.getSelection();
                  s.removeAllRanges();
                  s.addRange(range);
                  return (s.toString().trim().length > 0) || !!el.querySelector('img');
                }
                """,
                selector,
            )
        except Exception:  # noqa: BLE001
            return False

    # ══════════════════════════════════════════════════════════════════
    # 진단 전용 — 에디터까지만 열고 DOM 구조를 찍는다. 입력·저장·발행 전부 없음.
    # ══════════════════════════════════════════════════════════════════
    def probe_editor(self, wait_for_continue: WaitFn) -> str:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true일 때만 실행할 수 있습니다.")
        return asyncio.run(self._probe_editor(wait_for_continue))

    async def _probe_editor(self, wait_for_continue: WaitFn) -> str:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                self.log("[1/4] 네이버 로그인 페이지. 로그인/2차 인증을 직접 완료하세요.")
                await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
                await self._wait_until_logged_in(page, wait_for_continue)

                self.log("[2/4] 내 블로그 메인으로 이동합니다.")
                await page.goto(self.naver_blog_home_url, wait_until="domcontentloaded")
                await self._settle(page, lambda: self._write_button_exists(page), 8000)

                self.log("[3/4] 글쓰기 버튼을 찾습니다.")
                await self._click_write_button(page)
                await self._settle(page, lambda: self._editor_appeared(page), 8000)
                editor = context.pages[-1] if context.pages else page
                await self._dismiss_restore_popup(editor)
                await self._settle(editor, lambda: self._editor_appeared(editor), 6000)

                self.log(f"[4/4] 에디터 URL: {editor.url}")
                self.log(f"   프레임 {len(editor.frames)}개")
                for f in editor.frames:
                    self.log(f"      name={f.name!r} url={(f.url or '')[:70]}")

                self.log("")
                self.log("── 입력 전 구조 ──")
                await self._dump_editable_candidates(editor)

                # 사용자가 직접 제목/본문을 넣으면, '내용이 들어있는' 요소가 무엇인지
                # 확실히 알 수 있다. selector 를 추측하지 않아도 된다.
                wait_for_continue(
                    "브라우저에서 아래 두 문자열을 그대로 입력해 주세요.\n"
                    "      제목칸 → TITLEMARK123\n"
                    "      본문칸 → BODYMARK456\n"
                    "    입력만 하고(발행 전에) 돌아와서 Enter 를 누르세요."
                )

                self.log("")
                self.log("── 입력 후 구조 (내용이 들어간 요소를 확인) ──")
                await self._dump_editable_candidates(editor)
                await self._dump_shadow_editables(editor)
                self.log("")
                self.log("── 입력한 문자열의 실제 위치 ──")
                await self._find_text_location(editor, ["TITLEMARK123", "BODYMARK456"])
                self.log("진단만 수행했습니다. 이 도구는 저장·발행을 하지 않았습니다.")
                return editor.url
            finally:
                await context.close()
                self.log("브라우저를 정상 종료했습니다.")
