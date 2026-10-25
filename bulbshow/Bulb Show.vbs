' Bulb Show launcher: starts the app with no console window.
' Shows keep looping after the window is closed; the app quits by itself once
' the window is gone and nothing is running. Messages go to bulbshow.log here.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(here)
py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox "The Python environment is missing. In " & root & " run:" & vbCrLf & vbCrLf & _
         "    python -m venv .venv" & vbCrLf & _
         "    .venv\Scripts\python -m pip install -r requirements.txt", 48, "Bulb Show"
  WScript.Quit 1
End If
sh.CurrentDirectory = root
sh.Run """" & py & """ -m bulbshow", 0, False
