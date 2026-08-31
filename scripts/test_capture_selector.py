"""수동 selector 캡처 검증 (로그인 불필요).

확인 항목
  · 사용자가 누른 요소의 tag/id/class/text/aria/href/부모 3단계를 잡는가
  · 랜덤 해시 class 를 selector 로 쓰지 않는가(안정적인 속성 우선)
  · verify() 가 True 가 될 때까지 다음 단계로 넘어가지 않는가
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

PAGE = """<!doctype html><meta charset="utf-8"><body>
<div class="post_footer">
  <div class="btn_area">
    <a href="#" id="editBtn" class="_modifyPost a1b2c3d4e5 _param(224384356096|true)"
       aria-label="수정하기" title="글 수정">수정하기</a>
  </div>
</div>
<script>document.getElementById('editBtn').onclick = () => { window.__entered = true; };</script>
</body>"""


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        page = await br.new_page()
        await page.set_content(PAGE)

        bb = BrowserAutomation.__new__(BrowserAutomation)
        logs: list[str] = []
        bb.log = lambda m="", _l=logs: _l.append(str(m))

        async def verify():
            return bool(await page.evaluate("() => !!window.__entered"))

        # 3초 뒤 '사용자가 클릭'하는 상황을 만든다
        async def user_click():
            await asyncio.sleep(3)
            await page.click("#editBtn")

        task = asyncio.create_task(user_click())
        sel, ok = await BrowserAutomation._manual_capture(
            bb, page, "기준글 '수정' 버튼", verify, timeout_sec=20)
        await task

        print(f"{'✅' if ok else '❌'} 사용자 클릭 감지 후 성공 판정")
        ok_all &= ok
        print(f"{'✅' if sel else '❌'} selector 추출됨 → {sel}")
        ok_all &= bool(sel)

        rand_ok = sel is not None and "a1b2c3d4e5" not in sel
        print(f"{'✅' if rand_ok else '❌'} 랜덤 해시 class 미사용 (a1b2c3d4e5 제외)")
        ok_all &= rand_ok

        joined = "\\n".join(logs)
        for need in ("tag=a", "aria='수정하기'", "부모1:", "selector 후보"):
            hit = need in joined
            print(f"{'✅' if hit else '❌'} 로그에 {need!r} 포함")
            ok_all &= hit

        print()
        print("── 캡처 로그 ──")
        for l in logs:
            if l.strip().startswith(("┌", "│", "├", "└")):
                print(l)

        # 클릭이 없으면 성공 처리하면 안 된다
        await page.set_content(PAGE)
        await page.evaluate("() => { window.__entered = false; }")   # 상태 초기화
        logs.clear()
        sel2, ok2 = await BrowserAutomation._manual_capture(
            bb, page, "모바일 미리보기 버튼", verify, timeout_sec=3)
        no_false = (ok2 is False)
        print()
        print(f"{'✅' if no_false else '❌'} 클릭 없으면 실패 유지(거짓 성공 없음)")
        ok_all &= no_false

        await br.close()
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
