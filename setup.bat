@echo off
rem One-time setup: creates .venv and installs dependencies. Needs Python 3.10+ and ffmpeg on PATH.
cd /d "%~dp0"
python -m venv .venv || goto :err
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :err
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
echo.
echo Setup done. Start the app with start.bat
exit /b 0
:err
echo Setup failed.
exit /b 1
