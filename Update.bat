@echo off
rem Update Whisper Dictation to the latest version and restart it.
rem Double-click this file. Your settings and lectures are kept.
setlocal
cd /d "%~dp0"
title Updating Whisper Dictation

echo.
echo [1/4] Stopping Whisper Dictation...
rem Stops only the python process running this folder's app.py
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains('%~dp0app.py') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/4] Downloading the latest version...
where git >nul 2>&1
if errorlevel 1 (
    echo     Git is not installed. Install it from https://git-scm.com/download/win and run this again.
    goto :fail
)
git fetch origin || goto :fail
git checkout master || goto :fail
git pull --ff-only --autostash origin master || goto :fail

echo [3/4] Installing dependencies...
if not exist venv\Scripts\python.exe (
    echo     Creating virtual environment...
    python -m venv venv || goto :fail
)
venv\Scripts\python.exe -m pip install --quiet --upgrade pip
venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :fail

echo [4/4] Starting Whisper Dictation...
start "" wscript.exe "%~dp0WhisperDictation.vbs"

echo.
echo Done! Right-click the microphone icon in the system tray to find "Start lecture...".
timeout /t 8
exit /b 0

:fail
echo.
echo Update failed - see the message above.
pause
exit /b 1
