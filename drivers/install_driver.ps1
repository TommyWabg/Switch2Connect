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

# 1. Clean up existing WinUHid devices
Write-Host "Removing existing WinUHid device nodes..." -ForegroundColor Yellow
$deviceOutput = pnputil /enum-devices /deviceid "Root\WinUHid" /properties 2>&1
$deviceInstances = @($deviceOutput | Select-String -AllMatches -Pattern 'ROOT\\WINUHID\\[^\s]+' | ForEach-Object {
    $_.Matches | ForEach-Object { $_.Value.Trim().ToUpperInvariant() }
} | Select-Object -Unique)
foreach ($instance in $deviceInstances) {
    pnputil /remove-device $instance /force
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Exact removal failed for $instance; trying device-ID fallback." -ForegroundColor Yellow
    }
}
if ($deviceInstances.Count -gt 0) {
    $remainingOutput = pnputil /enum-devices /deviceid "Root\WinUHid" /properties 2>&1
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
$verifyDevices = pnputil /enum-devices /deviceid "Root\WinUHid" /properties 2>&1
$devicePresent = (($verifyDevices -join "`n") -match '(?is)ROOT\\WINUHID\\[^\s]+.*DEVPKEY_Device_IsPresent[^\r\n]*[\r\n]+\s*TRUE')
$verifyDrivers = pnputil /enum-drivers 2>&1
$packagePresent = (($verifyDrivers -join "`n") -match '(?i)winuhiddriver\.inf')
$serviceKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver"
$registryPresent = Test-Path $serviceKey
if (-not $devicePresent -or -not $packagePresent -or -not $registryPresent) {
    Write-Host "Driver verification failed: devicePresent=$devicePresent packagePresent=$packagePresent registryPresent=$registryPresent" -ForegroundColor Red
    Exit 1
}

Write-Host "Driver installation complete!" -ForegroundColor Green
if ($rebootRequired) {
    Write-Host "A system reboot is required for this installation to take effect." -ForegroundColor Yellow
}
Exit 0
