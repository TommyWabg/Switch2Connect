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

# cfgmgr32 is the API pnputil itself calls. It has been stable since Windows 2000
# and needs no elevation, unlike pnputil's /enum-devices options, whose command
# surface varies by Windows release. It is therefore the primary enumeration
# source; pnputil is only a fallback.
$CM_GETIDLIST_FILTER_ENUMERATOR = 1
$CM_GETIDLIST_FILTER_SERVICE = 2
$script:CfgMgrReady = $null

function Initialize-CfgMgr {
    if ($null -ne $script:CfgMgrReady) { return $script:CfgMgrReady }
    try {
        if (-not ("Switch2.CfgMgrWinUHid" -as [type])) {
            Add-Type -Namespace Switch2 -Name CfgMgrWinUHid -MemberDefinition @'
[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Get_Device_ID_List_SizeW(out int pulLen, string pszFilter, int ulFlags);

[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Get_Device_ID_ListW(string pszFilter, [Out] char[] Buffer, int BufferLen, int ulFlags);

[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Locate_DevNodeW(out uint pdnDevInst, string pDeviceID, int ulFlags);
'@ -ErrorAction Stop
        }
        $script:CfgMgrReady = $true
    }
    catch {
        Write-UninstallLog "cfgmgr32 unavailable ($($_.Exception.Message)); falling back to pnputil."
        $script:CfgMgrReady = $false
    }
    return $script:CfgMgrReady
}

function Get-CfgMgrIdList {
    # Returns $null when the query could not be answered, @() when there are none.
    param(
        [Parameter(Mandatory = $true)][string]$Filter,
        [int]$Flags = 1
    )
    if (-not (Initialize-CfgMgr)) { return $null }
    try {
        $len = 0
        if ([Switch2.CfgMgrWinUHid]::CM_Get_Device_ID_List_SizeW([ref]$len, $Filter, $Flags) -ne 0) { return $null }
        if ($len -le 0) { return @() }
        $buffer = New-Object char[] $len
        if ([Switch2.CfgMgrWinUHid]::CM_Get_Device_ID_ListW($Filter, $buffer, $len, $Flags) -ne 0) { return $null }
        return @((-join $buffer).Split([char]0) | Where-Object { $_ })
    }
    catch { return $null }
}

function Test-CfgMgrPresent {
    # A phantom node cannot be located at all, so locating it is exactly
    # equivalent to DEVPKEY_Device_IsPresent being TRUE.
    param([Parameter(Mandatory = $true)][string]$InstanceId)
    if (-not (Initialize-CfgMgr)) { return $null }
    try {
        $devinst = [uint32]0
        return ([Switch2.CfgMgrWinUHid]::CM_Locate_DevNodeW([ref]$devinst, $InstanceId, 0) -eq 0)
    }
    catch { return $null }
}

function Get-CfgMgrWinUHidInstances {
    $ids = Get-CfgMgrIdList -Filter "ROOT" -Flags $CM_GETIDLIST_FILTER_ENUMERATOR
    if ($null -eq $ids) { return $null }
    return @($ids | Where-Object { $_.ToUpperInvariant().StartsWith("ROOT\WINUHID\") } |
        ForEach-Object { $_.ToUpperInvariant() } | Select-Object -Unique)
}

# `/enum-devices` only gained `/properties` in Windows 11 21H2; older pnputil exits
# with code 1 and prints usage. Enumeration must degrade instead of throwing, or the
# whole uninstall dies before it removes anything.
$script:PnpUtilRichEnum = $null   # $null unknown / $true supported / $false not

function Invoke-PnpUtilEnum {
    param(
        [Parameter(Mandatory = $true)][string[]]$BaseArguments,
        [string[]]$RichOptions = @("/properties")
    )
    $attempts = @()
    if ($RichOptions.Count -gt 0 -and $script:PnpUtilRichEnum -ne $false) {
        $attempts += ,($BaseArguments + $RichOptions)
    }
    $attempts += ,$BaseArguments
    foreach ($attempt in $attempts) {
        $output = & pnputil @attempt 2>&1
        $code = $LASTEXITCODE
        if ($code -eq 0 -or $code -eq 259) {
            if ($attempt.Count -gt $BaseArguments.Count) { $script:PnpUtilRichEnum = $true }
            return @($output)
        }
        if ($attempt.Count -gt $BaseArguments.Count) {
            $script:PnpUtilRichEnum = $false
            Write-UninstallLog "pnputil does not support $($RichOptions -join ' ') (exit $code); retrying plain enumeration."
        }
        else {
            Write-UninstallLog "pnputil $($BaseArguments -join ' ') failed with exit code $code."
        }
    }
    # $null means "could not be answered" - distinct from @() ("none found"). An
    # empty list here would make the final check report a false clean-up.
    return $null
}

function Get-WinUHidDeviceInstances {
    $ids = Get-CfgMgrWinUHidInstances
    if ($null -ne $ids) { return $ids }
    $output = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/deviceid", "Root\WinUHid")
    if ($null -eq $output) { return $null }
    return @($output | Select-String -AllMatches -Pattern 'ROOT\\WINUHID\\[^\s]+' | ForEach-Object {
        $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
    } | Select-Object -Unique)
}

function Get-PresentWinUHidDeviceInstances {
    $ids = Get-CfgMgrWinUHidInstances
    if ($null -ne $ids) {
        return @($ids | Where-Object { (Test-CfgMgrPresent -InstanceId $_) -eq $true })
    }
    $output = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/deviceid", "Root\WinUHid")
    if ($null -eq $output) { return $null }
    if (-not ($output | Select-String -Quiet -Pattern 'DEVPKEY_Device_IsPresent')) {
        # Plain listing carries no IsPresent property. `/connected` is the locale-free
        # presence filter and pre-dates /properties, but it cannot be combined with
        # /deviceid (pnputil exits 87), so filter the unfiltered listing here.
        $connected = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/connected") -RichOptions @()
        return @($connected | Select-String -AllMatches -Pattern 'ROOT\\WINUHID\\[^\s]+' | ForEach-Object {
            $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
        } | Select-Object -Unique)
    }
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

function Get-DriverDatabasePackages {
    param([Parameter(Mandatory = $true)][string]$OriginalInf)
    # Same answer as /enum-drivers, but readable even when that call fails. Each
    # subkey is "<original>.inf_<arch>_<hash>" with the oemNN.inf as default value.
    $root = "HKLM:\SYSTEM\DriverDatabase\DriverPackages"
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    try { $keys = Get-ChildItem -LiteralPath $root -ErrorAction Stop }
    catch { return @() }
    return @($keys | Where-Object { $_.PSChildName -like "$OriginalInf`_*" } | ForEach-Object {
        $value = (Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue).'(default)'
        if ("$value" -match '(?i)\b(oem\d+\.inf)\b') { $Matches[1].ToLowerInvariant() }
    } | Where-Object { $_ } | Select-Object -Unique)
}

function Get-WinUHidDriverPackages {
    $lines = @()
    try {
        $lines = Invoke-PnpUtil -Arguments @("/enum-drivers") -LogOutput $false
    }
    catch {
        Write-UninstallLog "pnputil /enum-drivers failed; falling back to the driver database. $($_.Exception.Message)"
        return @(Get-DriverDatabasePackages -OriginalInf "WinUHidDriver.inf")
    }
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
    $instances = Get-WinUHidDeviceInstances
    $enumerationWorks = $null -ne $instances
    if (-not $enumerationWorks) {
        Write-UninstallLog "Device enumeration unavailable; removing blind by device ID."
    }
    foreach ($instance in @($instances)) {
        if (-not $instance) { continue }
        try {
            Invoke-PnpUtil -Arguments @("/remove-device", $instance, "/force") | Out-Null
        }
        catch {
            Write-UninstallLog "Exact instance removal failed for $instance; trying device-ID fallback. $($_.Exception.Message)"
        }
    }
    # The device-ID form needs no enumeration at all, so it is the blind-repair
    # path: run it whenever enumeration failed, or when nodes were found.
    if (-not $enumerationWorks -or @($instances).Count -gt 0) {
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
        if (-not $instance) { continue }
        try {
            Invoke-PnpUtil -Arguments @("/remove-device", $instance, "/force") | Out-Null
        }
        catch {
            Write-UninstallLog "Second-pass removal failed for $instance. $($_.Exception.Message)"
        }
    }
    Invoke-PnpUtil -Arguments @("/scan-devices") | Out-Null
    Start-Sleep -Milliseconds 500

    $remainingDevices = Get-PresentWinUHidDeviceInstances
    $remainingPackages = @(Get-WinUHidDriverPackages)
    $registryRemaining = Test-Path $registryPath
    $deviceLayerVerified = $null -ne $remainingDevices
    if (-not $deviceLayerVerified) {
        # Treating an unanswerable query as "nothing left" would report a false
        # clean-up. Fall back to the evidence that is still readable.
        $remainingDevices = @()
        $databaseRemaining = @(Get-DriverDatabasePackages -OriginalInf "WinUHidDriver.inf")
        Write-UninstallLog ("Device enumeration unavailable; verifying via Driver Store and registry only. " +
            "Driver database entries remaining: [$($databaseRemaining -join ', ')]")
        if ($databaseRemaining.Count -gt 0) {
            throw "WinUHid cleanup incomplete: driver package(s) [$($databaseRemaining -join ', ')] still registered."
        }
    }
    $remainingDevices = @($remainingDevices)
    if ($remainingDevices.Count -gt 0 -or $remainingPackages.Count -gt 0 -or $registryRemaining) {
        throw "WinUHid cleanup incomplete. Devices=[$($remainingDevices -join ', ')] Packages=[$($remainingPackages -join ', ')] Registry=$registryRemaining"
    }

    if ($deviceLayerVerified) {
        Write-Host "WinUHid uninstallation verified complete." -ForegroundColor Green
    }
    else {
        Write-Host "WinUHid uninstallation complete (device layer unverified)." -ForegroundColor Green
    }
    exit 0
}
catch {
    Write-UninstallLog "ERROR: $($_.Exception.Message)"
    exit 1
}
