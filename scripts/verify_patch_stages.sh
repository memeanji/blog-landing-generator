#!/usr/bin/env bash
# 단계별 패치 적용 검증 — HEAD 깨끗한 사본에 패치를 순서대로 얹어 가며
#   ① 패치가 적용되는가  ② 문법/임포트가 되는가  ③ 테스트가 통과하는가 를 본다.
#   ★9월 미커밋 변경은 **복사하지 않는다**(HEAD + 내 패치만).
#   ★운영 저장소를 건드리지 않는다. 임시 폴더만 쓴다.
#
#   사용:  bash scripts/verify_patch_stages.sh
set -u

SRC="/c/Users/894플러스/blog_landing_generator"
PY="$SRC/.venv/Scripts/python.exe"
STG="/tmp/stage_verify"
P="$SRC/patches"

# 패치에 **들어 있지 않은** 전체 파일(단계별 의존성)
STAGE2_FILES="v2/login_state.py v2/session.py v2/session_store.py v2/browser.py v2/pairing.py installer/BlogLandingAgent.iss"
STAGE3_FILES="v2/fastwait.py"
# 테스트 스크립트(배포 대상)
TESTS_S1="test_stage1_queue"
TESTS_S2="test_stage2_logout test_stage2_verify test_stage2b_nologinid test_session_identity test_hold_design"
TESTS_S3="test_stage3_fastwait"
TESTS_BASE="test_batch_plan test_flow_routing test_account_identity"

rm -rf "$STG"
git clone -q --no-hardlinks "$SRC" "$STG" 2>/dev/null
cd "$STG" || exit 1
# 운영 설정 파일은 테스트에 필요한 것만(키 없는 것만) 가져온다
for f in accounts.json brands.json accounts.example.json brands.example.json; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$STG/$f"
done
mkdir -p "$STG/out"
cp "$SRC"/out/tabs_*.json "$STG/out/" 2>/dev/null
cp "$SRC"/out/catalog_*.json "$STG/out/" 2>/dev/null
cp "$SRC"/out/v2_prod_*.log "$STG/out/" 2>/dev/null
BASE_COMMIT=$(git rev-parse HEAD)
echo "기준 커밋: $BASE_COMMIT  (9월 미커밋 변경 없음 = 깨끗한 HEAD)"

reset_tree() {
  cd "$STG" || exit 1
  git checkout -q -- . 2>/dev/null
  git clean -qfd -e out -e accounts.json -e brands.json 2>/dev/null
}

copy_files() {
  for f in $1; do
    mkdir -p "$STG/$(dirname "$f")"
    cp "$SRC/$f" "$STG/$f"
  done
}

copy_tests() {
  mkdir -p "$STG/scripts"
  for t in $1; do cp "$SRC/scripts/$t.py" "$STG/scripts/"; done
}

run_stage() {   # $1=이름  $2=패치목록  $3=추가파일  $4=테스트목록
  echo ""
  echo "=================================================================="
  echo " $1"
  echo "=================================================================="
  reset_tree
  for pat in $2; do
    printf "  패치 %-34s " "$pat"
    if git apply "$P/$pat.patch" 2>/tmp/apply.err; then echo "적용 OK"
    else echo "적용 실패"; cat /tmp/apply.err; return 1; fi
  done
  [ -n "$3" ] && copy_files "$3"
  copy_tests "$4"

  printf "  문법(compileall)                       "
  if "$PY" -m compileall -q v2 streamlit_app.py > /tmp/cmp.out 2>&1; then echo "OK"
  else echo "실패"; cat /tmp/cmp.out; fi

  printf "  임포트(v2 전체 + 화면 AST)             "
  if PYTHONPATH="$STG" "$PY" -c "
import ast, pathlib, importlib, sys
for m in ('queue_store','supabase_store','job','accounts','session_store',
          'session','browser','batch_plan','brands','pairing'):
    importlib.import_module('v2.' + m)
for extra in ('timeutil','login_state','fastwait'):
    p = pathlib.Path('v2/%s.py' % extra)
    if p.exists():
        importlib.import_module('v2.' + extra)
ast.parse(pathlib.Path('streamlit_app.py').read_text(encoding='utf-8'))
# ★화면이 쓰는 모듈이 실제로 있는지(단계 누락 탐지)
src = pathlib.Path('streamlit_app.py').read_text(encoding='utf-8')
need = [n for n in ('timeutil','login_state','pairing','fastwait')
        if ('import ' + n) in src or ('v2.' + n) in src or
           ('from v2 import' in src and n in src.split('from v2 import')[1][:200])]
missing = [n for n in need if not pathlib.Path('v2/%s.py' % n).exists()]
if missing:
    raise SystemExit('화면이 참조하는 모듈 없음: %s' % missing)
" 2>/tmp/imp.err; then echo "OK"
  else echo "실패"; sed -n '1,12p' /tmp/imp.err; fi

  for t in $4; do
    printf "  테스트 %-32s " "$t"
    out=$(cd "$STG" && PYTHONPATH="$STG" "$PY" "scripts/$t.py" 2>&1)
    pa=$(printf '%s' "$out" | grep -c "^  \[o\]\|^  ✅")
    fa=$(printf '%s' "$out" | grep -c "^  \[X\]\|^  ❌")
    if [ "$fa" -eq 0 ]; then echo "$pa 통과 / 0 실패"
    else echo "$pa 통과 / $fa 실패"; printf '%s' "$out" | grep "^  \[X\]\|^  ❌" | head -4; fi
  done
}

run_stage "1) Stage 1 단독" \
  "stage1_only" "" "$TESTS_S1 $TESTS_BASE"

run_stage "2) Stage 1 + Stage 2" \
  "stage1_only stage2_only" "$STAGE2_FILES" "$TESTS_S1 $TESTS_S2 $TESTS_BASE"

run_stage "3) Stage 1 + Stage 2 + 도움말 제거" \
  "stage1_only stage2_only help_removal_only" "$STAGE2_FILES" \
  "$TESTS_S1 $TESTS_S2 $TESTS_BASE"

run_stage "4) Stage 1 + Stage 2 + 도움말 제거 + Stage 3" \
  "stage1_only stage2_only help_removal_only stage3_fastwait_only" \
  "$STAGE2_FILES $STAGE3_FILES" "$TESTS_S1 $TESTS_S2 $TESTS_S3 $TESTS_BASE"

echo ""
echo "=================================================================="
echo " 9월 미커밋 변경 포함 여부 — 각 단계 트리에 그 파일들이 들어갔는지"
echo "=================================================================="
cd "$STG" || exit 1
for f in agent_tray.py v2/config.py v2/run.py v2/run_production.py v2/sheets.py \
         v2/source_view.py v2/landing_sheet.py v2/gauth.py v2/shared_cred.py; do
  if git diff --quiet -- "$f" 2>/dev/null; then st="HEAD 그대로"; else st="★변경됨"; fi
  printf "  %-28s %s\n" "$f" "$st"
done
echo ""
echo "임시 폴더: $STG  (확인 후 삭제하세요)"
