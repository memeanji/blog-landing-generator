"""하단 제품 링크 삭제 규칙 검증 (로그인 불필요·실제 코드 경로 사용).

가짜 본문 DOM(.se-main-container)을 만들어 `_delete_promo_paragraphs` / `_count_promo` 를
그대로 실행한다. 제품이 바뀌어도(올레놀샷 → 레모니티-C 등) 지워지는지, 본문은 남는지 확인.

실행: .venv\Scripts\python.exe scripts\test_promo_removal.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

# (설명, 문단들, 지워져야 하는 문단 인덱스)
CASES = [
    ("레모니티-C(흑자) — 이번에 안 지워졌던 것", [
        "흑자는 멜라닌 색소가 뭉쳐 생기는 색소 질환입니다.",           # 본문(유지)
        "레모니티-C - Re:purely",                                    # 삭제
        "사용 후 불만족시 100% 환불 보장! 바르는 멀티비타민, 레모니티-C 🍋얼룩덜룩 잡티 "
        "🍋칙칙한 피부톤 🍋딱 1병으로 해결해줄게!",                    # 삭제
        "repurely.com",                                             # 삭제
        "[출처] 흑자에 대한 일반적인 정보|작성자 행복하서연",            # (출처 로직이 따로 처리)
    ], {1, 2, 3}),
    ("올레놀샷(기존) — 회귀 확인", [
        "팔자주름은 나이가 들며 깊어집니다.",
        "Re:purely | 올레놀샷 NMN 포뮬러",
        "Re:purely의 올레놀샷 NMN 포뮬러 사용 후 불만족시 100% 환불",
        "repurely.com",
    ], {1, 2, 3}),
    ("본문만 있는 글 — 오삭제 없어야 함", [
        "기미는 자외선이 주된 원인입니다.",
        "매일 자외선 차단제를 바르는 것이 중요합니다.",
        "충분한 수면과 수분 섭취도 도움이 됩니다.",
    ], set()),
]

HTML = """<!doctype html><meta charset="utf-8"><body>
<div class="se-main-container">{paras}</div></body>"""


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        bb = BrowserAutomation.__new__(BrowserAutomation)          # __init__ 없이 규칙만 사용
        bb.log = lambda *_a, **_k: None

        for title, paras, expect_removed in CASES:
            body = "".join(
                f'<p class="se-text-paragraph" data-i="{i}">{t}</p>' for i, t in enumerate(paras))
            await page.set_content(HTML.format(paras=body))
            frame = page.main_frame

            res = await BrowserAutomation._delete_promo_paragraphs(bb, frame)
            left_idx = set(await frame.evaluate(
                "() => Array.from(document.querySelectorAll('p')).map(e => +e.dataset.i)"))
            removed_idx = set(range(len(paras))) - left_idx
            counts = await BrowserAutomation._count_promo(bb, frame)
            leftover = sum(v for k, v in (counts or {}).items())

            ok = (removed_idx == expect_removed)
            ok_all &= ok
            print(f"{'✅' if ok else '❌'} {title}")
            print(f"   삭제됨 {sorted(removed_idx)} / 기대 {sorted(expect_removed)} "
                  f"· 남은 문단 {len(left_idx)}개 · 제품문구 잔여 {leftover}")
            for d in res.get("detail", []):
                print(f"      - {d['text'][:52]}")
            if not ok:
                for i in sorted(set(paras and range(len(paras)))):
                    mark = "삭제" if i in removed_idx else "유지"
                    print(f"      [{mark}] {paras[i][:56]}")
        await browser.close()
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
