r"""이미지 복사 진단 — **수정 화면 vs 발행 화면** 을 나란히 비교한다(읽기 전용).

붙여넣기도 저장도 하지 않는다. 다음 세 가지만 확인한다.

  1) 원본 이미지의 `src` 가 무엇인가 — `blob:` / `data:` / 네이버 내부 URL / 실제 파일 URL
  2) 작은 Range(이미지 1~2장)를 복사했을 때 **클립보드에 실제로 담기는 MIME 종류**
  3) 클립보드 `text/html` 안의 `<img>` 태그가 어떤 주소를 가리키는가

    .\.venv\Scripts\python.exe -m v2.diag_copy --url https://blog.naver.com/<blog_id>/<logNo>
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import traceback

from .config import load_settings
from .logger import Log
from . import browser, edit_post, source_view

# 원본 DOM 의 이미지 src 를 훑는다.
IMG_SRC_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: 'root 없음'};
     const imgs = Array.from(root.querySelectorAll('img')).slice(0, 8);
     const kind = u => !u ? '(빈값)'
                    : u.startsWith('blob:') ? 'blob:'
                    : u.startsWith('data:') ? 'data:'
                    : /pstatic\.net|phinf|naver\.net/.test(u) ? '네이버 파일서버'
                    : u.startsWith('http') ? '외부 http' : '기타';
     return {ok: true, total: root.querySelectorAll('img').length,
             rows: imgs.map(im => ({
               kind: kind(im.getAttribute('src') || ''),
               src: (im.getAttribute('src') || '').slice(0, 110),
               dataSrc: (im.getAttribute('data-src') || '').slice(0, 80),
               cls: (im.className || '').toString().slice(0, 40),
               w: im.naturalWidth, h: im.naturalHeight}))};
   }"""

CLIP_TYPES_JS = r"""async () => {
     try {
       const items = await navigator.clipboard.read();
       return {ok: true, items: items.map(i => i.types)};
     } catch (e) { return {ok: false, why: String(e).slice(0, 120)}; }
   }"""

CLIP_HTML_JS = r"""async () => {
     try {
       const items = await navigator.clipboard.read();
       for (const it of items) {
         if (it.types.includes('text/html')) {
           const b = await it.getType('text/html');
           return {ok: true, html: (await b.text()).slice(0, 20000)};
         }
       }
       return {ok: false, why: 'text/html 없음'};
     } catch (e) { return {ok: false, why: String(e).slice(0, 120)}; }
   }"""


def _report_html(html: str, log) -> None:
    imgs = re.findall(r"<img[^>]*>", html, re.I)
    log(f"      클립보드 html 길이 {len(html)}자 · <img> 태그 {len(imgs)}개")
    for tag in imgs[:4]:
        src = re.search(r'src="([^"]*)"', tag, re.I)
        src = src.group(1) if src else ""
        kind = ("blob:" if src.startswith("blob:")
                else "data:" if src.startswith("data:")
                else "네이버 파일서버" if re.search(r"pstatic\.net|phinf|naver\.net", src)
                else "외부 http" if src.startswith("http") else "(없음/기타)")
        log(f"        · src 종류={kind} {src[:100]!r}")
        keep = re.findall(r'(data-[a-z-]+|id|class)="([^"]{0,40})"', tag, re.I)
        log(f"          속성: {keep[:5]}")
    if not imgs:
        log("      ⚠ <img> 태그가 하나도 없습니다 — 텍스트만 복사된 것입니다")
        log(f"      html 앞부분: {html[:200]!r}")


async def probe(src, label: str, log) -> None:
    """원본 이미지 src + 아주 작은 Range 복사 결과를 찍는다."""
    log("")
    log(f"════ {label} ════")
    fr = await src._fr()
    info = await fr.evaluate(IMG_SRC_JS)
    if not info.get("ok"):
        log(f"   본문 root 를 못 찾았습니다 — {info.get('why')}")
        return
    log(f"   원본 이미지 {info['total']}장 · 앞 {len(info['rows'])}장의 src:")
    for r in info["rows"]:
        log(f"      · {r['kind']:<12} {r['w']}x{r['h']} {r['src']!r}")
        if r["dataSrc"]:
            log(f"        data-src={r['dataSrc']!r}")

    # 이미지가 1~2장만 들어가는 아주 작은 구간을 만든다
    chunks = edit_post.plan_chunks(src, max_imgs=1)
    if not chunks:
        log("   복사할 구간이 없습니다")
        return
    ch = chunks[0]
    log(f"   작은 구간 복사 시도 — #{ch['first']}~#{ch['last']} "
        f"(컴포넌트 {ch['comps']}개 · 이미지 {ch['imgs']}장 · {ch['chars']}자)")
    try:
        got = await edit_post.copy_chunk(src, ch, log)
        log(f"   복사 성공 — 선택 결과 {got}")
    except Exception as exc:                                   # noqa: BLE001
        log(f"   복사 실패 — {exc}")
        return

    types = await src.page.evaluate(CLIP_TYPES_JS)
    if types.get("ok"):
        log(f"   클립보드 MIME: {types['items']}")
        flat = [t for row in types["items"] for t in row]
        if not any(t.startswith("image/") for t in flat):
            log("      ⚠ image/* 항목이 없습니다 — 이미지가 파일로는 안 담겼습니다")
    else:
        log(f"   클립보드 MIME 읽기 실패 — {types.get('why')}")

    html = await src.page.evaluate(CLIP_HTML_JS)
    if html.get("ok"):
        _report_html(html["html"], log)
    else:
        log(f"   클립보드 text/html 읽기 실패 — {html.get('why')}")


async def main_async(args, settings, log) -> int:
    pw = ctx = None
    try:
        pw, ctx = await browser.launch(settings, log)
        await browser.wait_manual_login(ctx, log, blog_home_url=settings.blog_home_url)

        if args.mode in ("both", "edit"):
            try:
                src_edit, _ = await edit_post.open_source_edit(ctx, args.url, log,
                                                               label="수정 화면")
                await probe(src_edit, "수정 화면(실전용 현재 방식)", log)
            except Exception as exc:                           # noqa: BLE001
                log(f"[수정 화면] 진단 실패 — {exc}")

        if args.mode in ("both", "view"):
            try:
                src_view = await source_view.open_source(ctx, args.url, log)
                await src_view.scan()
                await probe(src_view, "발행 화면(검수용에서 성공한 방식)", log)
            except Exception as exc:                           # noqa: BLE001
                log(f"[발행 화면] 진단 실패 — {exc}")

        log("")
        log("[진단] 끝났습니다. 붙여넣기·저장은 하지 않았습니다.")
        await asyncio.sleep(args.hold)
        return 0
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
    p = argparse.ArgumentParser(description="이미지 복사 진단(수정 화면 vs 발행 화면)")
    p.add_argument("--url", required=True, help="진단할 글 URL")
    p.add_argument("--mode", choices=("both", "edit", "view"), default="both")
    p.add_argument("--hold", type=int, default=20, help="끝나고 창을 열어 둘 초")
    args = p.parse_args(argv)

    settings = load_settings()
    log = Log(settings.out_dir, tag="v2_diag")
    log(f"[로그] {log.path}")
    try:
        return asyncio.run(main_async(args, settings, log))
    except KeyboardInterrupt:
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
