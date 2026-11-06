@echo off
REM ---------------------------------------------------------------------
REM  Build SLR Framework Studio into a Windows executable.
REM
REM  Double-click this file, or run it from a command prompt. It makes its
REM  own virtual environment, so it will not disturb any Python you already
REM  use for other work.
REM
REM  When it finishes, the application is at:
REM      dist\SLRFrameworkStudio\SLRFrameworkStudio.exe
REM ---------------------------------------------------------------------

setlocal
cd /d "%~dp0\.."

echo.
echo ===============================================================
echo   SLR Framework Studio - Windows build
echo ===============================================================
echo.

REM --- 1. Find a Python we can use ------------------------------------
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"

if not defined PY (
    echo   Python was not found on this computer.
    echo.
    echo   Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo   During installation, tick "Add python.exe to PATH".
    echo   Then run this file again.
    echo.
    pause
    exit /b 1
)

echo   Using: %PY%
%PY% --version
echo.

REM --- 2. A virtual environment of its own ----------------------------
if not exist ".venv\Scripts\python.exe" (
    echo   Creating a virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo   Could not create the virtual environment.
        pause
        exit /b 1
    )
)
set "VPY=.venv\Scripts\python.exe"

REM --- 3. Dependencies -------------------------------------------------
echo   Installing dependencies. The first run downloads about 200 MB
echo   and takes a few minutes.
echo.
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -e ".[gui]" --quiet
if errorlevel 1 (
    echo.
    echo   Installing the dependencies failed. The usual cause is no
    echo   internet connection or a proxy blocking pypi.org.
    pause
    exit /b 1
)
"%VPY%" -m pip install pyinstaller --quiet

REM --- 4. Check it runs before packaging it ----------------------------
echo   Checking the application starts ...
"%VPY%" -m slr_studio.cli --help >nul 2>&1
if errorlevel 1 (
    echo   The application does not start from source, so packaging it
    echo   would only package the fault. Run this to see why:
    echo       .venv\Scripts\python.exe -m slr_studio.cli --help
    pause
    exit /b 1
)

REM --- 5. Build ---------------------------------------------------------
echo   Building. This takes two to five minutes.
echo.
"%VPY%" -m PyInstaller packaging\slr_studio.spec --noconfirm
if errorlevel 1 (
    echo.
    echo   The build failed. The output above says why.
    pause
    exit /b 1
)

REM --- 6. Prove the built executable actually runs ----------------------
REM  Checked with slr.exe, not SLRFrameworkStudio.exe: the windowed one has
REM  no console on Windows, so its output goes nowhere and this check would
REM  print a blank line and call it a success.
echo.
echo   Checking the built executable ...
"dist\SLRFrameworkStudio\slr.exe" --version
if errorlevel 1 goto :built_but_broken
echo   Building a demonstration review with it ...
"dist\SLRFrameworkStudio\slr.exe" demo "%TEMP%\slr-build-check" --stage screen >nul
if errorlevel 1 goto :built_but_broken
rmdir /s /q "%TEMP%\slr-build-check" 2>nul
goto :built_ok

:built_but_broken
echo.
echo   The executable was built but does not work. Send the messages above
echo   to whoever is maintaining this.
pause
exit /b 1

:built_ok

echo.
echo ===============================================================
echo   Done.
echo.
echo   The application is at:
echo       %CD%\dist\SLRFrameworkStudio\SLRFrameworkStudio.exe
echo.
echo   Double-click it to start. The whole folder is the program --
echo   move the folder, not just the .exe.
echo.
echo   Beside it is slr.exe, the same program with a console, for running
echo   the pipeline from the command line.
echo.
echo   To try it with no API key, start it and choose
echo       Tools  -^>  Build demonstration project...
echo   and point it at a new empty folder.
echo ===============================================================
echo.
pause
