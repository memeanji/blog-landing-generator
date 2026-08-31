r"""기준글 **발행 화면**에서 본문을 '한 번에' 복사한다 (2026-08-21 사용자 시연 방식).

어제까지의 `source.py` 는 **수정 화면**을 열어 `.se-component` 를 9번 따로 복사했다.
사용자가 실제로 하는 방식은 정반대였다 — 발행 화면에서

    맨 위 텍스트 ──(드래그)── 맨 아래 텍스트

를 **한 번에 선택해 한 번 복사/붙여넣기**한다. 실측(브라우저 selection 을 그대로 읽음):

    selection: 컴포넌트 9개 · 이미지 4개 · 824자
    시작 = .se-component.se-text …  →  끝 = .se-component.se-text …

이 방식의 결과로 확인된 것:
  · `[출처] 제목|작성자 닉` 이 **아예 붙지 않는다** (컴포넌트 단위로 여러 번 복사할 때만 붙었다)
  · 붙여넣은 이미지가 **이미 se-section-align-center** 다 (하나씩 정렬할 필요 없음)
  · 맨 끝 **제품링크 이미지는 선택에서 뺀다** — href 가 빈 앵커라 복사해도 링크가 안 따라온다.
    그 자리는 제품 URL 을 붙여넣어 oglink 카드로 새로 만든다.
"""
from __future__ import annotations

import re

from . import browser

# 이 글자수 미만이면 '실질 텍스트'로 보지 않는다(제로폭 공백 1자짜리 꼬리 문단 제외용).
MIN_TAIL_TEXT = 5


def view_url(post_url: str) -> str:
    """`blog.naver.com/{id}/{no}` 형태로 정규화한다(발행 화면 그대로 연다)."""
    m = re.search(r"blog\.naver\.com/(?:PostView\.naver\?blogId=)?([A-Za-z0-9_\-]+)"
                  r"(?:/|&logNo=)(\d+)", post_url)
    if not m:
        return post_url
    return f"https://blog.naver.com/{m.group(1)}/{m.group(2)}"


SCAN_JS = r"""(opt) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '본문 root 없음'};
     const norm = t => (t || '').replace(/\s+/g, ' ').trim();

     document.querySelectorAll('[data-v2-c]').forEach(e => e.removeAttribute('data-v2-c'));
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));

     const rows = comps.map((c, i) => {
       c.setAttribute('data-v2-c', String(i));
       const cls = (c.className || '').toString();
       const txt = norm(c.innerText);
       return {i: i,
               kind: /se-oglink/.test(cls) ? 'oglink'
                   : /se-image/.test(cls) ? 'image'
                   : /se-text/.test(cls) ? 'text' : 'other',
               chars: txt.length,
               imgs: c.querySelectorAll('img').length,
               head: txt.slice(0, 50)};
     });

     const title = document.querySelector('.se-title-text')
                || document.querySelector('.se-documentTitle .se-text-paragraph');
     const og = document.querySelector("meta[property='og:title']");
     return {ok: true, rows: rows,
             title: norm(title ? title.innerText : '') || (og ? og.content : '')};
   }"""


SELECT_COPY_JS = r"""(opt) => {
     const a = document.querySelector('[data-v2-c="' + opt.first + '"]');
     const b = document.querySelector('[data-v2-c="' + opt.last + '"]');
     if (!a || !b) return {ok: false, why: '선택 시작/끝 컴포넌트를 찾지 못함'};
     a.scrollIntoView({block: 'center'});

     const r = document.createRange();
     r.setStartBefore(a);
     r.setEndAfter(b);
     const s = window.getSelection();
     s.removeAllRanges();
     s.addRange(r);

     const holder = document.createElement('div');
     holder.appendChild(r.cloneContents());
     const txt = (s.toString() || '').replace(/\s+/g, ' ').trim();
     const got = {chars: txt.length,
                  imgs: holder.querySelectorAll('img').length,
                  comps: holder.querySelectorAll('.se-component').length};

     // 검증에 실패하면 복사하지 않는다(엉뚱한 클립보드로 새 글을 만들지 않기 위해).
     if (got.imgs !== opt.wantImgs || got.comps !== opt.wantComps)
       return {ok: false, why: '선택 검증 실패', got: got};

     let copied = false, err = '';
     try { copied = document.execCommand('copy'); } catch (e) { err = String(e); }
     return {ok: true, copied: copied, err: err, got: got, text: txt.slice(0, 120)};
   }"""


class _Comp:
    """writer.verify_body 가 기대하는 최소 인터페이스(kind/chars/head)."""

    __slots__ = ("kind", "chars", "head", "imgs")

    def __init__(self, row: dict) -> None:
        self.kind = row["kind"]
        self.chars = row["chars"]
        self.head = row["head"]
        self.imgs = row["imgs"]


class SourceView:
    """기준글 발행 화면(읽기 전용). 절대 수정/저장하지 않는다."""

    def __init__(self, page, frame, log) -> None:
        self.page = page
        self.frame = frame
        self.log = log
        self.title = ""
        self.rows: list[dict] = []
        self.first = -1
        self.last = -1
        self.product_image = False
        self.extra_images = 0        # 제품링크 카드를 만든 뒤 1 로 올린다

    async def _fr(self):
        return await browser.fresh(self.page, self.frame, self.log, "기준글")

    # ── 1. 구조 파악 ────────────────────────────────────────────────
    async def scan(self) -> None:
        fr = await self._fr()
        res = await fr.evaluate(SCAN_JS, {})
        if not res.get("ok"):
            raise RuntimeError(f"[기준글] 본문을 읽지 못했습니다 — {res.get('why')}")
        self.rows = res["rows"]
        self.title = res["title"]

        for r in self.rows:
            self.log(f"      · #{r['i']:<2} {r['kind']:<7} {r['chars']:>4}자 "
                     f"이미지 {r['imgs']}개 {r['head']!r}")

        # 시작 = 첫 실질 컴포넌트 / 끝 = 마지막 '실질 텍스트' 컴포넌트
        #   ★끝을 텍스트로 잡으면 뒤에 붙은 제품링크 이미지와 제로폭 꼬리 문단이 자연히 빠진다.
        body = [r for r in self.rows if r["chars"] >= MIN_TAIL_TEXT or r["imgs"]]
        if not body:
            raise RuntimeError("[기준글] 본문 컴포넌트가 없습니다")
        texts = [r for r in body if r["kind"] == "text" and r["chars"] >= MIN_TAIL_TEXT]
        if not texts:
            raise RuntimeError("[기준글] 본문 텍스트 컴포넌트가 없습니다")
        self.first = body[0]["i"]
        self.last = texts[-1]["i"]

        tail = [r for r in self.rows if r["i"] > self.last and (r["imgs"] or r["chars"])]
        self.product_image = any(r["kind"] in ("image", "oglink") for r in tail)
        for r in tail:
            self.log(f"      · 제외(선택 밖) #{r['i']} {r['kind']} "
                     f"{r['chars']}자 이미지 {r['imgs']}개")

        self.log(f"[기준글] 제목 — {self.title!r}")
        self.log(f"[기준글] 복사 구간 #{self.first} ~ #{self.last} — "
                 f"컴포넌트 {self.want_comps}개 · 이미지 {self.want_imgs}개 · "
                 f"{self.want_chars}자")
        if self.product_image:
            self.log("[기준글] 맨 끝 제품링크는 복사하지 않고 URL 로 카드를 새로 만듭니다")

    @property
    def span(self) -> list[dict]:
        return [r for r in self.rows if self.first <= r["i"] <= self.last]

    @property
    def want_comps(self) -> int:
        return len(self.span)

    @property
    def want_imgs(self) -> int:
        return sum(r["imgs"] for r in self.span)

    @property
    def want_chars(self) -> int:
        return sum(r["chars"] for r in self.span)

    # ── writer.verify_body 호환 ─────────────────────────────────────
    #   기존 검증 코드가 src.total_images / total_chars / components 를 본다.
    @property
    def total_images(self) -> int:
        """복사 구간 이미지 + 나중에 만든 제품링크 카드 이미지."""
        return self.want_imgs + self.extra_images

    @property
    def total_chars(self) -> int:
        return self.want_chars

    @property
    def components(self) -> list:
        return [_Comp(r) for r in self.span]

    # ── 2. 한 번에 복사 ─────────────────────────────────────────────
    async def copy_all(self) -> dict:
        """본문 전체를 한 Range 로 선택해 클립보드에 넣는다.

        ★Ctrl+C(합성 키)는 브라우저 창이 OS 포커스를 가져야만 시스템 클립보드에 닿는다.
          그래서 렌더러에서 도는 `document.execCommand('copy')` 를 쓴다.
        """
        await self.page.bring_to_front()
        before = await self._clipboard()
        fr = await self._fr()
        res = await fr.evaluate(SELECT_COPY_JS, {
            "first": self.first, "last": self.last,
            "wantComps": self.want_comps, "wantImgs": self.want_imgs})
        if not res.get("ok"):
            raise RuntimeError(f"[복사] {res.get('why')} — 실제={res.get('got')} "
                               f"기대= 컴포넌트 {self.want_comps}개 이미지 {self.want_imgs}개")
        if not res.get("copied"):
            raise RuntimeError(f"[복사] execCommand('copy') 실패 "
                               f"({res.get('err') or '반환 false'})")

        after = await self._clipboard()
        if not after or after == before:
            raise RuntimeError("[복사] 클립보드가 바뀌지 않았습니다 — 새 글을 만들지 않습니다")
        got = res["got"]
        self.log(f"[복사] 본문 한 번에 복사 완료 — 컴포넌트 {got['comps']}개 · "
                 f"이미지 {got['imgs']}개 · {got['chars']}자")
        return got

    async def _clipboard(self) -> str:
        try:
            return await self.page.evaluate(
                "() => navigator.clipboard.readText().catch(() => '')")
        except Exception:                                      # noqa: BLE001
            return ""


async def open_source(ctx, url: str, log) -> SourceView:
    """기준글을 **발행 화면**으로 연다(수정 화면 아님 — 원본을 건드릴 일이 없다)."""
    target = view_url(url)
    page = await ctx.new_page()
    await page.goto(target, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    if "nidlogin" in (page.url or ""):
        raise RuntimeError("기준글이 로그인 페이지로 튕겼습니다.")
    frame = await browser.find_editor_frame(page, log, "기준글", timeout_sec=30, min_score=10)
    log(f"[기준글] 발행 화면 열기 완료 — {target}")
    return SourceView(page, frame, log)
