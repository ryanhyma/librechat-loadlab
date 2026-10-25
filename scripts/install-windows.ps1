# Install loadlab on Windows Server (native service, no containers).
# Usage:
#   .\install-windows.ps1 -InstallDir C:\loadlab -PythonExe C:\Python312\python.exe
#   .\install-windows.ps1 -InstallDir C:\loadlab -PythonExe C:\Python312\python.exe -NssmExe C:\tools\nssm.exe

param(
  [string]$InstallDir = "C:\loadlab",
  [string]$PythonExe = "python",
  [string]$NssmExe = "",
  [string]$ServiceName = "loadlab",
  [int]$Port = 8090,
  [string]$DataDir = "$env:ProgramData\loadlab"
)

$ErrorActionPreference = "Stop"
Write-Host "== loadlab install => $InstallDir"

New-Item -ItemType Directory -Force -Path $InstallDir, $DataDir | Out-Null
Copy-Item -Recurse -Force "$PSScriptRoot\..\loadlab" "$InstallDir\loadlab"
Copy-Item -Force "$PSScriptRoot\..\requirements.txt" "$InstallDir\requirements.txt"

if (-not (Test-Path "$InstallDir\.venv")) {
  & $PythonExe -m venv "$InstallDir\.venv"
}
$py = "$InstallDir\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r "$InstallDir\requirements.txt"

$envFile = "$DataDir\loadlab.env"
if (-not (Test-Path $envFile)) {
  @"
# loadlab configuration - edit for the production topology
LOADLAB_BASE_URL=https://librechat.example.internal
LOADLAB_MONGO_URI=mongodb://mongo-host:27017/LibreChat
LOADLAB_MEILI_URL=http://meili-host:7700
LOADLAB_MEILI_KEY=change-me
LOADLAB_RAG_URL=http://rag-host:8000
LOADLAB_METRICS_SECRET=change-me
LOADLAB_UPLOADS_DIR=C:\librechat\uploads
LOADLAB_PG_DSN=postgresql://user:pass@pg-host:5432/mydatabase
LOADLAB_RESTART_CMD=powershell -c "Restart-Service LibreChat"
"@ | Set-Content -Encoding UTF8 $envFile
}

$cmd = "`"$py`" -m loadlab.cli serve --host 0.0.0.0 --port $Port"
if ($NssmExe -and (Test-Path $NssmExe)) {
  & $NssmExe stop $ServiceName 2>$null | Out-Null
  & $NssmExe remove $ServiceName confirm 2>$null | Out-Null
  & $NssmExe install $ServiceName $py "-m loadlab.cli serve --host 0.0.0.0 --port $Port"
  & $NssmExe set $ServiceName AppDirectory $InstallDir
  & $NssmExe set $ServiceName AppStdout "$DataDir\loadlab.out.log"
  & $NssmExe set $ServiceName AppStderr "$DataDir\loadlab.err.log"
  & $NssmExe set $ServiceName AppEnvironmentExtra "LOADLAB_ENV_FILE=$envFile"
  & $NssmExe set $ServiceName Start SERVICE_AUTO_START
  & $NssmExe start $ServiceName
  Write-Host "service '$ServiceName' installed via NSSM on port $Port"
} else {
  Write-Host "NSSM not provided. Register manually, e.g.:"
  Write-Host "  sc.exe create $ServiceName binPath= `"$cmd`" start= auto"
  Write-Host "  sc.exe start $ServiceName"
  Write-Host "Set env LOADLAB_ENV_FILE=$envFile for the service, or use Task Scheduler."
}
Write-Host "Dashboard: http://localhost:$Port"
