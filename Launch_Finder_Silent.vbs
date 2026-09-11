Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonScript = scriptDir & "\sha256_duplicate_finder.py"

' Try running with pythonw first (no console), fallback to python in hidden window (0)
cmd = "cmd /c pythonw """ & pythonScript & """ || python """ & pythonScript & """"
WshShell.Run cmd, 0, False
Set WshShell = Nothing
Set fso = Nothing
