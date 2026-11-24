' Run the mail search app with no console window.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here
Set processEnv = shell.Environment("PROCESS")
processEnv("OUTLOOK_MAILSEARCH_NO_PAUSE") = "1"
shell.Run """" & here & "\start-windows.cmd""", 0, False
