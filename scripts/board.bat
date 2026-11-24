@echo off
REM Opens the JobPilot board in your browser. Keep this window open while you use it.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat 2>nul
python -m jobpilot board
pause
