# Get Administrator permissions
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script as Administrator!" -ForegroundColor Red
    Exit
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
pnputil /remove-device /deviceid "Root\WinUHid"

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
}


# 4. Install certificate to TrustedPublisher and Root store
Write-Host "Installing certificate to TrustedPublisher and Root stores..." -ForegroundColor Cyan
certutil -addstore -f "TrustedPublisher" $CertPath
certutil -addstore -f "Root" $CertPath

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

            if (!SetupDiCreateDeviceInfo(devInfoSet, @"Root\WinUHid\0000", ref classGuid, null, IntPtr.Zero, 0, ref devInfoData)) {
                int err = Marshal.GetLastWin32Error();
                // 0xE0000207 is ERROR_DEVINST_ALREADY_EXISTS
                if ((uint)err == 0xE0000207) {
                    Console.WriteLine("Device instance already exists in registry.");
                    created = true;
                } else {
                    Console.WriteLine("SetupDiCreateDeviceInfo failed: " + err);
                    return false;
                }
            } else {
                created = true;
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

Write-Host "Driver installation complete!" -ForegroundColor Green
if ($rebootRequired) {
    Write-Host "A system reboot is required for this installation to take effect." -ForegroundColor Yellow
}

