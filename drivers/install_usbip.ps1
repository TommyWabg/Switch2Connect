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

# Verify installation
$UsbIpExe = "C:\Program Files\USBip\usbip.exe"
if (Test-Path $UsbIpExe) {
    Write-Host "USBIP-win2 installed successfully!" -ForegroundColor Green
    Exit 0
} else {
    Write-Host "Error: Installation completed but usbip.exe was not found." -ForegroundColor Red
    Exit 1
}
