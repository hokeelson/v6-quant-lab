@echo off
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env

rem Local V8 auto stack. Core worker is supervised; auxiliary workers mirror cloud behavior.
start "V6 V8 Worker" /D "%~dp0" cmd /k ".venv\Scripts\python.exe worker_supervisor_v8.py"
start "V6 Realtime" /D "%~dp0" cmd /k ".venv\Scripts\python.exe realtime_supervisor.py"
start "V6 TCA" /D "%~dp0" cmd /k ".venv\Scripts\python.exe tca_supervisor.py"
start "V6 Trial Ledger" /D "%~dp0" cmd /k ".venv\Scripts\python.exe trial_ledger_worker.py"
start "V6 Crypto V2 Shadow" /D "%~dp0" cmd /k ".venv\Scripts\python.exe crypto_v2_shadow_worker.py"
python -m streamlit run dashboard_v8.py
pause