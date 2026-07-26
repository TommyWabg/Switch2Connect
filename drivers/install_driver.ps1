$ErrorActionPreference = "Stop"

# Get Administrator permissions
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script as Administrator!" -ForegroundColor Red
    Exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = $ScriptDir
if ((Split-Path $ScriptDir -Leaf) -eq "drivers") {
    $RootDir = Split-Path -Parent $ScriptDir
}

# Paths
$DriverDir = ""
$InfPath = ""
$CertPath = ""

# Define possible search paths
$SearchPaths = @(
    $ScriptDir,
    (Join-Path $RootDir "WinUHid-main\WinUHid Driver\build\Release\x64\WinUHid Driver"),
    (Join-Path $RootDir "external\WinUHid-main\WinUHid Driver\build\Release\x64\WinUHid Driver")
)

# Find the first path containing WinUHidDriver.inf
foreach ($path in $SearchPaths) {
    $tempInf = Join-Path $path "WinUHidDriver.inf"
    if (Test-Path $tempInf) {
        $DriverDir = $path
        $InfPath = [System.IO.Path]::GetFullPath($tempInf)
        break
    }
}

# Certificate path resolution: check next to inf or parent of inf
if ($DriverDir) {
    $tempCert = Join-Path $DriverDir "WinUHidDriver.cer"
    if (Test-Path $tempCert) {
        $CertPath = [System.IO.Path]::GetFullPath($tempCert)
    } else {
        # Try parent folder of DriverDir (for release build layout)
        $parent = Split-Path -Parent $DriverDir
        $tempCert = Join-Path $parent "WinUHidDriver.cer"
        if (Test-Path $tempCert) {
            $CertPath = [System.IO.Path]::GetFullPath($tempCert)
        }
    }
}

# Check if files exist
if (-not $InfPath -or -not (Test-Path $InfPath)) {
    Write-Host "Error: Driver INF not found!" -ForegroundColor Red
    Exit 1
}
if (-not $CertPath -or -not (Test-Path $CertPath)) {
    Write-Host "Error: Driver Certificate not found!" -ForegroundColor Red
    Exit 1
}

# cfgmgr32 is the API pnputil itself calls. It has been stable since Windows 2000
# and needs no elevation, unlike pnputil's /enum-devices options, whose command
# surface varies by Windows release. It is the primary enumeration source here.
$script:CfgMgrReady = $null

function Initialize-CfgMgr {
    if ($null -ne $script:CfgMgrReady) { return $script:CfgMgrReady }
    try {
        if (-not ("Switch2.CfgMgrInstall" -as [type])) {
            Add-Type -Namespace Switch2 -Name CfgMgrInstall -MemberDefinition @'
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
        Write-Host "cfgmgr32 unavailable ($($_.Exception.Message)); falling back to pnputil." -ForegroundColor Yellow
        $script:CfgMgrReady = $false
    }
    return $script:CfgMgrReady
}

function Get-CfgMgrWinUHidInstances {
    # Returns $null when the query could not be answered, @() when there are none.
    if (-not (Initialize-CfgMgr)) { return $null }
    try {
        $len = 0
        if ([Switch2.CfgMgrInstall]::CM_Get_Device_ID_List_SizeW([ref]$len, "ROOT", 1) -ne 0) { return $null }
        if ($len -le 0) { return @() }
        $buffer = New-Object char[] $len
        if ([Switch2.CfgMgrInstall]::CM_Get_Device_ID_ListW("ROOT", $buffer, $len, 1) -ne 0) { return $null }
        return @((-join $buffer).Split([char]0) | Where-Object { $_ } |
            Where-Object { $_.ToUpperInvariant().StartsWith("ROOT\WINUHID\") } |
            ForEach-Object { $_.ToUpperInvariant() } | Select-Object -Unique)
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
        return ([Switch2.CfgMgrInstall]::CM_Locate_DevNodeW([ref]$devinst, $InstanceId, 0) -eq 0)
    }
    catch { return $null }
}

# `/enum-devices` only gained `/properties` in Windows 11 21H2; older pnputil exits
# with code 1 and prints usage, so every enumeration here degrades to the plain form.
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
            Write-Host "pnputil does not support $($RichOptions -join ' ') (exit $code); retrying plain enumeration." -ForegroundColor Yellow
        }
        else {
            Write-Host "pnputil $($BaseArguments -join ' ') failed with exit code $code; assuming no matching devices." -ForegroundColor Yellow
        }
    }
    return @()
}

# 1. Clean up existing WinUHid devices
Write-Host "Removing existing WinUHid device nodes..." -ForegroundColor Yellow
$deviceInstances = Get-CfgMgrWinUHidInstances
if ($null -eq $deviceInstances) {
    $deviceOutput = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/deviceid", "Root\WinUHid")
    $deviceInstances = @($deviceOutput | Select-String -AllMatches -Pattern 'ROOT\\WINUHID\\[^\s]+' | ForEach-Object {
        $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
    } | Select-Object -Unique)
}
foreach ($instance in $deviceInstances) {
    pnputil /remove-device $instance /force
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Exact removal failed for $instance; trying device-ID fallback." -ForegroundColor Yellow
    }
}
if ($deviceInstances.Count -gt 0) {
    $remainingOutput = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/deviceid", "Root\WinUHid")
    if (($remainingOutput -join "`n") -match '(?i)ROOT\\WINUHID\\') {
        pnputil /remove-device /deviceid "Root\WinUHid" /force
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Historical WinUHid records could not be removed; installation will use a new Root instance ID." -ForegroundColor Yellow
        }
    }
}

# 2. Clean up existing driver packages from Driver Store
Write-Host "Scanning Driver Store for old WinUHid packages..." -ForegroundColor Yellow
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
    Write-Host "Deleting old driver package $inf from Driver Store..." -ForegroundColor Yellow
    pnputil /delete-driver $inf /uninstall /force
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to remove old Driver Store package $inf" -ForegroundColor Red
        Exit 1
    }
}


# 4. Install certificate to TrustedPublisher and Root store
Write-Host "Installing certificate to TrustedPublisher and Root stores..." -ForegroundColor Cyan
certutil -addstore -f "TrustedPublisher" $CertPath
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to install TrustedPublisher certificate" -ForegroundColor Red; Exit 1 }
certutil -addstore -f "Root" $CertPath
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to install Root certificate" -ForegroundColor Red; Exit 1 }

# 5. Install the driver and create the device node using SetupAPI & NewDev.dll
Write-Host "Installing new driver package and creating device node programmatically..." -ForegroundColor Cyan

$source = @"
using System;
using System.Runtime.InteropServices;

public class DeviceInstaller {
    [StructLayout(LayoutKind.Sequential)]
    public struct SP_DEVINFO_DATA {
        public int cbSize;
        public Guid classGuid;
        public uint devInst;
        public IntPtr reserved;
    }

    [DllImport("setupapi.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid classGuid, IntPtr hwndParent);

    [DllImport("setupapi.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool SetupDiCreateDeviceInfo(
        IntPtr deviceInfoSet,
        string deviceName,
        ref Guid classGuid,
        string deviceDescription,
        IntPtr hwndParent,
        uint creationFlags,
        ref SP_DEVINFO_DATA deviceInfoData
    );

    [DllImport("setupapi.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool SetupDiSetDeviceRegistryProperty(
        IntPtr deviceInfoSet,
        ref SP_DEVINFO_DATA deviceInfoData,
        uint property,
        byte[] propertyBuffer,
        uint propertyBufferSize
    );

    [DllImport("setupapi.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool SetupDiRegisterDeviceInfo(
        IntPtr deviceInfoSet,
        ref SP_DEVINFO_DATA deviceInfoData,
        uint flags,
        IntPtr compareContext,
        IntPtr compareInfo,
        IntPtr reserved
    );

    [DllImport("setupapi.dll", SetLastError = true)]
    public static extern bool SetupDiDestroyDeviceInfoList(IntPtr deviceInfoSet);

    [DllImport("newdev.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool UpdateDriverForPlugAndPlayDevices(
        IntPtr hwndParent,
        string hardwareId,
        string fullInfPath,
        uint installFlags,
        out bool rebootRequired
    );

    public const uint SPDRP_HARDWAREID = 0x00000001;

    public static bool CreateDeviceAndInstallDriver(string classGuidStr, string hardwareId, string infPath, out bool rebootRequired) {
        rebootRequired = false;
        Guid classGuid = new Guid(classGuidStr);
        IntPtr devInfoSet = SetupDiCreateDeviceInfoList(ref classGuid, IntPtr.Zero);
        if (devInfoSet == IntPtr.Zero || devInfoSet.ToInt64() == -1) {
            Console.WriteLine("SetupDiCreateDeviceInfoList failed: " + Marshal.GetLastWin32Error());
            return false;
        }

        bool created = false;
        try {
            SP_DEVINFO_DATA devInfoData = new SP_DEVINFO_DATA();
            devInfoData.cbSize = Marshal.SizeOf(devInfoData);

            // A previous uninstall can leave a non-present Enum history entry which
            // pnputil correctly says is not in the hardware tree. Try the next Root
            // instance ID instead of treating ERROR_DEVINST_ALREADY_EXISTS as a
            // usable SP_DEVINFO_DATA record.
            for (int instance = 0; instance < 100; instance++) {
                string deviceName = string.Format(@"Root\WinUHid\{0:D4}", instance);
                if (SetupDiCreateDeviceInfo(devInfoSet, deviceName, ref classGuid, null, IntPtr.Zero, 0, ref devInfoData)) {
                    created = true;
                    Console.WriteLine("Created device instance " + deviceName);
                    break;
                }
                int err = Marshal.GetLastWin32Error();
                if ((uint)err != 0xE0000207) {
                    Console.WriteLine("SetupDiCreateDeviceInfo failed: " + err);
                    return false;
                }
                devInfoData = new SP_DEVINFO_DATA();
                devInfoData.cbSize = Marshal.SizeOf(devInfoData);
            }
            if (!created) {
                Console.WriteLine("No free WinUHid Root instance ID was available.");
                return false;
            }

            if (created) {
                byte[] hwIdBytes = System.Text.Encoding.Unicode.GetBytes(hardwareId + "\0\0");
                if (!SetupDiSetDeviceRegistryProperty(devInfoSet, ref devInfoData, SPDRP_HARDWAREID, hwIdBytes, (uint)hwIdBytes.Length)) {
                    Console.WriteLine("SetupDiSetDeviceRegistryProperty failed: " + Marshal.GetLastWin32Error());
                    return false;
                }

                if (!SetupDiRegisterDeviceInfo(devInfoSet, ref devInfoData, 0, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero)) {
                    Console.WriteLine("SetupDiRegisterDeviceInfo failed: " + Marshal.GetLastWin32Error());
                    return false;
                }
            }
        } finally {
            SetupDiDestroyDeviceInfoList(devInfoSet);
        }

        Console.WriteLine("Updating driver using UpdateDriverForPlugAndPlayDevices...");
        if (!UpdateDriverForPlugAndPlayDevices(IntPtr.Zero, hardwareId, infPath, 0x00000001, out rebootRequired)) {
            Console.WriteLine("UpdateDriverForPlugAndPlayDevices failed: " + Marshal.GetLastWin32Error());
            return false;
        }

        return true;
    }
}
"@

Add-Type -TypeDefinition $source
$rebootRequired = $false
$success = [DeviceInstaller]::CreateDeviceAndInstallDriver("{4d36e97d-e325-11ce-bfc1-08002be10318}", "Root\WinUHid", $InfPath, [ref]$rebootRequired)
if (-not $success) {
    Write-Host "Failed to programmatically install driver!" -ForegroundColor Red
    Exit 1
}

# 6. Verify service status
Write-Host "Starting WUDFRd service if needed..." -ForegroundColor Cyan
sc.exe start WUDFRd

# 7. Verify every layer used by the application health check.
$deviceLayerVerified = $true
$verifyInstances = Get-CfgMgrWinUHidInstances
if ($null -ne $verifyInstances) {
    $devicePresent = @($verifyInstances | Where-Object { (Test-CfgMgrPresent -InstanceId $_) -eq $true }).Count -gt 0
}
else {
    $verifyDevices = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/deviceid", "Root\WinUHid")
    $verifyText = ($verifyDevices -join "`n")
    if ($verifyText -match 'DEVPKEY_Device_IsPresent') {
        $devicePresent = ($verifyText -match '(?is)ROOT\\WINUHID\\[^\s]+.*DEVPKEY_Device_IsPresent[^\r\n]*[\r\n]+\s*TRUE')
    }
    else {
        # Plain listing has no DEVPKEY properties, so the DEVPKEY regex could never
        # match and a perfectly good install would report as failed. `/connected`
        # answers the same question and pre-dates /properties, but it cannot be
        # combined with /deviceid (pnputil exits 87), so filter the full listing.
        $connected = Invoke-PnpUtilEnum -BaseArguments @("/enum-devices", "/connected") -RichOptions @()
        if ($null -eq $connected) {
            $deviceLayerVerified = $false
            $devicePresent = $false
        }
        else {
            $devicePresent = (($connected -join "`n") -match '(?i)ROOT\\WINUHID\\')
        }
    }
}
$verifyDrivers = pnputil /enum-drivers 2>&1
$packagePresent = (($verifyDrivers -join "`n") -match '(?i)winuhiddriver\.inf')
if (-not $packagePresent) {
    # /enum-drivers needs elevation and can fail; the driver database answers the
    # same question from the registry.
    $dbRoot = "HKLM:\SYSTEM\DriverDatabase\DriverPackages"
    if (Test-Path -LiteralPath $dbRoot) {
        $packagePresent = @(Get-ChildItem -LiteralPath $dbRoot -ErrorAction SilentlyContinue |
            Where-Object { $_.PSChildName -like "winuhiddriver.inf_*" }).Count -gt 0
    }
}
$serviceKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver"
$registryPresent = Test-Path $serviceKey
if (-not $deviceLayerVerified -and $packagePresent -and $registryPresent) {
    # Every layer we can still read says the install succeeded, and only the device
    # query was unanswerable. Failing here would report a working driver as broken;
    # the app's own runtime smoke test makes the final call.
    Write-Host "Driver installed; device enumeration unavailable so the device layer was not verified." -ForegroundColor Yellow
}
elseif (-not $devicePresent -or -not $packagePresent -or -not $registryPresent) {
    Write-Host "Driver verification failed: devicePresent=$devicePresent packagePresent=$packagePresent registryPresent=$registryPresent" -ForegroundColor Red
    Exit 1
}

Write-Host "Driver installation complete!" -ForegroundColor Green
if ($rebootRequired) {
    Write-Host "A system reboot is required for this installation to take effect." -ForegroundColor Yellow
}
Exit 0
