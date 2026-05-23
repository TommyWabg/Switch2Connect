# Get Administrator permissions
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script as Administrator!" -ForegroundColor Red
    Exit
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = $ScriptDir
if ((Split-Path $ScriptDir -Leaf) -eq "drivers") {
    $RootDir = Split-Path -Parent $ScriptDir
}

# 1. Remove the WinUHid device node
Write-Host "Removing WinUHid device node..." -ForegroundColor Yellow
pnputil /remove-device /deviceid "Root\WinUHid"

# 2. Delete the driver package from the Driver Store
Write-Host "Scanning Driver Store for WinUHid packages..." -ForegroundColor Yellow
$drivers = pnputil /enum-drivers
$oldInfs = @()
$currentInf = ""
foreach ($line in $drivers) {
    if ($line -match "^\s*$") {
        $currentInf = ""
    }
    elseif ($line -match "oem\d+\.inf") {
        $currentInf = $Matches[0]
    }
    elseif ($line -match "winuhiddriver\.inf") {
        if ($currentInf) {
            $oldInfs += $currentInf
        }
    }
}

foreach ($inf in $oldInfs) {
    Write-Host "Deleting driver package $inf from Driver Store..." -ForegroundColor Yellow
    pnputil /delete-driver $inf /uninstall /force
}

# 3. Delete the self-signed certificates
Write-Host "Removing WinUHidDriver certificates from store..." -ForegroundColor Yellow
certutil -delstore "TrustedPublisher" "WinUHidDriver"
certutil -delstore "Root" "WinUHidDriver"

# 4. Clean up registry service key if it remains
$registryPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver"
if (Test-Path $registryPath) {
    Write-Host "Removing registry service key from $registryPath..." -ForegroundColor Yellow
    Remove-Item -Path $registryPath -Recurse -Force
}

# 5. Reset config.yaml files in the workspace and user profile AppData
Write-Host "Resetting driver_installed flags in config.yaml files..." -ForegroundColor Yellow
Get-ChildItem -Path $RootDir -Filter "config.yaml" -Recurse | ForEach-Object {
    $content = Get-Content $_.FullName
    $content = $content -replace "driver_installed:\s*true", "driver_installed: false"
    $content | Set-Content $_.FullName
}
$AppDataConfig = Join-Path $env:APPDATA "Switch2Controllers\config.yaml"
if (Test-Path $AppDataConfig) {
    Write-Host "Resetting driver_installed flag in user AppData config..." -ForegroundColor Yellow
    $content = Get-Content $AppDataConfig
    $content = $content -replace "driver_installed:\s*true", "driver_installed: false"
    $content | Set-Content $AppDataConfig
}

Write-Host "Uninstallation complete!" -ForegroundColor Green
