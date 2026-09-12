@echo off
REM =====================================================================
REM  YouTube AutoPoster - im Hintergrund (minimiert) starten
REM  Das Konsolenfenster bleibt minimiert in der Taskleiste, das Dashboard
REM  oeffnet sich im Browser: http://127.0.0.1:8765
REM
REM  Beenden: minimiertes Fenster oeffnen und STRG+C druecken
REM           (oder Task-Manager -> python.exe beenden)
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [FEHLER] Die virtuelle Umgebung fehlt.
    echo Bitte zuerst setup.bat ausfuehren.
    echo.
    pause
    exit /b 1
)

start "" wscript.exe "%~dp0start_minimized.vbs"

echo YouTube AutoPoster laeuft im Hintergrund.
echo Dashboard: http://127.0.0.1:8765
echo Beenden:   das minimierte Fenster oeffnen und STRG+C druecken.
echo.
timeout /t 5 >nul
endlocal
