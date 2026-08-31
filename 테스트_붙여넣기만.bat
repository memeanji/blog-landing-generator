@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==========================================================
echo   본문 복사 테스트 (발행 안 함)
echo   - 기준글 수정화면에서 Ctrl+A / Ctrl+C 로 복사
echo   - 새 글에 Ctrl+V 로 붙여넣기
echo   - 붙여넣은 탭을 열어 둔 채 멈춥니다. 눈으로 확인하세요.
echo ==========================================================
echo.
set PYTHONIOENCODING=utf-8
rem 시트 3행 '팔자 / 머니(연습)' — 매체 칸이 비어 있어 --media 로는 조회되지 않는다.
rem   그래서 해당 행의 검수용 랜딩 주소를 직접 지정한다(--url 은 시트 조회를 건너뛴다).
.venv\Scripts\python.exe main.py --paste --edit-copy --url "https://blog.naver.com/<blog_id>/<logNo>" --bulk 1
echo.
echo ==========================================================
echo   종료되었습니다. 이 창을 닫으셔도 됩니다.
echo ==========================================================
pause
