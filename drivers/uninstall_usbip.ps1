$LogPath = Join-Path $env:TEMP 'Switch2Connect_USBIP_uninstall.log'
Set-Content -LiteralPath $LogPath -Value "USBIP uninstall started $(Get-Date -Format o)" -Encoding UTF8

function Write-CleanupLog {
    param([string]$Message)
    Write-Host $Message
    Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8
}

# Get Administrator privileges
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-CleanupLog 'ERROR: Please run this script as Administrator!'
    Exit 1
}

$AppDir = 'C:\Program Files\USBip'
$HWID = 'ROOT\USBIP_WIN2\UDE'
$InfNames = @('usbip2_filter', 'usbip2_ude')

# cfgmgr32 is the API pnputil itself calls; it is stable across Windows releases
# and needs no elevation. The UDE node's instance id is ROOT\USB\NNNN, which is
# generic - only the hardware id ROOT\USBIP_WIN2\UDE identifies it.
$script:CfgMgrReady = $null

function Initialize-CfgMgr {
    if ($null -ne $script:CfgMgrReady) { return $script:CfgMgrReady }
    try {
        if (-not ('Switch2.CfgMgrUsbip' -as [type])) {
            Add-Type -Namespace Switch2 -Name CfgMgrUsbip -MemberDefinition @'
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
        Write-CleanupLog "cfgmgr32 unavailable ($($_.Exception.Message)); node removal will use pnputil only."
        $script:CfgMgrReady = $false
    }
    return $script:CfgMgrReady
}

function Get-UsbipDeviceInstances {
    # Returns $null when the question could not be answered, @() when there are none.
    if (-not (Initialize-CfgMgr)) { return $null }
    try {
        $len = 0
        if ([Switch2.CfgMgrUsbip]::CM_Get_Device_ID_List_SizeW([ref]$len, 'ROOT', 1) -ne 0) { return $null }
        if ($len -le 0) { return @() }
        $buffer = New-Object char[] $len
        if ([Switch2.CfgMgrUsbip]::CM_Get_Device_ID_ListW('ROOT', $buffer, $len, 1) -ne 0) { return $null }
        $matched = @()
        foreach ($id in @((-join $buffer).Split([char]0) | Where-Object { $_ })) {
            $devinst = [uint32]0
            if ([Switch2.CfgMgrUsbip]::CM_Locate_DevNodeW([ref]$devinst, $id, 0) -ne 0) { continue }
            $plen = 0
            $kind = 0
            # CM_DRP_HARDWAREID is 2; this property length is reported in bytes.
            [Switch2.CfgMgrUsbip]::CM_Get_DevNode_Registry_PropertyW($devinst, 2, [ref]$kind, $null, [ref]$plen, 0) | Out-Null
            if ($plen -le 0) { continue }
            $bytes = New-Object byte[] $plen
            if ([Switch2.CfgMgrUsbip]::CM_Get_DevNode_Registry_PropertyW($devinst, 2, [ref]$kind, $bytes, [ref]$plen, 0) -ne 0) { continue }
            $hwids = @([Text.Encoding]::Unicode.GetString($bytes, 0, $plen).Split([char]0) |
                Where-Object { $_ } | ForEach-Object { $_.ToUpperInvariant() })
            if ($hwids -contains $HWID.ToUpperInvariant()) { $matched += $id.ToUpperInvariant() }
        }
        return @($matched | Select-Object -Unique)
    }
    catch { return $null }
}

function Get-UsbipDriverPackages {
    # Returns $null when neither pnputil nor the INF store could answer.
    $lines = @(& pnputil /enum-drivers 2>&1)
    if ($LASTEXITCODE -eq 0) {
        $packages = @()
        $currentInf = ''
        foreach ($lineObject in $lines) {
            $line = [string]$lineObject
            if ($line -match '^\s*$') { $currentInf = '' }
            elseif ($line -match '(?i)\b(oem\d+\.inf)\b') { $currentInf = $Matches[1].ToLowerInvariant() }
            elseif ($line -match "(?i)($($InfNames -join '|'))\.inf" -and $currentInf) {
                $packages += $currentInf
            }
        }
        return @($packages | Select-Object -Unique)
    }

    Write-CleanupLog "pnputil /enum-drivers failed with exit code $LASTEXITCODE; scanning the INF store instead."
    # This is what usbip-win2's own uninstall.bat does, and it needs no pnputil.
    try {
        $infs = @(Get-ChildItem -LiteralPath 'C:\Windows\INF' -Filter 'oem*.inf' -ErrorAction Stop)
    }
    catch {
        Write-CleanupLog "INF store is not readable: $($_.Exception.Message)"
        return $null
    }
    $found = @()
    foreach ($inf in $infs) {
        try { $text = Get-Content -LiteralPath $inf.FullName -Raw -ErrorAction Stop }
        catch { continue }
        if ($text -match "(?i)($($InfNames -join '|'))") { $found += $inf.Name.ToLowerInvariant() }
    }
    return @($found | Select-Object -Unique)
}

function Get-UsbipServices {
    return @($InfNames | Where-Object {
        Test-Path -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$_"
    })
}

$script:failures = @()
function Add-Failure {
    param([string]$Message)
    $script:failures += $Message
    Write-CleanupLog "FAILED: $Message"
}

# 0. The registered uninstaller handles everything when it is still present.
$UninstallerPath = Join-Path $AppDir 'unins000.exe'
if (Test-Path $UninstallerPath) {
    Write-CleanupLog 'Running USBIP silent uninstaller...'
    $Process = Start-Process -FilePath $UninstallerPath -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru -NoNewWindow
    if ($Process.ExitCode -eq 0) {
        Write-CleanupLog 'USBIP-win2 uninstalled via unins000.exe; verifying...'
    }
    else {
        Write-CleanupLog "Uninstaller returned exit code $($Process.ExitCode). Proceeding to manual cleanup..."
    }
}

Write-CleanupLog 'Performing manual cleanup...'

# 1. Detach all virtual devices first.
if (Test-Path (Join-Path $AppDir 'usbip.exe')) {
    & (Join-Path $AppDir 'usbip.exe') detach --all 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
}

# 2. Remove the device node. devnode.exe is the vendor's own tool and works on
#    every supported Windows; the pnputil instance-id form is the next best
#    (Windows 10 1903+). `/remove-device /deviceid <hwid> /subtree` is last
#    because usbip-win2's own uninstall.bat documents it as "since Windows 11,
#    version 21H2" - on Windows 10 it is rejected outright.
$nodeRemoved = $false
$devnode = Join-Path $AppDir 'devnode.exe'
if (Test-Path $devnode) {
    & $devnode remove $HWID root 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
    if ($LASTEXITCODE -eq 0) { $nodeRemoved = $true }
    else { Write-CleanupLog "devnode.exe remove returned exit code $LASTEXITCODE; trying pnputil." }
}
if (-not $nodeRemoved) {
    $instances = Get-UsbipDeviceInstances
    if ($null -ne $instances -and $instances.Count -gt 0) {
        foreach ($instance in $instances) {
            & pnputil /remove-device $instance /force 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
            if ($LASTEXITCODE -eq 0) { $nodeRemoved = $true }
            else { Add-Failure "pnputil /remove-device $instance returned exit code $LASTEXITCODE" }
        }
    }
    elseif ($null -eq $instances) {
        Write-CleanupLog 'Device enumeration unavailable; falling back to the device-ID form.'
        & pnputil /remove-device /deviceid $HWID /subtree /force 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
        if ($LASTEXITCODE -ne 0) {
            Add-Failure "pnputil /remove-device /deviceid $HWID returned exit code $LASTEXITCODE"
        }
    }
}

# 3. Delete the driver packages from the Driver Store.
$packages = Get-UsbipDriverPackages
if ($null -eq $packages) {
    Add-Failure 'Driver Store package list was unavailable.'
}
else {
    foreach ($inf in $packages) {
        Write-CleanupLog "Deleting driver package $inf from Driver Store..."
        & pnputil /delete-driver $inf /uninstall /force 2>&1 | ForEach-Object { Write-CleanupLog ([string]$_) }
        if ($LASTEXITCODE -ne 0) {
            Add-Failure "pnputil /delete-driver $inf returned exit code $LASTEXITCODE"
        }
    }
}

# 4. Clean up the install directory.
if (Test-Path $AppDir) {
    try { Remove-Item -Path $AppDir -Recurse -Force -ErrorAction Stop }
    catch { Add-Failure "Could not remove ${AppDir}: $($_.Exception.Message)" }
}

# 5. Verify. An unanswerable query must not read as "nothing left".
$remainingInstances = Get-UsbipDeviceInstances
$deviceLayerVerified = $null -ne $remainingInstances
if (-not $deviceLayerVerified) { $remainingInstances = @() }
$remainingPackages = Get-UsbipDriverPackages
$packageLayerVerified = $null -ne $remainingPackages
if (-not $packageLayerVerified) { $remainingPackages = @() }
$remainingServices = @(Get-UsbipServices)
$remainingFiles = Test-Path $AppDir

Write-CleanupLog ("Verification: nodes=[$(@($remainingInstances) -join ', ')] " +
    "packages=[$(@($remainingPackages) -join ', ')] services=[$($remainingServices -join ', ')] " +
    "files=$remainingFiles deviceVerified=$deviceLayerVerified packageVerified=$packageLayerVerified")

if (@($remainingInstances).Count -or @($remainingPackages).Count -or $remainingFiles) {
    Write-CleanupLog "ERROR: USBIP cleanup incomplete."
    Exit 1
}
if ((-not $deviceLayerVerified -or -not $packageLayerVerified) -and $script:failures.Count -gt 0) {
    Write-CleanupLog "ERROR: USBIP cleanup could not be verified. Errors: $($script:failures -join '; ')"
    Exit 1
}
if ($script:failures.Count -gt 0) {
    Write-CleanupLog "Completed with non-fatal errors: $($script:failures -join '; ')"
}
Write-CleanupLog 'Manual cleanup complete!'
Exit 0
