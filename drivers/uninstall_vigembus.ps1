# Get Administrator permissions
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script as Administrator!" -ForegroundColor Red
    Exit
}

Write-Host "=== ViGEmBus Cleaner / Uninstaller ===" -ForegroundColor Cyan

# 0. Remove the ViGEmBus device node
Write-Host "Removing ViGEmBus device node if present..." -ForegroundColor Yellow
pnputil /remove-device /deviceid "Root\ViGEmBus"

# 1. Uninstall via Windows registered programs (MSI Uninstall)
$uninstallKeys = @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
)

Write-Host "Searching for ViGEmBus in registered programs..." -ForegroundColor Yellow
$viGEmApps = Get-ItemProperty $uninstallKeys -ErrorAction SilentlyContinue | Where-Object { 
    $_.DisplayName -like "*ViGEm*" -or $_.DisplayName -like "*Virtual Gamepad Emulation Bus*" 
}

if ($viGEmApps) {
    foreach ($app in $viGEmApps) {
        Write-Host "Found: $($app.DisplayName) (Version: $($app.DisplayVersion))" -ForegroundColor Cyan
        if ($app.UninstallString -match "\{[-0-9a-fA-F]+\}") {
            $guid = $Matches[0]
            Write-Host "Uninstalling via Product Code: $guid ..." -ForegroundColor Yellow
            $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/X$guid /qb" -Wait -PassThru
            if ($proc.ExitCode -eq 0) {
                Write-Host "MSI Uninstallation successful." -ForegroundColor Green
            } else {
                Write-Host "MSI Uninstallation exited with code $($proc.ExitCode)." -ForegroundColor Red
            }
        } else {
            Write-Host "Running custom UninstallString: $($app.UninstallString) ..." -ForegroundColor Yellow
            $cmd = $app.UninstallString
            if ($cmd -match '^"([^"]+)"\s*(.*)$') {
                $exe = $Matches[1]
                $args = $Matches[2]
                Start-Process -FilePath $exe -ArgumentList $args -Wait
            } else {
                Invoke-Expression $cmd
            }
        }
    }
} else {
    Write-Host "No registered ViGEmBus installation found in system apps." -ForegroundColor Gray
}

# 2. Force delete ViGEmBus from the Windows Driver Store
Write-Host "Scanning Driver Store for leftover ViGEmBus packages..." -ForegroundColor Yellow
$drivers = pnputil /enum-drivers
$vigemInfs = @()
$currentInf = ""
foreach ($line in $drivers) {
    if ($line -match "^\s*$") {
        $currentInf = ""
    }
    elseif ($line -match "oem\d+\.inf") {
        $currentInf = $Matches[0]
    }
    elseif ($line -match "vigembus\.inf") {
        if ($currentInf) {
            $vigemInfs += $currentInf
        }
    }
}

if ($vigemInfs.Count -gt 0) {
    foreach ($inf in $vigemInfs) {
        Write-Host "Deleting ViGEmBus package $inf from Driver Store..." -ForegroundColor Yellow
        pnputil /delete-driver $inf /uninstall /force
    }
    Write-Host "Driver Store cleanup complete." -ForegroundColor Green
} else {
    Write-Host "No leftover ViGEmBus driver packages found in Driver Store." -ForegroundColor Gray
}

# 3. Clean up the service and service registry key
Write-Host "Checking for ViGEmBus service..." -ForegroundColor Yellow
if (Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue) {
    Write-Host "Removing ViGEmBus service..." -ForegroundColor Yellow
    sc.exe delete ViGEmBus
}
$servicePath = "HKLM:\SYSTEM\CurrentControlSet\Services\ViGEmBus"
if (Test-Path $servicePath) {
    Write-Host "Removing service registry key from $servicePath..." -ForegroundColor Yellow
    Remove-Item -Path $servicePath -Recurse -Force
}

Write-Host "Cleanup complete! A system reboot is highly recommended." -ForegroundColor Green
