' Sharing mode: no local tool window. Open config\access.txt's LAN link on a second device.
Option Explicit
Dim shell, fso, here, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here
cmd = "pythonw.exe """ & here & "\app.py"" --share-safe --silent"
shell.Run cmd, 0, False
