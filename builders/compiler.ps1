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

function Refresh-Path {
    # winget updates the registry PATH, but this session keeps the old
    # one; re-read Machine + User PATH so a just-installed tool is
    # visible immediately.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
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
        Refresh-Path
        if (-not (Has zig)) {
            Write-Host "  winget installed Zig but it is still not on PATH; restart the shell and re-run."
        }
    } else {
        $apiUrl = "https://ziglang.org/download/index.json"
        Write-Host "  Fetching latest Zig version..."
        $version = "0.17.1"
        try {
            $json = Invoke-RestMethod -Uri $apiUrl
            # The index lists "master" first: its version is a dev
            # build, not a release. Pick the latest stable x.y.z key.
            $stable = $json.PSObject.Properties.Name |
                Where-Object { $_ -match '^\d+\.\d+\.\d+$' } |
                Sort-Object { [version]$_ } |
                Select-Object -Last 1
            if ($stable) { $version = $stable }
        } catch {
            Write-Host "  Could not fetch latest version, using $version"
        }
        # Upstream names the zip zig-<arch>-windows-<version>.zip
        # (e.g. zig-x86_64-windows-0.17.1.zip) — arch before OS.
        $url = "https://ziglang.org/download/$version/zig-$ZigArch-windows-$version.zip"
        $zip = "$env:TEMP\zig.zip"
        Write-Host "  Downloading $version ($ZigArch) from $url"
        Invoke-WebRequest -Uri $url -OutFile $zip
        Expand-Archive $zip -DestinationPath "C:\zig"
        # The zip has a top-level dir (zig-x86_64-windows-<ver>), so
        # the exe lives one level down from C:\zig: add THAT dir.
        $zigDir = Get-ChildItem "C:\zig" -Directory |
            Where-Object { $_.Name -like "zig-*" } |
            Select-Object -First 1
        $zigPath = if ($zigDir) { $zigDir.FullName } else { "C:\zig" }
        $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
        [Environment]::SetEnvironmentVariable("Path", "$currentPath;$zigPath", "User")
        $env:Path += ";$zigPath"
        Remove-Item $zip
        Write-Host "  Zig $version installed to $zigPath"
    }
} else {
    Write-Host "[x] zig: $(zig version)"
}

# afw_media.zig uses the new std.Io API, which needs Zig 0.17.1+;
# older Zig fails with cryptic compile errors, so fail fast here.
$RequiredZig = [version]"0.17.1"
if (Has zig) {
    $haveRaw = (zig version) -replace '-.*$','' -replace '\+.*$',''
    $have = [version]$haveRaw
    if ($have -lt $RequiredZig) {
        Write-Host "  Zig $haveRaw is too old: afw needs Zig $RequiredZig or newer."
        Write-Host "  Install it from https://ziglang.org/download/ and re-run."
        exit 1
    }
}

if (-not (Has ffmpeg)) {
    Write-Host "[ ] ffmpeg: NOT FOUND"
    if (Has winget) {
        WingetInstall Gyan.FFmpeg
        Refresh-Path
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
        Refresh-Path
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
