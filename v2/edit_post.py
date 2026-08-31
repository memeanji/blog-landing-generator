r"""**이미 발행된 글을 수정 화면으로 열어** 제목/본문을 갈아끼운다 (실전용 전용).

검수용 흐름(`v2/run.py`)은 이 파일을 쓰지 않는다. `writer.NewPost` 의 메서드
(모바일 전환 · 제목 입력 · 붙여넣기 · 정렬 · 제품링크 · 발행)를 **그대로 재사용**하되,
대상 page/frame 만 '새 글쓰기'가 아니라 '수정 화면'으로 바꿔 끼운다.

★수정 화면 실측 지식 (2026-08-20~21)
  · URL 직접 진입: `blog.naver.com/{id}?Redirect=Update&logNo={no}` → PostUpdateForm.naver
    (화면에 누를 '수정' 버튼이 없어서 버튼 탐색은 계속 실패했다)
  · 본문 root 는 `.se-content` (수정 화면엔 `.se-main-container` 가 없다)
  · 실제 에디터는 **about:blank 중첩 iframe** 안에 있다 → frame 을 URL 로 거르면 못 찾는다.
    `.se-component` 개수로 고른다(browser.find_editor_frame 이 그렇게 한다).
  · **Ctrl+A 는 텍스트 문단을 클릭해 캐럿을 잡은 뒤에만** 쓴다. 클릭이 이미지 컴포넌트에
    걸리면 activeElement=body 가 되어 페이지 전체(제목·상단 메뉴)가 선택된다.
"""
from __future__ import annotations

import re

from . import browser, source_view, writer


def update_url(blog_id: str, log_no: str) -> str:
    """수정 화면 URL — 검수용에서 실제로 진입에 성공했던 형식 그대로(파라미터 포함)."""
    return (f"https://blog.naver.com/{blog_id}?Redirect=Update&"
            f"widgetTypeCall=true&noTrackingCode=true&directAccess=false&logNo={log_no}")


def parse_post_url(url: str) -> tuple[str, str]:
    """블로그 글 URL → (blogId, logNo)."""
    m = re.search(r"blog\.naver\.com/(?:PostView\.naver\?blogId=)?([A-Za-z0-9_\-]+)"
                  r"(?:/|&logNo=)(\d+)", url)
    if not m:
        raise RuntimeError(f"블로그 글 URL 에서 blogId/logNo 를 못 읽었습니다: {url!r}")
    return m.group(1), m.group(2)


EDIT_BTN_JS = r"""() => {
     const out = [];
     document.querySelectorAll("a,button,[role='button']").forEach(el => {
       const t = (el.innerText || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
       const href = el.getAttribute('href') || '';
       if (!/^수정$/.test(t) && !/Redirect=Update/i.test(href)) return;
       const r = el.getBoundingClientRect();
       if (r.width < 4 || r.height < 4) return;
       out.push({t: t, href: href.slice(0, 80)});
     });
     return out;
   }"""


def _entered(page, blog_id: str, log_no: str) -> tuple[bool, list[str], dict]:
    """수정 화면에 들어왔는지 판정한다.

    ★상단 주소창은 `?Redirect=Update…` 그대로 남고 **실제 화면은 iframe 안에서** 바뀐다.
      page.url 만 보면 멀쩡히 열린 수정 화면을 '아니다'라고 판정한다(2026-08-21 사고).
      검수용에서 동작하던 방식대로 **page.url + 모든 frame url** 을 함께 본다.
    """
    urls = [page.url or ""] + [f.url or "" for f in page.frames]
    joined = " ".join(urls)
    marks = {
        "form": bool(re.search(r"PostUpdateForm|postwrite", joined, re.I)),
        "logNo": log_no in joined,
        "owner": bool(re.search(rf"blogId={re.escape(blog_id)}|/{re.escape(blog_id)}[/?]",
                                joined, re.I)),
    }
    return all(marks.values()), urls, marks


async def _wait_entered(page, blog_id: str, log_no: str, log,
                        timeout_sec: int = 40) -> dict:
    """리다이렉트/iframe 로딩이 끝날 때까지 기다린다."""
    import time

    deadline = time.time() + timeout_sec
    marks = {}
    while time.time() < deadline:
        ok, urls, marks = _entered(page, blog_id, log_no)
        if ok:
            return marks
        await page.wait_for_timeout(1000)
    return marks


async def _try_edit_button(page, log) -> bool:
    """URL 직접 진입이 안 먹었을 때만 쓰는 대비책 — 화면의 '수정' 버튼을 눌러 본다.

    ★검수용에서 확인된 바로는 수정 화면에 누를 '수정' 버튼이 없어서 이 경로는 보통 실패한다.
      그래서 **직접 진입이 주 경로**이고, 이건 어디까지나 대비책이다.
    """
    for scope in [page.main_frame] + list(page.frames):
        try:
            cands = await scope.evaluate(EDIT_BTN_JS)
        except Exception:                                      # noqa: BLE001
            continue
        if not cands:
            continue
        log(f"[수정] '수정' 버튼 후보 {len(cands)}개 — {[c['t'] or c['href'][:30] for c in cands][:3]}")
        try:
            await scope.evaluate(
                r"""() => {
                     const els = Array.from(document.querySelectorAll("a,button,[role='button']"))
                       .filter(el => /^수정$/.test((el.innerText || '').trim())
                                  || /Redirect=Update/i.test(el.getAttribute('href') || ''));
                     if (els[0]) els[0].click();
                   }""")
            await page.wait_for_timeout(3000)
            return True
        except Exception as exc:                               # noqa: BLE001
            log(f"[수정] 버튼 클릭 실패({type(exc).__name__})")
    return False


async def open_for_edit(ctx, blog_url: str, log) -> tuple["writer.NewPost", str, str]:
    """발행된 글을 수정 화면으로 연다. (NewPost, blogId, logNo) 반환."""
    blog_id, log_no = parse_post_url(blog_url)
    page = await ctx.new_page()
    target = update_url(blog_id, log_no)
    log(f"[수정] 진입: {target[:100]}")
    await page.goto(target, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    if "nidlogin" in (page.url or ""):
        raise RuntimeError("수정 화면이 로그인 페이지로 튕겼습니다.")

    marks = await _wait_entered(page, blog_id, log_no, log)
    if not all(marks.values()):
        log(f"[수정] 직접 진입 판정 미달 {marks} — '수정' 버튼 경로를 시도합니다")
        if await _try_edit_button(page, log):
            marks = await _wait_entered(page, blog_id, log_no, log, timeout_sec=20)

    if not all(marks.values()):
        urls = [page.url or ""] + [f.url or "" for f in page.frames]
        log("[수정] ❌ 수정 화면 판정 실패. 실제로 열린 주소:")
        for u in urls[:10]:
            if u:
                log(f"         {u[:110]}")
        hint = ""
        if not marks.get("owner"):
            hint = (f" — 네이버가 다른 화면으로 돌려보냈을 수 있습니다"
                    f"(그 글 소유 계정 {blog_id} 로 로그인해야 합니다).")
        raise RuntimeError(f"수정 화면 진입 실패(form={marks.get('form')} "
                           f"logNo={marks.get('logNo')} owner={marks.get('owner')}){hint}")

    log(f"[수정] 진입 확인 ✅ blogId={blog_id} logNo={log_no}")

    # ★SmartEditor iframe(.se-component 가 들어있는 frame)이 생길 때까지 기다린다.
    frame = await browser.find_editor_frame(page, log, "수정", timeout_sec=60, min_score=5)
    post = writer.NewPost(page, frame, log)
    st = await post.stats()
    log(f"[수정] 기존 내용 — 컴포넌트 {st['comps']}개 · {st['chars']}자 · 이미지 {st['imgs']}개")
    if st["comps"] == 0:
        raise RuntimeError("[수정] 에디터는 찾았는데 기존 내용이 0개입니다 — 로딩이 덜 됐을 수 있습니다")
    return post, blog_id, log_no


# ── 본문/제목 '실제 내용'만 세는 JS (실전용 전용) ─────────────────────
#   ★에디터는 내용을 비우면 **플레이스홀더 안내문구**를 보여준다.
#     본문: '나를 돌아보는 회고, 뜻밖의 발견을 기다립니다. #모두의회고' (약 33자)
#     제목: '제목'
#     innerText 로 그냥 세면 이 안내문구가 '남은 본문'으로 잡혀서 영영 삭제 완료가 안 된다
#     (2026-08-21 실측: 본문이 화면상 비었는데 '남음 33자'로 판정).
#   → placeholder 요소를 떼어낸 뒤 센다. 검수용 STATS_JS 는 건드리지 않는다.
BODY_STATS_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: 'root 없음'};
     const clean = el => {
       const c = el.cloneNode(true);
       c.querySelectorAll("[class*='se-placeholder']").forEach(e => e.remove());
       return (c.textContent || '').replace(/[\u200b\s]+/g, ' ').trim();
     };
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))          // ★제목 영역 제외
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     let chars = 0, imgs = 0;
     const left = [];
     comps.forEach(c => {
       const t = clean(c);
       imgs += c.querySelectorAll('img').length;
       if (t) { chars += t.length; left.push(t.slice(0, 40)); }
     });
     return {ok: true, comps: comps.length, chars: chars, imgs: imgs,
             left: left.slice(0, 4)};
   }"""

TITLE_STATS_JS = r"""() => {
     const t = document.querySelector('.se-documentTitle');
     if (!t) return {found: false, text: '', ui: ''};
     // ★.se-documentTitle 전체 텍스트를 읽으면 제목 영역 **UI 버튼 글자**가 딸려 온다
     //   ('위치이동' / '제목 배경 사진' / '삭제' / '취소' / '확인').
     //   2026-08-21 실측: 제목은 이미 지워졌는데 그 UI 글자 때문에 '안 지워졌다'고 판정했다.
     //   → 실제 제목이 들어가는 **.se-text-paragraph 만** 읽는다.
     const paras = Array.from(t.querySelectorAll('.se-text-paragraph'));
     const clean = el => {
       const c = el.cloneNode(true);
       c.querySelectorAll("[class*='se-placeholder']").forEach(e => e.remove());
       return (c.textContent || '').replace(/[\u200b\s]+/g, ' ').trim();
     };
     const text = paras.map(clean).filter(Boolean).join(' ').trim();
     const whole = (t.textContent || '').replace(/[\u200b\s]+/g, ' ').trim();
     return {found: true, text: text, ui: whole.slice(0, 60)};
   }"""


async def body_stats(post) -> dict:
    """본문의 **실제 내용**만 센다(플레이스홀더 제외)."""
    fr = await post._fr()
    st = await fr.evaluate(BODY_STATS_JS)
    if not st.get("ok"):
        raise RuntimeError(f"[수정] 본문 상태를 읽지 못했습니다 — {st.get('why')}")
    return st


# 제목/본문 영역의 '클릭해서 캐럿 잡을 지점'을 찾는다.
#   ★이미지 컴포넌트는 절대 고르지 않는다(클릭이 이미지에 걸리면 activeElement=body 가 되어
#     Ctrl+A 가 편집영역이 아니라 페이지 전체를 잡는다).
SPOT_JS = r"""(which) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {found: false, why: 'root 없음'};
     const vis = el => {
       const r = el.getBoundingClientRect();
       const st = getComputedStyle(el);
       return r.width > 8 && r.height > 4 && st.display !== 'none' && st.visibility !== 'hidden';
     };
     let el = null;
     if (which === 'title') {
       // 검수용 TITLE_SPOT_JS 와 같은 우선순위: 제목 컴포넌트의 문단/placeholder
       el = Array.from(document.querySelectorAll(
              ".se-documentTitle .se-text-paragraph, .se-documentTitle [class*='se-placeholder']"))
            .filter(vis)[0]
         || document.querySelector('.se-documentTitle');
     } else {
       const paras = Array.from(root.querySelectorAll('.se-text-paragraph'))
         .filter(p => !p.closest('.se-documentTitle'))
         .filter(p => {
            const comp = p.closest('.se-component');
            return !comp || !comp.querySelector('img');      // ★이미지 컴포넌트 제외
         })
         .filter(vis);
       el = paras.find(p => (p.innerText || '').trim().length > 0)
         || paras[0]
         || Array.from(root.querySelectorAll("[class*='se-placeholder']")).filter(vis)[0];
     }
     if (!el) return {found: false, why: which + ' 자리를 찾지 못함'};
     el.scrollIntoView({block: 'center'});
     const r = el.getBoundingClientRect();
     if (r.width < 4 || r.height < 4) return {found: false, why: which + ' 영역이 0 크기'};
     return {found: true,
             x: Math.round(r.x + Math.min(40, r.width / 2)),
             y: Math.round(r.y + Math.min(12, r.height / 2)),
             text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
             cls: (el.className || '').toString().slice(0, 50)};
   }"""


async def title_text(post) -> str:
    """현재 제목 문자열. **플레이스홀더('제목')는 빈 값으로 본다.**

    검수용 `writer.STATS_JS` 도 제목을 주지만 innerText 라 비어 있을 때 '제목'
    안내문구가 딸려 온다 → 실전용에서는 placeholder 를 떼고 읽는다.
    """
    fr = await post._fr()
    st = await fr.evaluate(TITLE_STATS_JS)
    return (st.get("text") or "").strip()


async def _click_spot(post, which: str, log) -> dict:
    fr = await post._fr()
    spot = await fr.evaluate(SPOT_JS, which)
    if not spot.get("found"):
        raise RuntimeError(f"[수정] {which} 자리를 찾지 못했습니다 — {spot.get('why')}")
    await post.page.bring_to_front()
    await post.page.mouse.click(spot["x"], spot["y"])
    await post.page.wait_for_timeout(400)
    return spot


async def clear_body(post, log) -> None:
    """본문만 전부 지운다. 제목은 건드리지 않는다.

    완료 판정은 **본문 컴포넌트의 실제 글자/이미지가 0** 인지로 한다
    (플레이스홀더 안내문구는 내용으로 세지 않는다).
    """
    before = await body_stats(post)
    before_title = await title_text(post)
    log(f"[수정] 삭제 전 본문 — 컴포넌트 {before['comps']}개 · {before['chars']}자 · "
        f"이미지 {before['imgs']}개")

    spot = await _click_spot(post, "body", log)
    log(f"[수정] 본문 캐럿 — {spot['cls'][:40]!r} {spot['text']!r}")

    after = before
    for attempt in range(4):
        await post.page.keyboard.press("Control+A")
        await post.page.wait_for_timeout(250)
        await post.page.keyboard.press("Delete")
        await post.page.wait_for_timeout(700)
        after = await body_stats(post)
        if after["chars"] == 0 and after["imgs"] == 0:
            break
        log(f"      남음 — {after['chars']}자 / 이미지 {after['imgs']}개 "
            f"{after.get('left') or []} (재시도 {attempt + 1})")
        await _click_spot(post, "body", log)

    after_title = await title_text(post)

    # ★Ctrl+A 가 제목까지 먹었는지 반드시 확인한다(페이지 전체 선택 사고 방지).
    if before_title and not after_title:
        raise RuntimeError("[수정] 본문을 지우면서 제목까지 사라졌습니다 — 중단합니다"
                           "(Ctrl+A 가 편집영역 밖을 잡았을 수 있습니다)")
    if after["chars"] or after["imgs"]:
        raise RuntimeError(f"[수정] 본문이 다 지워지지 않았습니다 — "
                           f"{after['chars']}자 / 이미지 {after['imgs']}개 남음 "
                           f"{after.get('left') or []}")
    log(f"[수정] 기존 본문 삭제 완료 — {before['chars']}자/{before['imgs']}img → "
        f"0자/0img (제목은 그대로: {after_title[:30]!r})")


async def clear_title(post, log) -> None:
    """제목만 전부 지운다."""
    before = await title_text(post)
    if not before:
        log("[수정] 기존 제목 없음 — 건너뜁니다")
        return

    await _click_spot(post, "title", log)
    for attempt in range(3):
        await post.page.keyboard.press("Control+A")
        await post.page.wait_for_timeout(200)
        await post.page.keyboard.press("Delete")
        await post.page.wait_for_timeout(500)
        now = await title_text(post)
        if not now:
            break
        log(f"      제목 남음 — {now[:30]!r} (재시도 {attempt + 1})")
        await _click_spot(post, "title", log)

    now = await title_text(post)
    if now:
        raise RuntimeError(f"[수정] 제목이 지워지지 않았습니다 — {now[:40]!r}")

    st = await body_stats(post)
    if st["chars"] or st["imgs"]:
        log(f"      (참고) 제목 삭제 후 본문 {st['chars']}자 / 이미지 {st['imgs']}개")
    fr = await post._fr()
    dbg = await fr.evaluate(TITLE_STATS_JS)
    log(f"[수정] 기존 제목 삭제 완료 — {before[:40]!r} "
        f"(제목영역 전체: {dbg.get('ui', '')[:40]!r} ← UI 버튼 글자이며 제목 아님)")


# ── 실전용 참고글을 '수정 화면'에서 읽기 ──────────────────────────────
async def open_source_edit(ctx, blog_url: str, log, label: str = "참고글"):
    """실전용 참고글을 **수정 화면 + 모바일 미리보기**로 열고 복사 원본으로 쓴다.

    `source_view.SourceView`(검수용에서 검증된 '본문 한 번에 복사')를 **그대로 재사용**하되,
    대상 page/frame 만 발행 화면이 아니라 수정 화면으로 바꿔 끼운다.
    SourceView 의 스캔/복사 JS 는 root 를 `.se-main-container || .se-content` 로 찾으므로
    수정 화면(`.se-content`)에서도 그대로 동작한다.

    ⚠️ 이 탭은 **편집 가능한 상태**다. 복사(execCommand)만 하고 키 입력·저장·발행은
       절대 하지 않는다(참고글 원본이 바뀌면 안 된다).
    """
    log(f"[{label}] 새 탭 생성 → {blog_url}")
    post, blog_id, log_no = await open_for_edit(ctx, blog_url, log)
    log(f"[{label}] 수정 화면 진입 ✅ (logNo={log_no})")
    log(f"[{label}] PostUpdateForm frame 확보 ✅ — {(post.frame.url or '')[:70]}")

    await post.switch_to_mobile()
    await post.ensure_mobile()
    log(f"[{label}] 모바일 미리보기 전환 ✅")

    # ★지연 로딩 해제 — 안 하면 이미지가 data:image/svg+xml 자리표시자로 복사된다.
    await preload_images(post, log, label=label)

    log(f"[{label}] 제목/본문 복사 시작")
    src = source_view.SourceView(post.page, post.frame, log)
    await src.scan()
    return src, post


# ── 본문 구간 분할 복사/붙여넣기 (실전용 전용) ────────────────────────
#   ★한 번에 큰 Range 를 복사하면 이미지가 클립보드로 안 넘어오는 일이 있다
#     (2026-08-21 실측: 70컴포넌트/36장을 한 번에 복사 → 이미지 0/36).
#     구간을 나눠 **복사 → 붙여넣기 → 그 구간 이미지 생성 확인 → 다음 구간** 으로 간다.
#   ★자르는 단위는 반드시 `.se-component` — 텍스트 컴포넌트 중간을 자르지 않는다.
CHUNK_IMGS = 6            # 한 구간당 목표 이미지 수(5~8장)


def plan_chunks(src, max_imgs: int = CHUNK_IMGS) -> list[dict]:
    """복사 구간(src.first~src.last)을 이미지 수 기준으로 컴포넌트 단위로 쪼갠다."""
    rows = [r for r in src.rows if src.first <= r["i"] <= src.last]
    chunks, cur, imgs = [], [], 0
    for r in rows:
        cur.append(r)
        imgs += r["imgs"]
        if imgs >= max_imgs:
            chunks.append(cur)
            cur, imgs = [], 0
    if cur:
        chunks.append(cur)
    return [{"first": c[0]["i"], "last": c[-1]["i"], "comps": len(c),
             "imgs": sum(x["imgs"] for x in c),
             "chars": sum(x["chars"] for x in c)} for c in chunks]


async def copy_chunk(src, ch: dict, log, rescan: bool = True) -> dict:
    """구간 하나를 클립보드에 넣는다(검수용 SELECT_COPY_JS 를 그대로 재사용)."""
    from . import source_view

    await src.page.bring_to_front()
    before = await src._clipboard()
    fr = await src._fr()
    opt = {"first": ch["first"], "last": ch["last"],
           "wantComps": ch["comps"], "wantImgs": ch["imgs"]}
    res = await fr.evaluate(source_view.SELECT_COPY_JS, opt)

    # 마킹(data-v2-c)이 날아갔으면 한 번 다시 스캔하고 재시도
    if not res.get("ok") and rescan and "찾지 못함" in str(res.get("why")):
        log("      구간 마킹이 사라져 다시 스캔합니다")
        await src.scan()
        fr = await src._fr()
        res = await fr.evaluate(source_view.SELECT_COPY_JS, opt)

    if not res.get("ok"):
        raise RuntimeError(f"[복사] 구간 #{ch['first']}~#{ch['last']} 선택 실패 — "
                           f"{res.get('why')} 실제={res.get('got')}")
    if not res.get("copied"):
        raise RuntimeError(f"[복사] 구간 #{ch['first']}~#{ch['last']} "
                           f"execCommand('copy') 실패 ({res.get('err') or '반환 false'})")
    after = await src._clipboard()
    if not after or after == before:
        raise RuntimeError(f"[복사] 구간 #{ch['first']}~#{ch['last']} — "
                           f"클립보드가 바뀌지 않았습니다")
    return res["got"]


async def paste_in_chunks(post, src, log, tag: str = "", max_imgs: int = CHUNK_IMGS,
                          img_timeout_ms: int = 120_000) -> dict:
    """구간별로 복사 → 붙여넣기 → 이미지 생성 확인. 마지막에 원본과 총합 비교."""
    chunks = plan_chunks(src, max_imgs)
    log(f"{tag} 본문을 {len(chunks)}구간으로 나눠 옮깁니다 "
        f"(총 컴포넌트 {src.want_comps}개 · 이미지 {src.want_imgs}장 · {src.want_chars}자)")

    await post.page.bring_to_front()
    await post.prepare_body_caret()
    grand_before = await post.stats()

    for n, ch in enumerate(chunks, start=1):
        got = await copy_chunk(src, ch, log)
        await post.page.bring_to_front()
        await post.ensure_caret()
        before = await post.stats()
        await post.page.keyboard.press("Control+V")
        await post.page.wait_for_timeout(1200)

        waited, after = 0, await post.stats()
        while after["imgs"] < before["imgs"] + ch["imgs"] and waited < img_timeout_ms:
            await post.page.wait_for_timeout(1000)
            waited += 1000
            after = await post.stats()
            if waited % 15_000 == 0:
                log(f"      [{n}/{len(chunks)}] 이미지 대기 {waited // 1000}초 — "
                    f"{after['imgs'] - before['imgs']}/{ch['imgs']}장")

        d_chars = after["chars"] - before["chars"]
        d_imgs = after["imgs"] - before["imgs"]
        log(f"{tag} 구간 {n}/{len(chunks)} (#{ch['first']}~#{ch['last']}) — "
            f"+{d_chars}자 +{d_imgs}img (기대 {ch['chars']}자 / {ch['imgs']}img · "
            f"{waited // 1000}초 대기)")

        if d_imgs != ch["imgs"]:
            raise RuntimeError(
                f"[붙여넣기] 구간 {n}/{len(chunks)} 이미지 {d_imgs}개 ≠ 기대 {ch['imgs']}개 "
                f"— 발행하지 않습니다 (클립보드 복사 결과: {got})")
        if ch["chars"] and d_chars < max(1, int(ch["chars"] * 0.7)):
            raise RuntimeError(
                f"[붙여넣기] 구간 {n}/{len(chunks)} 글자 {d_chars}자 < 기대 "
                f"{ch['chars']}자의 70% — 발행하지 않습니다")

    final = await post.stats()
    t_chars = final["chars"] - grand_before["chars"]
    t_imgs = final["imgs"] - grand_before["imgs"]
    log(f"{tag} 본문 이동 완료 — 총 +{t_chars}자 +{t_imgs}img "
        f"(원본 {src.want_chars}자 / {src.want_imgs}img)")
    if t_imgs != src.want_imgs:
        raise RuntimeError(f"[붙여넣기] 총 이미지 {t_imgs}개 ≠ 원본 {src.want_imgs}개")
    if t_chars < max(1, int(src.want_chars * 0.8)):
        raise RuntimeError(f"[붙여넣기] 총 글자 {t_chars}자 < 원본 {src.want_chars}자의 80%")
    return final


# ── 수정 화면 이미지 지연 로딩 해제 ───────────────────────────────────
#   ★수정 화면은 이미지를 **지연 로딩**한다. 화면에 안 들어온 이미지는 실제 파일이 아니라
#     `data:image/svg+xml` 자리표시자로 들어 있고, 그대로 복사해 붙여넣으면 네이버가
#     "허용되지 않는 형식의 이미지는 제외합니다" 를 띄운다(2026-08-21 진단으로 확인).
#     발행 화면은 처음부터 `https://blogfiles.pstatic.net/...` 실제 파일이라 문제가 없었다.
#   → 본문을 끝까지 스크롤해 실제 파일 주소로 바뀌게 만든 뒤 복사한다.
IMG_KIND_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: 'root 없음'};
     const imgs = Array.from(root.querySelectorAll('img'));
     let real = 0, ph = 0;
     const sample = [];
     imgs.forEach(im => {
       const u = im.getAttribute('src') || '';
       if (u.startsWith('data:') || u.startsWith('blob:') || !u) { ph += 1; }
       else { real += 1; }
       if (sample.length < 2) sample.push(u.slice(0, 60));
     });
     return {ok: true, total: imgs.length, real: real, placeholder: ph,
             comps: root.querySelectorAll('.se-component').length, sample: sample};
   }"""

SCROLL_JS = r"""(step) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     const doc = document.scrollingElement || document.documentElement;
     // 에디터 본문이 자체 스크롤을 가진 경우와 문서 스크롤인 경우 모두 밀어 준다.
     let moved = false;
     const boxes = [root, root && root.parentElement, doc].filter(Boolean);
     for (const b of boxes) {
       const before = b.scrollTop;
       b.scrollTop = before + step;
       if (b.scrollTop !== before) moved = true;
     }
     window.scrollBy(0, step);
     return {moved: moved, y: doc.scrollTop};
   }"""


async def preload_images(post, log, label: str = "참고글",
                         timeout_sec: int = 120) -> dict:
    """본문을 끝까지 스크롤해 지연 로딩 이미지를 **실제 파일로** 바꾼다."""
    import time

    fr = await post._fr()
    st = await fr.evaluate(IMG_KIND_JS)
    if not st.get("ok"):
        raise RuntimeError(f"[{label}] 본문을 읽지 못했습니다 — {st.get('why')}")
    log(f"[{label}] 이미지 로딩 상태 — 실제 {st['real']}장 / 자리표시자 {st['placeholder']}장 "
        f"(컴포넌트 {st['comps']}개)")

    deadline = time.time() + timeout_sec
    stable = 0
    last = (st["total"], st["real"], st["comps"])
    while time.time() < deadline:
        for _ in range(6):
            await fr.evaluate(SCROLL_JS, 900)
            await post.page.wait_for_timeout(250)
        await post.page.wait_for_timeout(700)

        fr = await post._fr()
        st = await fr.evaluate(IMG_KIND_JS)
        now = (st["total"], st["real"], st["comps"])
        if st["placeholder"] == 0 and now == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        if now != last:
            log(f"      스크롤 중 — 컴포넌트 {st['comps']}개 · 이미지 {st['total']}장 "
                f"(실제 {st['real']} / 자리표시자 {st['placeholder']})")
        last = now

    # 맨 위로 되돌린다(복사 Range 는 위치와 무관하지만 화면 상태를 원래대로).
    await fr.evaluate(r"""() => {
         const root = document.querySelector('.se-main-container')
                   || document.querySelector('.se-content');
         [root, root && root.parentElement,
          document.scrollingElement || document.documentElement]
           .filter(Boolean).forEach(b => { b.scrollTop = 0; });
         window.scrollTo(0, 0);
       }""")
    await post.page.wait_for_timeout(500)

    fr = await post._fr()
    st = await fr.evaluate(IMG_KIND_JS)
    log(f"[{label}] 이미지 로딩 완료 — 컴포넌트 {st['comps']}개 · 이미지 {st['total']}장 "
        f"(실제 {st['real']} / 자리표시자 {st['placeholder']})")
    if st["placeholder"]:
        log(f"      예시 src: {st['sample']}")
        raise RuntimeError(
            f"[{label}] 아직 {st['placeholder']}장이 자리표시자(data:/blob:)입니다. "
            f"이대로 붙여넣으면 네이버가 '허용되지 않는 형식의 이미지는 제외합니다' 를 띄웁니다.\n"
            f"       --ref-copy-from view 로 발행 화면에서 복사하면 실제 파일 주소로 넘어옵니다.")
    return st


# ── 제품 링크 카드만 중앙정렬 (실전용 전용) ───────────────────────────
#   ★실전용은 참고글의 정렬/서식을 그대로 복사해 온다. 그래서 검수용의 `center_all()`
#     (본문 이미지를 전부 가운데로 옮기는 처리)을 쓰면 **원본 정렬을 덮어써서 깨진다**
#     (2026-08-21 사용자 확인). 마지막에 새로 만든 **제품 카드 하나만** 가운데로 옮긴다.
MARK_CARD_JS = r"""(host) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: 'root 없음'};
     document.querySelectorAll('[data-v2-card]').forEach(e => e.removeAttribute('data-v2-card'));
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     const hrefsOf = c => Array.from(c.querySelectorAll('a[href]'))
       .map(a => a.getAttribute('href') || '').filter(Boolean);
     const isCard = c => {
       if (!c.querySelector('img')) return false;
       const cls = (c.className || '').toString();
       if (/oglink|se-link/.test(cls)) return true;
       if (hrefsOf(c).some(h => host && h.indexOf(host) >= 0)) return true;
       const txt = (c.innerText || '').replace(/\s+/g, ' ').trim();
       return !!host && txt.indexOf(host) >= 0;
     };
     const cards = comps.filter(isCard);
     if (!cards.length) return {ok: false, why: '제품 카드를 찾지 못함'};
     const card = cards[cards.length - 1];              // 맨 아래 것이 방금 만든 카드
     card.setAttribute('data-v2-card', '1');
     const sec = card.querySelector("[class*='se-section']") || card;
     const cls = (sec.className || '').toString();
     card.scrollIntoView({block: 'center'});
     return {ok: true, centered: /se-section-align-center/.test(cls),
             cls: cls.replace(/\s+/g, ' ').slice(0, 60), count: cards.length};
   }"""


async def center_product_card(post, product_url: str, log, tag: str = "") -> bool:
    """방금 만든 제품 링크 카드 **하나만** 가운데 정렬한다. 본문은 건드리지 않는다."""
    host = re.sub(r"^https?://([^/]+).*$", r"\1", product_url)
    fr = await post._fr()
    st = await fr.evaluate(MARK_CARD_JS, host)
    if not st.get("ok"):
        raise RuntimeError(f"[정렬] 제품 카드를 찾지 못했습니다 — {st.get('why')}")
    if st["centered"]:
        log(f"{tag} 제품 카드가 이미 가운데 정렬입니다 — 건드리지 않습니다")
        return True

    log(f"{tag} 제품 카드 중앙정렬 시도 — {st['cls']!r}")
    try:
        await fr.locator('[data-v2-card="1"]').first.click(timeout=4000)
    except Exception:                                          # noqa: BLE001
        try:
            await fr.locator('[data-v2-card="1"]').first.click(timeout=4000, force=True)
        except Exception as exc:                               # noqa: BLE001
            log(f"{tag} ⚠ 제품 카드 클릭 실패({type(exc).__name__}) — 정렬을 건너뜁니다")
            return False
    await post.page.wait_for_timeout(500)

    # 정렬 버튼은 검수용 NewPost 의 선택자를 그대로 쓴다(정의를 바꾸지 않는다).
    fr = await post._fr()
    if not await post._click_first(fr, post.ALIGN_CENTER_BTN, "제품카드 정렬"):
        await post._click_first(fr, post.ALIGN_DROPDOWN_BTN, "제품카드 드롭다운")
        fr = await post._fr()
        await post._click_first(fr, post.ALIGN_CENTER_BTN, "제품카드 정렬(2단계)")
    await post.page.wait_for_timeout(400)

    fr = await post._fr()
    now = await fr.evaluate(MARK_CARD_JS, host)
    ok = bool(now.get("centered"))
    log(f"{tag} 제품 카드 중앙정렬 {'✅' if ok else '❌'} — {now.get('cls')!r}")
    return ok


# ── 이미지 클릭 → Ctrl+A → 복사 (실전용 전용, 2026-08-21 사용자 제안) ──
#   구간을 잘라 복사하면 **이미지 2장 가로배치·그룹·개별 정렬**이 깨진다.
#   에디터 본문에서 이미지를 하나 클릭해 포커스를 준 뒤 Ctrl+A 로 전체를 잡으면
#   그 묶음/정렬이 그대로 따라온다.
#   ★단, 두 가지를 반드시 처리한다.
#     ① Ctrl+C(합성 키)는 브라우저 창이 OS 포커스를 가져야 시스템 클립보드에 닿는다
#        (2026-08-20 실측: 사람이 창을 클릭하기 전엔 실패). → execCommand('copy') 를 먼저 쓴다.
#     ② Ctrl+A 가 편집영역 밖(제목·상단 메뉴)까지 잡을 수 있다 → 복사 전에 선택 내용을 검사한다.
FIRST_IMAGE_SPOT_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {found: false, why: 'root 없음'};
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'));
     for (const c of comps) {
       const im = c.querySelector('img');
       if (!im) continue;
       im.scrollIntoView({block: 'center'});
       const r = im.getBoundingClientRect();
       if (r.width < 20 || r.height < 20) continue;
       return {found: true,
               x: Math.round(r.x + r.width / 2),
               y: Math.round(r.y + r.height / 2),
               cls: (c.className || '').toString().slice(0, 45)};
     }
     return {found: false, why: '클릭할 이미지를 찾지 못함'};
   }"""

SELECTION_JS = r"""() => {
     const s = window.getSelection();
     if (!s || s.rangeCount === 0 || s.isCollapsed)
       return {ok: false, why: '선택된 것이 없음'};
     const r = s.getRangeAt(0);
     const div = document.createElement('div');
     div.appendChild(r.cloneContents());
     const txt = (s.toString() || '').replace(/\s+/g, ' ').trim();
     return {ok: true,
             chars: txt.length,
             imgs: div.querySelectorAll('img').length,
             comps: div.querySelectorAll('.se-component').length,
             hasTitle: !!div.querySelector('.se-documentTitle'),
             head: txt.slice(0, 50), tail: txt.slice(-50)};
   }"""

COPY_SELECTION_JS = r"""() => {
     let ok = false, err = '';
     try { ok = document.execCommand('copy'); } catch (e) { err = String(e); }
     return {copied: ok, err: err};
   }"""

# 붙여넣은 뒤 참고글에서 딸려온 **기존 제품 카드**를 지운다(그 자리엔 행별 링크로 새로 만든다).
MARK_LAST_CARD_JS = r"""(host) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: 'root 없음'};
     document.querySelectorAll('[data-v2-old]').forEach(e => e.removeAttribute('data-v2-old'));
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     const hrefsOf = c => Array.from(c.querySelectorAll('a[href]'))
       .map(a => a.getAttribute('href') || '').filter(Boolean);
     const norm = t => (t || '').replace(/[\u200b\s]+/g, ' ').trim();

     // 내용이 있는 마지막 컴포넌트들만 본다(맨 끝 빈 문단은 무시).
     const solid = comps.filter(c => norm(c.innerText) || c.querySelector('img'));
     const isCard = c => /oglink|se-link/.test((c.className || '').toString())
                      && c.querySelector('img');
     // ★참고글이 '링크 걸린 제품 이미지'로 끝나는 경우도 있다.
     //   본문 이미지를 실수로 지우지 않도록 **맨 끝 컴포넌트**이고
     //   제품 도메인 링크를 가진 경우에만 대상으로 삼는다.
     const isTailProductImage = (c, i) => i >= solid.length - 1
                      && /se-image/.test((c.className || '').toString())
                      && hrefsOf(c).some(h => host && h.indexOf(host) >= 0);

     let target = null, kind = '';
     for (let i = solid.length - 1; i >= 0; i--) {
       const c = solid[i];
       if (isCard(c)) { target = c; kind = 'oglink 카드'; break; }
       if (isTailProductImage(c, i)) { target = c; kind = '제품 이미지'; break; }
       // 카드/제품이미지가 아닌 실제 본문을 만나면 멈춘다(그 위로는 안 뒤진다).
       if (norm(c.innerText).length > 20) break;
     }
     const cards = solid.filter(isCard).length;
     if (!target) return {ok: true, found: false, cards: cards};
     target.setAttribute('data-v2-old', '1');
     target.scrollIntoView({block: 'center'});
     const im = target.querySelector('img');
     const r = (im || target).getBoundingClientRect();
     return {ok: true, found: true, kind: kind, cards: cards,
             text: norm(target.innerText).slice(0, 50),
             cls: (target.className || '').toString().slice(0, 45),
             x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
   }"""


async def _focus_body(src, how: str, log, label: str) -> bool:
    """에디터 본문에 포커스를 준다(클릭). 캐럿 상태는 **참고용으로 로그만** 남긴다."""
    from . import writer

    fr = await src._fr()
    spot = await (fr.evaluate(FIRST_IMAGE_SPOT_JS) if how == "image"
                  else fr.evaluate(SPOT_JS, "body"))
    if not spot.get("found"):
        log(f"[{label}] 포커스 대상({how}) 없음 — {spot.get('why')}")
        return False

    await src.page.bring_to_front()
    await src.page.mouse.click(spot["x"], spot["y"])
    await src.page.wait_for_timeout(600)

    fr = await src._fr()
    state = await fr.evaluate(writer.CARET_STATE_JS)
    log(f"[{label}] 본문 클릭({how}) — {spot.get('cls', '')[:40]!r} → "
        f"창포커스={state['focused']} 본문안={state['inBody']} 선택범위={state['ranges']}")
    return True


async def copy_whole_body(src, log, label: str = "참고글") -> dict:
    """본문 이미지 클릭 → Ctrl+A → Ctrl+C. (2026-08-21 사용자가 실제로 성공시킨 방식)

    ★스마트에디터의 Ctrl+A 는 일반 DOM Range 가 아니라 **에디터 내부 컴포넌트 선택**으로
      동작할 수 있다. 그래서 `window.getSelection()` 이 비어 있어도 정상이다.
      - DOM selection 이 없다고 중단하지 않는다(경고만 남긴다).
      - 같은 이유로 `execCommand('copy')`(=DOM selection 복사)를 주 경로로 쓸 수 없다.
        **Ctrl+C 가 주 경로**이고, 클립보드가 안 바뀔 때만 execCommand 로 보조 시도한다.
    ★성공 판정은 **클립보드가 바뀌었는가 + 붙여넣기 결과**로 한다.
    """
    await src.page.bring_to_front()
    before_clip = await src._clipboard()

    # 이미지 클릭이 원래 방식. 이미지를 못 찾을 때만 텍스트 문단으로 대체한다.
    if not await _focus_body(src, "image", log, label):
        await _focus_body(src, "text", log, label)

    await src.page.keyboard.press("Control+A")
    await src.page.wait_for_timeout(800)

    fr = await src._fr()
    sel = await fr.evaluate(SELECTION_JS)
    if sel.get("ok"):
        log(f"[{label}] 전체 선택 — 컴포넌트 {sel['comps']}개 · 이미지 {sel['imgs']}장 · "
            f"{sel['chars']}자")
        if sel.get("hasTitle"):
            log(f"[{label}] ⚠ 선택에 제목 영역이 포함돼 보입니다 — 붙여넣기 결과로 확인합니다")
    else:
        log(f"[{label}] (참고) DOM selection 없음 — {sel.get('why')} "
            f"· 에디터 내부 전체선택일 수 있어 그대로 진행합니다")

    # ★Ctrl+C 가 주 경로
    await src.page.keyboard.press("Control+C")
    await src.page.wait_for_timeout(900)
    after = await src._clipboard()

    if not after or after == before_clip:
        log(f"[{label}] Ctrl+C 로 클립보드가 안 바뀌었습니다 — execCommand 로 보조 시도")
        res = await fr.evaluate(COPY_SELECTION_JS)
        await src.page.wait_for_timeout(500)
        after = await src._clipboard()
        if not after or after == before_clip:
            raise RuntimeError(
                f"[{label}] 복사 실패 — 클립보드가 바뀌지 않았습니다 "
                f"(execCommand: {res.get('copied')} {res.get('err') or ''})")

    # 기대치: 선택 결과가 있으면 그것, 없으면 참고글 스캔 총합(꼬리 제품카드 포함)
    # ★기대치는 **실제 복사 구간**(want_*)을 기준으로 잡는다.
    #   `src.rows` 총합은 선택 밖으로 제외한 맨 끝 oglink/제품카드 이미지까지 세므로
    #   50장을 기다리며 49/50 에서 4분씩 헛기다렸다(2026-08-24 실측).
    want_imgs = getattr(src, "want_imgs", None)
    want_chars = getattr(src, "want_chars", None)
    want_comps = getattr(src, "want_comps", None)
    if sel.get("ok") and sel.get("imgs"):
        expect = {"chars": sel["chars"], "imgs": sel["imgs"], "comps": sel["comps"],
                  "from": "selection"}
    elif want_imgs:
        expect = {"chars": want_chars or 0, "imgs": want_imgs,
                  "comps": want_comps or 0, "from": "복사구간"}
    else:
        expect = {"chars": sum(r["chars"] for r in src.rows),
                  "imgs": sum(r["imgs"] for r in src.rows),
                  "comps": len(src.rows), "from": "scan"}
    log(f"[{label}] 복사 완료 — 클립보드 {len(after)}자 · "
        f"기대치({expect['from']}) 이미지 {expect['imgs']}장 / {expect['chars']}자")
    return expect


async def paste_whole_body(post, expect: dict, log, tag: str = "",
                           img_timeout_ms: int = 240_000) -> dict:
    """클립보드 내용을 본문에 한 번에 붙여넣고 이미지가 다 올라올 때까지 기다린다."""
    await post.page.bring_to_front()
    await post.prepare_body_caret()
    before = await post.stats()
    await post.page.keyboard.press("Control+V")
    await post.page.wait_for_timeout(1500)

    waited, after = 0, await post.stats()
    # ★기대치에 도달하면 즉시 다음 단계로 간다. 도달하지 못해도 이미지 수가 20초간
    #   전혀 안 늘면(=업로드 끝) 그만 기다린다. 타임아웃은 마지막 안전장치로 남긴다.
    stall_ms, last_imgs = 0, after["imgs"]
    while after["imgs"] < before["imgs"] + expect["imgs"] and waited < img_timeout_ms:
        await post.page.wait_for_timeout(1000)
        waited += 1000
        after = await post.stats()
        if after["imgs"] > last_imgs:
            stall_ms, last_imgs = 0, after["imgs"]
        else:
            stall_ms += 1000
        if waited % 15_000 == 0:
            log(f"{tag} 이미지 업로드 대기 {waited // 1000}초 — "
                f"{after['imgs'] - before['imgs']}/{expect['imgs']}장")
        if stall_ms >= 20_000:
            log(f"{tag} 이미지 수가 20초간 그대로입니다 "
                f"({after['imgs'] - before['imgs']}/{expect['imgs']}장) — "
                f"업로드가 끝난 것으로 보고 진행합니다")
            break

    d_chars = after["chars"] - before["chars"]
    d_imgs = after["imgs"] - before["imgs"]
    log(f"{tag} 붙여넣기 — +{d_chars}자 +{d_imgs}img "
        f"(기대 {expect['chars']}자 / {expect['imgs']}장 · {waited // 1000}초 대기)")
    if d_imgs == 0:
        raise RuntimeError("[붙여넣기] 이미지가 한 장도 붙지 않았습니다 — 발행하지 않습니다")
    if d_imgs < max(1, int(expect["imgs"] * 0.95)):
        raise RuntimeError(f"[붙여넣기] 이미지 {d_imgs}장 < 기대 {expect['imgs']}장의 95% "
                           f"— 발행하지 않습니다")
    if d_imgs > expect["imgs"]:
        log(f"{tag} (참고) 이미지가 기대({expect['imgs']})보다 {d_imgs - expect['imgs']}장 "
            f"많습니다 — 꼬리 제품 카드가 함께 붙은 경우입니다")
    if expect["chars"] and d_chars < max(1, int(expect["chars"] * 0.8)):
        raise RuntimeError(f"[붙여넣기] 글자 {d_chars}자 < 기대 {expect['chars']}자의 80%")
    return after


async def remove_pasted_card(post, log, tag: str = "", product_url: str = "") -> bool:
    """참고글에서 딸려온 **기존 제품 카드 / 제품 이미지**를 지운다.

    그 자리에는 잠시 뒤 '이 행의 링크'로 카드를 새로 만든다.
    본문 이미지를 실수로 지우지 않도록 맨 끝 컴포넌트만 대상으로 한다.
    """
    host = re.sub(r"^https?://([^/]+).*$", r"\1", product_url) if product_url else ""
    fr = await post._fr()
    st = await fr.evaluate(MARK_LAST_CARD_JS, host)
    if not st.get("ok"):
        raise RuntimeError(f"[정리] 본문을 읽지 못했습니다 — {st.get('why')}")
    if not st.get("found"):
        log(f"{tag} 참고글에서 딸려온 제품 카드/이미지 없음 — 건너뜁니다")
        return False

    before = await post.stats()
    log(f"{tag} 참고글 {st.get('kind')} 삭제 — {st.get('text')!r} ({st.get('cls')})")
    await post.page.bring_to_front()
    await post.page.mouse.click(st["x"], st["y"])
    await post.page.wait_for_timeout(400)
    await post.page.keyboard.press("Delete")
    await post.page.wait_for_timeout(800)

    fr = await post._fr()
    now = await fr.evaluate(MARK_LAST_CARD_JS, host)
    after = await post.stats()
    gone = (not now.get("found")) or (now.get("cards", 0) < st.get("cards", 0))
    log(f"{tag} 카드 삭제 {'✅' if gone else '❌'} — 이미지 {before['imgs']}→{after['imgs']}장 · "
        f"본문 {before['chars']}→{after['chars']}자")
    if not gone:
        raise RuntimeError("[정리] 참고글 제품 카드/이미지를 지우지 못했습니다 — 발행하지 않습니다")
    if before["chars"] - after["chars"] > 400:
        raise RuntimeError(f"[정리] 카드만 지웠는데 본문이 {before['chars'] - after['chars']}자 "
                           f"줄었습니다 — 본문이 함께 지워졌을 수 있습니다")
    return True


# ── 하단 최종 검증 (실전용 전용) ──────────────────────────────────────
TAIL_CHECK_JS = r"""(opt) => {
     const host = opt.host, url = opt.url;
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: 'root 없음'};
     const norm = t => (t || '').replace(/[\u200b\s]+/g, ' ').trim();
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     const hrefsOf = c => Array.from(c.querySelectorAll('a[href]'))
       .map(a => a.getAttribute('href') || '').filter(Boolean);
     const isCard = c => c.querySelector('img')
       && (/oglink|se-link/.test((c.className || '').toString())
           || hrefsOf(c).some(h => host && h.indexOf(host) >= 0)
           || (host && norm(c.innerText).indexOf(host) >= 0));

     const cards = comps.filter(isCard);
     const urlParas = Array.from(root.querySelectorAll('p.se-text-paragraph, .se-text-paragraph'))
       .filter(el => el.tagName === 'P')
       .filter(el => !el.closest('.se-documentTitle'))
       .map(el => norm(el.innerText))
       .filter(t => /^https?:\/\//.test(t.replace(/\s/g, '')));

     const solid = comps.filter(c => norm(c.innerText) || c.querySelector('img'));
     const last = solid[solid.length - 1] || null;
     const lastIsCard = !!(last && isCard(last));
     const card = cards[cards.length - 1] || null;

     let centered = false, cls = '', evidence = [], dump = {};
     if (card) {
       const sec = card.querySelector("[class*='se-section']") || card;
       cls = (sec.className || '').toString();
       centered = /se-section-align-center/.test(cls);

       // ★에디터 안에서는 링크가 <a href> 로 안 나온다. 링크 흔적을 여러 곳에서 찾는다.
       const hrefs = hrefsOf(card);
       if (hrefs.some(h => host && h.indexOf(host) >= 0)) evidence.push('a[href]');

       const attrs = [];
       const walk = el => {
         for (const a of Array.from(el.attributes || [])) {
           const v = a.value || '';
           if (v.length > 4) attrs.push([a.name, v.slice(0, 160)]);
         }
         Array.from(el.children).forEach(walk);
       };
       walk(card);
       const attrHit = attrs.filter(([n, v]) =>
         (host && v.indexOf(host) >= 0) || (url && v.indexOf(url.slice(0, 40)) >= 0));
       if (attrHit.length) evidence.push('data속성');

       const txt = norm(card.innerText);
       if (host && txt.indexOf(host) >= 0) evidence.push('표시텍스트');

       const imgSrc = Array.from(card.querySelectorAll('img'))
         .map(i => i.getAttribute('src') || '').slice(0, 2);
       const hasIframe = !!card.querySelector('iframe');

       dump = {hrefs: hrefs.slice(0, 3),
               attrSample: attrs.slice(0, 8),
               attrHit: attrHit.slice(0, 3),
               text: txt.slice(0, 70),
               imgSrc: imgSrc.map(u => u.slice(0, 70)),
               iframe: hasIframe,
               cls: (card.className || '').toString().slice(0, 60)};
     }
     return {ok: true, cards: cards.length, urlParas: urlParas.slice(0, 3),
             lastIsCard: lastIsCard, centered: centered, cls: cls.slice(0, 60),
             evidence: evidence, dump: dump,
             lastText: last ? norm(last.innerText).slice(0, 40) : ''};
   }"""


async def verify_product_tail(post, product_url: str, log, tag: str = "") -> dict:
    """하단이 '제품 카드 1개 + 벌거벗은 URL 없음' 인지 최종 확인한다.

    ★에디터 안에서는 oglink 카드에 `<a href>` 가 붙지 않는다(링크는 에디터 내부 모델).
      2026-08-21 실측: `hrefs=[]` 라서 href 만으로 검증하면 정상 카드도 실패한다.
      → a[href] · data 속성 · 표시 텍스트(도메인) 순으로 링크 흔적을 찾고,
        DOM 에서 전혀 확인할 수 없으면 **방금 그 URL 로 카드를 만들었다는 사실**과
        카드 존재 여부로 판정한다(발행 결과에서 최종 확인 가능).
    """
    host = re.sub(r"^https?://([^/]+).*$", r"\1", product_url)
    fr = await post._fr()
    st = await fr.evaluate(TAIL_CHECK_JS, {"host": host, "url": product_url})
    if not st.get("ok"):
        raise RuntimeError(f"[하단검증] 본문을 읽지 못했습니다 — {st.get('why')}")

    log(f"{tag} 하단 검증 — 제품카드 {st['cards']}개 · 남은 URL 문단 {len(st['urlParas'])}개 · "
        f"맨 끝이 카드={st['lastIsCard']} · 가운데정렬={st['centered']}")

    dump = st.get("dump") or {}
    if dump:
        log(f"{tag}   카드 구조 — class={dump.get('cls')!r} iframe={dump.get('iframe')}")
        log(f"{tag}   a[href]={dump.get('hrefs')}")
        log(f"{tag}   표시텍스트={dump.get('text')!r}")
        log(f"{tag}   이미지 src={dump.get('imgSrc')}")
        if dump.get("attrHit"):
            log(f"{tag}   링크가 담긴 속성={dump.get('attrHit')}")
        else:
            log(f"{tag}   (참고) 속성 예시={dump.get('attrSample')}")

    problems = []
    if st["cards"] != 1:
        problems.append(f"제품 카드가 {st['cards']}개(1개여야 함)")
    if st["urlParas"]:
        problems.append(f"벌거벗은 URL 문단 {st['urlParas']}")
    if not st["lastIsCard"]:
        problems.append(f"맨 끝이 제품 카드가 아님(끝 내용: {st['lastText']!r})")
    if problems:
        raise RuntimeError("[하단검증] 실패 — " + " / ".join(problems))

    ev = st.get("evidence") or []
    if ev:
        log(f"{tag} 카드 링크 확인 ✅ — 근거: {', '.join(ev)} (제품 도메인 {host})")
    else:
        # DOM 어디에서도 링크를 못 읽는 구조 → 카드 존재 + 방금 넣은 URL 로 판정한다.
        log(f"{tag} (참고) 카드 링크를 DOM 에서 읽을 수 없습니다 — 에디터 내부 모델에만 "
            f"있는 구조입니다. 방금 넣은 URL 로 만든 카드 1개가 확인되어 통과 처리합니다.")
        log(f"{tag}   넣은 URL: {product_url}")

    if not st["centered"]:
        log(f"{tag} ⚠ 제품 카드가 가운데 정렬이 아닙니다 — {st['cls']!r}")
    log(f"{tag} 하단 검증 통과 ✅ — 제품 카드 1개만 남음")
    return st


async def copy_whole_range(src, log, label: str = "참고글") -> dict:
    """**발행 화면** 참고글용 복사 — 본문 전체를 한 Range 로 잡아 복사한다.

    ★발행 화면은 일반 웹페이지라 Ctrl+A 를 누르면 본문이 아니라 **페이지 전체**(상단 메뉴·
      사이드바까지)가 잡힌다. 그래서 수정 화면에서 쓰던 '이미지 클릭 → Ctrl+A' 방식을
      여기에 그대로 쓰면 안 된다. 검수용에서 검증된 `SourceView.copy_all()`(첫~마지막
      본문 컴포넌트를 한 Range 로 선택 → execCommand) 을 그대로 쓴다.
    ★구간을 쪼개지 않으므로 이미지 2장 가로배치·그룹 정렬이 흩어지지 않는다.
    ★맨 끝 제품 카드는 `SourceView.scan()` 이 애초에 복사 구간에서 빼 준다.
    """
    got = await src.copy_all()
    log(f"[{label}] (발행 화면) 한 번에 복사 — 컴포넌트 {got['comps']}개 · "
        f"이미지 {got['imgs']}장 · {got['chars']}자")
    return {"chars": got["chars"], "imgs": got["imgs"], "comps": got["comps"],
            "from": "view-range"}
