@echo off
rem Records one manual run in Firefox into the recording\ folder (actions + page snapshots).
cd /d "%~dp0"
"venv\Scripts\python.exe" record.py
pause
