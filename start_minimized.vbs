' =====================================================================
'  YouTube AutoPoster - minimiert im Hintergrund starten
'
'  Doppelklick auf diese Datei startet start.bat minimiert. Das Fenster
'  bleibt in der Taskleiste sichtbar, das Dashboard laeuft im Browser.
'
'  Beenden: das minimierte Fenster oeffnen und STRG+C druecken
'           (oder Task-Manager -> python.exe beenden).
' =====================================================================
Option Explicit

Dim shell, fso, scriptDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
command = """" & scriptDir & "\start.bat"""

' 7 = Fenster minimiert, False = nicht auf das Ende warten
shell.Run command, 7, False

Set shell = Nothing
Set fso = Nothing
