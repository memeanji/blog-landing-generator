"""--edit-copy 실행 경로 추적 (브라우저 없이 어느 분기를 타는지 확인).

목적: '로그만 성공처럼 찍히던' 문제의 재발 방지.
      _edit_copy 플래그가 실제 실행 함수에서 세팅되는지, 그래서
      기준글 수정 진입(_open_source_editor)이 호출되는지, 옛 구간복사 경로가
      돌지 않는지를 호출 순서로 확인한다.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.browser import BrowserAutomation as B  # noqa: E402


def build(edit_copy: bool):
    calls: list[str] = []
    bb = B.__new__(B)
    bb.log = lambda m="", _c=calls: _c.append(f"LOG {m}")
    bb.user_data_dir = Path(".")
    bb.enabled = True

    async def stop(*a, **k):
        calls.append("→ 브라우저 시작 시도(테스트 중단)")
        raise RuntimeError("__STOP__")

    # 브라우저 진입 직전에서 멈춘다 — 그 전까지의 플래그 세팅만 검증
    import app.services.browser as M
    class FakePW:
        async def __aenter__(self):
            await stop(); return self
        async def __aexit__(self, *a):
            return False
    M.async_playwright = lambda: FakePW()
    return bb, calls


async def main() -> int:
    ok_all = True
    for edit_copy in (True, False):
        bb, calls = build(edit_copy)
        try:
            await B._paste_from_landing(
                bb, landing_url="https://blog.naver.com/x/1", title="t",
                wait_for_continue=lambda *a: None, section_selectors=[],
                bulk=1, edit_copy=edit_copy, mobile_preview=True,
                publish=False, capture_align=False)
        except Exception:      # 브라우저 실행 직전에서 어떤 이유로 멈추든 무방
            pass                # (플래그 세팅은 그보다 앞에서 끝난다)
        got_flag = getattr(bb, "_edit_copy", None)
        got_mob = getattr(bb, "_use_mobile_preview", None)
        ok = (got_flag is edit_copy) and (got_mob is (True if edit_copy else False))
        ok_all &= ok
        print(f"{'✅' if ok else '❌'} edit_copy={edit_copy} → _edit_copy={got_flag} "
              f"_use_mobile_preview={got_mob}")
        for c in calls:
            if "[모드]" in c:
                print(f"   {c}")
    print()
    # 호출부가 살아 있는지(정적)
    src = Path("app/services/browser.py").read_text(encoding="utf-8")
    checks = {
        "_open_source_editor 호출": "self._open_source_editor(context, landing_url)" in src,
        "_switch_preview_mobile 호출": "await self._switch_preview_mobile(editor)" in src,
        "구간분석 생략 분기": "랜딩 구간 분석 생략" in src,
        "옛 루프 가드": "_legacy_n = 0 if getattr(self, \"_edit_copy\", False) else bulk" in src,
    }
    for k, v in checks.items():
        print(f"{'✅' if v else '❌'} {k}")
        ok_all &= v
    print()
    print("전체 통과 ✅" if ok_all else "실패 있음 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
