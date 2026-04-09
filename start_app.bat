@echo off
setlocal
cd /d %~dp0

:: Launch the browser in a new window/tab immediately
start "" "http://127.0.0.1:8000/"

if exist venv\Scripts\activate.bat (
    start "Steam NLP Scraper" cmd /k "call venv\Scripts\activate.bat && python run.py"
) else (
    start "Steam NLP Scraper" cmd /k "python run.py"
)

endlocal
