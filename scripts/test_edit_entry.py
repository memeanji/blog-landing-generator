"""'수정 진입 / 모바일 전환' 검증 로직 테스트 (로그인 불필요).

핵심 확인: **클릭만 되고 아무 일도 안 일어나면 실패로 판정**하는가(거짓 성공 방지).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

DEAD = """<!doctype html><meta charset="utf-8"><body>
<a href="#" class="_modifyPost _returnFalse _param(224384356096|true)">수정하기</a>
<button>모바일</button>
</body>"""      # 눌러도 아무 일 없음 → 둘 다 실패로 나와야 정상

LIVE_MOBILE = """<!doctype html><meta charset="utf-8"><body>
<button id="m" class="preview-btn mobile">모바일</button>
<div id="box" class="mobile-preview" style="display:none;width:360px;height:400px"></div>
<script>document.getElementById('m').onclick = () => {
  document.getElementById('m').className = 'preview-btn mobile selected';
  document.getElementById('box').style.display = 'block';
};</script></body>"""


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        ctx = await br.new_context()
        page = await ctx.new_page()
        bb = BrowserAutomation.__new__(BrowserAutomation)
        logs: list[str] = []
        bb.log = lambda m="", _l=logs: _l.append(str(m))
        bb._shot = lambda *a, **k: asyncio.sleep(0)
        bb._dump_publish_layer = lambda *a, **k: asyncio.sleep(0)

        # ① 아무 동작 없는 '수정하기' → 에디터 증거 없음 → 예외
        logs.clear()
        await page.set_content(DEAD)
        ok = False
        try:
            await BrowserAutomation._open_source_editor(bb, ctx, "about:blank")
        except RuntimeError as e:
            ok = "진입 실패" in str(e)
        print(f"{'✅' if ok else '❌'} 죽은 '수정하기' → 실패로 판정 (거짓 성공 없음)")
        ok_all &= ok
        assert not any("진입 확인" in l for l in logs), "성공 로그가 잘못 찍힘"

        # ② 아무 동작 없는 '모바일' → 상태 변화 없음 → False
        logs.clear()
        await page.set_content(DEAD)
        r = await BrowserAutomation._switch_preview_mobile(bb, page)
        ok = (r is False) and not any("전환 확인" in l for l in logs)
        print(f"{'✅' if ok else '❌'} 죽은 '모바일' 버튼 → False 반환 (거짓 성공 없음)")
        ok_all &= ok

        # ③ 실제로 전환되는 '모바일' → True + 성공 로그
        logs.clear()
        await page.set_content(LIVE_MOBILE)
        r = await BrowserAutomation._switch_preview_mobile(bb, page)
        ok = (r is True) and any("전환 확인" in l for l in logs)
        print(f"{'✅' if ok else '❌'} 동작하는 '모바일' 버튼 → True + 성공 로그")
        for l in logs:
            if "전환" in l:
                print(f"   {l.strip()}")
        ok_all &= ok

        await br.close()
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
