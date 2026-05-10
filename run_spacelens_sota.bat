@echo off
setlocal
cd /d "%~dp0"
python SpaceLens_SOTA.py %*
pause
