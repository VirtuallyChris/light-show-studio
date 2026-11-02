' Bulb Studio launcher: starts the app with no console window.
' Closing the window stops every show, puts the bulbs back as they were and
' quits, like Light Show Studio. Messages go to bulbshow.log here.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(here)
py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox "The Python environment is missing. In " & root & " run:" & vbCrLf & vbCrLf & _
         "    python -m venv .venv" & vbCrLf & _
         "    .venv\Scripts\python -m pip install -r requirements.txt", 48, "Bulb Studio"
  WScript.Quit 1
End If
sh.CurrentDirectory = root
sh.Run """" & py & """ -m bulbshow", 0, False
