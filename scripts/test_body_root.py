"""수정 에디터 본문 root 탐색 검증 (로그인 불필요).

확인 항목
  · .se-main-container 가 아닌 구조에서도 본문을 찾는가
  · iframe 안에 본문이 있어도 찾는가(모든 frame 순회)
  · 빈 껍데기 컨테이너를 본문으로 오인하지 않는가
  · 못 찾으면 진단 로그(프레임/contenteditable/se-클래스/텍스트)를 남기는가
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

BODY = "흑자는 멜라닌 색소가 뭉쳐 생기는 색소 질환입니다. 자외선이 주된 원인입니다."

CASES = {
    "표준(.se-main-container)":
        f"<div class='se-main-container'><p class='se-text-paragraph'>{BODY}</p>"
        f"<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='></div>",
    "se-content 만 있는 구조":
        f"<div class='se-content'><p class='se-text-paragraph'>{BODY}</p></div>",
    "contenteditable 만 있는 구조":
        f"<div contenteditable='true'><p>{BODY}</p></div>",
    "빈 껍데기 + 진짜 본문":
        f"<div class='se-main-container'></div>"
        f"<div class='se-component-content'><p>{BODY}</p></div>",
}

FRAME_CASE = (
    "<iframe id='mainFrame' srcdoc=\"<div class='se-main-container'>"
    f"<p class='se-text-paragraph'>{BODY}</p></div>\"></iframe>")


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        page = await br.new_page()
        bb = BrowserAutomation.__new__(BrowserAutomation)
        logs: list[str] = []
        bb.log = lambda m="", _l=logs: _l.append(str(m))

        for name, html in CASES.items():
            logs.clear()
            await page.set_content(f"<!doctype html><meta charset='utf-8'><body>{html}</body>")
            sc, sel, head = await BrowserAutomation._find_body_root(bb, page)
            ok = sc is not None and BODY[:10] in (head or "")
            ok_all &= ok
            print(f"{'✅' if ok else '❌'} {name:26} → selector={sel!r}")
            if not ok:
                for l in logs:
                    print("     ", l)

        # ★뷰어(읽기전용) + 편집영역이 함께 있을 때 → 편집영역을 골라야 한다(이번 버그의 핵심)
        logs.clear()
        viewer_plus_editor = (
            "<div class='se-content'><p>흑자에 대한 일반적인 정보 본 콘텐츠의 광고주는 "
            "REPURELY 작성자는 행복하서연 입니다 " + BODY + "</p>"
            "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='></div>"
            "<div contenteditable='true'><div class='se-main-container'>"
            f"<p class='se-text-paragraph'>{BODY}</p>"
            "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='></div></div>")
        await page.set_content(
            f"<!doctype html><meta charset='utf-8'><body>{viewer_plus_editor}</body>")
        sc, sel, head = await BrowserAutomation._find_body_root(bb, page, wait_sec=2)
        editable = sc is not None and "광고주" not in (head or "")
        ok_all &= editable
        print(f"{'✅' if editable else '❌'} {'뷰어+편집영역 → 편집영역 선택':26} → selector={sel!r}")
        if not editable:
            print(f"     선택된 앞부분: {head!r}")

        # iframe 안 본문
        logs.clear()
        await page.set_content(f"<!doctype html><meta charset='utf-8'><body>{FRAME_CASE}</body>")
        await page.wait_for_timeout(600)
        sc, sel, head = await BrowserAutomation._find_body_root(bb, page)
        ok = sc is not None
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} {'iframe 안 본문':26} → selector={sel!r}")

        # 본문 없음 → 진단 로그
        logs.clear()
        await page.set_content("<!doctype html><meta charset='utf-8'><body><div>x</div></body>")
        sc, sel, head = await BrowserAutomation._find_body_root(bb, page)
        no_body = sc is None
        await BrowserAutomation._dump_body_diagnosis(bb, page)
        has_diag = any("[진단]" in l for l in logs)
        print(f"{'✅' if no_body else '❌'} {'본문 없음 → 미검출':26}")
        print(f"{'✅' if has_diag else '❌'} {'진단 로그 출력':26}")
        ok_all &= no_body and has_diag

        await br.close()
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
