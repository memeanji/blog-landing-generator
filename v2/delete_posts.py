r"""테스트로 발행한 글을 삭제한다. **지정한 글 번호만** 지운다.

    .\.venv\Scripts\python.exe -m v2.delete_posts --dry-run
    .\.venv\Scripts\python.exe -m v2.delete_posts --yes

기본은 dry-run — 무엇을 지울지 보여주고 아무것도 건드리지 않는다. `--yes` 를 붙여야 실제로
삭제한다(되돌릴 수 없다).

★기준글은 절대 지우지 않는다. `KEEP` 에 넣어 두고, 지울 목록과 겹치면 그 자리에서 멈춘다.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import traceback

from .config import load_settings
from .logger import Log
from . import browser

# ★절대 지우면 안 되는 글(기준글) — 실수 방지용 안전핀
KEEP = {
    "224385299393",   # 팔자주름에 대한 일반적인 정보 (gfa 기준글)
    "224385300749",   # 흑자에 대한 일반적인 정보   (카모 기준글)
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="발행된 글 삭제(지정한 번호만)")
    p.add_argument("--posts", nargs="*", default=[],
                   help="지울 글 번호(logNo) 목록. 비우면 --from-log 로 찾는다")
    p.add_argument("--from-log", action="store_true",
                   help="out/v2_*.log 의 '[발행] 완료' URL 에서 글 번호를 모은다")
    p.add_argument("--blog-id", help="내 블로그 ID 직접 지정")
    p.add_argument("--yes", action="store_true",
                   help="실제로 삭제한다(없으면 dry-run — 목록만 보여준다)")
    p.add_argument("--relogin", action="store_true", help="세션을 지우고 직접 로그인")
    return p.parse_args(argv)


def collect_from_logs(out_dir, log) -> list[str]:
    found: list[str] = []
    for path in sorted(out_dir.glob("v2_*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            continue
        for m in re.finditer(r"\[발행\] 완료 — https://blog\.naver\.com/[^/]+/(\d+)", text):
            no = m.group(1)
            if no not in found:
                found.append(no)
    log(f"[수집] 로그에서 발행 글 {len(found)}건 — {found}")
    return found


DELETE_BTN_JS = r"""() => {
     const out = [];
     document.querySelectorAll("a,button,[role='button']").forEach(el => {
       const t = (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
       if (!/^삭제$|삭제하기/.test(t)) return;
       const r = el.getBoundingClientRect();
       if (r.width < 4 || r.height < 4) return;
       out.push({t: t, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                 cls: (el.className || '').toString().slice(0, 40)});
     });
     return out;
   }"""


async def delete_one(ctx, blog_id: str, no: str, log) -> bool:
    """글 1개 삭제. 성공하면 True."""
    page = await ctx.new_page()
    url = f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={no}"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    ok = False
    try:
        for scope in [page.main_frame] + list(page.frames):
            try:
                cands = await scope.evaluate(DELETE_BTN_JS)
            except Exception:                                  # noqa: BLE001
                continue
            if not cands:
                continue
            log(f"   [{no}] 삭제 버튼 후보 {len(cands)}개 — {[c['t'] for c in cands][:3]}")
            await scope.evaluate(
                r"""() => {
                     const els = Array.from(document.querySelectorAll("a,button,[role='button']"))
                       .filter(el => /^삭제$|삭제하기/.test(
                           (el.innerText || '').replace(/\s+/g, ' ').trim()));
                     if (els[0]) els[0].click();
                   }""")
            await page.wait_for_timeout(1500)

            # 확인 팝업의 '삭제' 를 한 번 더
            for sc2 in [page.main_frame] + list(page.frames):
                try:
                    await sc2.evaluate(
                        r"""() => {
                             const els = Array.from(document.querySelectorAll(
                                 "a,button,[role='button']"))
                               .filter(el => /^삭제$/.test(
                                   (el.innerText || '').replace(/\s+/g, ' ').trim()));
                             if (els.length) els[els.length - 1].click();
                           }""")
                except Exception:                              # noqa: BLE001
                    pass
            await page.wait_for_timeout(2500)
            ok = True
            break
        if not ok:
            log(f"   [{no}] ⚠ 삭제 버튼을 찾지 못했습니다 — 직접 지워주세요: "
                f"https://blog.naver.com/{blog_id}/{no}")
    finally:
        try:
            await page.close()
        except Exception:                                      # noqa: BLE001
            pass
    return ok


async def verify_gone(no: str, blog_id: str) -> bool:
    """삭제됐는지 확인 — 모바일 뷰가 noPost 로 튕기면 삭제된 것."""
    import urllib.request
    req = urllib.request.Request(
        f"https://m.blog.naver.com/{blog_id}/{no}",
        headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(4000).decode("utf-8", "replace")
    except Exception:                                          # noqa: BLE001
        return False
    return "noPost" in body or "errorType" in body


async def main_async(args, settings, log) -> int:
    targets = [str(x).strip() for x in (args.posts or []) if str(x).strip()]
    if not targets and args.from_log:
        targets = collect_from_logs(settings.out_dir, log)
    if not targets:
        log("[중단] 지울 글 번호가 없습니다. --posts 로 지정하거나 --from-log 를 쓰세요.")
        return 2

    keep_hit = [t for t in targets if t in KEEP]
    if keep_hit:
        log(f"[중단] 기준글이 목록에 있습니다: {keep_hit} — 안전을 위해 아무것도 지우지 않습니다.")
        return 2

    log(f"[대상] {len(targets)}건 — {targets}")
    for t in targets:
        log(f"        https://blog.naver.com/{{blogId}}/{t}")
    if not args.yes:
        log("[dry-run] --yes 가 없어 실제로 지우지 않았습니다.")
        return 0

    pw = ctx = None
    try:
        pw, ctx = await browser.launch(settings, log)
        logged = await browser.wait_manual_login(
            ctx, log, force=args.relogin, blog_home_url=settings.blog_home_url)
        blog_id = args.blog_id or logged or await browser.resolve_blog_id(ctx, settings, log)
        log(f"[블로그] 삭제 대상 계정 = {blog_id}")

        done, failed = [], []
        for no in targets:
            log(f"[삭제] {no} …")
            await delete_one(ctx, blog_id, no, log)
            await asyncio.sleep(1.5)
            if await verify_gone(no, blog_id):
                log(f"   [{no}] 삭제 확인 ✅")
                done.append(no)
            else:
                log(f"   [{no}] ❌ 아직 남아 있습니다")
                failed.append(no)

        log("")
        log(f"[완료] 삭제 {len(done)}건 · 실패 {len(failed)}건")
        if failed:
            log(f"       남은 글: {failed}")
        return 0 if not failed else 1
    except Exception as exc:                                   # noqa: BLE001
        log(f"[오류] {exc}")
        log(traceback.format_exc())
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
    log = Log(settings.out_dir, tag="v2_delete")
    log(f"[로그] {log.path}")
    try:
        return asyncio.run(main_async(args, settings, log))
    except KeyboardInterrupt:
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
