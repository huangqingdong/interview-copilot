' Stop the background service started by start-hidden.vbs / start-phone-only.vbs.
' Only matches this project's app.py started as a silent background service.
Option Explicit
Dim wmi, list, p, line, n, fso, scriptPath
Set fso = CreateObject("Scripting.FileSystemObject")
scriptPath = """" & fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "app.py") & """"
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set list = wmi.ExecQuery("Select * From Win32_Process Where Name = 'pythonw.exe'")
n = 0
For Each p In list
  If Not IsNull(p.CommandLine) Then
    line = p.CommandLine
    If InStr(1, line, scriptPath, vbTextCompare) > 0 And InStr(line, "--silent") > 0 Then
      p.Terminate()
      n = n + 1
    End If
  End If
Next
If n = 0 Then
  WScript.Echo "No hidden background service is running."
End If
