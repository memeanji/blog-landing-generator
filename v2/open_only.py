"""브라우저만 띄우고 기다린다 — 사용자가 직접 조작하는 것을 보기 위한 모드.

자동화는 아무것도 하지 않는다. 로그인 화면만 띄우고 계속 열어 둔다.
`--remote-debugging-port` 를 열어 두므로, 사용자가 조작을 끝낸 뒤
`v2/probe_live.py` 로 그 화면의 DOM 을 그대로 읽을 수 있다.

    .\\.venv\\Scripts\\python.exe -m v2.open_only
"""
from __future__ import annotations

import asyncio

from .config import load_settings
from .logger import Log

DEBUG_PORT = 9222


async def main_async(settings, log) -> None:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(settings.user_data_dir),
        headless=False,
        viewport={"width": 1480, "height": 980},
        args=[
            "--disable-blink-features=AutomationControlled",
            f"--remote-debugging-port={DEBUG_PORT}",
        ],
    )
    try:
        await ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                                    origin="https://blog.naver.com")
    except Exception:                                          # noqa: BLE001
        pass

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
    log("[브라우저] 로그인 화면을 띄웠습니다. 자동화는 아무것도 하지 않습니다.")
    log(f"[브라우저] 디버깅 포트 {DEBUG_PORT} 열림 — 조작 끝나면 화면 상태를 읽을 수 있습니다.")
    log("[브라우저] 직접 조작해 보세요. 끝나면 알려주시면 그 화면을 그대로 분석합니다.")

    try:
        while True:
            await asyncio.sleep(5)
            if not ctx.pages:
                log("[브라우저] 모든 탭이 닫혔습니다. 종료합니다.")
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("[브라우저] 중단 요청 — 종료합니다.")


def main() -> int:
    settings = load_settings()
    log = Log(settings.out_dir, tag="v2_open")
    log(f"[로그] {log.path}")
    try:
        asyncio.run(main_async(settings, log))
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
