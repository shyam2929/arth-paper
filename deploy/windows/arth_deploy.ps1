# Arth paper desk: install on your Ubuntu server from this Windows PC.
# Double-click arth_deploy.cmd (next to this file). You will be asked for:
#   1. the server login, like ubuntu@203.0.113.10
#   2. your server password (twice: once to copy the files, once to run the installer), unless you use an SSH key
#   3. your sudo password on the server, if your account needs one
# Nothing here stores or sends a password anywhere; ssh and scp ask for it directly.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host ""
Write-Host "=== Arth paper desk: server install ===" -ForegroundColor Cyan

if (-not (Get-Command ssh -ErrorAction SilentlyContinue) -or -not (Get-Command scp -ErrorAction SilentlyContinue)) {
    Write-Host "STOP: the OpenSSH client is not installed on this PC." -ForegroundColor Red
    Write-Host "Windows Settings > Apps > Optional features > Add a feature > 'OpenSSH Client', then run this again."
    exit 1
}

$zip = Get-ChildItem -Path $here -Filter 'arth_repo_*.zip' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$setup = Join-Path $here 'arth_server_setup.sh'
if (-not $zip -or -not (Test-Path $setup)) {
    Write-Host "STOP: arth_repo_*.zip and arth_server_setup.sh must be in $here" -ForegroundColor Red
    exit 1
}

do {
    $target = (Read-Host "Server login (user@address, e.g. ubuntu@203.0.113.10)").Trim()
} until ($target -match '^[A-Za-z0-9._-]+@[A-Za-z0-9.:-]+$')
$port = (Read-Host "SSH port (press Enter for 22)").Trim()
if (-not $port) { $port = '22' }

Write-Host ""
Write-Host "Step 1 of 2: copying $($zip.Name) and the setup script to $target" -ForegroundColor Cyan
Write-Host "(type your SERVER password if asked; nothing shows while you type)"
& scp -P $port -o StrictHostKeyChecking=accept-new $zip.FullName $setup "${target}:"
if ($LASTEXITCODE -ne 0) { Write-Host "STOP: copying failed (exit $LASTEXITCODE). Check the address, port and password." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Step 2 of 2: installing on the server (about 15 minutes)" -ForegroundColor Cyan
Write-Host "(server password again if asked, then your sudo password if asked)"
& ssh -t -p $port -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 $target "bash ~/arth_server_setup.sh"
$rc = $LASTEXITCODE
Write-Host ""
if ($rc -eq 0) {
    Write-Host "=== ARTH DEPLOY FINISHED OK ===" -ForegroundColor Green
    Write-Host "Dashboard: run  ssh -L 8080:127.0.0.1:8080 -p $port $target  then open http://localhost:8080"
} else {
    Write-Host "=== ARTH DEPLOY STOPPED (exit $rc): leave this window open so Claude can read it ===" -ForegroundColor Red
    Write-Host "The full log is on the server in ~/arth_install.log. Re-running this is safe."
}
