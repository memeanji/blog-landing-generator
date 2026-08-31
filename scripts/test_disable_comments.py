"""발행 레이어 '댓글 허용 끄기' 검증 (로그인 불필요·실제 코드 경로 사용).

네이버 발행 레이어와 비슷한 형태의 가짜 DOM을 만들어 `_disable_comments` 를 그대로 돌린다.
확인 항목: 켜져 있으면 끈다 / 이미 꺼져 있으면 건드리지 않는다 / 댓글 아닌 옵션은 안 건드린다.

실행: .venv\\Scripts\\python.exe scripts\\test_disable_comments.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

LAYER = """<!doctype html><meta charset="utf-8"><body>
<div class="publish-layer">
  <label for="ck_open"><input type="checkbox" id="ck_open" {open}> 공개</label>
  <label for="ck_cmt"><input type="checkbox" id="ck_cmt" {cmt}> 댓글허용</label>
  <label for="ck_like"><input type="checkbox" id="ck_like" {like}> 공감허용</label>
  <label for="ck_search"><input type="checkbox" id="ck_search" {search}> 검색허용</label>
  <button class="confirm">발행</button>
</div></body>"""


async def state(page) -> dict:
    return await page.evaluate(
        "() => Object.fromEntries(Array.from(document.querySelectorAll(\"input[type='checkbox']\"))"
        ".map(b => [b.id, b.checked]))")


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        page = await br.new_page()
        bb = BrowserAutomation.__new__(BrowserAutomation)
        logs: list[str] = []
        bb.log = lambda m="", _l=logs: _l.append(str(m))
        bb._shot = lambda *a, **k: asyncio.sleep(0)          # 스크린샷 생략

        cases = [
            ("댓글 켜져 있음 → 꺼야 함",
             dict(open="checked", cmt="checked", like="checked", search="checked"),
             {"ck_cmt": False, "ck_open": True, "ck_like": True, "ck_search": True}),
            ("댓글 이미 꺼짐 → 그대로",
             dict(open="checked", cmt="", like="checked", search="checked"),
             {"ck_cmt": False, "ck_open": True, "ck_like": True, "ck_search": True}),
        ]
        for title, fill, expect in cases:
            logs.clear()
            await page.set_content(LAYER.format(**fill))
            await BrowserAutomation._disable_comments(bb, page)
            after = await state(page)
            ok = all(after.get(k) == v for k, v in expect.items())
            ok_all &= ok
            print(f"{'✅' if ok else '❌'} {title}")
            print(f"   결과 {after}")
            for l in logs:
                if l.strip():
                    print(f"   로그: {l.strip()}")
        await br.close()
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
