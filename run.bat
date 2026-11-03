@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv venv || python -m venv venv || goto :fail
)

rem Reinstall packages whenever requirements.txt changes.
fc /b requirements.txt "venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing Python packages...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
    copy /y requirements.txt "venv\requirements.installed" >nul
)

if not exist "venv\.firefox_installed" (
    echo Installing Playwright Firefox ^(first run only^)...
    "venv\Scripts\python.exe" -m playwright install firefox || goto :fail
    echo.> "venv\.firefox_installed"
)

"venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Something went wrong. See the messages above.
pause
exit /b 1
