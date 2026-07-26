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

# cfgmgr32 is the API pnputil itself calls. It has been stable since Windows 2000
# and needs no elevation, unlike pnputil's /enum-devices options, whose command
# surface varies by Windows release. It is the primary enumeration source here.
$script:CfgMgrReady = $null

function Initialize-CfgMgr {
    if ($null -ne $script:CfgMgrReady) { return $script:CfgMgrReady }
    try {
        if (-not ("Switch2.CfgMgrViGEm" -as [type])) {
            Add-Type -Namespace Switch2 -Name CfgMgrViGEm -MemberDefinition @'
[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Get_Device_ID_List_SizeW(out int pulLen, string pszFilter, int ulFlags);

[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Get_Device_ID_ListW(string pszFilter, [Out] char[] Buffer, int BufferLen, int ulFlags);

[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Locate_DevNodeW(out uint pdnDevInst, string pDeviceID, int ulFlags);

[DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
public static extern int CM_Get_DevNode_Registry_PropertyW(uint dnDevInst, int ulProperty, out int pulRegDataType, byte[] Buffer, ref int pulLength, int ulFlags);
'@ -ErrorAction Stop
        }
        $script:CfgMgrReady = $true
    }
    catch {
        Write-CleanupLog "cfgmgr32 unavailable ($($_.Exception.Message)); falling back to pnputil."
        $script:CfgMgrReady = $false
    }
    return $script:CfgMgrReady
}

function Get-CfgMgrIdList {
    # Flags: 1 = filter by enumerator, 2 = filter by driver service.
    # Returns $null when the query could not be answered, @() when there are none.
    param(
        [Parameter(Mandatory = $true)][string]$Filter,
        [Parameter(Mandatory = $true)][int]$Flags
    )
    if (-not (Initialize-CfgMgr)) { return $null }
    try {
        $len = 0
        if ([Switch2.CfgMgrViGEm]::CM_Get_Device_ID_List_SizeW([ref]$len, $Filter, $Flags) -ne 0) { return $null }
        if ($len -le 0) { return @() }
        $buffer = New-Object char[] $len
        if ([Switch2.CfgMgrViGEm]::CM_Get_Device_ID_ListW($Filter, $buffer, $len, $Flags) -ne 0) { return $null }
        return @((-join $buffer).Split([char]0) | Where-Object { $_ } |
            ForEach-Object { $_.ToUpperInvariant() } | Select-Object -Unique)
    }
    catch { return $null }
}

function Get-CfgMgrHardwareIds {
    # Empty for phantom nodes, which carry no properties at all.
    param([Parameter(Mandatory = $true)][string]$InstanceId)
    if (-not (Initialize-CfgMgr)) { return @() }
    try {
        $devinst = [uint32]0
        if ([Switch2.CfgMgrViGEm]::CM_Locate_DevNodeW([ref]$devinst, $InstanceId, 0) -ne 0) { return @() }
        $len = 0
        $kind = 0
        # CM_DRP_HARDWAREID is 2; this property length is reported in bytes.
        [Switch2.CfgMgrViGEm]::CM_Get_DevNode_Registry_PropertyW($devinst, 2, [ref]$kind, $null, [ref]$len, 0) | Out-Null
        if ($len -le 0) { return @() }
        $bytes = New-Object byte[] $len
        if ([Switch2.CfgMgrViGEm]::CM_Get_DevNode_Registry_PropertyW($devinst, 2, [ref]$kind, $bytes, [ref]$len, 0) -ne 0) { return @() }
        return @([Text.Encoding]::Unicode.GetString($bytes, 0, $len).Split([char]0) |
            Where-Object { $_ } | ForEach-Object { $_.ToUpperInvariant() })
    }
    catch { return @() }
}

function Get-CfgMgrViGEmInstances {
    # ViGEmBus's instance id is ROOT\SYSTEM\NNNN and contains no trace of
    # "ViGEmBus"; matching that pattern would also catch SWENUM and HidHide nodes.
    # Only the hardware id identifies it, plus the service's own bound list.
    $bound = Get-CfgMgrIdList -Filter "ViGEmBus" -Flags 2
    $rootIds = Get-CfgMgrIdList -Filter "ROOT" -Flags 1
    if ($null -eq $bound -and $null -eq $rootIds) { return $null }
    $wanted = @("ROOT\VIGEMBUS", "NEFARIUS\VIGEMBUS\GEN1")
    $matched = @()
    foreach ($id in @($rootIds)) {
        if (-not $id) { continue }
        $hwids = @(Get-CfgMgrHardwareIds -InstanceId $id)
        if (@($hwids | Where-Object { $wanted -contains $_ }).Count -gt 0) { $matched += $id }
    }
    $matched += @($bound)
    return @($matched | Where-Object { $_ } | Select-Object -Unique)
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
            Write-CleanupLog "pnputil does not support $($RichOptions -join ' ') (exit $code); retrying plain enumeration."
        }
        else {
            Write-CleanupLog "pnputil $($BaseArguments -join ' ') failed with exit code $code; assuming no matching devices."
        }
    }
    return @()
}

function Get-ViGEmDeviceInstances {
    param([switch]$PresentOnly)
    # Hardware-id matching only ever finds present nodes (phantoms carry no
    # properties), which is exactly the -PresentOnly question; phantom leftovers
    # are handled by the blind /remove-device /deviceid pass instead.
    $viaCfgMgr = Get-CfgMgrViGEmInstances
    if ($null -ne $viaCfgMgr) { return $viaCfgMgr }
    $instances = @()
    foreach ($deviceId in @("Root\ViGEmBus", "Nefarius\ViGEmBus\Gen1")) {
        $output = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/deviceid", $deviceId)
        if (-not ($output | Select-String -Quiet -Pattern 'DEVPKEY_Device_IsPresent')) {
            # Plain listing carries no IsPresent property. `/connected` cannot be
            # combined with /deviceid (pnputil exits 87), and ROOT\SYSTEM\NNNN also
            # matches unrelated devices, so intersect with this device id's own nodes.
            $found = @($output | Select-String -AllMatches -Pattern 'ROOT\\(?:SYSTEM|VIGEMBUS)\\\d+' | ForEach-Object {
                $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
            })
            if ($PresentOnly) {
                $connected = @(Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/connected") -RichOptions @() |
                    Select-String -AllMatches -Pattern 'ROOT\\(?:SYSTEM|VIGEMBUS)\\\d+' | ForEach-Object {
                        $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
                    })
                $found = @($found | Where-Object { $connected -contains $_ })
            }
            $instances += $found
            continue
        }
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

function Get-ViGEmDriverPackages {
    $lines = @()
    try {
        $lines = Invoke-PnpUtil -Arguments @("/enum-drivers") -LogOutput $false
    }
    catch {
        Write-CleanupLog "pnputil /enum-drivers failed; falling back to the driver database. $($_.Exception.Message)"
        return @(Get-DriverDatabasePackages -OriginalInf "vigembus.inf")
    }
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
