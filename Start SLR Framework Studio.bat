@echo off
REM Start SLR Framework Studio from the source in this folder.
REM Double-click this file. Nothing to type.
REM
REM It uses the virtual environment in .venv, created the first time by
REM   py -3 -m venv .venv
REM   .venv\Scripts\python -m pip install -e ".[gui]"
REM If .venv is missing it says so rather than failing in a way that looks
REM like the application is broken.

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo   The virtual environment is not set up yet.
    echo.
    echo   Open a Command Prompt in this folder and run:
    echo       py -3 -m venv .venv
    echo       .venv\Scripts\python -m pip install -e ".[gui]"
    echo.
    echo   Then double-click this file again.
    echo.
    pause
    exit /b 1
)

REM pythonw, not python: no console window behind the application.
start "" ".venv\Scripts\pythonw.exe" -m slr_studio.app
