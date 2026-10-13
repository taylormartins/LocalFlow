' Double-click to restart LocalFlow (and Ollama, if it is down) with no console
' window. Also what the "LocalFlow resume" scheduled task runs on wake from sleep.
' The real work is in restart.ps1.

Option Explicit
Dim fso, shell, root
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)

' 0 = hidden window, False = do not wait for it to exit.
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & root & "\restart.ps1""", 0, False
