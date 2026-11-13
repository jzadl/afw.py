#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ONLY_INSTALL=false
if [[ "${1:-}" == "--only-install-deps" ]]; then
    ONLY_INSTALL=true
fi

has() { command -v "$1" &>/dev/null; }

echo "================================"
echo "  afw compiler"
echo "================================"

_detect_pm() {
    if has pacman;   then echo pacman
    elif has apt;    then echo apt
    elif has dnf;    then echo dnf
    elif has yum;    then echo yum
    elif has apk;    then echo apk
    elif has brew;   then echo brew
    else echo none
    fi
}

PM="$(_detect_pm)"

_install_pkgs() {
    case "$PM" in
        pacman) sudo pacman -S --noconfirm "$@" ;;
        apt)    sudo apt-get update -qq && sudo apt-get install -y "$@" ;;
        dnf)    sudo dnf install -y "$@" ;;
        yum)    sudo yum install -y "$@" ;;
        apk)    sudo apk add "$@" ;;
        brew)   brew install "$@" ;;
        *)      echo "  No supported package manager. Install manually: $*" ; return 1 ;;
    esac
}

_install_zig_fallback() {
    echo "  Zig not in package manager, downloading binary..."
    # Pick the latest stable release from index.json (its first
    # "version" is the master dev build, not a release — never use
    # head -1 on it). Falls back to the pinned version below.
    local version="0.17.1"
    if has python3; then
        local detected
        detected="$(python3 -c '
import json, urllib.request
try:
    with urllib.request.urlopen("https://ziglang.org/download/index.json", timeout=15) as r:
        d = json.load(r)
    vers = sorted((k for k in d if k != "master"), key=lambda v: [int(x) for x in v.split(".")])
    print(vers[-1])
except Exception:
    print("")
' 2>/dev/null)" || detected=""
        if [ -n "$detected" ]; then
            version="$detected"
        else
            echo "  Could not fetch latest version, using $version"
        fi
    fi
    local arch="$(uname -m)"
    local os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$arch" in x86_64) arch="x86_64" ;; aarch64|arm64) arch="aarch64" ;; *) echo "  Unsupported architecture: $arch" ; return 1 ;; esac
    case "$os" in linux|darwin) ;; *) echo "  Unsupported OS: $os" ; return 1 ;; esac
    # Upstream names the tarball zig-<arch>-<os>-<version>.tar.xz
    # (e.g. zig-x86_64-linux-0.17.1.tar.xz) — arch before OS.
    local url="https://ziglang.org/download/${version}/zig-${arch}-${os}-${version}.tar.xz"
    echo "  URL: $url"
    local tmp="$(mktemp -d)"
    curl -fsSL "$url" -o "$tmp/zig.tar.xz"
    tar --no-same-owner -xf "$tmp/zig.tar.xz" -C "$tmp"
    sudo mv "$tmp"/zig-* /opt/zig
    sudo ln -sf /opt/zig/zig /usr/local/bin/zig
    rm -rf "$tmp"
    echo "  Zig $version installed to /opt/zig"
}

# _ver_ge HAVE REQUIRED: exit 0 if dotted version HAVE >= REQUIRED.
_ver_ge() {
    local IFS=.
    local i have=($1) req=($2)
    for i in 0 1 2; do
        local h=${have[$i]:-0} r=${req[$i]:-0}
        if (( 10#$h > 10#$r )); then return 0; fi
        if (( 10#$h < 10#$r )); then return 1; fi
    done
    return 0
}

if ! has zig; then
    echo "[ ] zig: NOT FOUND"
    case "$PM" in
        pacman|dnf|apk|brew) _install_pkgs zig ;;
        apt)
            if ! apt-cache show zig &>/dev/null || ! dpkg -s zig &>/dev/null 2>&1; then
                _install_zig_fallback
            else
                _install_pkgs zig
            fi
            ;;
        *) _install_zig_fallback ;;
    esac
else
    echo "[x] zig: $(zig version)"
fi

# afw_media.zig uses the new std.Io API, which needs Zig 0.17.1+;
# older Zig fails with cryptic compile errors, so fail fast here
# instead of halfway through the build.
REQUIRED_ZIG="0.17.1"
if has zig; then
    _have_zig="$(zig version | cut -d- -f1 | cut -d+ -f1)"
    if ! _ver_ge "$_have_zig" "$REQUIRED_ZIG"; then
        echo "  Zig $_have_zig is too old: afw needs Zig $REQUIRED_ZIG or newer."
        echo "  Install it from https://ziglang.org/download/ and re-run."
        exit 1
    fi
fi

if ! has ffmpeg; then
    echo "[ ] ffmpeg: NOT FOUND"
    case "$PM" in
        pacman|dnf|yum|apk|brew) _install_pkgs ffmpeg ;;
        apt) _install_pkgs ffmpeg ;;
        *) echo "  Install ffmpeg manually" ; exit 1 ;;
    esac
else
    echo "[x] ffmpeg: $(ffmpeg -version | head -1)"
fi

if ! has python3; then
    echo "[ ] python3: NOT FOUND"
    _install_pkgs python3
else
    echo "[x] python3: $(python3 --version)"
fi

if [[ "$ONLY_INSTALL" == true ]]; then
    echo ""
    echo "Dependencies installed. Skipping build (--only-install-deps)."
    exit 0
fi

echo ""
echo "================================"
echo "  building"
echo "================================"

echo "[1/4] zig: libafw_render.so"
if zig build-lib afw_render.zig -dynamic -fPIC -O ReleaseFast -femit-bin=libafw_render.so; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo "[2/4] zig: afw_media"
if zig build-exe afw_media.zig -O ReleaseFast -femit-bin=afw_media; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo "[3/4] python: unify afw.py"
if python3 builders/bundle.py; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo "[4/4] python: compile .py files"
if python3 -m py_compile afw.py afw_stream_player.py examples/fireworks.py examples/widget_showcase.py; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo ""
echo "================================"
echo "  done. test with:"
echo "    python3 examples/fireworks.py"
echo "    python3 examples/widget_showcase.py"
echo "================================"
