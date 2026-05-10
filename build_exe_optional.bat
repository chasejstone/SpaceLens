@echo off
setlocal
cd /d "%~dp0"
echo This optional builder uses PyInstaller. It is not required to run SpaceLens SOTA.
echo.
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name SpaceLens_SOTA SpaceLens_SOTA.py
echo.
echo exe should be in the dist folder.
pause
