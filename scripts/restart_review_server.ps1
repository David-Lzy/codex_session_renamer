param(
    [int]$Port = 8765,
    [string]$HostName = "127.0.0.1",
    [string]$Python = "python",
    [string]$ScriptPath = "",
    [string]$WorkingDirectory = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ScriptPath)) {
    $ScriptPath = Join-Path $PSScriptRoot "session_renamer.py"
}
if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
    $WorkingDirectory = (Get-Location).Path
}

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($listenerPid in $listeners) {
    if ($listenerPid) {
        Stop-Process -Id $listenerPid -Force
    }
}

Start-Sleep -Milliseconds 500
$proc = Start-Process -FilePath $Python `
    -ArgumentList @($ScriptPath, "serve-review", "--host", $HostName, "--port", [string]$Port) `
    -WorkingDirectory $WorkingDirectory `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 1
$status = (Invoke-WebRequest -UseBasicParsing "http://$HostName`:$Port/").StatusCode
[pscustomobject]@{
    server_pid = $proc.Id
    url = "http://$HostName`:$Port/"
    status = $status
} | ConvertTo-Json
