@echo off
rem ===========================================================================
rem  Builds DeskGuard into a Windows installer:
rem     dist\DeskGuard-Setup-<version>.exe   (give this to anyone - no Python needed)
rem     dist\DeskGuard\DeskGuard.exe         (the app itself, portable)
rem
rem  Needs Python 3.12+ and Inno Setup 6. If either is missing, this script
rem  installs it with winget (built into Windows 11).
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

rem ---- 1. Python ------------------------------------------------------------
set "PY="
rem (Run a real check: "python" on a fresh Windows 11 can be a Microsoft Store shortcut.)
py -3 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1 && set "PY=py -3"
if not defined PY ( python -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1 && set "PY=python" )
if not defined PY (
    echo Python not found - installing Python 3.12 with winget...
    winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto :fail_python
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

rem ---- 2. Virtual environment + dependencies -------------------------------
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    %PY% -m venv .venv || goto :fail
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip >nul
echo Installing dependencies...
"%VPY%" -m pip install -r requirements.txt -r requirements-dev.txt || goto :fail

rem ---- 2b. Face models (bundled; the app itself never downloads anything) --
"%VPY%" toolsetch_models.py || ( echo Could not get the face models. & goto :fail )

rem ---- 3. Tests ---------------------------------------------------------------
echo.
echo Running tests...
set QT_QPA_PLATFORM=offscreen
"%VPY%" -m pytest -q || ( echo Tests failed - not building. & goto :fail )
set QT_QPA_PLATFORM=

rem ---- 4. Build the app (PyInstaller) ----------------------------------------
echo.
echo Building DeskGuard.exe...
"%VPY%" -m PyInstaller --noconfirm --clean deskguard.spec || goto :fail

rem ---- 5. Build the installer (Inno Setup) -----------------------------------
for /f "usebackq delims=" %%v in (`"%VPY%" -c "import deskguard; print(deskguard.__version__)"`) do set "VER=%%v"
call :find_iscc
if not defined ISCC (
    echo Inno Setup not found - installing it with winget...
    winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
    call :find_iscc
)
if not defined ISCC (
    echo.
    echo Could not find Inno Setup. Install it from https://jrsoftware.org/isdl.php and run build.bat again.
    echo The app itself is ready: dist\DeskGuard\DeskGuard.exe
    goto :end
)
echo.
echo Building installer...
"%ISCC%" /Q /DAppVersion=%VER% installer\deskguard.iss || goto :fail

echo.
echo ===========================================================================
echo  Done!
echo    Installer : dist\DeskGuard-Setup-%VER%.exe
echo    App       : dist\DeskGuard\DeskGuard.exe
echo ===========================================================================
start "" explorer.exe "%CD%\dist"
goto :end

:find_iscc
set "ISCC="
for %%p in (
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do if exist %%p set "ISCC=%%~p"
if not defined ISCC ( for /f "delims=" %%i in ('where iscc 2^>nul') do set "ISCC=%%i" )
exit /b 0

:fail_python
echo.
echo Could not install Python automatically. Install Python 3.12+ from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run build.bat again.
goto :end

:fail
echo.
echo BUILD FAILED - see the messages above.

:end
echo.
pause
endlocal
