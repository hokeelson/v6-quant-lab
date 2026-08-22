@echo off
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
start "V6 Auto Worker" /D "%~dp0" cmd /k ".venv\Scripts\python.exe live_worker.py"
python -m streamlit run dashboard.py
pause
