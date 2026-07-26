$ErrorActionPreference = 'Stop'
$LogPath = Join-Path $env:TEMP 'Switch2Connect_HidHide_uninstall.log'
Set-Content -LiteralPath $LogPath -Value "HidHide uninstall started $(Get-Date -Format o)" -Encoding UTF8

function Write-CleanupLog {
    param([string]$Message)
    Write-Host $Message
    Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-CleanupLog 'ERROR: Administrator privileges are required.'
    exit 1
}

# A failure in one stage must never skip the remaining stages. Aborting early is
# what leaves a half-removed HidHide behind - e.g. an MSI failure skipping the
# service removal, after which the app still reports HidHide as installed.
# Failures are collected here and the final verification decides the exit code.
$script:failures = @()

function Add-Failure {
    param([string]$Message)
    $script:failures += $Message
    Write-CleanupLog "FAILED: $Message"
}

function Get-HidHideMsiEntries {
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    return @(Get-ItemProperty $roots -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match '(?i)HidHide' })
}

$script:DriverDatabaseReadable = $null

function Get-DriverDatabasePackages {
    param([Parameter(Mandatory = $true)][string]$OriginalInf)
    # Same answer as /enum-drivers, but readable even when that call fails. Each
    # subkey is "<original>.inf_<arch>_<hash>" with the oemNN.inf as default value.
    $root = 'HKLM:\SYSTEM\DriverDatabase\DriverPackages'
    try {
        $keys = Get-ChildItem -LiteralPath $root -ErrorAction Stop
        $script:DriverDatabaseReadable = $true
    }
    catch {
        $script:DriverDatabaseReadable = $false
        return @()
    }
    return @($keys | Where-Object { $_.PSChildName -like "$OriginalInf`_*" } | ForEach-Object {
        $value = (Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue).'(default)'
        if ("$value" -match '(?i)\b(oem\d+\.inf)\b') { $Matches[1].ToLowerInvariant() }
    } | Where-Object { $_ } | Select-Object -Unique)
}

function Get-HidHideDriverPackages {
    # Returns $null when the question could not be answered at all - distinct from
    # @() ("none installed"). Throwing here used to abort the whole uninstall
    # before anything was removed, and treating a failure as @() would report a
    # false clean-up at the end.
    $lines = @(& pnputil /enum-drivers 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-CleanupLog "pnputil /enum-drivers failed with exit code $LASTEXITCODE; falling back to the driver database."
        $fromDatabase = Get-DriverDatabasePackages -OriginalInf 'hidhide.inf'
        if ($script:DriverDatabaseReadable) { return $fromDatabase }
        return $null
    }
    $packages = @()
    $publishedInf = $null
    foreach ($lineObject in $lines) {
        $line = [string]$lineObject
        if ($line -match '(?i)\b(oem\d+\.inf)\b') { $publishedInf = $Matches[1].ToLowerInvariant() }
        if ($line -match '(?i)hidhide\.inf' -and $publishedInf) {
            $packages += $publishedInf
            $publishedInf = $null
        }
        if ([string]::IsNullOrWhiteSpace($line)) { $publishedInf = $null }
    }
    return @($packages | Select-Object -Unique)
}

$servicePath = 'HKLM:\SYSTEM\CurrentControlSet\Services\HidHide'

try {
    # Stage 1 - the registered product uninstaller, when it is still available.
    foreach ($app in @(Get-HidHideMsiEntries)) {
        $guid = $null
        if ($app.PSChildName -match '^\{[-0-9a-fA-F]+\}$') { $guid = $app.PSChildName }
        elseif ([string]$app.UninstallString -match '\{[-0-9a-fA-F]+\}') { $guid = $Matches[0] }

        try {
            if ($guid) {
                Write-CleanupLog "Running HidHide MSI uninstaller for $guid..."
                $process = Start-Process 'msiexec.exe' -ArgumentList "/x $guid /qn /norestart" -Wait -PassThru
                if (@(0, 1605, 1612, 1641, 3010) -notcontains $process.ExitCode) {
                    Add-Failure "HidHide MSI uninstall returned exit code $($process.ExitCode)"
                }
            }
            elseif ($app.QuietUninstallString -or $app.UninstallString) {
                $command = if ($app.QuietUninstallString) { $app.QuietUninstallString } else { "$($app.UninstallString) /exenoui /qn" }
                Write-CleanupLog 'Running registered HidHide uninstaller...'
                $process = Start-Process 'cmd.exe' -ArgumentList '/d', '/s', '/c', $command -Wait -PassThru
                if (@(0, 1641, 3010) -notcontains $process.ExitCode) {
                    Add-Failure "HidHide registered uninstaller returned exit code $($process.ExitCode)"
                }
            }
        }
        catch {
            Add-Failure "Registered HidHide uninstaller could not be started. $($_.Exception.Message)"
        }
    }

    # Stage 2 - the product registration may already be gone while the kernel
    # package is still installed, so always remove the package directly.
    $packages = Get-HidHideDriverPackages
    if ($null -eq $packages) {
        Add-Failure 'Driver Store package list was unavailable.'
    }
    else {
        foreach ($package in $packages) {
            Write-CleanupLog "Removing HidHide Driver Store package $package..."
            & pnputil /delete-driver $package /uninstall /force 2>&1 |
                ForEach-Object { Write-CleanupLog ([string]$_) }
            if ($LASTEXITCODE -ne 0) {
                Add-Failure "pnputil /delete-driver $package returned exit code $LASTEXITCODE"
            }
        }
    }

    # Stage 3 - remove the exact service registration even when the loaded .sys
    # file must remain until reboot. The file alone must not make the app report
    # HidHide as installed.
    try {
        if (Get-Service -Name 'HidHide' -ErrorAction SilentlyContinue) {
            & sc.exe stop HidHide 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
            & sc.exe delete HidHide 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
            $deleteCode = $LASTEXITCODE
            if (@(0, 1060, 1072) -notcontains $deleteCode) {
                Add-Failure "sc.exe delete HidHide returned exit code $deleteCode"
            }
        }
    }
    catch {
        Add-Failure "HidHide service removal failed. $($_.Exception.Message)"
    }

    # Stage 4 - service registry key.
    if (Test-Path -LiteralPath $servicePath) {
        try {
            # sc.exe delete can remove child keys asynchronously. A recursive
            # Remove-Item may then see the root but fail because a child vanished
            # while PowerShell was enumerating it. Final verification below is the
            # authority, so treat that race as non-fatal here.
            Remove-Item -LiteralPath $servicePath -Recurse -Force -ErrorAction Stop
        }
        catch {
            Write-CleanupLog "Service registry cleanup is still settling: $($_.Exception.Message)"
        }
    }

    & pnputil /scan-devices 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
    Start-Sleep -Milliseconds 750

    # Final verification decides the result, regardless of which stages reported
    # errors along the way.
    $remainingPackages = Get-HidHideDriverPackages
    $packageLayerVerified = $null -ne $remainingPackages
    if (-not $packageLayerVerified) {
        Write-CleanupLog 'Driver Store could not be re-checked; verifying on the service registration only.'
        $remainingPackages = @()
    }
    $remainingPackages = @($remainingPackages)
    $serviceRemaining = Test-Path -LiteralPath $servicePath
    $serviceDeletePending = $false
    if ($serviceRemaining) {
        $serviceDeletePending = ((Get-ItemProperty -LiteralPath $servicePath -ErrorAction SilentlyContinue).DeleteFlag -eq 1)
    }
    if ($remainingPackages.Count -or ($serviceRemaining -and -not $serviceDeletePending)) {
        throw "HidHide cleanup incomplete. Packages=[$($remainingPackages -join ', ')] Service=$serviceRemaining DeletePending=$serviceDeletePending"
    }
    if (-not $packageLayerVerified -and $script:failures.Count -gt 0) {
        throw "HidHide cleanup could not be verified. Errors: $($script:failures -join '; ')"
    }

    $driverFileRemaining = Test-Path -LiteralPath 'C:\Windows\System32\drivers\HidHide.sys'
    if ($script:failures.Count -gt 0) {
        # Every layer verifies clean despite the earlier errors, so the uninstall
        # did achieve its goal; keep the detail in the log rather than failing.
        Write-CleanupLog "Completed with non-fatal errors: $($script:failures -join '; ')"
    }
    Write-CleanupLog "HidHide uninstallation verified. ServiceDeletePending=$serviceDeletePending DriverFilePendingReboot=$driverFileRemaining PackageLayerVerified=$packageLayerVerified"
    exit 0
}
catch {
    Write-CleanupLog "ERROR: $($_.Exception.Message)"
    exit 1
}
