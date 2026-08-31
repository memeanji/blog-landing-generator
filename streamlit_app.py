r"""블로그 랜딩 생성기 — Streamlit UI.

    실행_Streamlit.bat   (또는  python -m streamlit run streamlit_app.py)

★이 화면은 **Playwright 를 직접 실행하지 않는다.** 고른 값으로 `Job` 을 만들어 큐에 넣기만
  하고(`v2.queue_store`), 실제 실행은 그 PC 의 로컬 에이전트(`v2.agent`)가 한다.

    Streamlit(UI) ──submit──▶ 큐 ──claim──▶ Local Agent ──▶ v2.run / v2.run_production
                     ▲                          │
                     └────── 로그·@@EVENT ───────┘

  · 화면이 하는 일은 **Job 조립 + 큐 저장 + 상태 보기** 뿐이다.
  · 브랜드를 고르면 기준시트와 UTM 빌더가 **한 세트로** 따라온다(`brands.json`).
  · `ready:false` 인 브랜드(준비 중)는 실행 버튼이 잠긴다. CLI 쪽에서도 한 번 더 막는다.
  · 네이버 로그인 세션은 항상 실행하는 PC 의 `sessions/<account>/` 에만 남는다.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import (accounts, batch_plan, brands, catalog, landing_sheet,  # noqa: E402
                queue_store, session_store, sheets)
from v2.job import FLOWS, KINDS, PROD_MODES, Job, python_exe              # noqa: E402

st.set_page_config(page_title="블로그 랜딩 생성기", page_icon="📝", layout="wide")

# ── 표시용 CSS (기능과 무관) ──────────────────────────────────────
#   · 제목은 기본보다 살짝 작게
#   · 브랜드/기준시트/UTM 빌더 안내는 **본문 글씨 크기·색**으로(st.caption 은 작고 연하다)
st.markdown("""
<style>
  /* 상단 여백을 '조금만' 줄인다.
     ★Streamlit 고정 헤더가 약 3.75rem 이라 그보다 작게 주면 제목이 헤더 뒤로 잘린다.
       기본값(약 6rem)에서 4.75rem 으로만 줄인다 — 음수 margin·translate 는 쓰지 않는다. */
  .block-container, [data-testid="stMainBlockContainer"] {
      padding-top: 4.75rem !important;
  }
  h1 { font-size: 1.9rem !important;
       padding-top: .25rem !important; padding-bottom: .1rem !important;
       margin-bottom: .35rem !important; }
  /* 라벨 → 줄바꿈 → 값. 항목마다 한 칸 띄운다(한 줄에 이어붙이지 않는다) */
  .info-block { margin: 0 0 .5rem 0; }
  .info-item  { margin: 0 0 .6rem 0; }
  .info-label { font-size: 1rem; font-weight: 700; line-height: 1.35;
                color: inherit; }
  .info-value { font-size: 1rem; line-height: 1.35; color: inherit; }
</style>
""", unsafe_allow_html=True)


def info_block(*pairs: tuple[str, str]) -> str:
    """`라벨 / 값` 을 항목마다 줄을 바꿔 쌓는다(본문 크기 · 라벨만 굵게)."""
    items = "".join(
        f"<div class='info-item'><div class='info-label'>{label}</div>"
        f"<div class='info-value'>{value}</div></div>"
        for label, value in pairs)
    return f"<div class='info-block'>{items}</div>"

RUNNING_STATES = (queue_store.PENDING, queue_store.RUNNING)
BADGE = {queue_store.PENDING: "⏳ 대기", queue_store.RUNNING: "▶ 실행 중",
         queue_store.DONE: "✅ 완료", queue_store.FAILED: "❌ 실패",
         queue_store.CANCELED: "⏹ 중단됨"}

# @@EVENT 의 stage 이름 → 사람이 읽는 단계 이름
STAGE_LABELS = {
    "brand_config": "브랜드 설정",
    "reference_sheet_selected": "기준시트 선택",
    "reference_sheet_access": "기준시트 접근",
    "utm_sheet_selected": "UTM 빌더 선택",
    "utm_sheet_access": "UTM 빌더 접근",
    "reference_lookup": "기준글 조회",
    "result_columns": "결과 기록 열 확인",
    "row_match": "대상 행 매칭",
    "product_url_lookup": "최종 제품 URL 조회",
    "product_link_find": "기존 제품링크 탐색",
    "product_link_remove": "기존 제품링크 제거",
    "product_link_insert": "신규 제품링크 입력",
    "product_link_verify": "신규 링크 검증",
    "post_build": "글 작성",
    "sheet_mark_done": "시트 완료 표시",
}


# ══════════════════════════════════════════════════════════════════
# 캐시 (읽기 전용 — 브라우저를 켜지 않는다)
# ══════════════════════════════════════════════════════════════════
@st.cache_resource
def get_store():
    return queue_store.get_store()


@st.cache_data(ttl=300, show_spinner=False)
def load_catalog(brand_id: str, account_id: str, ref_tab: str,
                 refresh: bool = False) -> dict:
    """★브랜드가 캐시 키의 맨 앞이다 — 브랜드끼리 목록이 섞이면 안 된다."""
    acc = accounts.resolve(account_id) if account_id else None
    return catalog.load(acc, ref_tab=ref_tab, refresh=refresh, brand=brand_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_tabs(brand_id: str, refresh: bool = False) -> dict:
    """`기준 계정` 선택지 = **그 브랜드 기준시트의 `… 기준랜딩` 탭들**.

    ★코드에 계정을 박지 않는다. 시트에 탭을 추가하면 화면에 그대로 늘어난다.
    """
    return catalog.load_tabs(brand_id, refresh=refresh)


@st.cache_data(ttl=120, show_spinner=False)
def preview_rows(brand_id: str, flow: str, media: str, deficiency: str, kind: str,
                 date: str, campaign: str, mode: str) -> dict:
    """실행 전에 시트만 읽어 '무엇이 매칭됐고 어떤 제품 URL 이 들어가는지' 보여준다.

    ★읽기 전용이다. Playwright 도, 시트 쓰기도 하지 않는다(쓰기 자리 확인은 Dry-run 에서).
    """
    from v2.config import load_settings
    brand = brands.resolve(brand_id)
    brand.require_ready()
    sheets.set_brand(brand)
    landing_sheet.set_brand(brand)
    settings = load_settings()
    settings.check()
    lines: list[str] = []
    out: dict = {"brand": brand.summary(), "rows": [], "reference": None,
                 "log": lines}

    def log(msg: str = "") -> None:
        lines.append(str(msg))

    ref = sheets.find_reference(settings.service_account_json,
                                settings.spreadsheet_id, media, deficiency, kind)
    out["reference"] = {"url": ref.url, "row": ref.row,
                        "product_url": ref.product_url}
    if flow == "production":
        finder = (landing_sheet.find_pending_rows if mode == "create"
                  else landing_sheet.find_published_rows)
        rows = finder(settings.service_account_json, settings.spreadsheet_id,
                      media, date, log, campaign=campaign or "",
                      note=deficiency or "", on_missing="drop")
        out["rows"] = [{"행": r["row"], "순번": r.get("seq", ""),
                        "utm_campaign": r.get("campaign", ""),
                        "최종 제품 URL": r.get("product_url", ""),
                        "블로그 링크": r.get("blog_url", "")} for r in rows]
    return out


@st.cache_data(ttl=60, show_spinner=False)
def brand_names() -> dict:
    """내부 브랜드 키 → 화면 표시명 (repurely → 리퓨어리 …)."""
    try:
        return {b.id: b.title for b in brands.load_brands(include_disabled=True)}
    except Exception:                                          # noqa: BLE001
        return {}


def brand_name(key: str) -> str:
    """★화면에는 항상 한글 브랜드명으로 보여 준다.
    내부 Job/queue/CLI 는 키(`repurely` · `doctor_nuscent`)를 그대로 쓴다.
    설정에 없는 키가 들어오면(옛 기록 등) 원문을 그대로 둔다."""
    k = (key or "").strip()
    return brand_names().get(k, k)


def today_tag() -> str:
    now = datetime.now()
    return f"{now.month}{now.day:02d}"


def start_local_agent() -> str:
    """이 PC 에 에이전트를 하나 띄운다(창 없이)."""
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen([python_exe(), "-m", "v2.agent"], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=flags)
    return f"{python_exe()} -m v2.agent"


# ══════════════════════════════════════════════════════════════════
# 실행 준비 — 로그인(수동) + 에이전트 확보를 버튼 하나로 묶는다.
#   ★기존 기능을 지우지 않고 **내부 함수로 재사용**한다.
#     · 로그인 창      = `v2.session --login <계정>`  (큐 kind="session")
#     · 세션 확인      = `v2.session --check <계정>`  (headless, 화면 버튼은 없앴지만 유지)
#     · 에이전트 시작  = `python -m v2.agent`         (이미 살아 있으면 띄우지 않는다)
#   ★로그인 창은 **에이전트가 도는 PC** 에서 열린다. 그래서 에이전트를 먼저 확보한 뒤
#     로그인 작업을 큐에 넣는다(순서를 바꾸면 로그인 창이 뜨지 않는다).
# ══════════════════════════════════════════════════════════════════
def live_agents(store) -> list[dict]:
    return [a for a in store.agents(max_age_sec=30)
            if a.get("alive") and a.get("state") != "stopped"]


def ensure_agent(store) -> bool:
    """에이전트가 없을 때만 이 PC 에 하나 띄운다. 띄웠으면 True."""
    if live_agents(store):
        return False                      # ★이미 돌고 있으면 중복 실행하지 않는다
    start_local_agent()
    for _ in range(10):                   # 하트비트가 찍힐 때까지 잠깐 기다린다
        time.sleep(0.5)
        if live_agents(store):
            break
    return True


def submit_session_job(store, brand, account, action: str) -> str:
    """세션 작업(`--login` / `--check`)을 큐에 넣는다. 실행은 에이전트가 한다."""
    titles = {"--login": "로그인", "--check": "세션 확인"}
    return store.submit(
        Job(brand=brand.id, account=account.id, extra=[action, account.id]),
        kind="session", title=f"{titles.get(action, action)} — {account.title}")


def session_job_state(store, account) -> str:
    """이 계정의 최근 세션 작업 상태 — running / pending / done / failed / ''."""
    for rec in store.list_jobs(limit=20):
        if rec.get("kind") != "session":
            continue
        if (rec.get("job") or {}).get("account") != account.id:
            continue
        return rec.get("status") or ""
    return ""


# ══════════════════════════════════════════════════════════════════
# 배치 진행 — 10개씩 **순차**로. 배치가 바뀔 때마다 사람이 다시 로그인한다.
#   ★큐·에이전트·CLI 는 그대로다. 여기서는 "다음에 무엇을 큐에 넣을지"만 판단한다.
#     한 번에 한 배치만 큐에 올리므로 동시에 도는 일이 없다.
# ══════════════════════════════════════════════════════════════════
def _job_status(store, job_id: str) -> tuple[str, dict]:
    rec = store.get(job_id) if job_id else None
    return ((rec or {}).get("status") or ""), (rec or {})


def plan_results(store, plan: dict) -> dict:
    """계획의 모든 배치 로그(@@EVENT)를 모아 **건별 성공/실패**를 정리한다.

    같은 행이 여러 번 나오면(재실행) **마지막 결과**를 쓰고, 한 번이라도 성공했으면
    성공으로 본다 — 이미 성공한 건이 실패 목록에 남지 않게 하기 위해서다.
    """
    by_row: dict = {}
    order: list = []
    for b in plan.get("batches") or []:
        jid = b.get("run_job")
        if not jid:
            continue
        events, _ = store.read_events(jid)
        for e in events:
            name = e.get("event")
            if name not in ("published", "post_failed"):
                continue
            row = str(e.get("row") or e.get("no") or "")
            item = {
                "batch": b.get("no"),
                "행": e.get("row") or "",
                "순번": e.get("seq") or e.get("no") or "",
                "utm_campaign": e.get("campaign") or "",
                "블로그 URL": e.get("url") or e.get("blog_url") or "",
            }
            if name == "published":
                item.update({"ok": True, "단계": "발행", "사유": ""})
            else:
                item.update({"ok": False,
                             "단계": e.get("stage") or "post_build",
                             "사유": e.get("reason") or "",
                             "오류": (e.get("error") or "")[:200],
                             "구분": "공통" if e.get("scope") == "common" else "행"})
            if row not in by_row:
                order.append(row)
            prev = by_row.get(row)
            # 한 번이라도 성공했으면 성공을 남긴다
            by_row[row] = item if (prev is None or item["ok"] or not prev["ok"]) \
                else prev
    items = [by_row[r] for r in order]
    fails = [x for x in items if not x["ok"]]
    return {"items": items, "ok": len([x for x in items if x["ok"]]),
            "fail": len(fails), "fails": fails,
            "failed_rows": [str(x["행"]) for x in fails if x["행"]]}


def advance_plan(store, plan: dict) -> dict:
    """큐 상태를 보고 계획을 한 칸 진행시킨다(화면을 그릴 때마다 호출).

    판단은 `v2.batch_plan.advance()` 가 하고, 여기서는 큐를 읽고/넣는 일만 한다.
    """
    def submit_run(batch: dict) -> str:
        total = len(plan.get("batches") or [])
        return store.submit(
            Job.from_dict(batch_plan.job_for(plan, batch)),
            title=f"{plan.get('title')} — 배치 {batch['no']}/{total}")

    plan, changed = batch_plan.advance(
        plan, job_status=lambda jid: _job_status(store, jid), submit_run=submit_run)
    return batch_plan.save(plan) if changed else plan


def start_batch_login(store, plan: dict, brand, account) -> None:
    """다음 배치를 위한 **수동 로그인**을 띄운다(사용자가 버튼을 눌렀을 때만)."""
    if batch_plan.current(plan) is None:
        return
    ensure_agent(store)                      # 이미 돌고 있으면 띄우지 않는다
    batch_plan.mark_logging_in(
        plan, submit_session_job(store, brand, account, "--login"))


def job_summary(rec: dict) -> dict:
    """최근 실행 기록 한 줄 — 시간·브랜드·계정·작업 종류·매체·결핍·건수·결과."""
    job = rec.get("job") or {}
    flow = job.get("flow") or ""
    kind_label = FLOWS.get(flow, {}).get("label", flow)
    if rec.get("kind") == "session":
        kind_label = "세션/로그인"
    elif flow == "production":
        kind_label = f"실전용 · {job.get('mode', '')}"
    elif job.get("dry_run"):
        kind_label = f"{kind_label} (dry-run)"
    err = (rec.get("error") or "").strip().splitlines()
    return {
        "시간": (rec.get("created_at") or "")[5:16].replace("T", " "),
        "브랜드": brand_name(rec.get("brand") or job.get("brand") or ""),
        "계정": job.get("account") or "",
        "작업": kind_label,
        "매체": job.get("media") or "",
        "결핍": job.get("deficiency") or "",
        "건수": job.get("count") if job.get("count") is not None else "",
        "결과": BADGE.get(rec.get("status"), rec.get("status") or ""),
        "발행": len(rec.get("published") or []),
        "실패 사유": (err[0][:80] if err else ""),
    }


def render_steps(events: list[dict]) -> None:
    """@@EVENT 를 사람이 읽는 진행 단계로."""
    if not events:
        st.caption("아직 단계 정보가 없습니다.")
        return
    lines: list[str] = []
    for e in events:
        name = e.get("event")
        if name == "run_started":
            lines.append(f"▶ 시작 — 브랜드 "
                         f"{e.get('brand_label') or brand_name(e.get('brand', ''))}"
                         f" · {e.get('media', '')} / {e.get('deficiency', '')}")
        elif name == "stage":
            label = STAGE_LABELS.get(e.get("stage"), e.get("stage"))
            ok = e.get("status") == "ok"
            tail = ""
            if e.get("row"):
                tail += f" (행 {e['row']})"
            if e.get("matched"):
                tail += f" — {e['matched']}건"
            if not ok:
                tail += f" — {e.get('reason', '')}"
            lines.append(f"{'✅' if ok else '❌'} {label}{tail}")
        elif name == "plan":
            lines.append(f"📋 계획 — {e.get('total', 0)}건")
        elif name == "post_ready":
            lines.append(f"📝 작성 완료 [{e.get('no')}/{e.get('total')}]")
        elif name == "published":
            lines.append(f"🚀 발행 [{e.get('no')}/{e.get('total')}] {e.get('url', '')}")
        elif name == "post_failed":
            lines.append(f"❌ 실패 [{e.get('no')}] {e.get('reason') or e.get('error', '')}")
        elif name == "run_finished":
            if e.get("ok"):
                lines.append(f"🏁 종료 — 발행 {len(e.get('published') or [])}건"
                             + (f" · 건너뛴 행 {e.get('failed_rows')}"
                                if e.get("failed_rows") else ""))
            else:
                lines.append(f"🛑 종료(실패) — {str(e.get('error', ''))[:120]}")
    st.markdown("  \n".join(lines[-60:]))


store = get_store()

# ══════════════════════════════════════════════════════════════════
# 사이드바 — 브랜드만 (계정/실행 준비는 본문에서 고른다)
# ══════════════════════════════════════════════════════════════════
with st.sidebar:
    # ══ 최상단: 브랜드 ══ 고르면 기준시트 + UTM 빌더가 **한 세트로** 따라온다.
    st.header("브랜드")
    try:
        # ★strict — 설정을 못 읽으면 **다른 브랜드로 대체하지 않고** 여기서 멈춘다.
        brand_rows = brands.load_brands(strict=True)
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"브랜드 설정을 읽지 못했습니다.{chr(10)}{chr(10)}{exc}")
        st.stop()
    if not brand_rows:
        st.error("`brands.json` 에 브랜드가 없습니다.")
        st.stop()

    brand_labels = {b.id: (b.title if b.ready else f"{b.title} (준비 중)")
                    for b in brand_rows}
    brand_id = st.selectbox("브랜드", list(brand_labels), key="brand_id",
                            format_func=lambda i: brand_labels[i])
    brand = next(b for b in brand_rows if b.id == brand_id)
    if brand.ready:
        st.success(f"● 실행 가능 — {brand.title}")
    else:
        st.warning(f"○ 준비 중 — {brand.title}")
    st.markdown(info_block(("기준시트", brand.reference_title),
                           ("UTM 빌더", brand.utm_title)),
                unsafe_allow_html=True)

    st.caption("네이버 로그인 세션은 실행하는 PC 안에만 저장됩니다"
               "(큐로 나가지 않습니다).")

# ══════════════════════════════════════════════════════════════════
# 계정(기준 계정) — 화면에서는 본문의 `기준 계정` 으로 고른다.
#   ★내부 구조는 그대로다: accounts.json · sessions/<id>/ · --account.
#     계정마다 기준랜딩 탭(ref_tab)이 다르므로, 그 탭 이름을 그대로 선택지로 쓴다.
# ══════════════════════════════════════════════════════════════════
alive = live_agents(store)          # 실행 에이전트 현황(아래 여러 곳에서 쓴다)

# ══════════════════════════════════════════════════════════════════
# 본문
# ══════════════════════════════════════════════════════════════════
st.title("네이버 블로그 랜딩 생성기")
st.markdown(info_block(("브랜드", brand.title),
                       ("기준시트", brand.reference_title),
                       ("UTM 빌더", brand.utm_title)),
            unsafe_allow_html=True)

left, right = st.columns([3, 2], gap="large")

# ── 왼쪽: 작업 설정 ────────────────────────────────────────────────
with left:
    if not brand.ready:
        # ★준비 중인 브랜드 — 화면에는 보이되 **실행은 잠근다**.
        st.subheader(f"{brand.title} — 준비 중")
        st.warning(f"**{brand.title}** 은(는) 아직 실행할 수 없습니다."
                   + (f"{chr(10)}{chr(10)}사유: {brand.status_note}"
                      if brand.status_note else ""))
        st.markdown(
            "- 기준시트(매체 · 결핍 · 검수용/실전용 블로그랜딩 · 제품 링크) 작성\n"
            "- UTM 빌더에 서비스 계정 편집 권한 부여\n"
            f"- 끝나면 `brands.json` 의 `{brand.id}` 항목에서 "
            "`\"ready\": true` 로만 바꾸면 됩니다(코드 수정 없음)")
        st.caption(f"기준시트 ID: `{brand.reference_sheet_id}`")
        st.caption(f"UTM 빌더 ID: `{brand.utm_sheet_id}`")
        b1, b2 = st.columns(2)
        b1.button("Dry-run (브라우저 안 켬)", use_container_width=True, disabled=True,
                  key="dry_locked")
        b2.button("실전 실행", type="primary", use_container_width=True, disabled=True,
                  key="run_locked")
        st.caption("⛔ 준비 중인 브랜드라 실행 버튼이 잠겨 있습니다. "
                   "왼쪽 사이드바에서 리퓨어리를 선택하세요.")
    else:
        st.subheader("무엇을 만들까요")
        flow = st.radio("작업 종류", list(FLOWS),
                        format_func=lambda k: FLOWS[k]["label"], horizontal=True,
                        key="flow")
        is_prod = flow == "production"

        # ★참고 랜딩 종류(=시트의 어느 열을 읽을지)는 플로우와 **별개 축**이다.
        kind = st.radio("참고 랜딩 종류 (기준시트 컬럼)", KINDS, horizontal=True,
                        index=1 if is_prod else 0, key=f"kind_{flow}",
                        help="검수용 블로그랜딩 / 실전용 블로그랜딩 중 어느 참고글을 읽을지. "
                             "어떤 작업을 돌릴지와는 다른 선택입니다.")

        # ★`기준 계정` 선택지는 **브랜드 기준시트에서 자동으로** 읽는다.
        #   시트에 `<이름> 기준랜딩` 탭을 추가하면 코드 수정 없이 그대로 늘어난다.
        #   위젯은 왼쪽부터 매체 · 결핍 · 기준 계정 순서로 보이지만, 탭이 정해져야
        #   결핍 목록을 읽을 수 있으므로 **탭을 먼저 만든다**(자리 배치는 그대로).
        mcol, dcol, acol = st.columns([1, 2, 2])
        try:
            tabinfo = load_tabs(brand.id)
        except Exception as exc:                               # noqa: BLE001
            st.error(f"[{brand.title}] 기준시트의 탭 목록을 읽지 못했습니다."
                     f"{chr(10)}{chr(10)}{exc}")
            st.stop()
        tab_list = tabinfo.get("tabs") or []
        if not tab_list:
            st.warning(f"[{brand.title}] 기준시트에 `기준랜딩` 이 이름에 들어간 탭이 "
                       f"없습니다. `<계정이름> 기준랜딩` 탭을 만들면 여기에 자동으로 "
                       f"나옵니다.")
            st.stop()

        ref_tab = acol.selectbox("기준 계정", tab_list, key=f"ref_tab_{brand.id}",
                                 help="기준시트의 어느 기준랜딩 탭을 쓸지 고릅니다. "
                                      "목록은 시트에서 자동으로 읽어옵니다")
        # 이 탭을 쓰는 계정(=세션 폴더). 아직 없으면 [실행 준비] 때 만든다.
        account = accounts.ensure_for_tab(ref_tab, brand, create=False)

        try:
            cat = load_catalog(brand.id, account.id if account else "", ref_tab)
        except Exception as exc:                               # noqa: BLE001
            st.error(f"[{brand.title}] 기준시트를 읽지 못했습니다.{chr(10)}{chr(10)}{exc}")
            st.stop()
        if not (cat.get("media") or []):
            st.warning(f"[{brand.title}] `{cat.get('tab')}` 탭에 매체/결핍 행이 없습니다. "
                       f"기준시트를 먼저 채워 주세요.")
            st.stop()

        media_list = cat.get("media") or []
        media = mcol.selectbox("매체", media_list, key="media")
        defs = catalog.deficiencies(cat, media, kind) or catalog.deficiencies(cat, media)
        deficiency = dcol.selectbox("결핍 / 제품", defs, key=f"def_{media}_{kind}")
        cap = st.columns([3, 1])
        cap[0].caption(f"목록 출처: `{brand.reference_title}` / `{cat.get('tab')}` · "
                       f"{str(cat.get('cached_at'))[:16]}"
                       f"{' (캐시)' if cat.get('from_cache') else ''} — "
                       f"기준랜딩 탭·결핍이 늘면 기준시트만 채우면 됩니다")
        if cap[1].button("목록 새로고침", use_container_width=True,
                         help="이 브랜드 기준시트에서 기준랜딩 탭 목록과 "
                              "결핍/제품 목록을 다시 읽어옵니다"):
            load_tabs.clear()
            load_catalog.clear()
            load_tabs(brand.id, refresh=True)                  # 기준 계정(탭) 목록
            load_catalog(brand.id, account.id if account else "", ref_tab,
                         refresh=True)                          # 결핍/제품 목록
            st.rerun()

        # ★캠페인 접두사는 'UTM 빌더 시트의 행을 좁힐 때' 만 쓴다.
        #   · 실전용  — 항상 쓴다(대상 행을 고르는 기준)
        #   · 검수용  — 날짜를 넣어 시트에 기록할 때만 쓴다. 날짜가 없으면 기록 자체를
        #               하지 않으므로 입력칸을 숨긴다(쓰이지 않는 값이라 혼란만 준다).
        needs_campaign = is_prod or bool(st.session_state.get("date", today_tag()).strip())

        cols = st.columns(3 if needs_campaign else 2)
        count = cols[0].number_input(
            "처리 건수 (0=전부)" if is_prod else "생성 개수",
            min_value=0, max_value=200,
            value=0 if is_prod else 1, step=1, key=f"count_{flow}")
        date = cols[1].text_input("시트 날짜", value=today_tag(), key="date",
                                  help="UTM 빌더 시트에서 이 날짜 행을 대상으로 합니다")
        if needs_campaign:
            campaign = cols[2].text_input(
                "utm_campaign 접두사", value="", key=f"camp_{flow}",
                placeholder="예: g_i_b_o_l_0831",
                help="같은 날짜에 그룹이 여럿일 때 그 그룹만 고릅니다. "
                     "`g_i_b_o_l_0831` 을 넣으면 `_1` 부터 `_20`, `_100` 까지 "
                     "순번 자릿수와 관계없이 모두 매칭됩니다. 비우면 그 날짜 전체입니다.")
        else:
            campaign = ""
            st.caption("· 시트 날짜가 비어 있어 `utm_campaign 접두사` 는 쓰이지 않습니다"
                       "(입력칸 숨김).")

        if is_prod:
            p1, p2 = st.columns(2)
            prod_mode = p1.selectbox("실전용 방식", list(PROD_MODES),
                                     format_func=lambda k: f"{k} — {PROD_MODES[k]}",
                                     key="prod_mode")
            content_from = p2.selectbox(
                "제목/본문 출처", ("ref", "review"),
                format_func=lambda k: ("ref — 실전용 참고글" if k == "ref"
                                       else "review — 기존 검수용 글"),
                key="content_from")
        else:
            prod_mode, content_from = "convert", "ref"

        show_window = st.checkbox("브라우저 창 보기", value=False,
                                  help="끄면 창 없이(headless) 돕니다. "
                                       "로그인할 때만 창이 뜹니다")
        keep_going = st.checkbox("한 건 실패해도 나머지 계속 (실전용)", value=True,
                                 disabled=not is_prod,
                                 help="실패한 건은 발행하지 않고 시트에도 남기지 않습니다. "
                                      "끄면 예전처럼 그 배치를 통째로 멈춥니다")

        # ── Job 조립 ─────────────────────────────────────────────
        #   ★고급 CLI 옵션(--ref-tab · --batch · --url · --product-url ·
        #     --sheet-product · --start · --copy-mode · --ref-copy-from · --no-sheet)은
        #     **화면에 두지 않는다.** 값을 넣지 않으면 각 CLI 의 기존 기본값이 그대로
        #     쓰인다(옵션 자체는 CLI 에 그대로 살아 있다 — 필요하면 터미널에서 준다).
        #       · --ref-tab       비움 → 계정(같은 브랜드) → 브랜드 기본 탭
        #       · --sheet-product 안 줌 → 결핍 앞 단어로 자동 대조 (run.py `_sheet_product`)
        #       · --batch/--start/--copy-mode/--ref-copy-from → CLI 기본값
        def build_job(dry: bool) -> Job:
            job = Job(flow=flow, brand=brand.id,
                      account=account.id if account else "",
                      ref_tab=ref_tab,          # ★고른 기준랜딩 탭을 그대로 넘긴다
                      media=media, deficiency=deficiency, kind=kind,
                      count=int(count), publish=not dry, dry_run=dry,
                      headless=(False if show_window else None), events=True)
            if is_prod:
                job.date = date.strip()
                job.campaign = campaign.strip()
                job.mode = prod_mode
                job.content_from = content_from
                job.on_error = "skip" if keep_going else "abort"
            else:
                job.count = max(1, int(count))
                if date.strip():
                    job.sheet_media = catalog.sheet_media_for(media)
                    job.sheet_date = date.strip()
                    job.sheet_campaign = campaign.strip()
                # job.sheet_product 는 None 그대로 = --sheet-product 를 주지 않는다
                #   → run.py 가 결핍 앞 단어로 자동 대조한다(기존 동작).
            return job

        preview = build_job(dry=False)
        problems = preview.validate()
        if problems:
            st.warning(" · ".join(problems))

        # ── 실행 전 확인 (읽기 전용 · 브라우저 안 켬) ──────────────
        with st.expander("실행 전 확인", expanded=True):
            st.write({
                "브랜드": brand.title,
                "기준 계정": ref_tab,
                "작업 종류": FLOWS[flow]["label"]
                          + (f" · {prod_mode}" if is_prod else ""),
                "참고 랜딩 종류": kind,
                "매체": media,
                "결핍": deficiency,
                "날짜": date.strip() or "(없음)",
                "캠페인": campaign.strip() or "(전체)",
                "건수": int(count) if int(count) else "전부",
                "기준시트": f"{brand.reference_title} / {cat.get('tab')}",
                "네이버 계정(세션)": (account.title if account
                                 else "(아직 없음 — 실행 준비 때 만듭니다)"),
                "UTM 빌더": brand.utm_title,
            })
            if st.button("시트 조회해서 매칭 확인 (읽기 전용)",
                         use_container_width=True):
                try:
                    info = preview_rows(brand.id, flow, media, deficiency, kind,
                                        date.strip(), campaign.strip(), prod_mode)
                except Exception as exc:                       # noqa: BLE001
                    st.error(f"{exc}")
                else:
                    ref = info.get("reference") or {}
                    st.success(f"기준시트 {ref.get('row')}행 — 참고글 {ref.get('url')}")
                    if ref.get("product_url"):
                        st.caption(f"기준시트 제품 링크: {ref['product_url']}")
                    rows = info.get("rows") or []
                    if rows:
                        st.caption(f"매칭된 행 {len(rows)}개 · "
                                   f"최종 제품 URL 은 행마다 다릅니다")
                        st.dataframe(rows, use_container_width=True, hide_index=True)
                    elif flow == "production":
                        st.warning("매칭된 행이 없습니다.")
                    else:
                        st.caption("검수용은 기록할 자리 확인을 Dry-run 에서 합니다.")
                    with st.popover("시트 조회 로그"):
                        st.code(chr(10).join(info.get("log") or []) or "(없음)")

        # ── 실행 준비 (로그인 + 실행 준비를 버튼 하나로) ──────────
        #   ★기존 기능을 그대로 재사용한다: `v2.session --login` 을 큐에 넣고
        #     그 PC 의 에이전트가 로그인 창을 연다. 로그인은 사람이 직접 한다.
        info = session_store.describe(account) if account else {
            "state_exists": False, "cookies": 0, "saved_at": "", "profile": ""}
        logged_in = bool(info["state_exists"])
        job_state = session_job_state(store, account) if account else ""
        ready = bool(alive) and logged_in

        st.divider()
        if ready:
            st.success(f"● 실행 준비 완료 — {ref_tab}")
        elif job_state in RUNNING_STATES:
            st.info("▶ 로그인 창이 열렸습니다 — 네이버 로그인을 직접 마쳐 주세요."
                    f"{chr(10)}로그인이 끝나면 세션이 저장되고 자동으로 준비 완료가 됩니다.")
        else:
            st.warning("○ 준비 필요 — 아래 [실행 준비] 를 눌러 주세요")

        if st.button("실행 준비", type="primary", use_container_width=True,
                     help="네이버 로그인 창을 엽니다. 직접 로그인하면 세션이 저장되고 "
                          "실행 준비가 끝납니다"):
            started = ensure_agent(store)        # ★이미 돌고 있으면 띄우지 않는다
            # ★이 기준랜딩 탭에 계정(세션 폴더)이 아직 없으면 여기서 만든다.
            #   기존 계정이 있으면 그대로 쓴다(세션 재사용).
            account = accounts.ensure_for_tab(ref_tab, brand, create=True)
            jid = submit_session_job(store, brand, account, "--login")
            st.session_state["job_id"] = jid
            st.toast("실행 준비를 시작했습니다 — 로그인 창을 확인해 주세요.")
            st.rerun()

        st.caption(("· 로그인 세션: 있음 "
                    f"(쿠키 {info['cookies']}개 · {info['saved_at'][:16]})"
                    if logged_in else "· 로그인 세션: 없음")
                   + f"{chr(10)}· 네이버 로그인은 매번 직접 하셔야 합니다.")

        if not ready:
            st.info("**실행 준비** 를 먼저 끝내 주세요 — "
                    "로그인 세션과 실행 준비가 완료되면 실행 버튼이 활성화됩니다.")

        b1, b2 = st.columns(2)
        if b1.button("Dry-run (브라우저 안 켬)", use_container_width=True,
                     disabled=bool(problems) or not ready):
            job = build_job(dry=True)
            st.session_state["job_id"] = store.submit(
                job, title=f"[dry][{brand.title}] {FLOWS[flow]['label']} · "
                           f"{media}/{deficiency}")
            st.rerun()

        # ★확인 체크박스는 두지 않는다(2026-08-31 사용자 요청).
        #   오발행 방지는 **실행 준비 완료 상태에서만 버튼이 열리는 것**으로 갈음한다.
        if b2.button("실전 실행", type="primary", use_container_width=True,
                     disabled=bool(problems) or not ready):
            job = build_job(dry=False)
            title = (f"[{brand.title}] {FLOWS[flow]['label']} · "
                     f"{media}/{deficiency}")
            if int(count) > 0:
                # ★10개씩 **순차** 배치로 쪼갠다. 첫 배치만 지금 큐에 올라가고,
                #   다음 배치는 사용자가 다시 로그인해야 이어진다.
                plan = batch_plan.create(
                    job.to_dict(), int(count), title=title, brand=brand.id,
                    account=account.id if account else "", flow=flow,
                    mode=prod_mode)
                st.session_state["plan_id"] = plan["id"]
                st.session_state.pop("job_id", None)
            else:
                # 건수 0(=전부)은 총 건수를 알 수 없어 나누지 않는다(기존 동작).
                st.session_state["job_id"] = store.submit(job, title=title)
            st.rerun()
        st.caption(f"버튼을 누르면 **큐에 넣기만** 합니다. 실제 실행은 에이전트가 합니다."
                   f"{chr(10)}건수를 넣으면 **{batch_plan.BATCH_SIZE}개씩 순차로** 나눠 "
                   f"실행하고, 배치가 바뀔 때마다 다시 로그인합니다"
                   f"(0=전부 는 나누지 않습니다).")

        # ══ 배치 진행 ═══════════════════════════════════════════
        plan = batch_plan.get(st.session_state.get("plan_id") or "")
        if plan is None:            # 새로고침 등으로 잃어버렸으면 진행 중인 것을 되찾는다
            open_plans = [q for q in batch_plan.list_plans(8) if batch_plan.is_open(q)]
            plan = open_plans[0] if open_plans else None
            if plan:
                st.session_state["plan_id"] = plan["id"]
        if plan:
            plan = advance_plan(store, plan)
            cur = batch_plan.current(plan)
            sizes = plan.get("sizes") or []
            done_n = batch_plan.done_count(plan)

            st.divider()
            st.markdown(f"#### 배치 진행 — {batch_plan.summary(plan)}")
            st.progress(min(1.0, done_n / max(1, int(plan.get("total") or 1))),
                        text=f"완료 {done_n}/{plan.get('total')}건")
            mark = {batch_plan.DONE: "✅", batch_plan.RUNNING: "▶",
                    batch_plan.LOGGING_IN: "🔑", batch_plan.LOGIN_WAIT: "⏸",
                    batch_plan.PENDING: "·", batch_plan.FAILED: "❌",
                    batch_plan.CANCELED: "⏹"}
            st.markdown("  \n".join(
                f"{mark.get(b['status'], '·')} 배치 {b['no']} — {b['size']}건"
                + (f" · 발행 {b['published']}건" if b.get("published") else "")
                + (f" · {b['error'][:80]}" if b.get("error") else "")
                for b in plan.get("batches") or []))

            # 지금 도는 작업을 아래 진행 패널에 연결한다(자동 새로고침도 여기서 걸린다)
            if cur and cur.get("status") == batch_plan.RUNNING and cur.get("run_job"):
                st.session_state["job_id"] = cur["run_job"]
            elif cur and cur.get("status") == batch_plan.LOGGING_IN \
                    and cur.get("login_job"):
                st.session_state["job_id"] = cur["login_job"]

            # ── 건별 결과 · 실패 목록 ──────────────────────────
            res = plan_results(store, plan)
            if res["items"]:
                st.caption(f"총 {plan.get('total')}건 / 성공 {res['ok']} / "
                           f"실패 {res['fail']}")
                with st.expander(f"건별 결과 ({len(res['items'])}건)",
                                 expanded=bool(res["fail"])):
                    st.markdown("  \n".join(
                        (f"✅ 순번 {x['순번']} — 완료" if x["ok"] else
                         f"❌ 순번 {x['순번']} — 실패: {x['사유']}"
                         + (f" ({x.get('구분')} 오류)" if x.get("구분") else ""))
                        + (f"  · {x['행']}행" if x["행"] else "")
                        for x in res["items"][-60:]))
            if res["fails"]:
                st.error(f"실패 {res['fail']}건 — 아래 목록을 확인하세요")
                st.dataframe(
                    [{"순번": x["순번"], "utm_campaign": x["utm_campaign"],
                      "시트 행": x["행"], "실패 단계": x["단계"],
                      "실패 사유": x["사유"], "구분": x.get("구분", ""),
                      "블로그 URL": x["블로그 URL"], "오류": x.get("오류", "")}
                     for x in res["fails"]],
                    use_container_width=True, hide_index=True)

            if plan.get("status") == batch_plan.DONE:
                if res["fail"]:
                    st.warning(f"🏁 모든 배치 종료 — 총 {plan.get('total')}건 / "
                               f"성공 {res['ok']} / 실패 {res['fail']}")
                else:
                    st.success(f"🏁 모든 배치 완료 — 총 {plan.get('total')}건 "
                               f"(성공 {res['ok']})")
                cc = st.columns(2)
                if res["failed_rows"] and cc[0].button(
                        f"실패 {res['fail']}건만 재실행", type="primary",
                        use_container_width=True,
                        help="실패한 시트 행만 다시 큐에 넣습니다. "
                             "이미 성공한 건은 절대 다시 실행하지 않습니다"):
                    retry_job = dict(plan.get("job") or {})
                    if plan.get("flow") == "production":
                        # ★행 번호를 콕 집어 넘긴다(--rows) — 성공한 행은 포함되지 않는다
                        retry_job["rows"] = ",".join(res["failed_rows"])
                        retry_job["start"] = 1
                    new_plan = batch_plan.create(
                        retry_job, res["fail"],
                        title=f"{plan.get('title')} · 실패 {res['fail']}건 재실행",
                        brand=plan.get("brand", ""), account=plan.get("account", ""),
                        flow=plan.get("flow", "review"),
                        mode=plan.get("mode", "convert"))
                    st.session_state["plan_id"] = new_plan["id"]
                    st.rerun()
                if cc[1].button("배치 진행 닫기", use_container_width=True):
                    st.session_state.pop("plan_id", None)
                    st.rerun()
            elif plan.get("status") == batch_plan.FAILED:
                st.error("배치가 실패해 멈췄습니다. 원인을 확인한 뒤 다시 실행해 주세요.")
                if st.button("배치 진행 닫기", use_container_width=True):
                    batch_plan.cancel(plan)
                    st.session_state.pop("plan_id", None)
                    st.rerun()
            elif cur and cur.get("status") == batch_plan.RECOVER_WAIT:
                # ★공통 오류(로그인 만료 · 브라우저 종료 · 시트 접근 불가)
                st.error(f"⛔ 재로그인/복구 필요 — 배치 {cur['no']} 가 공통 오류로 "
                         f"멈췄습니다.{chr(10)}{chr(10)}{cur.get('error', '')}")
                st.caption("이미 발행된 건은 건너뛰고, 남은 건부터 이어서 실행합니다.")
                rc = st.columns(2)
                if rc[0].button("재로그인하고 이어서 실행", type="primary",
                                use_container_width=True):
                    batch_plan.prepare_retry(plan)      # 발행된 만큼 건너뛴다
                    acc = accounts.ensure_for_tab(ref_tab, brand, create=True)
                    start_batch_login(store, plan, brand, acc)
                    st.rerun()
                if rc[1].button("여기서 중단", use_container_width=True):
                    batch_plan.cancel(plan)
                    st.session_state.pop("plan_id", None)
                    st.rerun()
            elif cur and cur.get("status") == batch_plan.LOGIN_WAIT:
                st.warning(f"⏸ 다음 배치 로그인 필요 — 배치 {cur['no']}"
                           f"({cur['size']}건)를 시작하려면 네이버에 다시 "
                           f"로그인해 주세요.")
                if st.button("로그인하고 이어서 실행", type="primary",
                             use_container_width=True):
                    acc = accounts.ensure_for_tab(ref_tab, brand, create=True)
                    start_batch_login(store, plan, brand, acc)
                    st.rerun()
            elif cur and cur.get("status") == batch_plan.LOGGING_IN:
                st.info("🔑 로그인 창에서 로그인해 주세요 — 끝나면 이 배치가 "
                        "자동으로 이어서 실행됩니다.")
            elif cur and cur.get("status") == batch_plan.RUNNING:
                st.info(f"▶ 배치 {cur['no']} 실행 중 — {cur['size']}건")

            if batch_plan.is_open(plan) and st.button("배치 전체 중단",
                                                      use_container_width=True):
                for key in ("run_job", "login_job"):
                    if cur and cur.get(key):
                        store.request_cancel(cur[key])
                batch_plan.cancel(plan)
                st.session_state.pop("plan_id", None)
                st.rerun()

# ── 오른쪽: 최근 실행 기록 ────────────────────────────────────────
with right:
    st.subheader("최근 실행 기록")
    jobs = store.list_jobs(limit=20)
    if not jobs:
        st.caption("아직 실행한 작업이 없습니다.")
    else:
        st.dataframe([job_summary(r) for r in jobs], use_container_width=True,
                     hide_index=True, height=340)
        pick = {f"{BADGE.get(r.get('status'), '')} {r.get('title')}  "
                f"[{r['id'][9:15]}]": r["id"] for r in jobs}
        chosen = st.selectbox("자세히 볼 작업", list(pick), key="pick_job")
        if st.button("이 작업 열기", use_container_width=True):
            st.session_state["job_id"] = pick[chosen]
            st.rerun()

# ══════════════════════════════════════════════════════════════════
# 아래 — 진행 상황 / 단계 / 로그 / 발행 URL
# ══════════════════════════════════════════════════════════════════
st.divider()
job_id = st.session_state.get("job_id")
rec = store.get(job_id) if job_id else None

if rec is None:
    st.info("작업을 실행하면 여기에 진행 상황과 발행 URL 이 나옵니다.")
else:
    status = rec.get("status")
    head = st.columns([3, 1, 1, 1, 1])
    head[0].markdown(f"### {BADGE.get(status, status)} — {rec.get('title')}")
    total = int(rec.get("total") or 0)
    made = int(rec.get("made") or 0)
    published = list(rec.get("published") or [])
    head[1].metric("전체", total or "—")
    head[2].metric("작성", made)
    head[3].metric("발행", len(published))
    if status in RUNNING_STATES:
        if head[4].button("중단", type="secondary", use_container_width=True):
            store.request_cancel(rec["id"])
            st.toast("중단을 요청했습니다.")
            time.sleep(1.0)
            st.rerun()
    else:
        head[4].caption(f"코드 {rec.get('exit_code')}")

    st.caption(f"브랜드 {brand_name(rec.get('brand')) or '(없음)'} · "
               f"계정 {(rec.get('job') or {}).get('account') or '(기본)'} · "
               f"에이전트 {rec.get('agent') or '—'} · {rec.get('created_at', '')[:19]}")

    if total:
        st.progress(min(1.0, (made + len(published)) / max(1, total * 2)),
                    text=f"작성 {made}/{total} · 발행 {len(published)}/{total}")
    elif status == queue_store.RUNNING:
        st.progress(0.0, text="시작하는 중…")

    if rec.get("error"):
        st.error(rec["error"])
    if status == queue_store.PENDING and not alive:
        st.warning("대기 중입니다 — 에이전트가 떠 있지 않으면 실행되지 않습니다"
                   "(왼쪽에서 시작하세요).")

    events, _ = store.read_events(rec["id"])
    tab_step, tab_log, tab_url, tab_ev = st.tabs(
        ["진행 단계", "진행 로그", f"발행 URL ({len(published)})", "원본 이벤트"])
    with tab_step:
        render_steps(events)
    with tab_log:
        text, _ = store.read_log(rec["id"])
        st.code(text[-12000:] or "(아직 출력이 없습니다)", language="log")
    with tab_url:
        if published:
            st.code(chr(10).join(published), language="text")
            for url in published:
                st.markdown(f"- [{url}]({url})")
        else:
            st.caption("아직 발행된 URL 이 없습니다.")
    with tab_ev:
        st.json(events[-40:] if events else [], expanded=False)

    auto = st.checkbox("자동 새로고침 (실행 중일 때)", value=True, key="auto_refresh")
    if auto and status in RUNNING_STATES:
        time.sleep(1.5)
        st.rerun()
