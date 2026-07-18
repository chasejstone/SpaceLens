@echo off
setlocal
cd /d "%~dp0"
echo This optional builder uses PyInstaller. It is not required to run SpaceLens SOTA.
echo.
python -c "import tkinter; root = tkinter.Tcl(); print('Tk', tkinter.TkVersion, 'Tcl', root.eval('info patchlevel'))"
if errorlevel 1 (
    echo SpaceLens needs a Python install with working Tcl/Tk to build the executable.
    exit /b 1
)
python -m pip install -r requirements-build.txt
if errorlevel 1 exit /b %errorlevel%
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SpaceLens_SOTA SpaceLens_SOTA.py
if errorlevel 1 exit /b %errorlevel%
echo.
echo exe should be in the dist folder.
pause
