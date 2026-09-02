r"""브랜드 분리 회귀 테스트 — 브라우저 없이 UI→큐→에이전트 전 구간을 확인한다.

    .\.venv\Scripts\python.exe scripts\test_streamlit_brands.py

`streamlit.testing.v1.AppTest` 로 **진짜 스크립트 런**을 돌린다(위젯·버튼 클릭 포함).
Playwright 는 켜지 않는다 — 큐에 들어간 Job 의 argv 까지만 본다.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from streamlit.testing.v1 import AppTest                       # noqa: E402

from v2 import accounts, brands, catalog, landing_sheet, sheets  # noqa: E402
from v2.job import Job                                          # noqa: E402
from v2.queue_store import LocalStore                           # noqa: E402

OK, NG = "  ✅", "  ❌"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        fails.append(name)


# ══════════════════════════════════════════════════════════════════
print("1) 브랜드 설정 계층")
rows = brands.load_brands()
ids = [b.id for b in rows]
check("brands.json 로드", "repurely" in ids and "doctor_nuscent" in ids, str(ids))

# ★시트 ID 를 테스트 코드에 적지 않는다(공개 저장소에 남기지 않기 위해).
#   로컬 `brands.json` 의 값이 그대로 실리는지를 본다.
CONF = {}
conf_path = ROOT / "brands.json"
if conf_path.exists():
    raw = json.loads(conf_path.read_text(encoding="utf-8"))
    CONF = {b["id"]: b for b in (raw.get("brands") or []) if b.get("id")}

rep = brands.resolve("")                    # 기본 = 리퓨어리(기존 동작)
check("기본 브랜드 = 리퓨어리", rep.id == "repurely", rep.title)
if CONF.get("repurely"):
    check("리퓨어리 기준시트 ID = brands.json 값",
          rep.reference_sheet_id == CONF["repurely"]["reference_sheet_id"]
          and bool(rep.reference_sheet_id))
    check("리퓨어리 UTM 빌더 ID = brands.json 값",
          rep.utm_sheet_id == CONF["repurely"]["utm_sheet_id"]
          and bool(rep.utm_sheet_id))
else:
    print("  · brands.json 이 없어 시트 ID 대조는 건너뜁니다")
check("리퓨어리 UTM 탭 규칙 보존",
      rep.utm_tab("카카오모먼트") == "카카오모먼트 블로그 랜딩 UTM 빌더")

doc = brands.resolve("닥터누센트")
if CONF.get("doctor_nuscent"):
    check("닥터누센트 시트 ID = brands.json 값",
          doc.reference_sheet_id == CONF["doctor_nuscent"]["reference_sheet_id"]
          and doc.utm_sheet_id == CONF["doctor_nuscent"]["utm_sheet_id"])
check("이름으로도 찾힌다(label)", brands.resolve("리퓨어리").id == "repurely")
try:
    brands.resolve("없는브랜드")
    check("없는 브랜드는 오류", False)
except RuntimeError as exc:
    check("없는 브랜드는 오류", "설정에 없는 브랜드" in str(exc))

# ══════════════════════════════════════════════════════════════════
print()
print("2) 브랜드 전환 — 시트가 한 세트로 바뀐다 (혼용 방지)")
sheets.set_brand("repurely"); landing_sheet.set_brand("repurely")
r_ref, r_utm, r_tab = sheets.active_sheet_id(), landing_sheet.active_sheet_id(), sheets.active_tab()
sheets.set_brand("doctor_nuscent"); landing_sheet.set_brand("doctor_nuscent")
d_ref, d_utm, d_tab = sheets.active_sheet_id(), landing_sheet.active_sheet_id(), sheets.active_tab()
check("기준시트가 브랜드마다 다르다", r_ref != d_ref, f"{r_ref[:12]}… / {d_ref[:12]}…")
check("UTM 빌더가 브랜드마다 다르다", r_utm != d_utm)
# ★탭 '이름' 은 브랜드끼리 같을 수 있다(둘 다 `스마일 현미 기준랜딩`).
#   중요한 것은 **시트+탭이 한 세트로** 바뀌는 것이다.
check("탭도 브랜드 설정을 따른다",
      bool(r_tab) and bool(d_tab) and (r_ref, r_tab) != (d_ref, d_tab),
      f"{r_ref[:8]}…/{r_tab!r} · {d_ref[:8]}…/{d_tab!r}")
sheets.set_brand("repurely"); landing_sheet.set_brand("repurely")
check("되돌리면 리퓨어리 그대로", sheets.active_sheet_id() == r_ref
      and sheets.active_tab() == r_tab)

rep_tabs = catalog.load_tabs("repurely", refresh=True)["tabs"]
doc_tabs = catalog.load_tabs("doctor_nuscent", refresh=True)["tabs"]
check("기준랜딩 탭 목록이 브랜드별로 분리된다",
      # ★탭 이름은 브랜드끼리 같을 수 있다(둘 다 `스마일 현미 기준랜딩` 등).
      #   중요한 것은 **각자 자기 시트에서 읽어 온다** 는 것이다.
      bool(rep_tabs) and bool(doc_tabs)
      and all(t.endswith("기준랜딩") for t in rep_tabs + doc_tabs),
      f"리퓨어리 {rep_tabs} / 닥터누센트 {doc_tabs}")
check("탭 캐시도 브랜드별로 나뉜다",
      catalog.tabs_cache_path("repurely") != catalog.tabs_cache_path("doctor_nuscent"))
# ★계정 id 를 테스트 코드에 적지 않는다(공개 저장소에 남기지 않기 위해).
#   accounts.json 에 등록된 탭이라면 그 계정이 그대로 재사용돼야 한다.
known = {(a.tab_for_brand("repurely") or ""): a.id
         for a in accounts.load_accounts(include_disabled=True)
         if a.tab_for_brand("repurely")}
for tab in rep_tabs:
    if tab not in known:
        continue
    hit = accounts.find_by_tab(tab, "repurely")
    check(f"`{tab}` → 기존 계정 재사용(세션 유지)",
          hit is not None and hit.id == known[tab], str(hit.id if hit else None))
check("새 탭은 계정이 아직 없다(실행 준비 때 생성)",
      accounts.find_by_tab("새계정 기준랜딩", "repurely") is None)
check("새 탭 id 는 항상 같은 값(세션 폴더가 흔들리지 않는다)",
      accounts.tab_slug("새계정 기준랜딩", "repurely")
      == accounts.tab_slug("새계정 기준랜딩", "repurely"))
check("같은 탭 이름이라도 브랜드가 다르면 id 가 다르다",
      accounts.tab_slug("새계정 기준랜딩", "repurely")
      != accounts.tab_slug("새계정 기준랜딩", "doctor_nuscent"))

acc = next((a for a in accounts.load_accounts(include_disabled=True)
            if a.ref_tab), None)
if acc:
    check("계정 탭은 같은 브랜드에서만",
          acc.tab_for_brand("repurely") == acc.ref_tab
          and acc.tab_for_brand("doctor_nuscent") == "")
check("카탈로그 캐시가 브랜드별로 분리",
      catalog.cache_path("t", "repurely") != catalog.cache_path("t", "doctor_nuscent"))

# ══════════════════════════════════════════════════════════════════
check("준비된 브랜드는 준비 상태로 나온다", rep.ready and doc.ready,
      f"repurely={rep.ready} doctor_nuscent={doc.ready}")
# ★'준비 안 됨' 검증은 **가짜 브랜드**로 한다. 실제 브랜드는 열리고 닫히므로
#   거기에 기대면 시험이 흔들린다(닥터누센트가 열리자 7건이 한꺼번에 깨졌다).
import dataclasses as _dc                                       # noqa: E402

not_ready = _dc.replace(rep, id="_test_locked", label="시험용(준비 중)",
                        ready=False, status_note="시험용")
try:
    not_ready.require_ready()
    check("준비 안 된 브랜드는 CLI 에서도 막힌다", False)
except RuntimeError as exc:
    check("준비 안 된 브랜드는 CLI 에서도 막힌다", "준비 중" in str(exc))
rep.require_ready()
doc.require_ready()
try:
    brands.load_brands(path=str(ROOT / "없는파일.json"), strict=True)
    check("설정을 못 읽으면 fallback 하지 않는다", False)
except brands.BrandConfigError:
    check("설정을 못 읽으면 fallback 하지 않는다", True)
check("CLI(--brand 없음)는 기본값으로 계속 산다",
      brands.load_brands(path=str(ROOT / "없는파일.json"))[0].id == "repurely")

print()
print("3) Job → CLI argv")
old = Job(account="test_account", media="카모", deficiency="흑자 / 머니", count=5,
          publish=True, sheet_media="카카오모먼트", sheet_date="831")
check("브랜드 없는 기존 Job 은 --brand 가 안 붙는다", "--brand" not in old.to_argv(),
      old.command_line()[-90:])

j = Job(flow="production", brand="doctor_nuscent", account="test_account",
        media="카모", deficiency="흑자 / 머니", kind="실전용", date="831",
        on_error="skip", dry_run=True)
argv = j.to_argv()
check("--brand 가 붙는다", "--brand" in argv and argv[argv.index("--brand") + 1]
      == "doctor_nuscent")
check("--on-error skip 이 붙는다", "--on-error" in argv
      and argv[argv.index("--on-error") + 1] == "skip")
check("잘못된 브랜드는 validate 가 잡는다",
      bool(Job(brand="없음", media="카모", deficiency="x").validate()))
jc = Job(flow="production", brand="repurely", media="카모", deficiency="목 / 지연",
         kind="실전용", date="831", campaign="k_i_b_l_m_0831", mode="create",
         start=3, count=10).to_argv()
check("실전용 --campaign 이 전달된다",
      "--campaign" in jc and jc[jc.index("--campaign") + 1] == "k_i_b_l_m_0831")
check("실전용 --mode/--start 전달", "--mode" in jc and "--start" in jc)

# ══════════════════════════════════════════════════════════════════
print()
print("4) 큐 — Job 만 보고 브랜드를 알 수 있다")
with tempfile.TemporaryDirectory() as tmp:
    store = LocalStore(tmp)
    jid = store.submit(j, title="[닥터누센트] 테스트")
    rec = store.get(jid)
    check("레코드에 brand", rec.get("brand") == "doctor_nuscent")
    check("Job dict 에도 brand", (rec.get("job") or {}).get("brand") == "doctor_nuscent")
    claimed = store.claim("TEST-PC")
    check("에이전트가 집을 수 있다", claimed and claimed["id"] == jid)
    cmd = Job.from_dict(claimed["job"]).command()
    check("에이전트가 만들 명령에 브랜드가 실린다", "--brand" in cmd and
          "v2.run_production" in cmd, " ".join(cmd[-8:]))
    store.finish(jid, "done", 0)
    check("종료 처리", store.get(jid)["status"] == "done")

# ══════════════════════════════════════════════════════════════════
print()
print("5) Streamlit UI — 실제 스크립트 런")
def open_app(timeout: int = 120):
    """앱을 열고 팀 공용 비밀번호 게이트를 통과한다(값은 secrets 에서 읽는다)."""
    a = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=timeout)
    a.run()
    gate = [t for t in a.text_input if t.key == "gate_pw"]
    if gate:
        import tomllib

        f = ROOT / ".streamlit" / "secrets.toml"
        pw = ""
        if f.exists():
            conf = tomllib.loads(f.read_text(encoding="utf-8"))
            pw = conf.get("TEAM_PASSWORD") or conf.get("APP_PASSWORD") or ""
        gate[0].set_value(pw).run()
        [b for b in a.button if b.label == "들어가기"][0].click().run()
    return a


at = open_app()
check("예외 없이 렌더링", not at.exception, str(at.exception)[:200] if at.exception else "")
check("비밀번호 게이트 통과", any(s.key == "brand_id" for s in at.selectbox))

sel = {s.key: s for s in at.selectbox}
check("최상단 브랜드 선택이 있다", "brand_id" in sel, str(list(sel)))
if "brand_id" in sel:
    # AppTest 의 options 는 format_func 을 거친 표시명이다(값은 .value 로 본다)
    check("브랜드 목록에 두 브랜드가 보인다",
          list(sel["brand_id"].options) == ["리퓨어리", "닥터누센트"],
          str(sel["brand_id"].options))
    check("기본 선택 = 리퓨어리", sel["brand_id"].value == "repurely")
ref_key = next((k for k in sel if str(k).startswith("ref_tab_")), "")
check("계정 선택은 본문 `기준 계정` 하나로 옮겼다",
      bool(ref_key) and "account_id" not in sel, str(list(sel)))
# ★선택지는 코드에 박지 않고 **브랜드 기준시트에서 읽는다**
sheet_tabs = catalog.load_tabs("repurely")["tabs"]
check("기준 계정 선택지 = 기준시트에서 읽은 기준랜딩 탭",
      list(sel[ref_key].options) == sheet_tabs,
      f"{list(sel[ref_key].options)} vs 시트 {sheet_tabs}")
check("기준랜딩 탭만 걸러낸다(`복사본`·`랜딩` 제외)",
      all("기준랜딩" in t for t in sheet_tabs) and len(sheet_tabs) >= 2,
      str(sheet_tabs))
check("사이드바 계정 안내가 사라졌다",
      not any(("세션 폴더" in c.value) or ("내부 계정 키" in c.value)
              for c in at.caption))
check("작업 종류(플로우) 라디오", any(r.key == "flow" for r in at.radio))
# ── 실행 준비(로그인+에이전트)를 버튼 하나로 ─────────────────────
labels_btn = [b.label for b in at.button]
check("[실행 준비] 버튼이 있다", "실행 준비" in labels_btn, str(labels_btn))
check("실행 준비 버튼은 잠겨 있지 않다",
      any(b.label == "실행 준비" and not b.disabled for b in at.button))
for gone in ("세션 확인", "로그인 창 열기", "이 PC 에서 에이전트 시작"):
    check(f"옛 버튼 '{gone}' 이 화면에서 사라졌다", gone not in labels_btn)
# ★에이전트가 떠 있지 않은 지금은 '준비 전' 이라 실행 버튼이 잠겨 있어야 한다
#   (로그인 전에 블로그 작업이 시작되면 안 된다)
locked = {b.label: b.disabled for b in at.button
          if b.label in ("Dry-run (브라우저 안 켬)", "실전 실행")}
check("준비 전에는 Dry-run·실전 실행이 잠긴다",
      locked and all(locked.values()), str(locked))
check("확인 체크박스는 없앴다",
      not any(c.key == "confirm_publish" for c in at.checkbox),
      str([c.key for c in at.checkbox if c.key]))

camp_keys = [t.key for t in at.text_input if str(t.key).startswith("camp_")]
check("캠페인 접두사 칸이 보인다(날짜 있음)", bool(camp_keys), str(camp_keys))
camp_labels = [t.label for t in at.text_input if str(t.key).startswith("camp_")]
check("라벨이 'utm_campaign 접두사'", camp_labels == ["utm_campaign 접두사"],
      str(camp_labels))
at.text_input(key="date").set_value("").run()
check("날짜를 비우면 캠페인 칸이 숨는다",
      not [t for t in at.text_input if str(t.key).startswith("camp_")],
      str([t.key for t in at.text_input]))
at.text_input(key="date").set_value(__import__("datetime").datetime.now()
                                    .strftime("%-m%d") if False else "831").run()

media_before = list(sel["media"].options) if "media" in sel else []
check("리퓨어리 매체 목록이 찬다", bool(media_before), str(media_before))

# ★화면 단순화(2026-08-31) — CLI 명령 미리보기와 고급 옵션은 UI 에서 뺐다.
#   CLI 옵션 자체는 그대로 살아 있고(테스트 3번에서 확인), 화면에만 안 보인다.
code_blocks = [c.value for c in at.code]
check("CLI 명령 미리보기가 화면에 없다",
      not any("-m v2." in c for c in code_blocks),
      next((c[-90:] for c in code_blocks if "-m v2." in c), ""))
adv_keys = {"ref_tab", "url_direct", "product_direct", "sheet_product",
            "sheet_product_off", "copy_mode", "ref_copy_from", "no_sheet",
            "start", "batch_review", "batch_production"}
shown = ({t.key for t in at.text_input} | {c.key for c in at.checkbox}
         | {x.key for x in at.selectbox} | {n.key for n in at.number_input})
check("고급 옵션 위젯이 화면에 없다", not (adv_keys & shown),
      str(sorted(k for k in (adv_keys & shown) if k)))
basic = {"brand_id", ref_key, "media", "date", "camp_review"}
check("기본 항목은 그대로 있다", basic <= shown | {"camp_review"},
      str(sorted(k for k in shown if k)))
check("실전 실행은 준비 완료 전까지만 잠긴다(체크박스 없이)",
      any(b.label == "실전 실행" and b.disabled for b in at.button))

# 브랜드를 닥터누센트로 바꾼다 → 기준시트가 비어 있으므로 '깨끗한 안내'가 떠야 한다
at2 = open_app()
at2.selectbox(key="brand_id").set_value("doctor_nuscent").run()
check("브랜드를 바꿔도 앱이 죽지 않는다", not at2.exception,
      str(at2.exception)[:200] if at2.exception else "")
msgs = [w.value for w in at2.warning] + [e.value for e in at2.error]
oks = [x.value for x in at2.success]
check("닥터누센트가 실행 가능으로 나온다",
      any("실행 가능" in m and "닥터누센트" in m for m in oks),
      " / ".join(m[:70] for m in oks))
media_after = [s for s in at2.selectbox if s.key == "media"]
check("다른 브랜드 매체 목록이 새어 들어오지 않는다",
      not media_after or list(media_after[0].options) != media_before)
check("닥터누센트 기준시트를 정상으로 읽는다",
      not any("읽지 못했" in m for m in msgs),
      " / ".join(m[:70] for m in msgs))
tab_after = [x for x in at2.selectbox if str(x.key).startswith("ref_tab_")]
check("닥터누센트 기준랜딩 탭이 나온다",
      bool(tab_after) and all(str(o).endswith("기준랜딩")
                              for o in tab_after[0].options),
      str(list(tab_after[0].options)) if tab_after else "(없음)")

# ── 실전용으로 바꾸면 실전용 CLI 옵션이 붙는다 ─────────────────────
at3 = open_app()
at3.radio(key="flow").set_value("production").run()
check("실전용 전환 후에도 예외 없음", not at3.exception,
      str(at3.exception)[:200] if at3.exception else "")
prod_keys = {x.key for x in at3.selectbox}
check("실전용 방식(convert/create) 선택이 있다", "prod_mode" in prod_keys)
check("제목/본문 출처 선택이 있다", "content_from" in prod_keys)
check("실전용에도 고급 옵션은 안 보인다",
      not ({"start", "copy_mode", "ref_copy_from", "no_sheet"}
           & ({x.key for x in at3.selectbox} | {c.key for c in at3.checkbox}
              | {n.key for n in at3.number_input})))
check("실전용에서도 CLI 명령 미리보기 없음",
      not any("-m v2." in c.value for c in at3.code))
# CLI 옵션 자체가 살아 있는지는 Job 단계에서 확인한다(위 3번 참고)
ui_job = Job(flow="production", brand="repurely", account="test_account",
             media="gfa", deficiency="팔자 / 현미", kind="실전용", date="831",
             campaign="g_i_b_o_l_0831", mode="convert", content_from="ref",
             on_error="skip", publish=True, events=True)
ui_argv = ui_job.to_argv()
for opt in ("--brand", "--account", "--media", "--deficiency", "--ref-kind",
            "--date", "--campaign", "--mode", "--content-from", "--on-error"):
    check(f"화면 값이 만드는 argv 에 {opt}", opt in ui_argv)
check("고급 옵션은 argv 에 안 붙는다(각 CLI 기본값 사용)",
      not ({"--ref-tab", "--batch", "--url", "--product-url", "--sheet-product",
            "--start", "--copy-mode", "--ref-copy-from", "--no-sheet"}
           & set(ui_argv)), str(ui_argv))
rev_job = Job(flow="review", brand="repurely", account="test_account", media="gfa",
              deficiency="팔자 / 현미", kind="검수용", count=1, publish=True,
              events=True, sheet_media="GFA", sheet_date="831",
              sheet_campaign="g_i_b_o_l_0831")
check("검수용도 고급 옵션 없이 만들어진다",
      "--sheet-product" not in rev_job.to_argv()
      and "--sheet-campaign" in rev_job.to_argv())
summary = {}
for j in at3.json:
    try:
        summary = json.loads(j.value)
        break
    except Exception:                                          # noqa: BLE001
        continue
need = ("브랜드", "기준 계정", "작업 종류", "매체", "결핍", "날짜",
        "캠페인", "건수", "네이버 계정(세션)")
check("실행 전 확인에 필요한 항목이 다 있다",
      all(k in summary for k in need), str(list(summary)))

# ── 캠페인 접두사 매칭 ────────────────────────────────────────────
from v2.landing_sheet import _campaign_ok                       # noqa: E402

PRE = "g_i_b_o_l_0831"
check("접두사 1~20 전부 매칭(자릿수 무관)",
      all(_campaign_ok(f"{PRE}_{i}", PRE) for i in range(1, 21)),
      str([i for i in range(1, 21) if not _campaign_ok(f"{PRE}_{i}", PRE)]))
check("세 자리 순번도 매칭", _campaign_ok(f"{PRE}_100", PRE))
check("접두사 자체도 매칭", _campaign_ok(PRE, PRE))
check("숫자가 이어 붙은 다른 그룹은 제외", not _campaign_ok("g_i_b_o_l_08319", PRE))
check("다른 그룹은 제외", not _campaign_ok("k_i_b_l_m_0831_5", PRE))
check("순번까지 준 접두사(_1)는 _10 을 잡지 않는다",
      _campaign_ok(f"{PRE}_1", f"{PRE}_1")
      and not _campaign_ok(f"{PRE}_10", f"{PRE}_1"))
check("시트 값 공백은 무시", _campaign_ok(f" {PRE}_7 ", PRE))
check("접두사를 비우면 전체 통과", _campaign_ok("아무값", ""))

# 조회 3경로가 같은 규칙을 쓰는지(startswith 잔재가 없어야 한다)
src = (ROOT / "v2" / "landing_sheet.py").read_text(encoding="utf-8")
check("느슨한 startswith 매칭 잔재 없음(설명 주석 제외)",
      "not camp.startswith(campaign)" not in src)

print()
print("6) 안전장치 — 실패 건 격리 · 단계 로그")
import asyncio                                                  # noqa: E402

from v2 import run_production as RP                             # noqa: E402


class FakeLog:
    def __init__(self):
        self.lines: list[str] = []
        self.events: list[dict] = []

    def __call__(self, msg=""):
        self.lines.append(str(msg))

    def event(self, name, **fields):
        self.events.append({"event": name, **fields})


flog = FakeLog()
RP.stage(flog, doc, "utm_sheet_access", "failed", "permission_denied")
check("단계 로그 형식",
      any("brand=doctor_nuscent stage=utm_sheet_access status=failed "
          "reason=permission_denied" in ln for ln in flog.lines), flog.lines[-1])
check("단계 이벤트", flog.events[-1]["event"] == "stage"
      and flog.events[-1]["reason"] == "permission_denied")

flog2 = FakeLog()
reason = RP.stage_failed(flog2, doc, "utm_sheet_access",
                         RuntimeError("[시트] brand=x stage=utm_sheet_access "
                                      "status=failed reason=permission_denied"))
check("예외에서 reason 을 뽑아낸다", reason == "permission_denied", reason)

# 제품 링크가 비었거나 형식이 틀린 행만 떨어뜨린다
rows = [{"row": 3, "product_url": "https://repurely.com/surl/P/1?utm_source=k"},
        {"row": 4, "product_url": ""},
        {"row": 5, "product_url": "곰도리"}]
flog3 = FakeLog()
kept = landing_sheet.check_product_urls(rows, flog3, "drop")
check("제품링크 없음/형식오류 행만 제외", [r["row"] for r in kept] == [3],
      str([r["row"] for r in kept]))
check("제외한 행마다 실패 이벤트",
      [e.get("reason") for e in flog3.events]
      == ["product_url_missing", "product_url_invalid"])
try:
    landing_sheet.check_product_urls(rows, flog3)               # 기본은 예전처럼 예외
    check("기본은 예전처럼 전체 중단", False)
except RuntimeError:
    check("기본은 예전처럼 전체 중단", True)


class FakePage:
    def __init__(self, name):
        self.name, self.closed = name, False

    async def close(self):
        self.closed = True


class FakeHolder:
    def __init__(self, page):
        self.page = page


class FakeCtx:
    def __init__(self, pages):
        self.pages = pages


blank, keep1, src1, stray = (FakePage("blank"), FakePage("ready"),
                             FakePage("src"), FakePage("stray"))
ctx = FakeCtx([blank, keep1, src1, stray])
closed = asyncio.run(RP.close_stray_pages(ctx, [FakeHolder(keep1)],
                                          [FakeHolder(src1)], flog))
check("실패 탭만 닫는다", closed == 1 and stray.closed
      and not keep1.closed and not src1.closed and not blank.closed)

print()
if fails:
    print(f"실패 {len(fails)}건: {fails}")
    sys.exit(1)
print("전부 통과 ✅")
