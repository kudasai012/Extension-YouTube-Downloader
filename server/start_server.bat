@echo off
chcp 65001 >nul
title YouTube Downloader Server
echo ============================================
echo   YouTube Downloader - local server
echo ============================================

cd /d "%~dp0"

REM Check ffmpeg (needed to merge video and audio)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [!] ffmpeg not found in PATH.
    echo     Download it from https://www.gyan.dev/ffmpeg/builds/ and add to PATH,
    echo     otherwise high-quality downloads cannot merge video and audio.
    echo.
)

REM Install dependencies if needed
python -m pip install --quiet --disable-pip-version-check -r requirements.txt

REM Start server
python server.py

pause
