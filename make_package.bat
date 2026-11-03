@echo off
rem Builds dist\odisha-tool.zip with only the files needed on another laptop.
rem Leaves out venv, Firefox profile, API key, logs, sessions and recordings.
setlocal
cd /d "%~dp0"

set "WORK=%TEMP%\odisha-tool-package"
set "STAGE=%WORK%\odisha-tool"
if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%STAGE%\extractors" || goto :fail

for %%F in (app.py automation.py registration.py reg_ui.py employment.py emp_ui.py ui_common.py docprep.py runlog.py record.py config.json requirements.txt run.bat record.bat INSTALL.txt) do (
    if not exist "%%F" (
        echo Missing file: %%F
        goto :fail
    )
    copy /y "%%F" "%STAGE%\" >nul || goto :fail
)
copy /y "extractors\*.py" "%STAGE%\extractors\" >nul || goto :fail
rem dropdown lists learned from the site (e.g. sub-castes), if any run has saved them
if exist "sub_castes.json" copy /y "sub_castes.json" "%STAGE%\" >nul

if not exist dist mkdir dist
if exist "dist\odisha-tool.zip" del /q "dist\odisha-tool.zip"
powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%' -DestinationPath 'dist\odisha-tool.zip'" || goto :fail
rmdir /s /q "%WORK%"

echo.
echo Package ready: %CD%\dist\odisha-tool.zip
echo Copy this zip to the other laptop and follow INSTALL.txt inside it.
explorer /select,"%CD%\dist\odisha-tool.zip"
pause
exit /b 0

:fail
echo.
echo Could not build the package. See the messages above.
pause
exit /b 1
