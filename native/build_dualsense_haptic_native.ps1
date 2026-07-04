$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $PSScriptRoot "dualsense_haptic_native.cpp"
$outDir = Join-Path $root "drivers"
$out = Join-Path $outDir "dualsense_haptic_native.dll"

if (-not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$cl = Get-Command cl.exe -ErrorAction SilentlyContinue
if ($cl) {
    & $cl.Source /nologo /O2 /EHsc /LD $src /Fe:$out
    if ($LASTEXITCODE -ne 0) { throw "cl.exe failed with exit code $LASTEXITCODE" }
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot "dualsense_haptic_native.obj") -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $outDir "dualsense_haptic_native.lib") -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $outDir "dualsense_haptic_native.exp") -ErrorAction SilentlyContinue
    Write-Output "Built $out"
    exit 0
}

$gxx = Get-Command g++.exe -ErrorAction SilentlyContinue
if ($gxx) {
    & $gxx.Source -O3 -std=c++17 -shared -static-libgcc -static-libstdc++ -o $out $src
    if ($LASTEXITCODE -ne 0) { throw "g++.exe failed with exit code $LASTEXITCODE" }
    Write-Output "Built $out"
    exit 0
}

throw "No supported C++ compiler found. Install Visual Studio Build Tools or MinGW-w64."
