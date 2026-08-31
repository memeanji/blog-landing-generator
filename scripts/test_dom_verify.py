"""본문 DOM 비교 검증 테스트 (로그인 불필요, 클립보드 권한 불필요).

2026-08-20 사고: Ctrl+C 가 실패했는데도 진행해, 실행 전 CMD 명령어가
새 글 본문에 그대로 붙여넣어졌다. 이제 붙여넣은 DOM 을 기준글과 대조해 막는다.

확인 항목
  · 기준글 본문 DOM 읽기(source_text)
  · CMD 명령어가 붙었으면 → 차단
  · 본문이 비었거나 전혀 다른 글이면 → 차단
  · 공백/줄바꿈만 다른 정상 붙여넣기 → 통과
  · 뒤에 문구가 조금 붙어도(네이버 자동 추가) → 통과
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.services.browser import BrowserAutomation  # noqa: E402

BODY = ("팔자주름은 나이가 들면서 피부 탄력이 떨어져 생깁니다. "
        "콜라겐 감소가 주된 원인이며 꾸준한 관리가 필요합니다. "
        "특히 자외선 차단과 보습이 기본입니다.")

CMD = ("powershell\ncd C:\\Users\\894플러스\\blog_landing_generator\n"
       ".venv\\Scripts\\python.exe main.py --paste --edit-copy --bulk 1")

# ★수정 화면 구조 재현. **index 로 판단하지 않는지** 확인하는 게 핵심이라
#   일부러 제목을 첫 번째가 아닌 자리에 두고, 자리표시자/빈 컴포넌트도 섞는다.
TITLE = "팔자주름에 대한 일반적인 정보"
IMG1 = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
EDITOR_DOC = (
    "<body><div class='se-content' data-blg-master='1'>"
    "<div class='se-component se-sectionTitle'></div>"                    # 빈 컴포넌트
    f"<div class='se-component se-documentTitle'><p>{TITLE}</p></div>"    # 제목(2번째!)
    f"<div class='se-component se-text'><p class='se-text-paragraph'>{BODY}</p></div>"
    f"<div class='se-component se-image'><img src='{IMG1}'></div>"
    "<div class='se-component se-text'><p>짧음</p></div>"                  # 10자 미만
    f"<div class='se-component se-image'><img src='{IMG1}'></div>"
    "<div class='se-component se-placeholder'>추가할 컴포넌트를 선택하세요.</div>"
    "</div></body>")


async def main() -> int:
    ok_all = True
    bb = BrowserAutomation.__new__(BrowserAutomation)
    logs: list[str] = []
    bb.log = lambda m="", _l=logs: _l.append(str(m))

    print("── 비교 판정 ──")
    cases = [
        ("정상(동일)", BODY, True),
        ("공백/줄바꿈만 다름", BODY.replace(" ", "\n  "), True),
        ("뒤에 문구 추가됨", BODY + "\n\n출처: 리퓨어리", True),
        ("CMD 명령어", CMD, False),
        ("빈 본문", "", False),
        ("전혀 다른 글", "오늘 점심은 김치찌개를 먹었습니다. 맛있었어요.", False),
        ("앞부분만 붙음(절반)", BODY[:40], False),
    ]
    for name, target, want in cases:
        ok, why = bb._text_match(BODY, target)
        good = ok is want
        ok_all &= good
        print(f"{'✅' if good else '❌'} {name:18} → {'통과' if ok else '차단'} · {why}")

    # 기준글을 못 읽은 경우 → 검증 불가이므로 차단
    ok, why = bb._text_match("", BODY)
    good = ok is False
    ok_all &= good
    print(f"{'✅' if good else '❌'} {'기준글 못 읽음':18} → {'통과' if ok else '차단'} · {why}")

    print()
    print("── 실제 DOM 읽기 ──")
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        page = await br.new_page()

        async def _serve(route, req):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=EDITOR_DOC)
        await page.route("**/PostUpdateForm.naver*", _serve)
        await page.route("**/host*", lambda r, q: asyncio.ensure_future(r.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body="<!doctype html><meta charset='utf-8'><body><iframe id='mainFrame' "
                 "src='https://blog.naver.com/PostUpdateForm.naver?blogId=x'></iframe></body>")))
        await page.goto("https://blog.naver.com/host")
        await page.wait_for_timeout(700)

        src = await bb._read_body_text(page, "PostUpdateForm")
        good = BODY[:20] in src
        ok_all &= good
        print(f"{'✅' if good else '❌'} 기준글 본문 DOM 읽기 → {len(src)}자")

        logs.clear()
        st = await bb._copy_master_body(page)
        # ★제목이 선택에 들어가면 안 된다(사용자 지시 2026-08-20).
        no_title = TITLE not in "".join(l for l in logs if "선택 앞부분" in l)
        no_ph = "추가할 컴포넌트" not in (st.get("text") or "")
        good = (st["chars"] > 0 and st["imgs"] == 2
                and BODY[:20] in (st.get("text") or "") and no_title and no_ph)
        ok_all &= good
        print(f"{'✅' if good else '❌'} 선택+복사 → {st['chars']}자 · 이미지 {st['imgs']}개 "
              f"· 제목 제외 {'✅' if no_title else '❌'} "
              f"· 자리표시자 제외 {'✅' if no_ph else '❌'}")
        for l in logs:
            print("     ", l)

        # 편집영역이 없으면 복사 실패로 잡혀야 한다
        await page.goto("data:text/html;charset=utf-8,<body>편집영역 없음</body>")
        await page.wait_for_timeout(300)
        st2 = await bb._copy_master_body(page)
        good = st2["chars"] == 0
        ok_all &= good
        print(f"{'✅' if good else '❌'} 편집영역 없음 → 복사 실패 (chars={st2['chars']})")

        await br.close()

    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
