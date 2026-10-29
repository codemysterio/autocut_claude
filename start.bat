@echo off
rem Starts AutoCut and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
".venv\Scripts\python.exe" run_server.py
