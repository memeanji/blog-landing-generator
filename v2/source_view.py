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
                   : /se-table/.test(cls) ? 'table'
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

     // ★편집기/뷰어가 그려 넣는 **장식 노드**를 복사 범위에서 잠시 뺀다(2026-09-14).
     //   '대표(대표사진 배지)' 같은 UI 요소가 선택에 섞이면, 붙여넣을 때 스마트에디터가
     //   그것을 **표(se-table) 컴포넌트로 바꿔 넣는다**(실측: 생성글에 se-table 5개,
     //   그중 하나가 초록 배경 '대표'). 기준글 문서에는 없던 블록이라 배치가 어긋난다.
     //   ⚠️원본 글을 고치는 게 아니다 — 화면에 떠 있는 DOM 에서만 잠시 떼었다가 되돌린다.
     //     (이 화면은 저장/발행하지 않는다.)
     const parked = [];
     const isDecoration = (el) => {
       if (!el || el.nodeType !== 1) return false;
       if (el.classList && el.classList.contains('se-component')) return false;
       const cls = (el.className || '').toString();
       if (/se-cover|badge|representative|se-thumbnail-label|se-repImage/i.test(cls)) return true;
       const t = (el.textContent || '').trim();
       // ★실측(2026-09-14): 붙여넣기 결과에 1×1 표가 여러 개 생겼다 —
       //   4개는 **빈 표**, 1개는 초록 배경 '대표'. 기준글 문서에는 표가 0개다.
       //   그래서 **이미지가 없고 내용이 비었거나 '대표' 인 표**를 복사 범위에서 뺀다.
       //   (이미지가 든 표는 건드리지 않는다 — 본문 이미지를 잃으면 안 된다.)
       if (el.tagName === 'TABLE' && !el.querySelector('img')
           && (t === '' || t === '대표' || t.length <= 4)) return true;
       return t === '대표' && (el.children.length === 0 || t.length <= 4);
     };
     for (let i = opt.first; i <= opt.last; i++) {
       const comp = document.querySelector('[data-v2-c="' + i + '"]');
       if (!comp) continue;
       Array.from(comp.querySelectorAll('*')).forEach(el => {
         if (!isDecoration(el)) return;
         parked.push({node: el, parent: el.parentNode, next: el.nextSibling,
                      info: {tag: el.tagName,
                             cls: ((el.className || '') + '').slice(0, 60),
                             text: (el.textContent || '').trim().slice(0, 20),
                             html: (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 180)}});
         el.parentNode.removeChild(el);
       });
     }

     // 복사 범위 안에 원래 표가 몇 개 있는지(장식 제거 뒤 기준) — 판정 근거로 남긴다.
     let tablesInRange = 0;
     for (let i = opt.first; i <= opt.last; i++) {
       const comp = document.querySelector('[data-v2-c="' + i + '"]');
       if (comp) tablesInRange += comp.querySelectorAll('table').length;
     }

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

     // 떼어 둔 장식 노드를 제자리에 되돌린다(화면을 원래대로).
     parked.slice().reverse().forEach(p => {
       try { p.parent.insertBefore(p.node, p.next); } catch (e) {}
     });

     return {ok: true, copied: copied, err: err, got: got, text: txt.slice(0, 120),
             stripped: parked.map(p => p.info), tablesInRange: tablesInRange};
   }"""



SELECT_ALL_COPY_JS = r"""(opt) => {
     // ★사람이 하는 방식 그대로: 본문 전체를 선택해 한 번에 복사한다(2026-09-14 전환).
     //   예전에는 Range 로 '첫~마지막 텍스트' 만 잘라 복사했는데, 그 부분 선택이
     //   기준글에 따라 1×1 빈 표(se-table)를 함께 끌고 들어왔다(프리즈젯 실측 4~5개).
     //   수동으로 Ctrl+A → 복사 → 붙여넣기 한 글은 표가 0개였다 — 그 방식을 따른다.
     //   제품 링크 카드도 같이 복사되지만, 붙여넣은 뒤 edit_post.remove_pasted_card 가 지운다.
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '본문 root 없음'};

     const s = window.getSelection();
     s.removeAllRanges();
     const r = document.createRange();
     r.selectNodeContents(root);            // = Ctrl+A (본문 영역 전체)
     s.addRange(r);

     const holder = document.createElement('div');
     holder.appendChild(r.cloneContents());
     const txt = (s.toString() || '').replace(/\s+/g, ' ').trim();
     const comps = Array.from(holder.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     const got = {chars: txt.length,
                  imgs: holder.querySelectorAll('img').length,
                  comps: comps.length,
                  tables: holder.querySelectorAll('table').length};

     if (got.comps < opt.minComps || got.imgs < opt.minImgs)
       return {ok: false, why: '전체 선택 검증 실패', got: got};

     let copied = false, err = '';
     try { copied = document.execCommand('copy'); } catch (e) { err = String(e); }
     return {ok: true, copied: copied, err: err, got: got, text: txt.slice(0, 120)};
   }"""


MARK_BODY_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '본문 root 없음'};
     document.querySelectorAll('[data-v2-body]').forEach(e => e.removeAttribute('data-v2-body'));
     // 첫 텍스트 컴포넌트를 클릭 대상으로(이미지를 클릭하면 이미지가 선택된다)
     const first = Array.from(root.querySelectorAll('.se-component.se-text'))
       .find(c => !c.closest('.se-documentTitle')) || root;
     first.setAttribute('data-v2-body', '1');
     return {ok: true};
   }"""

def is_decor_table(row) -> bool:
    """붙여넣기에 딸려오는 **에디터 장식 표**인가(2026-09-14).

    실측: 프리즈젯 검수용 글은 이미지마다 그 뒤에 빈 `se-table` 이 하나씩 붙고,
    첫 이미지 뒤에는 '대표'(대표사진 배지) 표가 하나 더 붙는다. 원본 기준 랜딩에는
    없던 것이라 사람 눈에는 안 보여도 레이아웃을 망가뜨린다.
    **글자도 사진도 없는 표**만 장식으로 본다 — 내용이 있는 진짜 표는 절대 건드리지 않는다.
    """
    if (row.get("kind") if isinstance(row, dict) else None) != "table":
        return False
    if row.get("imgs"):
        return False
    t = (row.get("head") or "").strip()
    return (row.get("chars") or 0) <= 6 and t in ("", "대표", "대표사진", "대표 사진")


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
        self.full_copy = False       # True = 본문 전체 복사(사람의 Ctrl+A 방식)
        self.tables_in_range = 0     # 복사본 안의 <table> 수(진단용)
        self.clipboard_tables = 0    # 클립보드 HTML 안의 <table> 수(진단·폴백용)

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

    # ★전체 복사(full_copy)면 기대치도 **기준글 전체** 기준이어야 한다(2026-09-14).
    #   이걸 빼먹어서 v1.2.0 이 붙여넣기에서 '이미지 5개 ≠ 기대 4개' 로 죽었다 —
    #   클립보드엔 제품카드 이미지까지 5장이 들어 있는데 기대치는 구간(4장)이었다.
    #   제품카드를 지운 뒤에는 run.py 가 full_copy 를 False 로 돌려 구간 기준으로 검증한다.
    @property
    def _basis(self) -> list:
        return self.rows if getattr(self, "full_copy", False) else self.span

    @property
    def want_comps(self) -> int:
        return len(self._basis)

    @property
    def want_imgs(self) -> int:
        return sum(r["imgs"] for r in self._basis)

    @property
    def want_chars(self) -> int:
        return sum(r["chars"] for r in self._basis)

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
    async def copy_all_body_keys(self) -> dict:
        """**사람과 똑같이** 본문을 클릭 → Ctrl+A → Ctrl+C 로 복사한다.

        ★2026-09-14 실측으로 확정한 방식이다.
          같은 기준글·같은 에디터인데 결과가 갈렸다:
            · 기준글 수정화면            <table> 0개 (이미지 안에 '대표' 배지 텍스트만 있음)
            · 사람이 Ctrl+A→Ctrl+V 한 글  <table> 0개  ✅
            · 프로그램이 만든 글          <table> 5개  ✗ (빈 표 4 = 이미지 4개와 일치 + '대표' 1)
          즉 표는 원본에도, 붙여넣기에도 없었고 **합성 복사**
          (`createRange()` + `execCommand('copy')`)가 만들어낸 것이다.
          그래서 진짜 키 입력으로 바꾼다 — 브라우저가 사람의 복사와 같은 경로를 타게 된다.
        """
        await self.page.bring_to_front()
        before = await self._clipboard()
        fr = await self._fr()

        # 본문 안을 클릭해 포커스를 준다(캐럿이 본문에 있어야 Ctrl+A 가 본문을 잡는다).
        clicked = await fr.evaluate(MARK_BODY_JS)
        if not clicked.get("ok"):
            raise RuntimeError(f"[복사] 본문을 찾지 못했습니다 — {clicked.get('why')}")
        try:
            await fr.locator('[data-v2-body]').first.click(timeout=8000,
                                                           position={"x": 20, "y": 8})
        except Exception as exc:                               # noqa: BLE001
            self.log(f"[복사] 본문 클릭 실패({type(exc).__name__}) — 그대로 진행합니다")
        await self.page.wait_for_timeout(300)

        await self.page.keyboard.press("Control+A")
        await self.page.wait_for_timeout(250)
        await self.page.keyboard.press("Control+C")
        await self.page.wait_for_timeout(700)

        after = await self._clipboard()
        if not after or after == before:
            raise RuntimeError("[복사] 클립보드가 바뀌지 않았습니다(Ctrl+A/Ctrl+C) — "
                               "새 글을 만들지 않습니다")

        self.full_copy = True
        report, clip_tables = await self.clipboard_html_report2()
        self.clipboard_tables = clip_tables
        self.tables_in_range = 0
        got = {"chars": len(after), "imgs": self.want_imgs, "comps": self.want_comps,
               "tables": clip_tables}
        self.log(f"[복사] 사람 방식(Ctrl+A → Ctrl+C)으로 복사 — 클립보드 {len(after):,}자")
        self.log("[복사] 기준 구성(전체) — " + self.kind_summary_full())
        self.log("[복사] " + report)
        if clip_tables:
            self.log(f"[복사] ⚠️ 클립보드에 <table> {clip_tables}개 — 붙여넣은 뒤 검증이 막습니다")
        return got

    async def copy_all_body(self) -> dict:
        """본문 **전체**를 선택해 복사한다(사람의 Ctrl+A 와 같은 범위).

        ★2026-09-14 전환: 부분 Range 복사(copy_all)가 기준글에 따라 빈 표를 끌고 와
          레이아웃이 틀어졌다. 사람이 수동으로 한 글은 표가 0개였으므로 그 방식으로 바꾼다.
          제품 링크 카드까지 함께 복사되는데, 그건 붙여넣은 뒤 지우고 이 행의 링크로 새로 만든다.
        """
        await self.page.bring_to_front()
        before = await self._clipboard()
        fr = await self._fr()
        res = await fr.evaluate(SELECT_ALL_COPY_JS,
                                {"minComps": max(1, self.want_comps),
                                 "minImgs": self.want_imgs})
        if not res.get("ok"):
            raise RuntimeError(f"[복사] {res.get('why')} — 실제={res.get('got')} "
                               f"기대= 컴포넌트 {self.want_comps}개 이상 "
                               f"이미지 {self.want_imgs}개 이상")
        if not res.get("copied"):
            raise RuntimeError(f"[복사] execCommand('copy') 실패 "
                               f"({res.get('err') or '반환 false'})")
        after = await self._clipboard()
        if not after or after == before:
            raise RuntimeError("[복사] 클립보드가 바뀌지 않았습니다 — 새 글을 만들지 않습니다")

        got = res["got"]
        self.full_copy = True
        self.tables_in_range = int(got.get("tables") or 0)
        self.log(f"[복사] 본문 전체 복사(Ctrl+A 방식) — 컴포넌트 {got['comps']}개 · "
                 f"이미지 {got['imgs']}개 · {got['chars']}자 · 표 {got['tables']}개")
        self.log("[복사] 기준 구성(전체) — " + self.kind_summary_full())
        report, clip_tables = await self.clipboard_html_report2()
        self.log("[복사] " + report)
        self.clipboard_tables = clip_tables
        return got

    def kind_counts_full(self) -> dict:
        """**기준글 전체**(제품링크 포함)의 종류별 개수 — 전체 복사일 때의 대조 기준."""
        out: dict = {}
        for r in self.rows:
            k = "table(빈)" if is_decor_table(r) else r["kind"]
            out[k] = out.get(k, 0) + 1
        return out

    def kind_summary_full(self) -> str:
        c = self.kind_counts_full()
        return " · ".join(f"{k} {v}개" for k, v in sorted(c.items())) or "(없음)"

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
        # ★무엇을 빼고 복사했는지 남긴다 — 없으면 한 줄도 안 찍힌다(2026-09-14).
        stripped = res.get("stripped") or []
        if stripped:
            self.log(f"[복사] 편집기 장식 {len(stripped)}개를 복사에서 제외했습니다")
            for info in stripped[:8]:
                if isinstance(info, dict):
                    self.log(f"        · <{info.get('tag','')}> class={info.get('cls','')!r} "
                             f"text={info.get('text','')!r}")
                    self.log(f"          {info.get('html','')}")
                else:
                    self.log(f"        · {info}")
        else:
            self.log("[복사] 편집기 장식 0개 — 제외한 노드 없음")
        self.log(f"[복사] 복사 범위 안 <table> {res.get('tablesInRange', '?')}개(장식 제거 후)")
        self.log(f"[복사] 본문 한 번에 복사 완료 — 컴포넌트 {got['comps']}개 · "
                 f"이미지 {got['imgs']}개 · {got['chars']}자")
        # 붙여넣기 뒤 비교용으로 기준 구성(종류별 개수)을 남겨 둔다.
        self.log("[복사] 기준 구성 — " + self.kind_summary())
        report, clip_tables = await self.clipboard_html_report2()
        self.log("[복사] " + report)
        # ★폴백 판단용으로 남긴다(run.py 가 본다).
        self.tables_in_range = int(res.get("tablesInRange") or 0)
        self.clipboard_tables = clip_tables
        return got

    async def clipboard_html_report2(self):
        """(사람이 읽는 요약, 표 개수) — 표 개수는 폴백 판단에 쓴다."""
        text = await self.clipboard_html_report()
        import re as _re
        m = _re.search(r"<table> (\d+)개", text)
        return text, int(m.group(1)) if m else 0

    def kind_counts(self) -> dict:
        """붙여넣기 결과와 대조할 기준. 전체 복사면 기준글 전체, 아니면 복사 구간."""
        if getattr(self, "full_copy", False):
            return self.kind_counts_full()
        out: dict = {}
        for r in self.span:
            k = "table(빈)" if is_decor_table(r) else r["kind"]
            out[k] = out.get(k, 0) + 1
        return out

    def kind_summary(self) -> str:
        c = self.kind_counts()
        return " · ".join(f"{k} {v}개" for k, v in sorted(c.items())) or "(없음)"

    async def clipboard_html_report(self) -> str:
        """클립보드의 text/html 을 들여다보고 한 줄 요약을 만든다(진단 전용).

        ★이게 있어야 **표가 어디서 생겼는지** 갈린다(2026-09-14).
          · 클립보드에 이미 <table> 이 있다  → 복사 단계(기준글 화면)에서 섞인 것
          · 클립보드엔 없는데 결과에 표가 생김 → 붙여넣기 단계(스마트에디터)가 만든 것
        실패해도 본 작업에 영향을 주지 않는다.
        """
        js = r'''() => navigator.clipboard.read().then(async items => {
             for (const it of items) {
               if (!it.types.includes('text/html')) continue;
               const b = await it.getType('text/html');
               const html = await b.text();
               const tables = (html.match(/<table/gi) || []).length;
               const comps = (html.match(/se-component/g) || []).length;
               const imgs = (html.match(/<img/gi) || []).length;
               let sample = '';
               const at = html.search(/<table/i);
               if (at >= 0) sample = html.slice(at, at + 240).replace(/\s+/g, ' ');
               return {ok: true, len: html.length, tables: tables, comps: comps,
                       imgs: imgs, sample: sample};
             }
             return {ok: false, why: 'text/html 없음'};
           }).catch(e => ({ok: false, why: String(e)}))'''
        try:
            r = await self.page.evaluate(js)
        except Exception as exc:                               # noqa: BLE001
            return f"클립보드 HTML 확인 실패({type(exc).__name__})"
        if not r or not r.get("ok"):
            return f"클립보드 HTML 확인 불가 — {(r or {}).get('why')}"
        out = (f"클립보드 HTML {r['len']:,}자 · se-component {r['comps']} · "
               f"img {r['imgs']} · <table> {r['tables']}개")
        if r.get("sample"):
            out += chr(10) + "        표 원문: " + r["sample"]
        return out

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
