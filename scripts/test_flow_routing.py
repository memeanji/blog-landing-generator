# -*- coding: utf-8 -*-
"""작업 종류 × 참고 랜딩 → 내부 flow/mode 라우팅 검증.

브라우저·시트·발행 없이 **매핑 규칙만** 확인한다. 실행:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/test_flow_routing.py
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from v2.job import FLOWS, Job                              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "streamlit_app.py"

_ok = _fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✅ {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  ❌ {name}" + (f"  — {detail}" if detail else ""))


def literal_from_app(name: str):
    """streamlit_app.py 에서 이름이 `name` 인 리터럴 dict 를 그대로 읽는다.

    ★화면 스크립트를 import 하면 Streamlit 이 실행되므로, 소스를 파싱해서
      **실제 코드에 적힌 값**을 확인한다(테스트용 사본이 아니다).
    """
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} 을 streamlit_app.py 에서 찾지 못했습니다")


print("=" * 74)
print("작업 종류 × 참고 랜딩 → flow / mode 라우팅")
print("=" * 74)

TASKS = literal_from_app("TASKS")
ROUTES = literal_from_app("ROUTES")
print(f"화면 선택지: {list(TASKS.values())} × 검수용/실전용\n")

# ── 1~3. 지원 조합이 정확한 경로로 가는가 ───────────────────────────
EXPECT = {
    ("new", "검수용"): ("review", "v2.run", None),
    ("new", "실전용"): ("production", "v2.run_production", "create"),
    ("edit", "실전용"): ("production", "v2.run_production", "convert"),
}
for (task, kind), (want_flow, want_mod, want_mode) in EXPECT.items():
    got = ROUTES.get((task, kind))
    label = f"{TASKS[task]} + {kind}"
    if got is None:
        check(label, False, "라우팅 표에 없다")
        continue
    flow, mode = got
    module = FLOWS[flow]["module"]
    ok = flow == want_flow and module == want_mod
    if want_mode is not None:
        ok = ok and mode == want_mode
    check(label, ok,
          f"flow={flow} · module={module}"
          + (f" · mode={mode}" if want_mode else " (mode 미사용)"))

# ── 4. 지원하지 않는 조합은 표에 아예 없어야 한다 ───────────────────
check("기존 글 수정 + 검수용 = 라우팅 없음(차단)",
      ("edit", "검수용") not in ROUTES,
      "표에 없으면 화면이 st.error 로 막고 st.stop() 한다")
src = APP.read_text(encoding="utf-8")
check("차단 시 안내 문구가 있다", "지원하지 않는 조합입니다" in src)
check("차단 시 실행을 멈춘다", "st.stop()" in src)
check("임의 경로로 흘려보내지 않는다 — route None 이면 stop",
      "route = ROUTES.get((task, kind))" in src and "if route is None:" in src)

# ── 5. 내부 값 무변경 ───────────────────────────────────────────────
print()
check("FLOWS 키 review/production 유지", set(FLOWS) == {"review", "production"},
      str(set(FLOWS)))
check("review → v2.run", FLOWS["review"]["module"] == "v2.run")
check("production → v2.run_production",
      FLOWS["production"]["module"] == "v2.run_production")
check("화면 라벨 = 새 글 생성 / 기존 글 수정",
      FLOWS["review"]["label"] == "새 글 생성"
      and FLOWS["production"]["label"] == "기존 글 수정",
      f"{FLOWS['review']['label']} / {FLOWS['production']['label']}")

# ── 6. Job 이 실제로 그 모듈을 부르는가(실행 경로 회귀) ─────────────
print()
for (task, kind), (want_flow, want_mod, want_mode) in EXPECT.items():
    flow, mode = ROUTES[(task, kind)]
    job = Job(flow=flow, brand="repurely", media="구글애즈",
              deficiency="팔자 / 머니", kind=kind, count=1)
    if flow == "production":
        job.mode = mode
        job.date = "917"
    module = job.module()                      # ★메서드다(프로퍼티 아님)
    cmdline = job.command_line() if hasattr(job, "command_line") else ""
    check(f"Job({TASKS[task]}+{kind}) → module {module}",
          module == want_mod,
          cmdline[-88:] if cmdline else "")

# ── 7. 중복이던 '실전용 방식' 선택 UI 가 없어졌는가 ─────────────────
print()
check("`실전용 방식` selectbox 제거됨", '"실전용 방식"' not in src)
check("prod_mode 는 라우팅에서 결정", "flow, prod_mode = route" in src)
check("`제목/본문 출처` 는 그대로 남아 있음", '"제목/본문 출처"' in src)

print("\n" + "=" * 74)
print(f"결과: 성공 {_ok} · 실패 {_fail}")
print("=" * 74)
sys.exit(1 if _fail else 0)
