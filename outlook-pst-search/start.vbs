' Run the mail search app with no console window.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here
shell.Run """" & here & "\start-windows.cmd"" --embedding-model bge-m3 --ollama-port 11434", 0, False
