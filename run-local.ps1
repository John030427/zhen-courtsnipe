param([int]$Port = 8766, [switch]$Console)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) { throw 'Create .venv and install requirements.txt first.' }
# The mitm capture posts tokens back to this port; keep it in step with -Port.
$env:SUSTECH_APP_PORT = "$Port"
if (-not $Console) {
    $PythonWindowless = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $PythonWindowless)) { throw 'Windowless Python is missing. Repair .venv first.' }
    Start-Process -FilePath $PythonWindowless -ArgumentList @(('"' + (Join-Path $ProjectRoot 'launcher.py') + '"')) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
    Write-Host "Workbench starts in the background: http://127.0.0.1:$Port"
    Write-Host 'You may close this terminal. Use the workbench Exit service action to stop it.'
    return
}
if (-not (Test-Path -LiteralPath 'config.toml')) { Copy-Item -LiteralPath 'config.example.toml' -Destination 'config.toml' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is occupied. Choose -Port with a free port." }
Write-Host "http://127.0.0.1:$Port"
# Development mode: the server lives in this window. Everyday use goes through start-workbench.cmd.
Write-Host 'Console development mode: closing this window stops the server.'
Write-Host 'For everyday use, open start-workbench.vbs or run without -Console.'
& $PythonExe -m uvicorn app:app --host 127.0.0.1 --port $Port
