Option Explicit
Dim shell, files, root, python, entry, quote
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
entry = files.BuildPath(root, "launcher.py")
If Not files.FileExists(python) Or Not files.FileExists(entry) Then
    MsgBox "The workbench environment is missing. Set up .venv first.", 16, "Zhen CourtSnipe"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
quote = Chr(34)
shell.Run quote & python & quote & " " & quote & entry & quote, 0, False
