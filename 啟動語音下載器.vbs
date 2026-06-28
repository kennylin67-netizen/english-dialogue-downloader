Dim sh, base, pyexe, script
Set sh = CreateObject("WScript.Shell")
base = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
pyexe = base & "..\python_embed\pythonw.exe"
script = base & "launcher.pyw"
sh.Run Chr(34) & pyexe & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
