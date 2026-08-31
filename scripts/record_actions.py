"""사용자의 실제 작업을 그대로 기록하는 관찰 도구 (아무것도 자동으로 하지 않음).

목적: '수정 화면에서 본문을 어떻게 복사해 붙여넣는지'를 사람이 직접 시연하고,
      그 클릭/키보드/선택 영역을 전부 기록해 자동화에 반영한다.

기록 항목
  · 클릭한 요소: tag/id/class/text/aria/href + 부모 3단계 + selector 후보
  · 키보드: Ctrl+A / Ctrl+C / Ctrl+V 등 조합키와 그때의 포커스 요소
  · 선택(Selection): 선택된 글자 수·이미지 수·시작/끝 컨테이너
  · 프레임: 어느 frame 에서 일어난 일인지(URL)

실행: .venv\\Scripts\\python.exe scripts\\record_actions.py [기준글URL]
"""
import asyncio
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.config import load_settings  # noqa: E402

print = functools.partial(print, flush=True)

DEFAULT_URL = "https://blog.naver.com/<blog_id>/<logNo>"
LOGIN_SEC = 900
RECORD_SEC = 1800          # 시연 시간 30분

RECORDER_JS = r"""
() => {
  if (window.__blgRec) return true;
  window.__blgRec = {ev: []};
  const RAND = (c) => !c || c.length > 40 || /\d{4,}/.test(c)
                      || /^[a-f0-9]{6,}$/i.test(c) || /[^a-zA-Z0-9_-]/.test(c);
  const info = (el) => el && el.tagName ? {
    tag: el.tagName.toLowerCase(),
    id: el.id || '',
    cls: (el.className || '').toString().slice(0, 90),
    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
    aria: el.getAttribute && (el.getAttribute('aria-label') || ''),
    ce: el.getAttribute && el.getAttribute('contenteditable'),
  } : null;
  const cands = (el) => {
    const out = [], me = info(el);
    if (!me) return out;
    if (me.id && !RAND(me.id)) out.push(`${me.tag}#${me.id}`);
    if (me.aria) out.push(`${me.tag}[aria-label="${me.aria}"]`);
    (me.cls || '').split(/\s+/).filter(c => c && !RAND(c)).forEach(c => out.push(`${me.tag}.${c}`));
    if (me.text) out.push(`${me.tag}:has-text("${me.text.slice(0, 18)}")`);
    return out;
  };
  const parents = (el) => {
    const o = []; let c = el && el.parentElement;
    for (let i = 0; i < 3 && c; i++) { o.push(info(c)); c = c.parentElement; }
    return o;
  };
  const selInfo = () => {
    const s = document.getSelection();
    if (!s || s.rangeCount === 0) return {none: true};
    const r = s.getRangeAt(0);
    const frag = r.cloneContents();
    const div = document.createElement('div'); div.appendChild(frag);
    return {
      chars: (s.toString() || '').length,
      imgs: div.querySelectorAll('img').length,
      start: info(r.startContainer.nodeType === 1 ? r.startContainer
                                                  : r.startContainer.parentElement),
      collapsed: r.collapsed,
    };
  };
  document.addEventListener('click', (e) => {
    const el = e.target.closest('a,button,[role="button"],div,p,span') || e.target;
    window.__blgRec.ev.push({kind: 'click', el: info(el), parents: parents(el),
                             cands: cands(el), url: location.href});
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) && e.key.length === 1) return;   // 일반 타이핑은 무시
    const combo = (e.ctrlKey ? 'Ctrl+' : '') + (e.shiftKey ? 'Shift+' : '')
                + (e.altKey ? 'Alt+' : '') + e.key;
    window.__blgRec.ev.push({kind: 'key', combo,
                             focus: info(document.activeElement),
                             sel: selInfo(), url: location.href});
  }, true);
  document.addEventListener('selectionchange', () => {
    const s = selInfo();
    if (s.none || s.collapsed) return;
    const last = window.__blgRec.ev[window.__blgRec.ev.length - 1];
    if (last && last.kind === 'sel' && last.sel && last.sel.chars === s.chars) return;
    window.__blgRec.ev.push({kind: 'sel', sel: s, url: location.href});
  }, true);
  return true;
}"""


async def arm(page):
    for sc in [page.main_frame] + list(page.frames):
        try:
            await asyncio.wait_for(sc.evaluate(RECORDER_JS), timeout=3)
        except Exception:      # 타임아웃 포함 — 한 프레임 때문에 전체가 멈추지 않게
            pass


async def drain(page) -> list:
    out = []
    for sc in [page.main_frame] + list(page.frames):
        try:
            out += await asyncio.wait_for(sc.evaluate(
                "() => { const r = window.__blgRec; if (!r) return [];"
                " const e = r.ev.slice(); r.ev.length = 0; return e; }"), timeout=3) or []
        except Exception:
            pass
    return out


def show(i: int, e: dict) -> None:
    u = (e.get("url") or "")[:56]
    if e["kind"] == "click":
        el = e.get("el") or {}
        print(f"\n[{i}] 클릭  <{el.get('tag')}> id={el.get('id')!r} ce={el.get('ce')!r}")
        print(f"     class={el.get('cls')!r}")
        print(f"     text={el.get('text')!r} aria={el.get('aria')!r}")
        for k, par in enumerate(e.get("parents") or [], 1):
            if par:
                print(f"     부모{k}: <{par.get('tag')}> class={(par.get('cls') or '')[:46]!r} "
                      f"ce={par.get('ce')!r}")
        for c in (e.get("cands") or [])[:5]:
            print(f"     후보: {c}")
        print(f"     frame: {u}")
    elif e["kind"] == "key":
        f, s = e.get("focus") or {}, e.get("sel") or {}
        print(f"\n[{i}] 키    {e.get('combo')}")
        print(f"     포커스 <{f.get('tag')}> class={(f.get('cls') or '')[:46]!r} ce={f.get('ce')!r}")
        if not s.get("none"):
            print(f"     선택   글자 {s.get('chars')} · 이미지 {s.get('imgs')}")
        print(f"     frame: {u}")
    else:
        s = e.get("sel") or {}
        st = s.get("start") or {}
        print(f"\n[{i}] 선택  글자 {s.get('chars')} · 이미지 {s.get('imgs')} "
              f"· 시작 <{st.get('tag')}> class={(st.get('cls') or '')[:40]!r}")
        print(f"     frame: {u}")


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    st = load_settings()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(Path(st.playwright_user_data_dir)), headless=False,
            args=["--start-maximized"], no_viewport=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await page.goto("https://nid.naver.com/nidlogin.login",
                        wait_until="domcontentloaded", timeout=60_000)
        print("=" * 66)
        print("  ① 이 창에서 네이버에 로그인해 주세요.")
        print("=" * 66)
        for i in range(LOGIN_SEC):
            await page.wait_for_timeout(1000)
            try:
                names = {c["name"] for c in await ctx.cookies()
                         if "naver" in (c.get("domain") or "")}
                if {"NID_AUT", "NID_SES"} <= names:
                    print("   로그인 확인 OK")
                    break
            except Exception:
                pass

        m = __import__("re").search(r"blog\.naver\.com/([^/?#]+)/(\d{6,})", url)
        edit = (f"https://blog.naver.com/{m.group(1)}?Redirect=Update&logNo={m.group(2)}"
                if m else url)
        # 로그인 직후엔 네이버가 아직 리다이렉트 중이라 goto 가 중단된다("interrupted by
        # another navigation") → 잠시 가라앉히고, 실패해도 재시도하며 창은 절대 닫지 않는다.
        await page.wait_for_timeout(4000)
        print(f"   기준글 수정화면으로 이동: {edit}")
        for attempt in range(1, 6):
            try:
                await page.goto(edit, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2500)
                if "nidlogin" not in (page.url or ""):
                    break
                print(f"   (로그인 화면으로 되돌아옴 — {attempt}/5 재시도)")
            except Exception as exc:
                print(f"   (이동 실패 {attempt}/5: {type(exc).__name__}) 재시도")
            await page.wait_for_timeout(3000)
        print(f"   실제 URL: {page.url}")

        # ★새 글쓰기 탭도 같이 띄운다 — 복사(원본) → 붙여넣기(새 글) 를 시연하려면 둘 다 필요.
        try:
            writer = await ctx.new_page()
            await writer.goto(f"https://blog.naver.com/{m.group(1)}?Redirect=Write",
                              wait_until="domcontentloaded", timeout=60_000)
            await writer.wait_for_timeout(3000)
            print(f"   글쓰기 탭 열림: {(writer.url or '')[:70]}")
        except Exception as exc:  # noqa: BLE001
            print(f"   글쓰기 탭 열기 실패(무시): {type(exc).__name__} — 직접 열어주세요")
        try:
            await page.bring_to_front()          # 원본(수정화면)을 앞으로
        except Exception:  # noqa: BLE001
            pass

        print()
        print("=" * 66)
        print("  탭 2개가 열려 있습니다 — [수정화면=원본] · [글쓰기=붙여넣을 곳]")
        print("  ② 이제 평소 하시는 대로 시연해 주세요.")
        print("     (본문 클릭 → Ctrl+A → Ctrl+C → 새 글에서 Ctrl+V 등)")
        print("     하시는 클릭·키·선택을 전부 기록합니다. 끝나면 창을 닫으세요.")
        print("=" * 66)

        n = 0
        try:
            await _record_loop(ctx, page, lambda: n)
        except Exception as exc:  # noqa: BLE001
            print(f"   [기록 중단] {type(exc).__name__}: {str(exc)[:120]}")
        await ctx.close()
        return 0

    return 0


async def _record_loop(ctx, page, _n):
    n = 0
    if True:
        for sec in range(RECORD_SEC):
            await page.wait_for_timeout(1000)
            for pg in list(ctx.pages):
                try:
                    await arm(pg)
                except Exception:
                    pass
            for pg in list(ctx.pages):
                try:
                    for e in await drain(pg):
                        n += 1
                        show(n, e)
                except Exception:
                    pass
            if sec and sec % 15 == 0:
                try:
                    armed = 0
                    for pg in list(ctx.pages):
                        for fr in [pg.main_frame] + list(pg.frames):
                            try:
                                if await asyncio.wait_for(
                                        fr.evaluate("() => !!window.__blgRec"), timeout=2):
                                    armed += 1
                            except Exception:
                                pass
                    print(f"   … 기록 중 {sec}s · 이벤트 {n}건 · 탭 {len(ctx.pages)}개 "
                          f"· 감지기 {armed}개 · 현재URL {(page.url or '')[:50]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"   … 기록 중 {sec}s · 상태확인 실패 {type(exc).__name__}")
        print(f"\n[종료] 총 이벤트 {n}건")
        await ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
