@echo off
REM Run JobPilot once. Used by Windows Task Scheduler. Logs to data\output\run.log
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat 2>nul
python -m jobpilot run %* >> data\output\run.log 2>&1
