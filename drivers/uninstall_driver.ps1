$ErrorActionPreference = "Stop"
$LogPath = Join-Path $env:TEMP "Switch2Connect_WinUHid_uninstall.log"
Set-Content -LiteralPath $LogPath -Value "WinUHid uninstall started $(Get-Date -Format o)" -Encoding UTF8

function Write-UninstallLog {
    param([string]$Message)
    Write-Host $Message
    Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script as Administrator."
    exit 1
}

function Invoke-PnpUtil {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0),
        [bool]$LogOutput = $true
    )
    $output = & pnputil @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($LogOutput) {
        $output | ForEach-Object { Write-UninstallLog ([string]$_) }
    }
    if ($AllowedExitCodes -notcontains $code) {
        throw "pnputil $($Arguments -join ' ') failed with exit code $code"
    }
    return @($output)
}

function Get-WinUHidDeviceInstances {
    $output = Invoke-PnpUtil -Arguments @("/enum-devices", "/deviceid", "Root\WinUHid", "/properties") -AllowedExitCodes @(0, 259) -LogOutput $false
    return @($output | Select-String -AllMatches -Pattern 'ROOT\\WINUHID\\[^\s]+' | ForEach-Object {
        $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
    } | Select-Object -Unique)
}

function Get-PresentWinUHidDeviceInstances {
    $output = Invoke-PnpUtil -Arguments @("/enum-devices", "/deviceid", "Root\WinUHid", "/properties") -AllowedExitCodes @(0, 259) -LogOutput $false
    $present = @()
    $current = $null
    $awaitingPresence = $false
    foreach ($lineObject in $output) {
        $line = [string]$lineObject
        if ($line -match '(?i)(ROOT\\WINUHID\\[^\s]+)') {
            $current = $Matches[1].Trim().ToUpperInvariant()
        }
        if ($line -match 'DEVPKEY_Device_IsPresent') {
            $awaitingPresence = $true
            continue
        }
        if ($awaitingPresence -and $line -match '(?i)^\s*(TRUE|FALSE)\s*$') {
            if ($Matches[1].ToUpperInvariant() -eq 'TRUE' -and $current) {
                $present += $current
            }
            $awaitingPresence = $false
        }
    }
    return @($present | Select-Object -Unique)
}

function Get-WinUHidDriverPackages {
    $lines = Invoke-PnpUtil -Arguments @("/enum-drivers") -LogOutput $false
    $packages = @()
    $publishedInf = $null
    foreach ($lineObject in $lines) {
        $line = [string]$lineObject
        if ($line -match '(?i)\b(oem\d+\.inf)\b') {
            $publishedInf = $Matches[1].ToLowerInvariant()
        }
        if ($line -match '(?i)winuhiddriver\.inf' -and $publishedInf) {
            $packages += $publishedInf
            $publishedInf = $null
        }
        if ([string]::IsNullOrWhiteSpace($line)) {
            $publishedInf = $null
        }
    }
    return @($packages | Select-Object -Unique)
}

try {
    Write-Host "Removing WinUHid device nodes..." -ForegroundColor Yellow
    foreach ($instance in @(Get-WinUHidDeviceInstances)) {
        try {
            Invoke-PnpUtil -Arguments @("/remove-device", $instance, "/force") | Out-Null
        }
        catch {
            Write-UninstallLog "Exact instance removal failed for $instance; trying device-ID fallback. $($_.Exception.Message)"
        }
    }
    if (@(Get-WinUHidDeviceInstances).Count -gt 0) {
        try {
            Invoke-PnpUtil -Arguments @("/remove-device", "/deviceid", "Root\WinUHid", "/force") | Out-Null
        }
        catch {
            Write-UninstallLog "Device-ID removal fallback failed; final verification will decide the result. $($_.Exception.Message)"
        }
    }

    Write-Host "Removing WinUHid packages from Driver Store..." -ForegroundColor Yellow
    foreach ($package in @(Get-WinUHidDriverPackages)) {
        Invoke-PnpUtil -Arguments @("/delete-driver", $package, "/uninstall", "/force") | Out-Null
    }

    Write-Host "Removing WinUHidDriver certificates..." -ForegroundColor Yellow
    & certutil -delstore "TrustedPublisher" "WinUHidDriver" 2>&1 | ForEach-Object { Write-Host $_ }
    & certutil -delstore "Root" "WinUHidDriver" 2>&1 | ForEach-Object { Write-Host $_ }

    $registryPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver"
    if (Test-Path $registryPath) {
        Remove-Item -LiteralPath $registryPath -Recurse -Force
    }

    # A second pass catches a phantom node exposed only after its package is removed.
    foreach ($instance in @(Get-WinUHidDeviceInstances)) {
        try {
            Invoke-PnpUtil -Arguments @("/remove-device", $instance, "/force") | Out-Null
        }
        catch {
            Write-UninstallLog "Second-pass removal failed for $instance. $($_.Exception.Message)"
        }
    }
    Invoke-PnpUtil -Arguments @("/scan-devices") | Out-Null
    Start-Sleep -Milliseconds 500

    $remainingDevices = @(Get-PresentWinUHidDeviceInstances)
    $remainingPackages = @(Get-WinUHidDriverPackages)
    $registryRemaining = Test-Path $registryPath
    if ($remainingDevices.Count -gt 0 -or $remainingPackages.Count -gt 0 -or $registryRemaining) {
        throw "WinUHid cleanup incomplete. Devices=[$($remainingDevices -join ', ')] Packages=[$($remainingPackages -join ', ')] Registry=$registryRemaining"
    }

    Write-Host "WinUHid uninstallation verified complete." -ForegroundColor Green
    exit 0
}
catch {
    Write-UninstallLog "ERROR: $($_.Exception.Message)"
    exit 1
}
