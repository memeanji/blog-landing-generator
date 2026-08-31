"""에디터 body 포커스 기반 복사/붙여넣기 검증 (로그인 불필요).

2026-08-20 사용자 시연 실측을 그대로 재현한다.
  · 포커스가 <iframe> 이면 Ctrl+A 로 아무것도 안 잡힌다
  · 포커스가 iframe 안 body[contenteditable=true] 여야 본문+이미지가 잡힌다
확인 항목
  · _editor_body_frame 이 PostUpdateForm / PostWriteForm 을 구분해 찾는가
  · _focus_editor_body 가 실제로 activeElement 를 body 로 만드는가
  · _select_all_and_copy 가 글자수·이미지수를 잡아내는가(이미지 유실 방지의 핵심)
  · 정렬이 2단계(드롭다운 → 가운데)로 동작하는가
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

IMG = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
BODY = "흑자는 멜라닌 색소가 뭉쳐 생기는 색소 질환입니다. 자외선이 주된 원인입니다."

# 네이버 에디터 구조 재현: iframe 안의 body[contenteditable=true] 가 진짜 편집영역
EDITOR_DOC = (
    "<body contenteditable='true'><div class='se-main-container'>"
    f"<p class='se-text-paragraph'>{BODY}</p>"
    + "".join(f"<div class='se-section-image se-section-align-left'>"
              f"<img src='{IMG}'></div>" for _ in range(5))
    + "</div></body>")


def _host(frame_url_marker: str) -> str:
    """iframe 안에 에디터를 담은 호스트 페이지."""
    return ("<!doctype html><meta charset='utf-8'><body>"
            f"<iframe id='mainFrame' name='{frame_url_marker}' "
            f"srcdoc=\"{EDITOR_DOC}\"></iframe></body>")


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        page = await br.new_page()
        bb = BrowserAutomation.__new__(BrowserAutomation)
        logs: list[str] = []
        bb.log = lambda m="", _l=logs: _l.append(str(m))

        # 실제 URL 로 frame 을 구분하므로 route 로 가짜 URL 을 만든다
        async def _serve(route, req):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=EDITOR_DOC)
        await page.route("**/PostUpdateForm.naver*", _serve)
        await page.route("**/PostWriteForm.naver*", _serve)
        await page.route("**/host*", lambda r, q: asyncio.ensure_future(r.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body="<!doctype html><meta charset='utf-8'><body>"
                 "<iframe id='mainFrame' src='https://blog.naver.com/PostUpdateForm.naver"
                 "?blogId=x'></iframe></body>")))

        await page.goto("https://blog.naver.com/host")
        await page.wait_for_timeout(800)

        # ① frame 탐색
        fr = await BrowserAutomation._editor_body_frame(bb, page, "PostUpdateForm")
        ok = fr is not None
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} PostUpdateForm 편집 frame 탐색")

        # ② 엉뚱한 화면은 못 찾아야 한다
        wrong = await BrowserAutomation._editor_body_frame(bb, page, "PostWriteForm")
        ok = wrong is None
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} PostWriteForm 은 없으므로 미검출(화면 구분)")

        # ③ 포커스 → activeElement 가 body[contenteditable] 인가
        logs.clear()
        ok = await BrowserAutomation._focus_editor_body(bb, page, "PostUpdateForm")
        act = await fr.evaluate("() => document.activeElement.tagName")
        ok = ok and act.lower() == "body"
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} body[contenteditable] 포커스 (activeElement={act})")

        # ④ Ctrl+A → 선택 통계에 본문과 이미지 5개가 잡히는가 (핵심)
        logs.clear()
        st = await BrowserAutomation._select_all_and_copy(bb, page, "PostUpdateForm")
        ok = st["chars"] > 20 and st["imgs"] == 5
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} 전체선택 결과 {st['chars']}자 · 이미지 {st['imgs']}개 "
              f"(기대: 이미지 5개)")
        for l in logs:
            print("     ", l)

        # ⑤ 정렬 2단계 — 드롭다운을 열어야 가운데 버튼이 보이는 UI 를 재현
        await page.goto("data:text/html;charset=utf-8," + (
            "<body>"
            "<button class='se-property-toolbar-drop-down-button se-align-left-toolbar'>"
            "<span class='se-toolbar-icon'>정렬</span></button>"
            "<div id='menu' style='display:none'>"
            "<button class='__se-sentry se-toolbar-option-icon-button "
            "se-toolbar-option-align-center-button'>가운데 정렬</button></div>"
            "<script>"
            "document.querySelector('.se-property-toolbar-drop-down-button').onclick="
            "()=>{document.getElementById('menu').style.display='block'};"
            "document.querySelector('.se-toolbar-option-align-center-button').onclick="
            "()=>{window.__centered=true};"
            "</script></body>"))
        await page.wait_for_timeout(300)
        clicked = await BrowserAutomation._click_image_align_center(bb, page.main_frame)
        centered = await page.evaluate("() => !!window.__centered")
        ok = clicked and centered
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} 정렬 2단계(드롭다운 열기 → 가운데 정렬) 동작")

        await br.close()
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
