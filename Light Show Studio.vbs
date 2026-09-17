' Light Show Studio launcher: starts the studio with no console window.
' The server runs in the background and quits by itself when you close the
' studio window. Messages go to studio.log in this folder.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
py = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox "The Python environment is missing. In this folder run:" & vbCrLf & vbCrLf & _
         "    python -m venv .venv" & vbCrLf & _
         "    .venv\Scripts\python -m pip install -r requirements.txt", 48, "Light Show Studio"
  WScript.Quit 1
End If
sh.CurrentDirectory = here
sh.Run """" & py & """ -m lightshow studio", 0, False
