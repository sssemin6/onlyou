' Run the mail search app with no console window.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here
shell.Run """" & here & "\start-windows.cmd""", 0, False
