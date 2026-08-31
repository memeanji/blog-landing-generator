"""원본 이미지 URL 추출 검증 (로그인 불필요).

2026-08-20 사고: 5개 중 1개만 blogfiles URL, 나머지 4개는
`data:image/svg+xml;base64,...` 자리표시자로 잡혀 복구가 전부 실패했다.
SmartEditor 는 화면 밖 이미지를 lazy-load 자리표시자로 채우고,
실제 주소는 data-src / srcset / __se_module_data JSON 에 들어 있다.

확인 항목
  · src 가 data: 자리표시자여도 실제 http(s) URL 을 찾아내는가
  · data-src / data-lazy-src / srcset / module JSON / a[href] 를 모두 보는가
  · data: URL 을 원본으로 오인하지 않는가
  · 아무 데도 없으면 후보와 outerHTML 을 남기는가
  · 앵커(바로 앞 텍스트 문단)를 같이 잡는가
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

PH = ("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmci"
      "IHZpZXdCb3g9IjAgMCAxIDEiPjwvc3ZnPg==")
B = "https://blogfiles.pstatic.net/MjAyNjA4MjBfMjkz/photo_%d.png"


def comp_text(t):
    return (f"<div class='se-component se-text'>"
            f"<p class='se-text-paragraph'><span>{t}</span></p></div>")


# 5개 이미지 — 실제 URL이 서로 다른 자리에 숨어 있는 상황을 재현
DOC = (
    "<body><div class='se-content' data-blg-master='1'>"
    "<div class='se-component se-documentTitle'><p>흑자에 대한 일반적인 정보</p></div>"
    + comp_text("본 콘텐츠의 광고주는 REPURELY 작성자는 행복하서연 입니다")
    # ① 정상: src 에 실제 URL
    + f"<div class='se-component se-image'><img src='{B % 1}'></div>"
    + comp_text("흑자는 피부 표면에 나타나는 갈색 반점을 말합니다 관리가 필요합니다")
    # ② src 는 자리표시자, 실제는 data-src
    + f"<div class='se-component se-image'><img src='{PH}' data-src='{B % 2}'></div>"
    + comp_text("자외선 차단이 가장 기본이며 꾸준한 보습도 중요한 관리 방법입니다")
    # ③ src 는 자리표시자, 실제는 data-lazy-src + srcset
    + (f"<div class='se-component se-image'><img src='{PH}' "
       f"data-lazy-src='{B % 3}' srcset='{B % 3}?type=w773 773w'></div>")
    + comp_text("생활 습관을 바꾸는 것만으로도 도움이 되는 경우가 많다고 알려져 있습니다")
    # ④ img 에는 아무것도 없고 SE ONE 모듈 JSON 에만 있음
    + (f"<div class='se-component se-image'><img src='{PH}'>"
       f"<script type='text/data' class='__se_module_data' "
       f"data-module='{{\"type\":\"v2_image\",\"data\":{{\"src\":\"{B % 4}\"}}}}'></script>"
       f"</div>")
    + comp_text("색소 질환은 종류가 다양해서 정확한 구분이 먼저라고 이야기합니다")
    # ⑤ 부모 a[href] 에만 있음
    + (f"<div class='se-component se-image'>"
       f"<a href='{B % 5}'><img src='{PH}'></a></div>")
    # ⑥ 어디에도 실제 URL 없음 → 후보/outerHTML 이 찍혀야 한다
    + f"<div class='se-component se-image'><img src='{PH}'></div>"
    + "</div></body>")


async def main() -> int:
    ok_all = True
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        page = await br.new_page()
        bb = BrowserAutomation.__new__(BrowserAutomation)
        logs: list[str] = []
        bb.log = lambda m="", _l=logs: _l.append(str(m))

        await page.goto("data:text/html;charset=utf-8," + DOC.replace("#", "%23"))
        await page.wait_for_timeout(400)

        images = await bb._master_image_manifest(page, page.main_frame)

        print("── 추출 결과 ──")
        for im in images:
            print(f"  [{im['order']}] from={im['from']!r:<16} url={im['url'][:60]}")

        got = {im["order"]: im["url"] for im in images}
        cases = [
            (1, B % 1, "src 에 실제 URL"),
            (2, B % 2, "data-src"),
            (3, B % 3, "data-lazy-src / srcset"),
            (4, B % 4, "__se_module_data JSON"),
            (5, B % 5, "부모 a[href]"),
        ]
        for order, want, why in cases:
            good = got.get(order, "").split("?")[0] == want
            ok_all &= good
            print(f"{'✅' if good else '❌'} [{order}] {why:<24} → {got.get(order, '')[:56]}")

        # data: URL 을 원본으로 인정하면 안 된다
        no_data = all(not (u or "").startswith("data:") for u in got.values())
        ok_all &= no_data
        print(f"{'✅' if no_data else '❌'} data: URL 을 원본으로 오인하지 않음")

        # ⑥번은 못 찾고, 후보/outerHTML 이 남아야 한다
        six = next((im for im in images if im["order"] == 6), {})
        dbg = (not six.get("url")) and bool(six.get("html")) and bool(six.get("cands"))
        ok_all &= dbg
        print(f"{'✅' if dbg else '❌'} 실제 URL 없는 컴포넌트 → 후보+outerHTML 디버그 출력")

        # 앵커(바로 앞 텍스트)를 잡았는지
        anch = all(len(im.get("anchor") or "") > 5 for im in images if im["order"] > 1)
        ok_all &= anch
        print(f"{'✅' if anch else '❌'} 앵커 문단 확보 "
              f"(예: {images[1].get('anchor', '')[:24]!r})")

        print()
        print("── 로그 ──")
        for line in logs:
            print("   ", line)
        await br.close()

    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
