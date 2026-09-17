@echo off
rem Launch Light Show Studio (opens your browser at http://127.0.0.1:8765).
rem Double-click this file, or run it from a terminal. Close the window to stop the server.
title Light Show Studio
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The Python environment is missing. From this folder run:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m lightshow studio %*
if errorlevel 1 pause
