@echo off
rem Run DeskGuard from source. First run sets up .venv and installs libraries (needs Python 3.12+).
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\pythonw.exe (
    echo First run: setting up DeskGuard...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 ( echo Python 3.12 or newer is required: https://www.python.org/downloads/ & pause & exit /b 1 )
    .venv\Scripts\python.exe -m pip install --upgrade pip >nul
    .venv\Scripts\python.exe -m pip install -r requirements.txt || ( pause & exit /b 1 )
)

start "" .venv\Scripts\pythonw.exe main.py %*
endlocal
