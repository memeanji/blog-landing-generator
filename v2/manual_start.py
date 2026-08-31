r"""로그인 → 참고글 탭 → 새 글 탭(모바일 전환) 까지만 하고 **멈춰서 기다린다**.

자동 입력/복사/붙여넣기/발행은 **하나도 하지 않는다**. 사람이 직접 조작하는 방식을
보여주기 위한 관찰 모드다. `--remote-debugging-port 9222` 를 열어 두므로,
조작이 끝난 뒤 그 화면의 DOM 을 그대로 읽어 분석할 수 있다.

    .\.venv\Scripts\python.exe -m v2.manual_start --url https://blog.naver.com/<blog_id>/<logNo>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback

from .config import load_settings
from .logger import Log
from . import browser, writer

DEBUG_PORT = 9222


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="로그인+새 글(모바일)까지만 열고 대기")
    p.add_argument("--url", required=True, help="참고글 URL (읽기용 탭으로 함께 열어 둔다)")
    p.add_argument("--ref-update", action="store_true",
                   help="참고글을 발행 화면 대신 '수정' 화면(?Redirect=Update)으로 연다")
    p.add_argument("--blog-id", help="내 블로그 ID 직접 지정(자동 탐색 건너뜀)")
    p.add_argument("--relogin", action="store_true",
                   help="저장된 네이버 세션을 지우고 반드시 직접 로그인한다")
    p.add_argument("--no-write-tab", action="store_true",
                   help="새 글 탭을 열지 않는다(로그인+참고글만)")
    return p.parse_args(argv)


async def launch_with_debug(settings, log):
    """browser.launch 와 같되 디버깅 포트를 추가로 연다."""
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
    except Exception as exc:                                   # noqa: BLE001
        log(f"[브라우저] 클립보드 권한 부여 실패(무시): {type(exc).__name__}")
    log(f"[브라우저] 실행 완료 · 프로필={settings.user_data_dir}")
    log(f"[브라우저] 디버깅 포트 {DEBUG_PORT} 열림 — 조작 끝난 화면을 그대로 읽을 수 있습니다.")
    return pw, ctx


def _update_url(ref_url: str) -> str:
    import re
    m = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d+)", ref_url)
    if not m:
        return ref_url
    return (f"https://blog.naver.com/{m.group(1)}"
            f"?Redirect=Update&logNo={m.group(2)}")


# 모바일 확정 로직은 writer.NewPost.ensure_mobile() 로 옮겼다(2026-08-21).


async def main_async(args, settings, log) -> int:
    pw = ctx = None
    try:
        pw, ctx = await launch_with_debug(settings, log)

        # 1. 사람이 직접 로그인
        logged = await browser.wait_manual_login(
            ctx, log, force=args.relogin, blog_home_url=settings.blog_home_url)
        blog_id = args.blog_id or logged or await browser.resolve_blog_id(ctx, settings, log)

        # 2. 참고글 탭 (읽기 전용 — 아무것도 건드리지 않는다)
        ref_url = _update_url(args.url) if args.ref_update else args.url
        ref_page = await ctx.new_page()
        await ref_page.goto(ref_url, wait_until="domcontentloaded")
        await ref_page.wait_for_timeout(2000)
        log(f"[참고글] 탭 열기 완료 — {ref_page.url[:90]}")

        # 3. 새 글 탭 → 모바일 전환 (여기까지만)
        if not args.no_write_tab:
            post = await writer.open_write(ctx, blog_id, log)
            await post.switch_to_mobile()
            await post.ensure_mobile()
            await post.page.bring_to_front()
            await post.shot("v2_manual_ready", settings.out_dir)
            log("[새글] 모바일 화면까지 준비 완료 — 여기서부터는 아무것도 하지 않습니다.")

        log("")
        log("──────────────────────────────────────────────")
        log(" 이제 직접 조작해 보세요. 제목/본문/복사 전부 사람이 하시면 됩니다.")
        log(" 끝나고 알려주시면 그 화면 상태를 그대로 읽어서 분석하겠습니다.")
        log(" (창을 닫거나 Ctrl+C 를 누르면 종료됩니다)")
        log("──────────────────────────────────────────────")

        while True:
            await asyncio.sleep(5)
            if not ctx.pages:
                log("[브라우저] 모든 탭이 닫혔습니다. 종료합니다.")
                return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("[중단] 사용자가 중단했습니다.")
        return 130
    except Exception as exc:                                   # noqa: BLE001
        log(f"[오류] {exc}")
        log(traceback.format_exc())
        for i, pg in enumerate(list(ctx.pages) if ctx else []):
            try:
                shot = settings.out_dir / f"v2_manual_error_{i}.png"
                await pg.screenshot(path=str(shot))
                log(f"[오류] 화면 저장: {shot}  (url={pg.url[:80]})")
            except Exception:                                  # noqa: BLE001
                pass
        return 1
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:                                  # noqa: BLE001
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:                                  # noqa: BLE001
                pass


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    log = Log(settings.out_dir, tag="v2_manual")
    log(f"[로그] {log.path}")
    try:
        return asyncio.run(main_async(args, settings, log))
    except KeyboardInterrupt:
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
