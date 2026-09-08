Set WshShell = CreateObject("WScript.Shell")
Dim fso: Set fso = CreateObject("Scripting.FileSystemObject")
Dim scriptDir: scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Muda para o diretório video_cutter_web e inicia o servidor Python sem janela
WshShell.CurrentDirectory = scriptDir & "\video_cutter_web"
WshShell.Run "pythonw.exe app.py", 0, False

' Aguarda 1.5s para o servidor subir e abre o navegador
WScript.Sleep 1500
WshShell.Run "http://localhost:5000"
