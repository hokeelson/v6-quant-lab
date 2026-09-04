@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
  echo [V6] Creating Python virtual environment...
  py -m venv .venv
  if errorlevel 1 goto :error
)

call .venv\Scripts\activate
if errorlevel 1 goto :error

echo [V6] Checking Python dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

if not exist .env copy .env.example .env >nul

set "V6_SINGLE_CRYPTO_ACCOUNT=1"
set "V6_ENABLE_CRYPTO_V2_SHADOW=0"
set "V6_ENABLE_TRIAL_LEDGER=0"
set "V6_LOCAL_MODE=1"
set "V6_DATA_DIR=%~dp0data_crypto_lite"
set "V6_RUNTIME_DATA_DIR=%~dp0data_crypto_lite"
set "V6_PERSISTENT_DATA_DIR=%~dp0data_crypto_lite"
set "V6_STORAGE_DEGRADED=0"

if not exist "%V6_DATA_DIR%" mkdir "%V6_DATA_DIR%"
if exist "%~dp0.v6_local_stop" del /q "%~dp0.v6_local_stop"

echo.
echo ============================================
echo V6 Crypto Lite - LOCAL 24/7 MODE
echo Data: %V6_DATA_DIR%
echo Dashboard: http://127.0.0.1:8501
echo Broker orders: DISABLED
echo ============================================
echo.
echo Keep this window open while V6 is running.
echo To stop safely, double-click stop_v6_local.bat.
echo.

.venv\Scripts\python.exe local_crypto_lite.py
goto :eof

:error
echo.
echo [V6] Startup failed. See the error above.
pause
exit /b 1
