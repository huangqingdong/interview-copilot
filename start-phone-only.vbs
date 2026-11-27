' Maximum stealth: zero windows on this computer.
' The service runs in the background with LAN access; open the URL from
' config\access.txt on a phone or a second device.
' Stop it with stop-hidden.vbs or the "Exit service" button in the UI.
Option Explicit
Dim shell, fso, here, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here
cmd = "pythonw.exe """ & here & "\app.py"" --share-safe --silent"
shell.Run cmd, 0, False
