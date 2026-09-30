@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 dashboard.py
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python dashboard.py
    if errorlevel 1 pause
    exit /b
)
echo Python was not found. Install Python 3.10 or newer with Tcl/Tk support,
echo then run this launcher again. No pip packages are required.
pause
