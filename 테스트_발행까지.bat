@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==========================================================
echo   ★ 발행까지 진행합니다 (되돌릴 수 없습니다)
echo   붙여넣기 결과를 먼저 확인하고 싶으면
echo   테스트_붙여넣기만.bat 을 쓰세요.
echo ==========================================================
echo.
set /p CNT=몇 건 발행할까요? (숫자 입력, 기본 1):
if "%CNT%"=="" set CNT=1
echo.
set PYTHONIOENCODING=utf-8
rem 시트 3행 '팔자 / 머니(연습)' — 매체 칸이 비어 있어 --media 로는 조회되지 않는다.
.venv\Scripts\python.exe main.py --paste --edit-copy --publish --url "https://blog.naver.com/<blog_id>/<logNo>" --bulk %CNT%
echo.
echo ==========================================================
echo   종료되었습니다. 발행 주소는 시트 K열에 기록됩니다.
echo ==========================================================
pause
