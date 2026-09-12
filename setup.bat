@echo off
REM =====================================================================
REM  YouTube AutoPoster - EINRICHTUNG (einmalig ausfuehren)
REM  * virtuelle Umgebung anlegen
REM  * Python-Abhaengigkeiten installieren
REM  * Ordnerstruktur anlegen
REM  * SQLite-Datenbank initialisieren
REM  * config.json erzeugen
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title YouTube AutoPoster - Setup

echo.
echo ============================================================
echo   YouTube AutoPoster - Einrichtung
echo ============================================================
echo.

REM --- Python suchen -------------------------------------------------
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo [FEHLER] Python wurde nicht gefunden.
    echo.
    echo Bitte Python 3.11 oder neuer installieren:
    echo    https://www.python.org/downloads/windows/
    echo Bei der Installation unbedingt "Add python.exe to PATH" aktivieren.
    echo.
    pause
    exit /b 1
)

echo [1/6] Python gefunden:
%PYTHON_CMD% --version
echo.

REM --- virtuelle Umgebung -------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [2/6] Virtuelle Umgebung wird angelegt (.venv) ...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo [FEHLER] venv konnte nicht erstellt werden.
        echo Ggf. fehlt das Paket "venv": %PYTHON_CMD% -m ensurepip --upgrade
        pause
        exit /b 1
    )
) else (
    echo [2/6] Virtuelle Umgebung ist bereits vorhanden.
)
echo.

call ".venv\Scripts\activate.bat"

REM --- Abhaengigkeiten ----------------------------------------------
echo [3/6] pip wird aktualisiert ...
python -m pip install --upgrade pip wheel
echo.
echo [4/6] Abhaengigkeiten werden installiert (requirements.txt) ...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [FEHLER] Installation der Abhaengigkeiten fehlgeschlagen.
    pause
    exit /b 1
)
echo.
echo        Optionale Test-Pakete (requirements-dev.txt) ...
python -m pip install -r requirements-dev.txt
echo.

REM --- Ordner + Datenbank -------------------------------------------
echo [5/6] Ordner, Datenbank und config.json werden angelegt ...
python app.py init
if errorlevel 1 (
    echo [FEHLER] Initialisierung fehlgeschlagen.
    pause
    exit /b 1
)
echo.

REM --- System-Check --------------------------------------------------
echo [6/6] System-Check (Dry-Run, es wird nichts hochgeladen) ...
python app.py --dry-run
echo.

echo ============================================================
echo   Einrichtung abgeschlossen.
echo.
echo   NAECHSTE SCHRITTE:
echo   1. Google Cloud Projekt anlegen + YouTube Data API v3 aktivieren
echo   2. OAuth-Client vom Typ "Desktop-App" erstellen
echo   3. JSON herunterladen und hier ablegen:
echo        %CD%\credentials\credentials.json
echo   4. start.bat ausfuehren
echo   5. Im Browser auf "Google-/YouTube-Konto einmalig verbinden" klicken
echo   6. Fertige Videos in den Ordner "folders\READY" legen
echo.
echo   Ausfuehrliche Anleitung: README.md  (bzw. http://127.0.0.1:8765/setup)
echo ============================================================
echo.
pause
endlocal
