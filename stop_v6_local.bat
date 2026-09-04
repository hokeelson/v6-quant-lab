@echo off
cd /d "%~dp0"
echo stop>"%~dp0.v6_local_stop"
echo [V6] Stop request sent.
echo The main V6 window will close all workers safely.
timeout /t 2 /nobreak >nul
