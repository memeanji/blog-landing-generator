@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo.
echo  [1/2] 로컬 에이전트를 띄웁니다 (이 창은 최소화해 두세요)
start "블로그 랜딩 에이전트" /min ".venv\Scripts\python.exe" -m v2.agent
echo  [2/2] Streamlit UI 를 엽니다...
echo.
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py
pause
