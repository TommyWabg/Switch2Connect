# Uninstalls the HidHide driver. Invoked elevated (runas) by the app.
$ErrorActionPreference = 'SilentlyContinue'

$roots = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)

$done = $false
foreach ($root in $roots) {
    Get-ItemProperty $root -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like '*HidHide*' } |
        ForEach-Object {
            $quiet = $_.QuietUninstallString
            $normal = $_.UninstallString
            if ($quiet) {
                Start-Process 'cmd.exe' -ArgumentList '/c', $quiet -Wait
                $done = $true
            } elseif ($normal) {
                if ($normal -match 'msiexec') {
                    $code = ($normal -replace '.*({[0-9A-Fa-f\-]+}).*', '$1')
                    Start-Process 'msiexec.exe' -ArgumentList "/x $code /qn /norestart" -Wait
                } else {
                    # Advanced Installer EXE: request a silent uninstall.
                    Start-Process 'cmd.exe' -ArgumentList '/c', "$normal /exenoui /qn" -Wait
                }
                $done = $true
            }
        }
}

if (-not $done) { Write-Output 'HidHide uninstall entry not found.' }
exit 0
