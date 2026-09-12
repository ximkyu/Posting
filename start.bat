@echo off
REM =====================================================================
REM  YouTube AutoPoster - START
REM  Startet Dashboard, Watch-Folder und Upload-Queue.
REM  Beenden mit STRG+C oder durch Schliessen dieses Fensters.
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title YouTube AutoPoster

if not exist ".venv\Scripts\python.exe" (
    echo [FEHLER] Die virtuelle Umgebung fehlt.
    echo Bitte zuerst setup.bat ausfuehren.
    echo.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

REM Alle an dieses Skript uebergebenen Argumente werden durchgereicht,
REM z. B.:  start.bat --dry-run   oder   start.bat --port 9000
python app.py %*

endlocal
