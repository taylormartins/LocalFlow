' Launch the LocalFlow tray app with no console window.
'
' Runs the venv's pythonw.exe directly rather than "uv run", so it does not
' depend on uv being on PATH in a login shell. If dependencies change, run
' "uv sync" once from a terminal; this launcher does not need to know.
'
' To start at login: Win+R -> shell:startup -> put a shortcut to this file there,
' or run  uv run localflow install-startup

Option Explicit
Dim fso, shell, root, pythonw

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = root & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonw) Then
    MsgBox "LocalFlow: no virtualenv found at" & vbCrLf & pythonw & vbCrLf & vbCrLf & _
           "Run 'uv sync' in " & root & " first.", 16, "LocalFlow"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
' 0 = hidden window, False = do not wait for it to exit.
shell.Run """" & pythonw & """ -m localflow.cli run", 0, False
