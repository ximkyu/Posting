@echo off
REM =====================================================================
REM  YouTube AutoPoster - Tests ausfuehren
REM  Die Tests nutzen Mocks: Es wird NIEMALS ein echtes Video hochgeladen
REM  oder veroeffentlicht.
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [FEHLER] Bitte zuerst setup.bat ausfuehren.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

python -m pytest tests -v %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Alle Tests erfolgreich.) else (echo Es gab fehlgeschlagene Tests - bitte Ausgabe pruefen.)
pause
endlocal & exit /b %RC%
