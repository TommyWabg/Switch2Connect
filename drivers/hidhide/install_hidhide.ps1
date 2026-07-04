# Installs the HidHide kernel driver. Invoked elevated (runas) by the app,
# mirroring install_driver.ps1.
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$installer = Join-Path $dir 'HidHide_x64.exe'

if (-not (Test-Path $installer)) {
    # Fallback: fetch the latest official installer if the bundled one is missing.
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $h = @{ 'User-Agent' = 'Switch2Controllers' }
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/nefarius/HidHide/releases/latest' -Headers $h
        $asset = $rel.assets | Where-Object { $_.name -like '*_x64.exe' } | Select-Object -First 1
        if ($asset) {
            $installer = Join-Path $env:TEMP $asset.name
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer -Headers $h
        }
    } catch {
        Write-Output "Could not obtain HidHide installer: $_"
        exit 1
    }
}

if (-not (Test-Path $installer)) {
    Write-Output 'HidHide installer not found.'
    exit 1
}

try {
    # HidHide is packaged with Advanced Installer; /exenoui /qn performs a silent install.
    # REBOOT=ReallySuppress stops the installer from restarting the PC on its own — the
    # app asks the user about rebooting instead.
    $p = Start-Process -FilePath $installer -ArgumentList '/exenoui', '/qn', 'REBOOT=ReallySuppress' -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
        # 3010 = success, reboot required. Any other non-zero: retry with the wizard.
        $p = Start-Process -FilePath $installer -ArgumentList 'REBOOT=ReallySuppress' -Wait -PassThru
    }
    exit $p.ExitCode
} catch {
    Write-Output "HidHide install failed: $_"
    exit 1
}
