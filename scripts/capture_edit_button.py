"""기준글 '수정' 버튼 selector만 잡는 전용 도구 (발행·복사 없음).

동작: 기준글을 열고 클릭 캡처를 심은 뒤 대기 → 사용자가 '수정' 버튼을 직접 클릭 →
      그 요소의 tag/id/class/text/aria/href/부모3단계와 selector 후보를 출력하고 종료.
브라우저는 기존 프로필을 그대로 쓰므로(로그인 유지) 별도 로그인 절차가 없다.

실행: .venv\\Scripts\\python.exe scripts\\capture_edit_button.py [기준글URL]
"""
import asyncio
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.services.browser import BrowserAutomation  # noqa: E402

print = functools.partial(print, flush=True)   # 진행 상황을 즉시 보이게

DEFAULT_URL = "https://blog.naver.com/<blog_id>/<logNo>"   # 흑자/머니(연습) 기준글
WAIT_SEC = 900        # 클릭 캡처 대기(15분)
LOGIN_SEC = 900       # 로그인 대기(15분)


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    st = load_settings()
    profile = Path(st.playwright_user_data_dir)
    print(f"프로필: {profile}")
    print(f"기준글: {url}")

    bb = BrowserAutomation.__new__(BrowserAutomation)
    bb.log = print

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False,
            args=["--start-maximized"], no_viewport=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # ── ① 로그인 대기 ────────────────────────────────────────────
        #   프로필에 세션이 없으면 '수정' 버튼 자체가 안 보인다 → 먼저 사람이 로그인.
        await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded",
                        timeout=60_000)
        print()
        print("=" * 66)
        print("  ① 이 창에서 네이버에 직접 로그인해 주세요.")
        print("     로그인이 확인되면 기준글로 자동 이동합니다.")
        print("=" * 66)
        logged = False
        for i in range(LOGIN_SEC):                      # 로그인 대기
            await page.wait_for_timeout(1000)
            try:
                cookies = await ctx.cookies()
                names = {c["name"] for c in cookies if "naver" in (c.get("domain") or "")}
                if {"NID_AUT", "NID_SES"} <= names:
                    logged = True
                    break
            except Exception:  # noqa: BLE001
                pass
            if i and i % 30 == 0:
                print(f"   로그인 대기 중… {i}s")
        if not logged:
            print("   로그인이 확인되지 않았습니다. 그래도 기준글로 이동해 봅니다.")
        else:
            print("   로그인 확인 ✅")

        # ── ② 기준글로 이동 ─────────────────────────────────────────
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1500)
        print(f"실제 URL: {page.url}")

        # 자동 후보가 실제로 잡히는지 먼저 보고
        print()
        print("── 자동 후보 탐색 결과 ──")
        for sel in BrowserAutomation.EDIT_SELECTORS:
            for scope in [page] + list(page.frames):
                try:
                    n = await scope.locator(sel).count()
                except Exception as e:  # noqa: BLE001
                    print(f"   {sel:34} @{(scope.url or '')[:34]} → 오류 {type(e).__name__}")
                    continue
                if n:
                    vis = False
                    try:
                        vis = await scope.locator(sel).first.is_visible()
                    except Exception:  # noqa: BLE001
                        pass
                    print(f"   {sel:34} @{(scope.url or '')[:34]} → {n}개 (보임={vis})")

        print()
        print("=" * 66)
        print(f"  브라우저에서 '수정' 버튼을 직접 클릭해 주세요. (최대 {WAIT_SEC // 60}분)")
        print("  클릭하시면 selector 후보를 출력합니다.")
        print("=" * 66)

        await bb._arm_capture(page)
        waited = 0
        clicks = 0
        entered = False
        while waited < WAIT_SEC:
            await page.wait_for_timeout(1000)
            waited += 1
            for pg in list(ctx.pages):            # 새 탭/프레임에도 계속 캡처를 심는다
                try:
                    await bb._arm_capture(pg)
                except Exception:  # noqa: BLE001
                    pass
            # ★첫 클릭에서 끝내지 않는다 — 두 번째(확인 등) 클릭까지 전부 기록한다
            for pg in list(ctx.pages):
                try:
                    hits = await bb._read_capture(pg)
                except Exception:  # noqa: BLE001
                    continue
                for hit in hits:
                    clicks += 1
                    print("")
                    print(f"   ===== 클릭 #{clicks} =====")
                    bb._report_capture("기준글 '수정' 관련", hit)
            if not entered:
                ed, why = await bb._editor_evidence(ctx, page)
                if ed:
                    entered = True
                    print("")
                    print(f"   [확인] 수정 화면 진입 감지 OK — {why}")
                    print("   (추가 클릭 30초 더 받고 종료합니다.)")
                    waited = max(waited, WAIT_SEC - 30)
            if waited % 60 == 0:
                print(f"   대기 중… {waited}s · 클릭 {clicks}건 · 수정화면진입={entered}")
        print("")
        print(f"   [종료] 총 클릭 {clicks}건 · 수정화면진입={entered}")
        await ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
