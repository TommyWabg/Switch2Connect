# Get Administrator privileges
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script as Administrator!" -ForegroundColor Red
    Exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallerPath = Join-Path $ScriptDir "USBip-0.9.7.7-x64.exe"

if (-not (Test-Path $InstallerPath)) {
    Write-Host "Error: USBIP Installer not found at $InstallerPath" -ForegroundColor Red
    Exit 1
}

Write-Host "Running USBIP WHQL-Signed Installer silently..." -ForegroundColor Cyan
Write-Host "NOTE: USB Hub devices will restart briefly during installation." -ForegroundColor Yellow

# Run InnoSetup installer silently, preventing desktop shortcut creation
$Process = Start-Process -FilePath $InstallerPath -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", '/MERGETASKS="!desktopicon"' -Wait -PassThru -NoNewWindow

if ($Process.ExitCode -ne 0) {
    Write-Host "Warning: Installer returned exit code $($Process.ExitCode)" -ForegroundColor Yellow
}

# Verify installation. usbip.exe alone is not proof the driver landed - the
# Driver Store package and the services are what actually make USBIP work.
$UsbIpExe = "C:\Program Files\USBip\usbip.exe"
$FilesPresent = Test-Path $UsbIpExe

$ServicesPresent = @(@('usbip2_ude', 'usbip2_filter') | Where-Object {
    Test-Path -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$_"
}).Count -gt 0

$PackagePresent = $false
$dbRoot = "HKLM:\SYSTEM\DriverDatabase\DriverPackages"
if (Test-Path -LiteralPath $dbRoot) {
    $PackagePresent = @(Get-ChildItem -LiteralPath $dbRoot -ErrorAction SilentlyContinue |
        Where-Object { $_.PSChildName -like "usbip2_ude.inf_*" -or $_.PSChildName -like "usbip2_filter.inf_*" }).Count -gt 0
}

if ($FilesPresent -and $ServicesPresent -and $PackagePresent) {
    Write-Host "USBIP-win2 installed successfully!" -ForegroundColor Green
    Exit 0
}
if ($FilesPresent -and ($ServicesPresent -or $PackagePresent)) {
    # Some layers are still settling (the installer restarts USB hubs); the app's
    # own status check makes the final call rather than failing outright here.
    Write-Host "USBIP-win2 installed; driver layers still settling (services=$ServicesPresent package=$PackagePresent)." -ForegroundColor Yellow
    Exit 0
}
Write-Host "Error: installation incomplete. files=$FilesPresent services=$ServicesPresent package=$PackagePresent" -ForegroundColor Red
Exit 1
