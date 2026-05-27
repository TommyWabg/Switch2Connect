# Get Administrator privileges
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script as Administrator!" -ForegroundColor Red
    Exit 1
}

$UninstallerPath = "C:\Program Files\USBip\unins000.exe"

if (Test-Path $UninstallerPath) {
    Write-Host "Running USBIP silent uninstaller..." -ForegroundColor Cyan
    $Process = Start-Process -FilePath $UninstallerPath -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait -PassThru -NoNewWindow
    
    if ($Process.ExitCode -eq 0) {
        Write-Host "USBIP-win2 uninstalled successfully via unins000.exe!" -ForegroundColor Green
        Exit 0
    } else {
        Write-Host "Warning: Uninstaller returned exit code $($Process.ExitCode). Proceeding to manual cleanup..." -ForegroundColor Yellow
    }
}

# Fallback: Manual cleanup if uninstaller is missing or failed
Write-Host "Performing manual cleanup..." -ForegroundColor Cyan

# 1. Detach all virtual devices first
if (Test-Path "C:\Program Files\USBip\usbip.exe") {
    & "C:\Program Files\USBip\usbip.exe" detach --all
}

# 2. Remove the device node
$HWID = "ROOT\USBIP_WIN2\UDE"
if (Test-Path "C:\Program Files\USBip\devnode.exe") {
    & "C:\Program Files\USBip\devnode.exe" remove $HWID root
} else {
    # Win 11 alternative
    pnputil.exe /remove-device /deviceid $HWID /subtree
}

# 3. Scan and delete the driver package from driver store
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
    elseif ($line -match "(usbip2_filter|usbip2_ude)\.inf") {
        if ($currentInf) {
            $oldInfs += $currentInf
        }
    }
}

foreach ($inf in $oldInfs) {
    Write-Host "Deleting driver package $inf from Driver Store..." -ForegroundColor Yellow
    pnputil /delete-driver $inf /uninstall /force
}

# 4. Clean up directories
if (Test-Path "C:\Program Files\USBip") {
    Remove-Item -Path "C:\Program Files\USBip" -Recurse -Force
}

Write-Host "Manual cleanup complete!" -ForegroundColor Green
Exit 0
