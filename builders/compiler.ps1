#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location "$ScriptDir\.."

$OnlyInstall = $false
if ($args.Count -gt 0 -and $args[0] -eq "--only-install-deps") {
    $OnlyInstall = $true
}

function Has($cmd) {
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

function WingetInstall($id) {
    winget install --id $id --accept-source-agreements --accept-package-agreements
}

Write-Host "================================"
Write-Host "  afw compiler"
Write-Host "================================"

$Arch = $env:PROCESSOR_ARCHITECTURE
if (-not $Arch) { $Arch = "AMD64" }
$ZigArch = if ($Arch -in @("ARM64","aarch64")) { "aarch64" } else { "x86_64" }

if (-not (Has zig)) {
    Write-Host "[ ] zig: NOT FOUND"
    if (Has winget) {
        WingetInstall zig.zig
    } else {
        $apiUrl = "https://ziglang.org/download/index.json"
        Write-Host "  Fetching latest Zig version..."
        try {
            $json = Invoke-RestMethod -Uri $apiUrl
            $version = $json.master.version
        } catch {
            $version = "0.16.0"
            Write-Host "  Could not fetch latest version, using $version"
        }
        $url = "https://ziglang.org/download/$version/zig-windows-$ZigArch-$version.zip"
        $zip = "$env:TEMP\zig.zip"
        Write-Host "  Downloading $version ($ZigArch) from $url"
        Invoke-WebRequest -Uri $url -OutFile $zip
        Expand-Archive $zip -DestinationPath "C:\zig"
        $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
        [Environment]::SetEnvironmentVariable("Path", "$currentPath;C:\zig", "User")
        $env:Path += ";C:\zig"
        Remove-Item $zip
        Write-Host "  Zig $version installed to C:\zig"
    }
} else {
    Write-Host "[x] zig: $(zig version)"
}

if (-not (Has ffmpeg)) {
    Write-Host "[ ] ffmpeg: NOT FOUND"
    if (Has winget) {
        WingetInstall Gyan.FFmpeg
    } else {
        Write-Host "  Please install ffmpeg manually (https://ffmpeg.org/download.html)"
    }
} else {
    $fv = (ffmpeg -version 2>&1)[0]
    Write-Host "[x] ffmpeg: $fv"
}

if (-not (Has python3) -and -not (Has python)) {
    Write-Host "[ ] python3: NOT FOUND"
    if (Has winget) {
        WingetInstall Python.Python.3.12
    } else {
        Write-Host "  Please install Python 3.10+ manually"
    }
} else {
    $py = if (Has python3) { "python3" } else { "python" }
    $pv = & $py --version
    Write-Host "[x] python: $pv"
}

if ($OnlyInstall) {
    Write-Host ""
    Write-Host "Dependencies installed. Skipping build (--only-install-deps)."
    exit 0
}

Write-Host ""
Write-Host "================================"
Write-Host "  building"
Write-Host "================================"

Write-Host "[1/4] zig: afw_render.dll"
if (Has zig) {
    zig build-lib afw_render.zig -dynamic -fPIC -O ReleaseFast -femit-bin=afw_render.dll
    if ($?) { Write-Host "      -> ok" }
    else    { Write-Host "      -> FAILED"; exit 1 }
} else {
    Write-Host "      -> SKIPPED (zig not found)"
}

Write-Host "[2/4] zig: afw_media.exe"
if (Has zig) {
    zig build-exe afw_media.zig -O ReleaseFast -femit-bin=afw_media.exe
    if ($?) { Write-Host "      -> ok" }
    else    { Write-Host "      -> FAILED"; exit 1 }
} else {
    Write-Host "      -> SKIPPED (zig not found)"
}

Write-Host "[3/4] python: unify afw.py"
$py = if (Has python3) { "python3" } else { "python" }
if (Has $py) {
    & $py builders\bundle.py
    if ($?) { Write-Host "      -> ok" }
    else    { Write-Host "      -> FAILED"; exit 1 }
} else {
    Write-Host "      -> SKIPPED (python not found)"
}

Write-Host "[4/4] python: compile .py files"
if (Has $py) {
    & $py -m py_compile afw.py afw_stream_player.py examples\fireworks.py examples\widget_showcase.py
    if ($?) { Write-Host "      -> ok" }
    else    { Write-Host "      -> FAILED"; exit 1 }
} else {
    Write-Host "      -> SKIPPED (python not found)"
}

Write-Host ""
Write-Host "================================"
Write-Host "  done. test with:"
Write-Host "    python examples\fireworks.py"
Write-Host "    python examples\widget_showcase.py"
Write-Host "================================"
