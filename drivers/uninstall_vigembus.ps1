$ErrorActionPreference = "Stop"
$LogPath = Join-Path $env:TEMP "Switch2Connect_ViGEmBus_uninstall.log"
Set-Content -LiteralPath $LogPath -Value "ViGEmBus uninstall started $(Get-Date -Format o)" -Encoding UTF8

function Write-CleanupLog {
    param([string]$Message)
    Write-Host $Message
    Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-CleanupLog "ERROR: Administrator privileges are required."
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
    if ($LogOutput) { $output | ForEach-Object { Write-CleanupLog ([string]$_) } }
    if ($AllowedExitCodes -notcontains $code) {
        throw "pnputil $($Arguments -join ' ') failed with exit code $code"
    }
    return @($output)
}

function Get-ViGEmDeviceInstances {
    param([switch]$PresentOnly)
    $instances = @()
    foreach ($deviceId in @("Root\ViGEmBus", "Nefarius\ViGEmBus\Gen1")) {
        $output = Invoke-PnpUtil -Arguments @("/enum-devices", "/deviceid", $deviceId, "/properties") -AllowedExitCodes @(0, 259) -LogOutput $false
        $current = $null
        $awaitingPresence = $false
        foreach ($lineObject in $output) {
            $line = [string]$lineObject
            if ($line -match 'DEVPKEY_Device_InstanceId') {
                $current = $null
                $awaitingPresence = $false
                continue
            }
            if (-not $current -and $line -match '(?i)^\s*(ROOT\\(?:SYSTEM|VIGEMBUS)\\\d+)\s*$') {
                $current = $Matches[1].ToUpperInvariant()
                if (-not $PresentOnly) { $instances += $current }
                continue
            }
            if ($line -match 'DEVPKEY_Device_IsPresent') {
                $awaitingPresence = $true
                continue
            }
            if ($awaitingPresence -and $line -match '(?i)^\s*(TRUE|FALSE)\s*$') {
                if ($PresentOnly -and $Matches[1].ToUpperInvariant() -eq 'TRUE' -and $current) {
                    $instances += $current
                }
                $awaitingPresence = $false
            }
        }
    }
    return @($instances | Select-Object -Unique)
}

function Get-ViGEmDriverPackages {
    $lines = Invoke-PnpUtil -Arguments @("/enum-drivers") -LogOutput $false
    $packages = @()
    $publishedInf = $null
    foreach ($lineObject in $lines) {
        $line = [string]$lineObject
        if ($line -match '(?i)\b(oem\d+\.inf)\b') { $publishedInf = $Matches[1].ToLowerInvariant() }
        if ($line -match '(?i)vigembus\.inf' -and $publishedInf) {
            $packages += $publishedInf
            $publishedInf = $null
        }
        if ([string]::IsNullOrWhiteSpace($line)) { $publishedInf = $null }
    }
    return @($packages | Select-Object -Unique)
}

function Get-ViGEmMsiEntries {
    $keys = @(
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    return @(Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object {
        $_.DisplayName -match '(?i)ViGEm|Virtual Gamepad Emulation'
    })
}

function ConvertTo-PackedProductCode {
    param([Parameter(Mandatory = $true)][string]$ProductCode)
    $hex = ([guid]$ProductCode).ToString("N").ToUpperInvariant()
    $reverse = {
        param([string]$Text)
        $characters = $Text.ToCharArray()
        [array]::Reverse($characters)
        return -join $characters
    }
    $tail = for ($index = 16; $index -lt 32; $index += 2) {
        $hex[$index + 1]
        $hex[$index]
    }
    return (& $reverse $hex.Substring(0, 8)) +
        (& $reverse $hex.Substring(8, 4)) +
        (& $reverse $hex.Substring(12, 4)) +
        (-join $tail)
}

function Remove-OrphanedMsiRegistration {
    param(
        [Parameter(Mandatory = $true)][string]$ProductCode,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )
    if ($DisplayName -notmatch '(?i)^ViGEm( Bus Driver|Bus| Virtual Gamepad Emulation)') {
        throw "Refusing to remove unexpected MSI registration: $DisplayName ($ProductCode)"
    }

    $packedCode = ConvertTo-PackedProductCode -ProductCode $ProductCode
    Write-CleanupLog "Removing source-missing MSI registration for $DisplayName ($ProductCode, packed $packedCode)..."
    $exactPaths = @(
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductCode",
        "HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$ProductCode",
        "HKLM:\Software\Classes\Installer\Products\$packedCode",
        "HKLM:\Software\Classes\Installer\Features\$packedCode"
    )
    foreach ($path in $exactPaths) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
            Write-CleanupLog "Removed exact MSI key: $path"
        }
    }
    foreach ($userDataRoot in @(Get-ChildItem "HKLM:\Software\Microsoft\Windows\CurrentVersion\Installer\UserData" -ErrorAction SilentlyContinue)) {
        $productPath = Join-Path $userDataRoot.PSPath "Products\$packedCode"
        if (Test-Path -LiteralPath $productPath) {
            Remove-Item -LiteralPath $productPath -Recurse -Force
            Write-CleanupLog "Removed exact MSI user-data key: $productPath"
        }
    }
}

try {
    $sourceMissingProducts = @()
    Write-CleanupLog "Removing ViGEmBus PnP nodes..."
    foreach ($instance in @(Get-ViGEmDeviceInstances)) {
        try {
            Invoke-PnpUtil -Arguments @("/remove-device", $instance, "/force") | Out-Null
        }
        catch {
            Write-CleanupLog "Node removal failed for $instance; final verification will decide. $($_.Exception.Message)"
        }
    }

    Write-CleanupLog "Removing registered ViGEmBus MSI products..."
    foreach ($app in @(Get-ViGEmMsiEntries)) {
        $guid = $null
        if ($app.PSChildName -match '^\{[-0-9a-fA-F]+\}$') { $guid = $app.PSChildName }
        elseif ($app.UninstallString -match '\{[-0-9a-fA-F]+\}') { $guid = $Matches[0] }
        if (-not $guid) { throw "Cannot determine MSI product code for $($app.DisplayName)" }
        $process = Start-Process -FilePath "msiexec.exe" -ArgumentList "/X$guid /qn /norestart" -Wait -PassThru
        if ($process.ExitCode -eq 1612) {
            # Windows Installer still knows the ProductCode, but its cached/source
            # MSI is gone. Defer exact registration cleanup until we have proved
            # that no ViGEm device, package, or service remains.
            $sourceMissingProducts += [pscustomobject]@{
                ProductCode = $guid
                DisplayName = [string]$app.DisplayName
            }
            Write-CleanupLog "MSI source is missing for $guid (1612); deferring orphan registration cleanup."
        }
        elseif (@(0, 1605, 1641, 3010) -notcontains $process.ExitCode) {
            throw "MSI uninstall for $guid failed with exit code $($process.ExitCode)"
        }
    }

    Write-CleanupLog "Removing ViGEmBus Driver Store packages..."
    foreach ($package in @(Get-ViGEmDriverPackages)) {
        Invoke-PnpUtil -Arguments @("/delete-driver", $package, "/uninstall", "/force") | Out-Null
    }

    $servicePath = "HKLM:\SYSTEM\CurrentControlSet\Services\ViGEmBus"
    if (Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue) {
        & sc.exe stop ViGEmBus 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
        & sc.exe delete ViGEmBus 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
    }
    if (Test-Path $servicePath) { Remove-Item -LiteralPath $servicePath -Recurse -Force }

    # MSI/package removal can expose additional nodes, so perform a second exact pass.
    foreach ($instance in @(Get-ViGEmDeviceInstances)) {
        try { Invoke-PnpUtil -Arguments @("/remove-device", $instance, "/force") | Out-Null }
        catch { Write-CleanupLog "Second-pass removal failed for $instance. $($_.Exception.Message)" }
    }
    Invoke-PnpUtil -Arguments @("/scan-devices") | Out-Null
    Start-Sleep -Milliseconds 750

    $remainingNodes = @(Get-ViGEmDeviceInstances -PresentOnly)
    $remainingPackages = @(Get-ViGEmDriverPackages)
    $serviceRemaining = Test-Path $servicePath
    if ($remainingNodes.Count -or $remainingPackages.Count -or $serviceRemaining) {
        throw "ViGEmBus cleanup incomplete; refusing orphan MSI cleanup. Nodes=[$($remainingNodes -join ', ')] Packages=[$($remainingPackages -join ', ')] Service=$serviceRemaining"
    }

    foreach ($orphan in $sourceMissingProducts) {
        Remove-OrphanedMsiRegistration -ProductCode $orphan.ProductCode -DisplayName $orphan.DisplayName
    }

    $remainingMsi = @(Get-ViGEmMsiEntries)
    if ($remainingMsi.Count) {
        throw "ViGEmBus cleanup incomplete. Nodes=[$($remainingNodes -join ', ')] Packages=[$($remainingPackages -join ', ')] MSI=$($remainingMsi.Count) Service=$serviceRemaining"
    }

    Write-CleanupLog "ViGEmBus uninstallation verified complete."
    exit 0
}
catch {
    Write-CleanupLog "ERROR: $($_.Exception.Message)"
    exit 1
}
