"""발행된 글 주소를 블로그 RSS로 수집한다 (2026-08-20 신설).

왜 RSS인가:
  · 발행 직후 에디터에서 URL을 잡으려 하면 리디렉트 타이밍 때문에 자주 놓친다
    (실측: 발행은 성공했는데 'postwrite'로 읽혀 실패로 오판 + 지저분한 PostView URL 기록).
  · 20~30건을 한 번에 발행하는 흐름에선 건별로 URL을 붙잡는 것보다
    **전부 발행한 뒤 목록에서 한 번에 걷는 편**이 안정적이다(사용자 제안).
  · RSS는 로그인도 DOM 선택자도 필요 없고 `https://blog.naver.com/{id}/{logNo}` 형태의
    깔끔한 주소와 발행시각(pubDate)을 준다.

주의: RSS 반영이 몇 초~수십 초 늦을 수 있어 원하는 건수가 찰 때까지 폴링한다.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

RSS_URL = "https://rss.blog.naver.com/{blog_id}.xml"
KST = timezone(timedelta(hours=9))


def clean_url(url: str) -> str:
    """'?fromRss=true' 같은 추적 파라미터를 떼고 표준형으로."""
    u = (url or "").split("?")[0].strip()
    m = re.search(r"blog\.naver\.com/([^/]+)/(\d+)", u)
    return f"https://blog.naver.com/{m.group(1)}/{m.group(2)}" if m else u


def fetch_posts(blog_id: str, timeout: int = 20) -> list[dict]:
    """RSS의 글 목록 → [{url, title, published(datetime)}] (최신순 그대로)."""
    r = requests.get(RSS_URL.format(blog_id=blog_id), timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.findall(".//item"):
        link = clean_url(it.findtext("link") or "")
        if not link:
            continue
        when = None
        raw = it.findtext("pubDate") or ""
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=KST)
        except Exception:  # noqa: BLE001
            pass
        out.append({"url": link, "title": (it.findtext("title") or "").strip(), "published": when})
    return out


def collect_new(blog_id: str, since: datetime, expect: int,
                timeout_sec: int = 180, interval: int = 10, log=print) -> list[str]:
    """since 이후 올라온 글 URL을 발행순(오래된 것 → 최신)으로 수집.

    expect 건이 채워질 때까지 최대 timeout_sec 동안 폴링하고, 다 못 채워도 모인 만큼 돌려준다
    (일부만 발행 성공한 경우도 있으므로 실패로 보지 않는다)."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=KST)
    deadline = time.time() + timeout_sec
    best: list[dict] = []
    while True:
        try:
            posts = fetch_posts(blog_id)
        except Exception as exc:  # noqa: BLE001
            log(f"   [RSS] 조회 실패(재시도): {type(exc).__name__}: {exc}")
            posts = []
        fresh = [p for p in posts if p["published"] and p["published"] >= since]
        if len(fresh) > len(best):
            best = fresh
            log(f"   [RSS] 새 글 {len(fresh)}/{expect}건 확인")
        if len(best) >= expect or time.time() >= deadline:
            break
        time.sleep(interval)
    best.sort(key=lambda p: p["published"])          # 발행순 = 시트 기록순
    if len(best) < expect:
        log(f"   [RSS] {expect}건 중 {len(best)}건만 확인됨(반영 지연 가능) — 확인된 것만 기록")
    return [p["url"] for p in best]
